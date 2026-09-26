"""celonis - ask in words, land on the thing.

    celonis search "<phrase>"      candidates with ids, other instances listed (no Jev)
    celonis open --id <id>         open one candidate in Chrome, or print its link
    celonis read --id <id>         run one KPI candidate's PQL, show the rows
    celonis ask "<phrase>"         resolve and go there, or print the link
    celonis resolve "<phrase>"     where does this point?          (Jev)
    celonis open "<phrase>"        resolve, then open it in Chrome
    celonis view                   the same resolve, in a page that shows the Jev JSON
    celonis read "<phrase>"        resolve to a KPI, run its PQL, show the rows
    celonis tables [text]          the pool tables the index knows (--twins)
    celonis table "<name>"         columns, row count and last load of one table
    celonis query "<SQL>"          one read statement through the pool workbench
    celonis uses "<text>"          who mentions it: PQL, object fields, table columns
    celonis columns [text]         scan pool tables into cache/table-columns.json
    celonis verify                 is the index still the tenant's shape?
    celonis aliases                the vocabulary, curated and learned
    celonis aliases --promote "<phrase>" --why "<reason>"
                                   move a learned phrase into celonis-aliases.json
    celonis doctor                 what is missing, and how to fix it
    celonis kpis [text]            KPI definitions from the semantic layer
    celonis tabs                   what the signed-in browser has open
    celonis index                  rebuild the local index

`search` needs only the local index: no Jev key, no tenant, no browser.
`resolve`/`open` need no browser at all; `read` runs the PQL through the
signed-in tab (CDP), because the query engine wants the app session.

Add --no-jev to resolve/open to use the code-only baseline (vocabulary, tenant
name search, then BM25) for comparison. `open`/`ask` write the accepted phrase
into cache/aliases-learned.json; --no-learn suppresses that.
"""

from __future__ import annotations

import collections
import json
import os
import sys
import time
from pathlib import Path

import requests

import cdp
import celonis_api
import celonis_pql as P
import celonis_resolve as R
import cliargs
import jev

ENV = "develop"
ALIASES = Path(__file__).with_name("celonis-aliases.json")
LEARNED = Path(__file__).with_name("cache") / "aliases-learned.json"
VERIFY_STATE = Path(__file__).with_name("cache") / "verify.json"


def tenant_counts() -> dict[tuple[str, str], int]:
    """What the tenant says it holds, per kind and per container. Read-only."""
    want: dict[tuple[str, str], int] = {}
    pools = requests.get(celonis_api.base() + "/integration/api/pools", headers=celonis_api.headers(), timeout=60).json()
    want[("pools", "")] = len(pools)
    packages = requests.get(celonis_api.base() + "/package-manager/api/packages", headers=celonis_api.headers(), timeout=60).json()
    want[("packages", "")] = len(packages)
    nodes = requests.get(celonis_api.base() + "/package-manager/api/nodes", headers=celonis_api.headers(), timeout=60).json()
    want[("assets", "")] = sum(1 for n in nodes if n.get("nodeType") == "ASSET")
    for p in pools:
        pid = p["id"]
        for kind, path in (("object", "types/objects"), ("event", "types/events")):
            r = requests.get(celonis_api.base() + f"/bl/api/v2/workspaces/{pid}/{path}"
                             f"?requestMode=ALL&environment={ENV}", headers=celonis_api.headers(), timeout=60)
            if r.status_code == 200:
                rows = r.json()
                rows = rows if isinstance(rows, list) else rows.get("content", [])
                want[(kind, pid)] = len([x for x in rows if isinstance(x, dict)])
        r = requests.get(celonis_api.base() + f"/integration/api/pools/{pid}/tables", headers=celonis_api.headers(), timeout=60)
        if r.status_code == 200:
            # Distinct names: the index holds one entry per table name, and the
            # tenant lists the same name once per schema that carries it.
            want[("table", pid)] = len({t.get("name") for t in r.json() if t.get("name")})
        r = requests.get(celonis_api.base() + f"/integration/api/pools/{pid}/jobs", headers=celonis_api.headers(), timeout=60)
        if r.status_code == 200:
            want[("datajob", pid)] = len(r.json())
    for n in nodes:
        if n.get("nodeType") == "ASSET" and n.get("assetType") == "SEMANTIC_MODEL":
            r = requests.get(celonis_api.base() + f"/semantic-layer/api/knowledge-model/{n['id']}/kpis",
                             headers=celonis_api.headers(), timeout=60)
            if r.status_code == 200:
                rows = r.json()
                want[("kpi", n["id"])] = len(rows if isinstance(rows, list) else rows.get("kpis", []))
    return want


def index_counts(index: R.Index) -> dict[tuple[str, str], int]:
    """What the index holds, keyed the way `tenant_counts` keys the tenant."""
    have: dict[tuple[str, str], int] = {}
    for e in index.entries:
        if e["kind"] == "kpi":
            have[("kpi", e.get("model") or e.get("package") or "")] = \
                have.get(("kpi", e.get("model") or e.get("package") or ""), 0) + 1
        elif e.get("asset"):
            have[("assets", "")] = have.get(("assets", ""), 0) + 1
        elif e["kind"] in ("pool", "package"):
            have[(e["kind"] + "s", "")] = have.get((e["kind"] + "s", ""), 0) + 1
        elif e["kind"] in ("object", "event", "table", "datajob"):
            key = (e["kind"], e.get("poolId") or "")
            have[key] = have.get(key, 0) + 1
    return have


def verify(index: R.Index) -> int:
    """Compare the index with the tenant, count by count.

    An index that is one refresh behind answers with yesterday's names, and
    nothing in the resolver says so. This is the check that says so.
    """
    live = tenant_counts()
    have = index_counts(index)
    names = {e["id"]: e["name"] for e in index.entries if e["kind"] in ("pool", "semantic_model")}
    drift = 0
    for (kind, container), n in sorted(live.items()):
        mine = have.get((kind, container), 0)
        if mine == n:
            continue
        drift += 1
        where = names.get(container) or "tenant-wide"
        short = "index is behind" if mine < n else "index has extra"
        print(f"  DRIFT {kind:<9} {where[:30]:<32} tenant {n:>5}  index {mine:>5}  ({short})")
    print(f"  index built {index.built}: {len(index.entries)} entries; "
          f"{len(live)} counts compared, {drift} drifted")
    if drift:
        print("  refresh with: python3 celonis_index.py")
    VERIFY_STATE.parent.mkdir(exist_ok=True)
    VERIFY_STATE.write_text(json.dumps(
        {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "counts": len(live), "drift": drift,
         "built": index.built}, indent=1) + "\n")
    return 1 if drift else 0


def promote_alias(key: str, why: str, index: R.Index) -> int:
    """Move a learned phrase into the curated vocabulary, with the reason it is true.

    `remember` writes what a human accepted into the learned store, which is a cache
    beside the phrases the resolver learns by itself. This is how one graduates: a
    reviewed edit, because the curated file is a claim about the tenant and not a
    place to stash whatever the eval asked for. `--why` is required for that reason.
    """
    item = next((a for a in index.vocab.items if a["source"] == "learned"
                 and key in (a["phrase"], a["key"])), None)
    if not item:
        print(f"  no learned phrase {key!r} - `celonis aliases` lists what is there")
        return 1
    if not why:
        print(f"  {item['phrase']!r} -> {item['target']}: say why it is a tenant fact, with --why")
        return 1
    if not index.by_id.get(item["target"] or ""):
        print(f"  {item['phrase']!r} points at {item['target']}, which the index no longer has")
        return 1
    blob = json.loads(ALIASES.read_text())
    keep = [a for a in blob.get("aliases", []) if R.phrase_key(a.get("phrase", "")) != item["key"]]
    keep.append({"phrase": item["phrase"], "target": item["target"], "why": why})
    ALIASES.write_text(json.dumps({**blob, "aliases": keep}, indent=1) + "\n")
    try:                                  # one claim, one home: drop the learned copy
        learned = json.loads(LEARNED.read_text())
        learned["aliases"] = [a for a in learned.get("aliases", [])
                              if R.phrase_key(a.get("phrase", "")) != item["key"]]
        LEARNED.write_text(json.dumps(learned, indent=1) + "\n")
    except Exception:
        pass
    entry = index.by_id[item["target"]]
    print(f"  promoted {item['phrase']!r} -> [{entry['kind']}] {entry['name']}  ({ALIASES.name})")
    print(f"  why: {why}")
    return 0


def doctor(report: bool = True) -> list[str]:
    """What works right now, and the one command that fixes what does not."""
    import platform
    lines: list[str] = []

    def add(ok: str, text: str, fix: str = "") -> None:
        lines.append(f"{ok:<5}{text}" + (f"   -> {fix}" if fix else ""))

    add("OK", f"python {platform.python_version()}")
    try:
        import requests  # noqa: F401
        add("OK", "requests importable")
    except Exception:
        add("FAIL", "requests missing", "pip3 install --break-system-packages requests")

    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    env_file = Path.home() / ".config/typesafe/env.sh"
    if key:
        add("OK", "TYPESAFE_API_KEY from environment")
    elif env_file.exists() and "TYPESAFE_API_KEY" in env_file.read_text():
        add("OK", f"TYPESAFE_API_KEY from {env_file}")
    else:
        add("FAIL", "no TypeSafe key", "export TYPESAFE_API_KEY=... (or ~/.config/typesafe/env.sh)")

    cel = Path.home() / ".celonis/environments.json"
    if not cel.exists():
        add("FAIL", "~/.celonis/environments.json missing",
            '{"environments":{"sandbox":{"url":"https://<tenant>","api_key":"<team key>"}}}')
    else:
        try:
            pools = requests.get(celonis_api.base() + "/integration/api/pools", headers=celonis_api.headers(), timeout=20).json()
            add("OK", f"celonis api reachable ({len(pools)} pools)")
        except Exception as e:
            add("FAIL", f"celonis api unreachable ({type(e).__name__})", "check the key and url")

    idx = Path(__file__).with_name("celonis-index.json")
    if not idx.exists():
        add("FAIL", "no index", "python3 celonis_index.py")
    else:
        age_min = (time.time() - idx.stat().st_mtime) / 60
        entries = len(json.loads(idx.read_text())["entries"])
        tag = "OK" if age_min < 24 * 60 else "WARN"
        add(tag, f"index {entries} entries, {age_min:.0f} min old",
            "" if tag == "OK" else "python3 celonis_index.py")

    try:
        st = json.loads(VERIFY_STATE.read_text())
        drift = int(st.get("drift", 0))
        add("OK" if not drift else "WARN",
            f"last verify {st.get('at', '?')}: {st.get('counts', 0)} counts, {drift} drifted",
            "" if not drift else "python3 celonis_cli.py verify")
    except Exception:
        add("WARN", "never verified against the tenant",
            "python3 celonis_cli.py verify   (~75 s, one pass of tenant counts)")

    if idx.exists():
        index = R.Index()
        counts = collections.Counter(e["kind"] for e in index.entries)
        twins = table_twins(index.entries)
        add("OK", f"index {len(index.entries)} entries: {counts['table']} pool tables, "
                  f"{counts['datajob']} data jobs, {counts['kpi']} KPIs, "
                  f"{counts['object']} objects, {counts['board_v2']} views")
        add("WARN" if twins else "OK",
            f"{len(twins)} table(s) sitting beside their own double upload",
            "python3 celonis_cli.py tables --twins" if twins else "")
        dead = [a["phrase"] for a in index.vocab.items
                if not index.by_id.get(a.get("target") or (a.get("container") or {}).get("id") or "")]
        add("OK" if not dead else "WARN",
            f"vocabulary {len(index.vocab.items)} phrases"
            + (f", {len(dead)} unresolved" if dead else ""),
            "" if not dead else f"{', '.join(dead[:3])}: the index no longer has them")

    if not cdp.alive():
        add("FAIL", "no CDP browser on 127.0.0.1:9222",
            'open -na "Google Chrome" --args --user-data-dir=$HOME/.omp/chrome-celonis '
            '--remote-debugging-port=9222 --new-window "' + celonis_api.base() + '/ui/"')
    else:
        add("OK", "CDP browser on 127.0.0.1:9222")
        tab = cdp.find_tab("celonis.cloud")
        if not tab:
            add("WARN", "no tab on the tenant", f'open "{celonis_api.base()}/package-manager/ui/views/ui/spaces"')
        elif "id.celonis.cloud" in tab["url"]:
            add("FAIL", "the tab is on the login page", "sign in in that window")
        else:
            add("OK", f'signed-in tab: {tab["url"].split("/ui/")[-1][:48]}')

    work = not any(l.startswith("FAIL") for l in lines)
    add("OK" if work else "FAIL", f"resolve/open {'ready' if work else 'degraded'}"
        + ("; read needs the signed-in tab" if work else ""))
    if report:
        for line in lines:
            print(line)
    return lines


def pool_arg(raw: str | None, index: R.Index) -> str | None:
    """`--pool tax` means what the vocabulary says it means - the tax pool.

    A phrase is matched against the vocabulary first (as written, then as any part
    of an alias, so `--pool tax` finds "the tax pool"), then handed to the tenant
    as a pool name or id.
    """
    if not raw:
        return None
    for alias in index.vocab.hits(raw):
        container = alias.get("container") or {}
        if container.get("kind") == "pool":
            return container["id"]
    words = set(R.tokens(raw))
    for alias in index.vocab.items:
        container = alias.get("container") or {}
        if container.get("kind") == "pool" and words and words <= set(alias["tokens"]):
            return container["id"]
    return raw


def table_twins(entries: list[dict]) -> list[dict]:
    """Tables sitting next to their own double upload: X and X_X in one pool.

    A repeated .xlsx upload creates a new table rather than replacing the old one,
    and the factories keep reading whichever name they were written with. Both
    copies then sit in the pool, one generation behind, and nothing reports it.
    """
    names = {(e["poolId"], e["name"]) for e in entries if e["kind"] == "table"}
    pools = {e["id"]: e["name"] for e in entries if e["kind"] == "pool"}
    out = []
    for pid, name in sorted(names):
        twin = f"{name}_{name}"
        if (pid, twin) in names:
            out.append({"pool": pools.get(pid, pid), "poolId": pid, "kept": name, "extra": twin})
    return out


def cmd_tables(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
               pql: str | None) -> int:
    rows = [e for e in index.entries if e["kind"] == "table"]
    twins = table_twins(index.entries)
    if phrase:
        rows = [e for e in rows
                if phrase.lower() in e["name"].lower()
                or phrase.lower() in (e.get("pool") or "").lower()]
    if "--twins" in flags:
        print(f"  {len(twins)} table(s) sitting next to their own double upload")
        for t in twins:
            print(f"    {t['pool']:<26} {t['kept']:<16} + {t['extra']}")
        return 0
    shown = {t["kept"] for t in twins} | {t["extra"] for t in twins}
    for e in sorted(rows, key=lambda e: (e.get("pool") or "", e["name"]))[:400]:
        mark = " TWIN" if e["name"] in shown else ""
        print(f"  {e['name']:<34} {(e.get('pool') or '')[:24]:<26}{mark}")
    print(f"  {len(rows)} tables in the index"
          + (f"; {len(twins)} twin pairs - see --twins" if twins else ""))
    return 0


def cmd_table(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
              pql: str | None) -> int:
    import workbench as W
    try:
        info = W.describe(phrase, pool=pool_arg(opts.get("--pool"), index))
    except LookupError as e:
        print(f"  {e}")
        return 1
    w = info["where"]
    print(f'  {info["table"]}  in {w["pool"]}  [{w["job"]} / {w["task"]}]')
    if info.get("error"):
        print(f'  error {str(info["error"])[:220]}')
        return 1
    print(f'  rows {info.get("rows")}   last load {info.get("loaded")}')
    cols = info["columns"]
    print(f'  {len(cols)} columns: ' + ", ".join(cols[:16]) + (" ..." if len(cols) > 16 else ""))
    return 0


def cmd_query(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
              pql: str | None) -> int:
    import workbench as W
    try:
        out = W.run(phrase, pool=pool_arg(opts.get("--pool"), index),
                    job=opts.get("--job") or None, task=opts.get("--task") or None)
    except (LookupError, ValueError) as e:
        print(f"  {e}")
        return 1
    w = out["where"]
    print(f'  {w["pool"]} / {w["job"]} / {w["task"]}')
    print(f'  status {out["status"]}')
    if out["error"]:
        print(f'  error {str(out["error"])[:300]}')
    print("  cols " + ", ".join(c["name"] for c in out["columns"]))
    for row in out["rows"][:40]:
        print("   ", row)
    return 0 if out["status"] == "SUCCESS" else 1


def cmd_uses(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
             pql: str | None) -> int:
    import workbench as W
    needle = phrase.lower()
    pools = {e["id"]: e["name"] for e in index.entries if e["kind"] == "pool"}
    rows = []
    for e in index.entries:
        if needle in (e.get("pql") or "").lower():
            rows.append((e["kind"], e["name"], "PQL", e.get("package") or e.get("pool") or ""))
        if any(needle in str(f).lower() for f in (e.get("fields") or [])):
            rows.append((e["kind"], e["name"], "field", e.get("pool") or ""))
    for pool_id_, tables in W.load_columns().items():
        for name, info in tables.items():
            if any(needle in str(c).lower() for c in (info.get("columns") or [])):
                rows.append(("table", name, "column", pools.get(pool_id_, pool_id_[:8])))
    seen = set()
    for kind, name, via, where in rows:
        if (kind, name, via) in seen:
            continue
        seen.add((kind, name, via))
        print(f"  {kind:<12} {name[:44]:<46} {via:<7} {where[:24]}")
    print(f"  {len(seen)} entries mention {phrase!r}"
          + ("" if any(v == "column" for _, _, v, _ in rows)
             else "  (no column scan cached yet: celonis columns --pool <pool>)"))
    return 0


def cmd_columns(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
                pql: str | None) -> int:
    import workbench as W
    pool = pool_arg(opts.get("--pool"), index)
    # A phrase narrows the scan to the tables it names, and picks the pool when
    # it names tables in exactly one of them.
    hits = [e for e in index.entries
            if e["kind"] == "table" and phrase.lower() in e["name"].lower()] if phrase else []
    if phrase and not pool and len({e["poolId"] for e in hits}) == 1:
        pool = hits[0]["poolId"]
    wanted = sorted(e["name"] for e in hits
                    if pool is None or e["poolId"] == pool) if hits else None
    if not pool:
        print("  which pool? --pool takes a name, an id, or a vocabulary phrase")
        print("  a phrase scans only the tables it names")
        return 1
    limit = int(opts["--limit"]) if isinstance(opts.get("--limit"), str) else None

    def report(i: int, n: int, name: str, info: dict) -> None:
        state = (f"{len(info['columns'])} cols" if "columns" in info
                 else "ERR " + str(info.get("error"))[:46])
        print(f"  {i:>4}/{n:<4} {name[:36]:<38} {state}", flush=True)

    have = W.scan_columns(pool, tables_wanted=wanted, refresh="--refresh" in flags,
                          limit=limit, report=report)
    good = sum(1 for v in have.values() if "columns" in v)
    bad = sum(1 for v in have.values() if "error" in v)
    print(f"  cache/table-columns.json: {len(have)} tables recorded, {good} with columns"
          + (f", {bad} failed (rerun to retry just those)" if bad else ""))
    if wanted:                      # a phrase names tables, so show what they hold
        for name in wanted:
            info = have.get(name) or {}
            if "columns" in info:
                cols = ", ".join(f"{c}" for c in info["columns"][:20])
                more = "" if len(info["columns"]) <= 20 else f" ... (+{len(info['columns'])-20})"
                print(f"  {name}: {len(info['columns'])} columns - {cols}{more}")
            else:
                print(f"  {name}: {info.get('error', 'not scanned')}")
    return 0 if good else 1


def cmd_verify(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
               pql: str | None) -> int:
    return verify(index)


def cmd_aliases(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
                pql: str | None) -> int:
    if opts.get("--promote"):
        return promote_alias(str(opts["--promote"]), str(opts.get("--why") or ""), index)
    for item in index.vocab.items:
        target = item.get("target")
        ref = index.by_id.get(target) if target else index.by_id.get((item.get("container") or {}).get("id"))
        state = "OK " if ref else "DEAD"
        where = f"{ref['kind']} {ref['name']}" if ref else f"no longer in the index: {target}"
        print(f"  {state} {item['source']:<7} {item['phrase']:<24} -> {where}")
        if item.get("why"):
            print(f"        {item['why'][:96]}")
    return 0


def cmd_index(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
              pql: str | None) -> int:
    import celonis_index
    celonis_index.main()
    return 0


def cmd_tabs(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
             pql: str | None) -> int:
    for t in cdp.targets():
        print(f"  {t['url'][:110]}")
    return 0


def cmd_kpis(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
             pql: str | None) -> int:
    for d in P.kpi_defs(R.km_for_ask(phrase, index)["id"]):
        if phrase and phrase.lower() not in json.dumps(d).lower():
            continue
        print(f"  {d.get('id'):<44} {str(d.get('displayName'))[:34]:<36} {(d.get('pql') or '')[:60]}")
    return 0


def search_report(phrase: str, index: R.Index, k: int = 10) -> dict:
    """`R.search` with browser-ready urls when a tenant is configured."""
    candidates, warnings = R.search(phrase, index, k), []
    for c in candidates:
        c["url"], warnings = celonis_api.absolute_or_relative(c["url"])
    return {"candidates": candidates, "index_built": index.built,
            "next": "pick one and call celonis_open or celonis_read with its id",
            "warnings": warnings}


def navigate(url: str) -> bool:
    """Point the signed-in tab at `url`. False when there is no browser to drive."""
    if not url.startswith("http"):
        return False
    try:
        tab = cdp.find_tab("celonis.cloud") or cdp.new_tab(url)
        cdp.navigate(tab, url)
        return True
    except Exception:
        return False


def cmd_search(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
               pql: str | None) -> int:
    report = search_report(phrase, index, int(cliargs.flag(opts, "--limit") or 10))
    if "--json" in flags:
        print(json.dumps(report, indent=1))
        return 0 if report["candidates"] else 2
    for w in report["warnings"]:
        print(f"  ! {w}")
    for c in report["candidates"]:
        tag = f" exact ({c['why']})" if c.get("exact") else ""
        copies = f"  x{c['instances']}" if c["instances"] > 1 else ""
        print(f"  {c['score']:>6.2f}  [{c['kind']}] {c['name']}{copies}{tag}")
        print(f"          id {c['id']}   {c['container']}")
        if c["copies"]:
            more = c["instances"] - 1 - len(c["copies"])
            print(f"          also in: {'; '.join(x['container'] for x in c['copies'])}"
                  f"{f' (+{more} more)' if more else ''}")
        print(f"          {c['url']}")
    if not report["candidates"]:
        print(f"  nothing in the index shares a word with {phrase!r}")
    return 0 if report["candidates"] else 2


def cmd_open_id(handle: str, index: R.Index) -> int:
    try:
        entry = R.entry_for(handle, index)
    except LookupError as e:
        print(f"  {e}")
        return 2
    full, warnings = celonis_api.absolute_or_relative(entry["url"])
    for w in warnings:
        print(f"  ! {w}")
    print(f"  [{entry['kind']}] {entry['name']}")
    print(f"  opened: {full}" if navigate(full) else f"  no browser to open; link: {full}")
    return 0


def cmd_ask(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
            pql: str | None) -> int:
    handle = cliargs.flag(opts, "--id")
    if handle and cmd == "open":
        return cmd_open_id(handle, index)
    use_jev = "--no-jev" not in flags
    if use_jev:
        hit = R.resolve(phrase, index=index, verbose=(cmd == "resolve" and "--json" not in flags))
        if "--json" in flags:
            print(json.dumps({k: v for k, v in hit.to_dict().items() if k != "cached"}, indent=1))
        else:
            for w in hit.warnings:
                print(f"  ! {w}")
        if cmd == "ask" and hit.name:
            print(f'  [{hit.kind}] {hit.name}  (p={hit.confidence}'
                  f'{"  confirm first" if hit.confirm else ""})')
    else:
        hit = R.resolve_code_only(phrase, index)
        print(json.dumps(hit.to_dict(), indent=1))
    if cmd == "ask" and hit.confirm and "--force" not in flags:
        print(f'  ambiguous (top {hit.confidence}, margin {hit.margin}) - '
              "which one?")
        for cand in [{"name": hit.name, "kind": hit.kind, "url": hit.url}] + hit.alternatives:
            print(f'    {cand.get("prob", hit.confidence):.2f}  [{cand.get("kind")}] '
                  f'{cand["name"]}')
            print(f'          {celonis_api.base()}{cand["url"]}')
        print("  reopen with the clearer phrase, or --force to take the top one")
        return 3
    if cmd in ("open", "ask") and hit.url:
        full = celonis_api.absolute(hit.url)
        print(f"  opened: {full}" if navigate(full) else f"  no browser to open; link: {full}")
        # Navigating to it is accepting it: the phrase joins the vocabulary, so
        # the next time it is a lookup instead of a judgment. Only judged
        # resolutions are worth learning - the deterministic paths already
        # answer theirs.
        if hit.path == "ranked" and hit.entity_id and "--no-learn" not in flags:
            index.vocab.remember(phrase, {"id": hit.entity_id, "kind": hit.kind or ""})
            print(f"  learned: {phrase!r} -> {hit.kind} {hit.name}")
    return 0 if hit.name else 2


def cmd_read(cmd: str, phrase: str, index: R.Index, opts: dict, flags: set,
             pql: str | None) -> int:
    use_jev = "--no-jev" not in flags
    t0 = time.perf_counter()
    hit = None
    handle = cliargs.flag(opts, "--id")
    title = ""
    if handle:
        try:
            entry = R.entry_for(handle, index)
        except LookupError as e:
            print(f"  {e}")
            return 2
        try:
            k, km = P.kpi_definition(entry, index)
        except LookupError as e:
            print(f"  {e}")
            if entry["kind"] != "kpi":
                print(f"  open it with: celonis open --id {handle}")
            return 2
        pql = pql or k["pql"]
        title = f'{handle} -> KPI {k.get("displayName") or k.get("id")}'
    elif pql is None:
        hit = (R.resolve(phrase, index=index, verbose=False) if use_jev
               else R.resolve_code_only(phrase, index))
        for w in hit.warnings:
            print(f"  ! {w}")
        if not hit.name:
            print(f'no match for "{phrase}"')
            return 2
        if hit.kind == "kpi" and hit.name:
            found = P.find_kpi(hit.name, index)
            if not found:
                print(f'resolved to a KPI but no definition matched: {hit.name}')
                return 2
            k, km = found
            pql = k["pql"]
            hit.kpi = {"id": k.get("id"), "displayName": k.get("displayName"),
                       "format": k.get("format")}
            title = f'"{phrase}" -> KPI {hit.kpi.get("displayName") or hit.name}'
        else:
            print(f'"{phrase}" resolved to [{hit.kind}] {hit.name} — not a KPI, no value to run')
            print(f"  open it with: celonis open {phrase!r}")
            return 0
    else:
        km = R.km_for_ask(phrase, index)
    try:
        tab = cdp.find_tab("celonis.cloud") or \
            cdp.new_tab(celonis_api.base() + "/package-manager/ui/views/ui/spaces")
    except OSError as e:
        print(f"  {title}\n  PQL   {P.pql_table(pql)[:110]}")
        print(f"  no CDP browser to run it in ({type(e).__name__}); see: celonis doctor")
        return 1
    out = P.execute(pql, km, index, tab)
    total = (time.perf_counter() - t0) * 1000
    if title:
        print(title)
    print(f"  PQL   {P.pql_table(pql)[:110]}")
    if out["error"]:
        print(f"  error {out['error'][:200]}")
        return 1
    print(f"  cols  {out['columns']}")
    for row in out["rows"][:12]:
        print(f"    {row}")
    print(f"  query {out['ms']}ms | end-to-end {total:.0f}ms"
          f"{' | resolve tokens ' + str(hit.tokens) if hit else ''}")
    return 0


COMMANDS = {"tables": cmd_tables, "table": cmd_table, "query": cmd_query, "uses": cmd_uses,
            "columns": cmd_columns, "verify": cmd_verify, "aliases": cmd_aliases,
            "index": cmd_index, "tabs": cmd_tabs, "kpis": cmd_kpis,
            "search": cmd_search, "resolve": cmd_ask, "open": cmd_ask, "ask": cmd_ask, "read": cmd_read}


def main() -> int:
    args, opts = cliargs.parse_argv(sys.argv[1:])
    flags = set(opts)
    if not args:
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    phrase = " ".join(rest)
    if cmd == "doctor":
        return 0 if not any(l.startswith("FAIL") for l in doctor()) else 1
    if cmd == "view":
        import celonis_view
        port = opts.get("--port")
        celonis_view.serve(int(port) if isinstance(port, str) else celonis_view.DEFAULT_PORT,
                           open_browser="--no-open" not in flags)
        return 0
    if cmd == "index":
        import celonis_index
        celonis_index.main()
        return 0
    index = R.Index()
    run = COMMANDS.get(cmd)
    if run is None:
        print(__doc__)
        return 1
    pql = next((a for a in rest if a.startswith("TABLE(") or a.startswith("SUM(")), None)
    try:
        return run(cmd, phrase, index, opts, flags, pql)
    except jev.JevError as e:
        print(e, file=sys.stderr)
        return 1
    except (celonis_api.NoTenant, requests.RequestException) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
