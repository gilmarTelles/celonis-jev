"""One local index of everything addressable in the tenant.

Navigation is a lookup, not a click: spaces, packages, nodes, pools, and the
object-centric model all come from APIs with stable ids. This builds the table the
resolver searches, and prints what it holds.

  python3 celonis_index.py            # refresh and summarise
  python3 celonis_index.py --quiet    # refresh only
  python3 celonis_index.py --embed    # embed the current index for semantic search (resumable)
  python3 celonis_index.py --describe # describe each item for semantic search (claude CLI, ~$2.6)

A normal refresh also embeds new entries when a local Ollama answers (see celonis_embed).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import celonis_api as api
import celonis_describe
import celonis_embed
import celonis_types
import cliargs

OUT = Path(__file__).with_name("celonis-index.json")
COLUMNS_OUT = Path(__file__).with_name("celonis-columns.json")
SCAN_CACHE = Path(__file__).with_name("cache") / "table-columns.json"
# the configured pool carrying the target model contributes objects and events
ENV = "develop"


POOL_SECTIONS = [("Data Jobs", "data-configuration/data-jobs"),
                 ("Data Connections", "data-connections"),
                 ("Process Data Models", "data-configuration/process-data-models"),
                 ("Studio packages", "studio"), ("Overview", "overview")]
OE_SECTIONS = [("Dashboard", "dashboard"), ("Objects", "objects"), ("Events", "events"),
               ("Transformations", "transformations"), ("SQL editor", "transformations/sql-sandbox"),
               ("Perspectives", "perspectives_v1"), ("Catalog", "catalog")]
APP_SECTIONS = [
    ("Data Pools list", "Data Integration", "/integration/ui/pools"),
    ("Data Consumption", "Data Integration", "/integration/ui/data-consumption"),
    ("Monitoring", "Data Integration", "/integration/ui/monitoring"),
    ("Workspaces", "Machine Learning", "/machine-learning/ui/workspaces"),
    ("Celonis Apps", "Machine Learning", "/machine-learning/ui/celonis-apps"),
    ("Projects", "Task Mining", "/task-mining/ui/projects"),
    ("Packages and views", "Apps", "/package-manager/ui/views/ui/spaces"),
    ("Inbox", "Apps", "/package-manager/ui/views/ui/task-inbox"),
    ("Studio spaces", "Studio", "/package-manager/ui/studio/ui/spaces"),
    ("Studio settings", "Studio", "/package-manager/ui/studio/ui/settings"),
    ("Studio trash", "Studio", "/package-manager/ui/studio/ui/spaces/trash"),
    ("Team settings", "Admin & Settings", "/ui/team/settings"),
    ("Team members", "Admin & Settings", "/ui/team/members"),
    ("Team groups", "Admin & Settings", "/ui/team/groups"),
    ("Applications", "Admin & Settings", "/ui/team/applications"),
    ("Permissions", "Admin & Settings", "/ui/team/permission-management"),
    ("Single Sign-On", "Admin & Settings", "/ui/team/sso"),
    ("License", "Admin & Settings", "/ui/team/license"),
    ("Notifications", "Admin & Settings", "/ui/team/notifications/admin"),
    ("Audit logs", "Admin & Settings", "/ui/team/logs"),
    ("Login history", "Admin & Settings", "/ui/team/login-history"),
    ("User adoption", "Admin & Settings", "/ui/team/user-adoption"),
    ("System integrations", "Admin & Settings", "/ui/team/system/integrations"),
    ("AI and LLM settings", "Admin & Settings", "/ui/team/system/ai-llm-settings"),
    ("Downloads and on-prem clients", "Admin & Settings", "/ui/team/system/download"),
    ("Transformation Hub opportunities", "Transformation Hub", "/transformation-hub/ui/opportunities"),
    ("Storage buckets", "Storage Manager", "/storage-manager/ui"),
    ("SAP user management", "SAP User Management", "/sap-account/ui"),
    ("KPI snapshots", "KPI Snapshots", "/kpi-logging/ui"),
    ("Process repository", "Process Repository", "/process-repository/ui"),
    ("Celonis Gallery", "Celonis Gallery", "/try/ui/celonis-gallery/ui"),
    ("Marketplace", "Marketplace", "/store/ui/discover"),
]


# --- one builder per kind of entry; each dict comes from celonis_types ----------

def _space_entries(spaces: dict[str, str]) -> list[dict]:
    out = []
    for s in spaces.values():
        # the id is found by reverse lookup: the API answers names, not the map
        out.append(celonis_types.space_entry(s, next(k for k, v in spaces.items() if v == s)))
    return out


def _package_entries(packages: list[dict], spaces: dict[str, str]) -> list[dict]:
    return [celonis_types.package_entry(
        p["name"], p["id"], p["key"], spaces.get(p.get("spaceId"), ""), p.get("spaceId"),
        f"/package-manager/ui/studio/ui/spaces/{p.get('spaceId')}/packages/{p['id']}")
        for p in packages]


def _asset_entries(nodes: list[dict], pkg_by_key: dict[str, dict]) -> list[dict]:
    out = []
    for n in nodes:
        if n.get("nodeType") != "ASSET":
            continue
        p = pkg_by_key.get(n.get("rootNodeKey"), {})
        out.append(celonis_types.asset_entry(
            (n.get("assetType") or "").lower(), n.get("name") or n["key"], n["id"], n["key"],
            p.get("name", ""), p.get("spaceId"), p.get("id"),
            f"/package-manager/ui/studio/ui/spaces/{p.get('spaceId')}/packages/{p.get('id')}/nodes/{n['id']}"))
    return out


def _pool_entries(pools: list[dict]) -> tuple[list[dict], dict[str, list[str]]]:
    """One block per pool: the pool, its pages, its object model, its data layer.

    Also returns the object names per pool: what the pool actually holds, which
    the hint pass attaches to its entries.
    """
    entries: list[dict] = []
    pool_objects: dict[str, list[str]] = {}
    for pool in pools:
        pid = pool["id"]
        entries.append(celonis_types.pool_entry(
            pool["name"], pid, f"/integration/ui/pools/{pid}/overview"))
        for label, path in POOL_SECTIONS:
            entries.append(celonis_types.section_entry(
                f"{pool['name']} {label}", celonis_types.section_id(pid, path),
                pool["name"], pid, f"/integration/ui/pools/{pid}/{path}"))
        for label, path in OE_SECTIONS:
            entries.append(celonis_types.section_entry(
                f"{pool['name']} Objects and Events {label}", celonis_types.section_id(pid, path),
                pool["name"], pid, f"/objects-and-events/ui/bge/workspace/{pid}/{path}"))
        for kind, path in (("object", "types/objects"), ("event", "types/events"),
                           ("perspective", "perspectives")):
            try:
                rows = api.get(f"/bl/api/v2/workspaces/{pid}/{path}?requestMode=ALL&environment={ENV}")
            except Exception:
                continue
            rows = rows if isinstance(rows, list) else rows.get("content", [])
            for r in rows:
                if not isinstance(r, dict):          # the API sometimes returns bare ids
                    continue
                name = r.get("name") or r.get("id")
                key = r.get("key") or name
                if kind == "object":
                    pool_objects.setdefault(pid, []).append(name)
                seg = {"object": "objects", "event": "events", "perspective": "perspectives_v1"}[kind]
                entries.append(celonis_types.oem_entry(
                    kind, name, key, r.get("id"), pool["name"], pid,
                    [f.get("name") for f in (r.get("fields") or []) if f.get("name")],
                    f"/objects-and-events/ui/bge/workspace/{pid}/{seg}/{key}"
                    + (f"?tab={'object_details' if kind == 'object' else 'event_details'}"
                       if kind in ("object", "event") else "")))
        entries.extend(_table_entries(pool))
        entries.extend(_datajob_entries(pool))
    return entries, pool_objects


def _table_entries(pool: dict) -> list[dict]:
    """What the pool holds: one entry per table name.

    A table has no deep link of its own (checked - /pools/{id}/tables and
    /data-configuration/tables answer 404), so the entry points at the data
    model that contains it; the content is read through the workbench.
    """
    pid = pool["id"]
    try:
        models = api.get(f"/integration/api/pools/{pid}/data-models")
    except Exception:
        models = []
    model_url = (f"/integration/ui/pools/{pid}/data-configuration/process-data-models"
                 f"/{models[0]['id']}?tab=data-model" if models
                 else f"/integration/ui/pools/{pid}/data-configuration/process-data-models")
    try:
        schema_of: dict[str, str] = {}
        for t in api.get(f"/integration/api/pools/{pid}/tables"):
            name = t.get("name")
            if not name:
                continue
            schema = t.get("schemaName") or ""
            # One entry per table name: the same name can appear in the pool's
            # storage schema and in its _OCDM schema, and it is one table to
            # whoever asks for it. Keep the storage schema - that is what a
            # bare name resolves to in the workbench.
            if name not in schema_of or schema_of[name].endswith("_OCDM"):
                schema_of[name] = schema
    except Exception:
        schema_of = {}
    return [celonis_types.table_entry(name, schema, pool["name"], pid, model_url)
            for name, schema in schema_of.items()]


def _datajob_entries(pool: dict) -> list[dict]:
    """What fills the pool: each data job's status, size and last run."""
    pid = pool["id"]
    try:
        overview = {j["id"]: j for j in
                    api.get(f"/integration/api/pools/{pid}/overviews/data-jobs")}
    except Exception:
        overview = {}
    out = []
    for j in api.get(f"/integration/api/pools/{pid}/jobs"):
        seen = overview.get(j["id"], {})
        stamp = seen.get("lastExecutionDate") or j.get("timeStamp")
        out.append(celonis_types.datajob_entry(
            j["name"], j["id"], pool["name"], pid,
            seen.get("executionStatus", ""),
            seen.get("numberOfTransformations", 0),
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stamp / 1000))
            if isinstance(stamp, (int, float)) else "",
            f"/integration/ui/pools/{pid}/data-configuration/data-jobs"
            f"?jobId={j['id']}&tab=tasks"))
    return out


def _kpi_entries(nodes: list[dict], pkg_by_key: dict[str, dict]) -> list[dict]:
    """The KPIs each knowledge model defines, with a sample of their PQL."""
    out = []
    for n in nodes:
        if n.get("assetType") != "SEMANTIC_MODEL" or n.get("nodeType") != "ASSET":
            continue
        try:
            kpis = api.get(f"/semantic-layer/api/knowledge-model/{n['id']}/kpis")
        except Exception:
            continue
        p = pkg_by_key.get(n.get("rootNodeKey"), {})
        for k in (kpis if isinstance(kpis, list) else kpis.get("kpis", [])):
            out.append(celonis_types.kpi_entry(
                k.get("name") or k.get("id"), k.get("id"), p.get("name", ""), n["id"],
                (k.get("pql") or "")[:200],
                f"/package-manager/ui/studio/ui/spaces/{p.get('spaceId')}/packages/{p.get('id')}/nodes/{n['id']}?objectType=KPI"))
    return out


def _hint_pass(entries: list[dict], pool_objects: dict[str, list[str]]) -> None:
    """What a pool actually holds, so "the tax pool" resolves."""
    df: dict[str, int] = {}
    for names in pool_objects.values():
        for name in set(names):
            df[name] = df.get(name, 0) + 1
    for e in entries:
        if e["kind"] in ("pool", "section") and e.get("poolId") in pool_objects:
            names = sorted(set(pool_objects[e["poolId"]]), key=lambda n: (df.get(n, 1), n))[:6]
            e["hint"] = "holds " + ", ".join(names) if names else ""


def _app_sections() -> list[dict]:
    """The app-level pages: no tenant content, but addressable all the same."""
    return [{"kind": "section", "name": f"{app} {label}", "id": path, "app": app, "url": path}
            for label, app, path in APP_SECTIONS]


def build() -> dict:
    spaces = {s["id"]: s["name"] for s in api.get("/package-manager/api/spaces")}
    packages = api.get("/package-manager/api/packages")
    nodes = api.get("/package-manager/api/nodes")
    pools = api.get("/integration/api/pools")

    pkg_by_key = {p["key"]: p for p in packages}
    entries = (_space_entries(spaces) + _package_entries(packages, spaces)
               + _asset_entries(nodes, pkg_by_key))
    pool_entries, pool_objects = _pool_entries(pools)
    entries += pool_entries + _kpi_entries(nodes, pkg_by_key)
    _hint_pass(entries, pool_objects)
    entries += _app_sections()
    return {"origin": api.base(), "built": time.strftime("%Y-%m-%dT%H:%M:%S"), "entries": entries}


def columns_snapshot() -> dict:
    """The committed column snapshot: which tables hold which columns, per pool.

    `celonis columns` reads every table of a pool through the workbench - ~7 s each,
    ~15 minutes for a pool - into `cache/table-columns.json`, which is a scan
    artifact and not in git. A column question must not cost that on a fresh clone,
    so the scan is folded into `celonis-columns.json` beside the index, and a rebuild
    with no fresh scan keeps the snapshot it finds rather than dropping the layer.
    """
    try:
        scan = json.loads(SCAN_CACHE.read_text())
    except Exception:
        try:
            return json.loads(COLUMNS_OUT.read_text())
        except Exception:
            return {"built": "", "pools": {}}
    pools = {pool: {name: sorted({str(c) for c in (info.get("columns") or [])})
                    for name, info in tables.items() if info.get("columns")}
             for pool, tables in scan.items()}
    return {"built": time.strftime("%Y-%m-%dT%H:%M:%S"), "pools": pools}


def add_columns(doc: dict, snap: dict) -> int:
    """Attach each table's columns to its entry - the evidence a column question needs."""
    n = 0
    for e in doc["entries"]:
        if e["kind"] != "table":
            continue
        cols = (snap.get("pools", {}).get(e.get("poolId")) or {}).get(e["name"])
        if cols:
            e["columns"] = cols
            n += 1
    return n


def main() -> None:
    _, opts = cliargs.parse_argv(sys.argv[1:])
    if "--describe" in opts:
        celonis_describe.describe(json.loads(OUT.read_text())["entries"])
        return
    if "--embed" in opts:
        celonis_embed.embed_index(json.loads(OUT.read_text())["entries"])
        return
    doc = build()
    snap = columns_snapshot()
    COLUMNS_OUT.write_text(json.dumps(snap, indent=1))
    with_columns = add_columns(doc, snap)
    doc["columnsBuilt"] = snap["built"]
    OUT.write_text(json.dumps(doc, indent=1))
    counts: dict[str, int] = {}
    for e in doc["entries"]:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    if "--quiet" not in opts:
        print(f"{len(doc['entries'])} entries -> {OUT.name} ({OUT.stat().st_size:,} bytes)")
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<14} {v}")
        print(f"  {with_columns} tables carry columns -> {COLUMNS_OUT.name} "
              f"({COLUMNS_OUT.stat().st_size:,} bytes, scan {snap['built'] or 'unknown'})")
    celonis_embed.embed_index(doc["entries"], required=False)


if __name__ == "__main__":
    main()
