"""Read-only SQL against a Celonis data pool, through the team API key.

The workbench is the only way to see the data layer without a browser: which
columns a table has, how many rows it holds, and when it was last loaded. The
index knows that the table exists; this answers what is inside it.

Nothing here may write. Every statement passes a check first and only a single
SELECT gets through - the workbench itself can write, so that guard is the whole
safety story.

The coordinates come from the tenant, never from constants:

    pool  /integration/api/pools
    job   /integration/api/pools/{pool}/jobs
    task  /integration/api/pools/{pool}/jobs/{job}/tasks/       (TRANSFORMATION)

    POST /integration/api/pools/{p}/jobs/{j}/workbench/{t}/execute   -> {id, status}
    GET  .../workbench/{t}/{id}                                       -> poll the status
    GET  .../workbench/{t}/{id}/results                               -> rows and column metadata

Run:  python3 workbench.py "SELECT COUNT(*) FROM \"orders\"" [--pool main] [--job refresh]
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

import celonis_api as api
import cliargs

INDEX = Path(__file__).with_name("celonis-index.json")
COLUMNS = Path(__file__).with_name("cache") / "table-columns.json"
SNAPSHOT = Path(__file__).with_name("celonis-columns.json")
REMEMBERED = Path(__file__).with_name("cache") / "workbench.json"
# The object tables of the OCPM model. Their columns are the object's fields, which
# the index already holds, so the scan leaves them alone - and they are views over
# several tables, which is why asking the workbench for one times out anyway.
OBJECT_TABLE = re.compile(r"^t_[a-z_]*custom_", re.I)
POOLS = "/integration/api/pools"
WRITE_WORDS = ("insert", "update", "delete", "merge", "upsert", "create", "drop", "alter",
               "truncate", "grant", "revoke", "copy", "unload", "export", "call", "replace",
               "rename", "vacuum", "refresh", "set", "use")


def pools() -> list[dict]:
    return api.get(POOLS)


def jobs(pool_id: str) -> list[dict]:
    return api.get(f"{POOLS}/{pool_id}/jobs")


def tasks(pool_id: str, job_id: str) -> list[dict]:
    return api.get(f"{POOLS}/{pool_id}/jobs/{job_id}/tasks/")


def pool_of(table: str) -> str | None:
    """Which pool the index says holds this table. Only the index knows that."""
    try:
        entries = json.loads(INDEX.read_text())["entries"]
    except Exception:
        return None
    hits = [e for e in entries
            if e.get("kind") == "table" and (e.get("name") or "").lower() == table.lower()]
    return hits[0]["poolId"] if len({e["poolId"] for e in hits}) == 1 else None


def pool_id(name_or_id: str, pool_list: list[dict] | None = None) -> dict:
    """The pool the caller means: a full id, an id prefix, or part of a name."""
    rows = pool_list if pool_list is not None else pools()
    needle = name_or_id.strip().lower()
    hits = [p for p in rows if p["id"] == name_or_id] or \
           [p for p in rows if p["id"].startswith(name_or_id)] or \
           [p for p in rows if needle and needle in (p.get("name") or "").lower()]
    if len({p["id"] for p in hits}) == 1:
        return hits[0]
    if not hits:
        raise LookupError(f"no pool matches {name_or_id!r} "
                          f"({[p['name'] for p in rows]})")
    raise LookupError(f"{name_or_id!r} matches {[p['name'] for p in hits]}")


def coordinates(pool: str, job: str | None = None, task: str | None = None) -> dict:
    """Resolve a pool + job + transformation task from names, not constants.

    A pool holds several jobs and only some run transformations. With no job
    named, the one whose transformations were edited most recently wins - that is
    the workbench a human last worked in; with no task named, the task that
    matches `task`, else the first available task.
    """
    p = pool_id(pool)
    rows = jobs(p["id"])
    if job:
        jobs_by_name = [j for j in rows if job.lower() in (j.get("name") or "").lower()]
        if not jobs_by_name:
            raise LookupError(f"no job matches {job!r} in {p['name']} "
                              f"({[j['name'] for j in rows]})")
        chosen = jobs_by_name[0]
    else:
        with_tasks = [(j, tasks(p["id"], j["id"])) for j in rows]
        with_tasks = [(j, t) for j, t in with_tasks if t]
        if not with_tasks:
            raise LookupError(f"no job in {p['name']} runs transformations "
                              f"({[j['name'] for j in rows]})")
        chosen, _ = max(with_tasks,
                        key=lambda jt: (max((t.get("taskEditedAt") or 0) for t in jt[1]),
                                        len(jt[1])))
    task_rows = tasks(p["id"], chosen["id"])
    if not task_rows:
        raise LookupError(f"job {chosen['name']!r} has no transformation task")
    pick = next((t for t in task_rows if task and task.lower() in (t.get("name") or "").lower()),
                task_rows[0])
    if task and task.lower() not in (pick.get("name") or "").lower():
        raise LookupError(f"no task matches {task!r} in {chosen['name']} "
                          f"({[t['name'] for t in task_rows]})")
    return {"pool": p["name"], "poolId": p["id"], "job": chosen["name"], "jobId": chosen["id"],
            "task": pick.get("name", ""), "taskId": pick["id"],
            "schema": p["id"] + "_OCDM"}


def guard(sql: str) -> str:
    """Return the statement if it is a single read, else raise."""
    body = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    body = re.sub(r"--[^\n]*", " ", body).strip().rstrip(";").strip()
    if not body:
        raise ValueError("empty statement")
    if ";" in body:
        raise ValueError("one statement at a time")
    head = body.split(None, 1)[0].lower()
    if head not in ("select", "with"):
        raise ValueError(f"read-only: {head} is not a query")
    low = f" {body.lower()} "
    for word in WRITE_WORDS:
        if re.search(rf"\b{word}\b", low):
            raise ValueError(f"read-only: {word} is not allowed")
    return body


def run(sql: str, pool: str | None = None, job: str | None = None, task: str | None = None,
        seconds: float = 45.0, tries: int = 200, where: dict | None = None) -> dict:
    """Execute one read statement. -> {status, error, columns, rows, where, sql}

    `seconds` is the wall clock this statement gets, poll gaps and slow responses
    included - a workbench task can sit on a statement indefinitely, and a scan of
    132 tables must not be held hostage by one of them.
    """
    statement = guard(sql)
    if where is None:
        if pool is None:
            raise LookupError("which pool? pass --pool (a name or an id), or use describe() "
                              "on a table the index knows")
        where = coordinates(pool, job, task)
    base, h = api.base(), api.headers()
    work = f"{POOLS}/{where['poolId']}/jobs/{where['jobId']}/workbench/{where['taskId']}"
    out = {"where": where, "sql": statement, "status": None, "error": None,
           "columns": [], "rows": [], "execution": None}
    r = requests.post(base + work + "/execute", headers=h,
                      data=json.dumps({"statement": statement}), timeout=60)
    r.raise_for_status()
    eid = r.json().get("id")
    out["execution"] = eid
    deadline = time.time() + seconds

    def poll(path: str, times: int = 8):
        """The workbench answers an empty body now and then; that is a hiccup."""
        for _ in range(times):
            if time.time() > deadline:
                return {}
            try:
                return requests.get(base + work + path, headers=h, timeout=30).json()
            except Exception:
                time.sleep(1)
        return {}

    for _ in range(tries):
        if time.time() > deadline:
            break
        st = poll(f"/{eid}")
        if st.get("status") in ("SUCCESS", "FAILED", "ERROR"):
            items = poll(f"/{eid}/results", times=8)
            items = items if isinstance(items, list) else []
            # The results list is the task's history, so the item has to be *this*
            # execution: by id first, then by the statement text. Falling back to
            # the last item would report somebody else's columns as this answer.
            match = next((i for i in items if i.get("id") == eid), None) or \
                next((i for i in items if " ".join(str(i.get("query") or "").split()) == statement), None)
            if match is None:
                time.sleep(1)
                continue
            result = match.get("result") or {}
            out["status"] = match.get("status") or st.get("status")
            out["error"] = match.get("error") or st.get("error")
            out["columns"] = [{"name": c.get("name"), "type": c.get("data_type") or c.get("type")}
                              for c in (result.get("columns") or [])]
            out["rows"] = result.get("tableContent") or []
            return out
        time.sleep(1)
    out["status"] = "TIMEOUT"
    return out


def load_remembered() -> dict:
    try:
        return json.loads(REMEMBERED.read_text())
    except Exception:
        return {}


def remember(where: dict) -> None:
    """Keep the coordinate that last answered, so the next call starts there."""
    store = load_remembered()
    store[where["poolId"]] = {k: where[k] for k in ("job", "jobId", "task", "taskId")}
    REMEMBERED.parent.mkdir(exist_ok=True)
    REMEMBERED.write_text(json.dumps(store, indent=1) + "\n")


def candidate_workbenches(pool: dict, task: str | None = None) -> list[dict]:
    """Every transformation task that could run SQL, best first.

    Any task works - the workbench executes against the pool - but they are not
    equally healthy: one task answered a count in 4 s while another timed out past
    40 s on the same statement. So the caller tries them in turn, starting with the
    one that answered last time, then the most recently edited.
    """
    out = []
    for j in jobs(pool["id"]):
        for t in tasks(pool["id"], j["id"]):
            out.append({"pool": pool["name"], "poolId": pool["id"], "schema": pool["id"] + "_OCDM",
                        "job": j["name"], "jobId": j["id"], "task": t.get("name", ""),
                        "taskId": t["id"], "edited": t.get("taskEditedAt") or 0})
    if task:
        picked = [w for w in out if task.lower() in w["task"].lower()]
        out = picked or out
    out.sort(key=lambda w: -w["edited"])
    last = load_remembered().get(pool["id"])
    if last:
        first = next((w for w in out if w["taskId"] == last.get("taskId")), None)
        if first:
            out.remove(first)
            out.insert(0, first)
    return out


def run_any(sql: str, pool: str | None = None, job: str | None = None, task: str | None = None,
            seconds: float = 30.0, attempts: int = 3) -> dict:
    """Run a read on the first workbench coordinate that answers."""
    statement = guard(sql)
    if pool is None:
        raise LookupError("which pool? pass --pool (a name or an id)")
    p = pool_id(pool)
    candidates = candidate_workbenches(p, task)
    if job:
        candidates = [w for w in candidates if job.lower() in w["job"].lower()] or candidates
    if not candidates:
        raise LookupError(f"no transformation task in {p['name']} to run SQL through")
    last = None
    for where in candidates[:attempts]:
        last = run(statement, where=where, seconds=seconds)
        usable = last["status"] != "TIMEOUT" and (
            last["columns"] or not statement.lower().lstrip().startswith("select *"))
        if usable:
            remember(where)
            return last
    return last


def describe(table: str, pool: str | None = None, where: dict | None = None) -> dict:
    """Columns, row count and last load of one table - two cheap reads."""
    name = table.strip().strip('"')
    pool = pool or pool_of(name)
    if not pool:
        raise LookupError(f"the index does not say which pool holds {name!r}; pass --pool")
    shape = run_any(f'SELECT * FROM "{name}" LIMIT 1', pool=pool, seconds=45) if where is None \
        else run(f'SELECT * FROM "{name}" LIMIT 1', where=where, seconds=45)
    where = shape["where"]
    out = {"table": name, "where": where}
    out["columns"] = [c["name"] for c in shape["columns"]]
    out["error"] = shape["error"]
    if shape["status"] == "SUCCESS":
        stats = run(f'SELECT COUNT(*) AS n_rows, MAX("_CELONIS_CHANGE_DATE") AS loaded '
                    f'FROM "{name}"', where=where, seconds=45)
        row = (stats["rows"] or [[None, None]])[0]
        out["rows"] = int(row[0]) if row and str(row[0]).isdigit() else row[0]
        out["loaded"] = row[1] if len(row) > 1 else None
        out["error"] = out["error"] or stats["error"]
    return out


def load_columns() -> dict:
    """The cached column scan: {poolId: {table: {columns, types, at|error}}}.

    The scan itself is a cache, not in git; the index builder writes a committed
    snapshot of it, so a clone with no cache still answers a column question. What
    was scanned here wins for the tables it covers, the snapshot fills the rest.
    """
    out: dict = {}
    try:
        snap = json.loads(SNAPSHOT.read_text())
    except Exception:
        snap = {}
    for pool, tables in (snap.get("pools") or {}).items():
        out[pool] = {name: {"columns": cols, "at": snap.get("built", "")}
                     for name, cols in tables.items()}
    try:
        for pool, tables in json.loads(COLUMNS.read_text()).items():
            out.setdefault(pool, {}).update(tables)
    except Exception:
        pass
    return out


def save_columns(store: dict) -> None:
    COLUMNS.parent.mkdir(exist_ok=True)
    COLUMNS.write_text(json.dumps(store, indent=1) + "\n")


def _save_pool(store: dict, pool: str, have: dict) -> None:
    """Fold one pool's tables into the scan cache and write it out."""
    store[pool] = have
    save_columns(store)


def failover_reader(candidates: list[dict], seconds: float):
    """A read function over the candidate workbenches, failing over between them.

    When the current task stalls or answers without columns, the next candidate
    is tried and a working one sticks for the tables that follow; the coordinate
    worth remembering is exposed as `read.where`.
    """

    def read(name: str) -> dict:
        """One table's columns. Tries the next task when this one stalls or lies."""
        out = run(f'SELECT * FROM "{name}" LIMIT 1', where=read.where, seconds=seconds)
        for spare in candidates[:3]:
            if out["status"] == "SUCCESS" and out["columns"]:
                break
            if spare["taskId"] == read.where["taskId"]:
                continue
            out = run(f'SELECT * FROM "{name}" LIMIT 1', where=spare, seconds=seconds)
            if out["status"] == "SUCCESS" and out["columns"]:
                read.where = spare
        return out

    read.where = candidates[0]
    return read


def scan_columns(pool: str, tables_wanted: list[str] | None = None, refresh: bool = False,
                 limit: int | None = None, seconds: float = 18.0, stop_after: int = 5,
                 report=None) -> dict:
    """Columns and types for every table in a pool, cached and resumable.

    `/tables` answers with `columns: []` for every table, so the column list has to
    come from the workbench, one cheap read per table. A pool of 131 tables takes
    minutes, so every result is written out as it arrives: interrupt it, run it
    again, and only what is missing (or failed) is retried.
    """
    p = pool_id(pool)
    store = load_columns()
    have = {} if refresh else dict(store.get(p["id"], {}))
    names = tables_wanted if tables_wanted is not None else \
        sorted(t.get("name") for t in requests.get(
            api.base() + f"{POOLS}/{p['id']}/tables", headers=api.headers(), timeout=60).json()
            if t.get("name"))
    stamp0 = time.strftime("%Y-%m-%dT%H:%M:%S")
    for name in names:
        if OBJECT_TABLE.match(name):     # always: an earlier run may have timed out on it
            have[name] = {"note": "an object table: its columns are the object's own "
                                  "fields, which the index already holds - celonis uses <field>",
                          "at": stamp0}
    _save_pool(store, p["id"], have)
    # Never seen first, then the ones that failed last time: a rerun always makes
    # progress, and stopping early can only cut short the retries.
    todo = [n for n in names if n not in have] + \
           [n for n in names if n in have and have[n].get("error")]
    if limit is not None:                       # limit=0 asks for the state, not a scan
        todo = todo[:limit]
    candidates = candidate_workbenches(p)
    if not candidates:
        raise LookupError(f"no transformation task in {p['name']} to run SQL through")
    read = failover_reader(candidates, seconds)

    misses = 0
    for i, name in enumerate(todo, 1):
        out = read(name)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        if out["status"] == "TIMEOUT":
            # The object tables are views over several tables and some are simply
            # too slow to sweep. Stop rather than grind through them; they stay
            # recorded, and the next run retries them last.
            misses += 1
            if stop_after and misses >= stop_after:
                save_columns(store)
                if report:
                    report(i, len(todo), name, {"error": f"stopped after {misses} timeouts in a row"})
                break
        else:
            misses = 0
        if out["status"] == "SUCCESS" and out["columns"]:
            have[name] = {"columns": [c["name"] for c in out["columns"]],
                          "types": [c.get("type") for c in out["columns"]], "at": stamp}
            remember(read.where)
        elif out["status"] == "SUCCESS":
            have[name] = {"error": "no columns came back - is that a table in this pool?",
                          "at": stamp}
        else:
            have[name] = {"error": str(out["error"] or out["status"])[:200], "at": stamp}
        _save_pool(store, p["id"], have)
        if report:
            report(i, len(todo), name, have[name])
    _save_pool(store, p["id"], have)
    return have


def main() -> int:
    args, opts = cliargs.parse_argv(sys.argv[1:])
    pool = cliargs.flag(opts, "--pool")
    if not args:
        print(__doc__)
        return 0
    sql = args[0]
    if sql.lower() in ("describe", "table") and len(args) > 1:
        print(json.dumps(describe(args[1], pool=pool), indent=1))
        return 0
    out = run_any(sql, pool=pool, job=cliargs.flag(opts, "--job"),
                  task=cliargs.flag(opts, "--task"))
    w = out["where"]
    print(f"{w['pool']} / {w['job']} / {w['task']}  [{w['poolId'][:8]} {w['jobId'][:8]} {w['taskId'][:8]}]")
    print("status:", out["status"])
    if out["error"]:
        print("error:", str(out["error"])[:400])
    print("cols:", [c["name"] for c in out["columns"]])
    for row in out["rows"][:40]:
        print("  ", row)
    return 0 if out["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
