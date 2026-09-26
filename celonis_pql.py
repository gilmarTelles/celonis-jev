"""PQL and KPI definitions: the data path of a read.

KPI definitions are a semantic-layer GET. The PQL itself runs *in the signed-in
tab* (`cdp.evaluate`), because the query engine wants the app session plus its
x-xsrf-token. The two coordinates the query URL is built from - the package key
and the knowledge model key - come from the index entries of the KPI, i.e. from
the tenant's own ids. They used to be constants here; a second package would
have silently read the wrong model.
"""

from __future__ import annotations

import json

import celonis_api as api


def pql_table(pql: str) -> str:
    return pql if pql.strip().upper().startswith("TABLE(") else f"TABLE({pql})"


def kpi_defs(km_node_id: str) -> list[dict]:
    rows = api.get(f"/semantic-layer/api/knowledge-model/{km_node_id}/kpis", timeout=30)
    return rows if isinstance(rows, list) else rows.get("kpis", [])


def models(index) -> list[dict]:
    """The knowledge models the index holds. `Index.models` does the ordering (the
    vocabulary's designated package first - several models share names and ids)."""
    return index.models()


def km_coord(index, km_entry: dict) -> tuple[str, str]:
    """(package_key, km_key): what the blueprint query URL is built from."""
    pkg = next((e for e in index.entries
                if e["kind"] == "package" and e["id"] == km_entry.get("packageId")), None)
    if not (pkg and pkg.get("key") and km_entry.get("key")):
        raise LookupError(f"the index does not carry the package key of "
                          f"{km_entry.get('name')!r}; refresh it (celonis_index.py)")
    return pkg["key"], km_entry["key"]


def find_kpi(name: str, index) -> tuple[dict, dict] | None:
    """The KPI definition answering to `name`, and the model that owns it.

    Exact id or display name wins wherever it sits; a folded substring of the
    same fields is the floor. One lookup per model, and a name that fits no
    definition returns None rather than guessing.
    """
    fold = name.strip().casefold()
    loose = []
    for km in models(index):
        for d in kpi_defs(km["id"]):
            if d.get("id") == name or d.get("displayName") == name:
                return d, km
            if fold in f'{d.get("id", "")} {d.get("displayName", "")}'.casefold():
                loose.append((d, km))
    return loose[0] if loose else None


def kpi_definition(entry: dict, index) -> tuple[dict, dict]:
    """The definition of one indexed KPI and its model, from that model alone.

    One request: the entry already names its model, so no other model is scanned.
    """
    if entry["kind"] != "kpi":
        raise LookupError(f"read needs a KPI; {entry['name']!r} is a {entry['kind']}, "
                          f"open it instead")
    found = next((d for d in kpi_defs(entry["model"]) if d.get("id") == entry["id"]), None)
    if found is None:
        raise LookupError(f"its model no longer defines KPI {entry['id']!r}; "
                          f"refresh the index (celonis_index.py)")
    return found, index.by_id[entry["model"]]


def execute(pql: str, km_entry: dict, index, tab: dict) -> dict:
    """Run one PQL against the model `km_entry` names, through the signed-in tab."""
    pkg_key, km_key = km_coord(index, km_entry)
    return _run_pql(pql, pkg_key, km_key, tab)


def _run_pql(pql: str, package_key: str, km_key: str, tab: dict) -> dict:
    """Execute PQL through the signed-in tab. Returns {ms, columns, rows, error}."""
    body = {"batchRequest": {"requests": [{"id": "cli", "request": {"commands": [{"queries": [pql_table(pql)]}]}}]}}
    url = f"/blueprint/api/v2/queries/{package_key}.{km_key}/streaming/{package_key}.{km_key}?draft=true"
    js = """(async () => {
      const m = document.cookie.match(/XSRF-TOKEN=([^;]+)/); const tok = m ? decodeURIComponent(m[1]) : '';
      const body = %s;
      const t0 = performance.now();
      const r = await fetch(%s, {method: "POST", credentials: "include",
        headers: {"Content-Type": "application/json", "x-xsrf-token": tok,
                  "Accept": "text/event-stream, application/json"},
        body: JSON.stringify(body)});
      const txt = await r.text();
      return JSON.stringify({status: r.status, ms: Math.round(performance.now() - t0), txt});
    })()""" % (json.dumps(body), json.dumps(url))
    import cdp
    out = json.loads(cdp.evaluate(tab, js))
    payload = None
    for line in out["txt"].splitlines():
        if line.startswith("data:"):
            try:
                payload = json.loads(line[5:])
            except Exception:
                continue
    if payload is None:
        return {"ms": out["ms"], "error": out["txt"][:300], "rows": [], "columns": []}
    comp = payload.get("result", {}).get("components", {}).get("0", {})
    if comp.get("message"):
        return {"ms": out["ms"], "error": comp["message"], "rows": [], "columns": []}
    res = (comp.get("results") or [{}])[0]
    cols = [m.get("columnName") for m in res.get("metaData") or []]
    return {"ms": out["ms"], "columns": cols, "rows": res.get("data") or [], "error": None}
