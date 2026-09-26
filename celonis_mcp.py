"""celonis as an MCP server: use it from any chat without leaving the conversation.

    {"command": "python3", "args": ["celonis_mcp.py"], "cwd": "<this repo>"}

Stdio transport, newline-delimited JSON-RPC, no dependencies beyond this repo
(the resolver, the index, cdp.py). Tools:

    celonis_search(phrase)       -> candidates with ids, other instances as copies (no Jev)
    celonis_open(id | phrase)    -> navigate the signed-in tab; the URL either way
    celonis_read(id | phrase)    -> run a KPI's PQL, return the rows
    celonis_resolve(phrase)      -> where a phrase points, with alternatives (Jev)
    celonis_doctor()             -> what is missing, and the one command that fixes it

The flow an agent follows: search, pick a candidate, open or read it by id.
Only a phrase that needs judgment reaches Jev, so search and every by-id call
work with no TypeSafe key.
"""

from __future__ import annotations

import json
import sys
import traceback

import celonis_api as api
import celonis_cli as C
import celonis_pql as P
import celonis_resolve as R
import cdp

VERSION = "0.1.0"
PROTOCOL = "2024-11-05"

ONE_OF = {"id": {"type": "string", "description": "A candidate id from celonis_search."},
          "phrase": {"type": "string", "description": "What the user wants, in their words."}}

TOOLS = [
    {
        "name": "celonis_search",
        "description": ("Find Celonis assets, KPIs, objects, tables and pages matching a phrase. "
                        "Returns ranked candidates with ids, one per kind and name; copies lists "
                        "other instances, so pick the one in the right container. Local index only: "
                        "no Jev key, tenant or browser needed. Then pass an id to celonis_open "
                        "or celonis_read."),
        "inputSchema": {"type": "object", "properties": {
            "phrase": {"type": "string", "description": "What to look for."},
            "k": {"type": "integer", "description": "How many candidates (default 10)."}},
            "required": ["phrase"]},
    },
    {
        "name": "celonis_open",
        "description": ("Open one Celonis location in the signed-in browser tab and return its URL "
                        "either way. Pass an id from celonis_search, or a phrase to resolve "
                        "(a phrase may need a Jev judgment). Exactly one of id or phrase."),
        "inputSchema": {"type": "object", "properties": ONE_OF},
    },
    {
        "name": "celonis_read",
        "description": ("Run a KPI's PQL through the signed-in session and return the rows. Pass a "
                        "KPI id from celonis_search, or a phrase to resolve to a KPI. Exactly one "
                        "of id or phrase; optional pql runs instead of the KPI's own."),
        "inputSchema": {"type": "object", "properties": {
            **ONE_OF,
            "pql": {"type": "string", "description": "Optional PQL to run instead."}}},
    },
    {
        "name": "celonis_resolve",
        "description": ("Resolve a plain-language request to one Celonis location with a "
                        "confidence and the runner-ups, each with an id for celonis_open. Uses a "
                        "Jev judgment when the phrase is ambiguous. Read-only; no browser."),
        "inputSchema": {"type": "object", "properties": {
            "phrase": {"type": "string", "description": "What the user wants, in their words."}},
            "required": ["phrase"]},
    },
    {
        "name": "celonis_doctor",
        "description": ("Check this environment: credentials, tenant reachability, index age, "
                        "browser session. Call it first when a tool fails."),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text(payload, is_error: bool = False) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=1)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


NEEDS = {"celonis_search": "phrase", "celonis_resolve": "phrase",
         "celonis_open": "id|phrase", "celonis_read": "id|phrase"}


def parse_args(name: str, raw) -> tuple[dict, str | None]:
    """The tool boundary: cleaned arguments, or a short message saying what is wrong.

    Strings are stripped and an empty one counts as absent, so the handlers can
    trust `args["phrase"]`, `args.get("id")` and `args["k"]`.
    """
    if not isinstance(raw, dict):
        return {}, "arguments must be a JSON object"
    args: dict = {}
    for key in ("phrase", "id", "pql"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            return {}, f"{key} must be a string"
        if value.strip():
            args[key] = value.strip()
    k = raw.get("k", 10)
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 50:
        return {}, "k must be an integer from 1 to 50"
    args["k"] = k
    need = NEEDS.get(name)
    if need == "phrase" and "phrase" not in args:
        return {}, "phrase is required"
    if need == "id|phrase" and ("id" in args) == ("phrase" in args):
        return {}, "pass exactly one of id (from celonis_search) or phrase"
    return args, None


_index: dict = {}


def shared_index() -> R.Index:
    """One index per server process, reloaded when celonis-index.json changes.

    Loading it (and the embedding cache behind search) is the slow part of a call.
    """
    try:
        stamp = R.INDEX.stat().st_mtime_ns
    except OSError:
        stamp = None
    if _index.get("stamp", object()) != stamp:
        _index.update(stamp=stamp, index=R.Index())
    return _index["index"]


def _tab() -> dict | None:
    """The signed-in tenant tab, or None when there is no CDP browser at all."""
    try:
        return cdp.find_tab("celonis.cloud")
    except OSError:
        return None


def search(args: dict) -> dict:
    return _text(C.search_report(args["phrase"], shared_index(), args["k"],
                                 "pick one and call celonis_open or celonis_read with its id"))


def resolve(args: dict) -> dict:
    hit = R.resolve(args["phrase"], index=shared_index(), verbose=False)
    return _text({"hit": {"id": hit.ref, "name": hit.name, "kind": hit.kind,
                          "url": hit.url} if hit.name else None,
                  "reason": hit.reason,
                  "confidence": hit.confidence, "confirm": hit.confirm,
                  "intent": hit.intent,
                  "alternatives": hit.alternatives[:3],
                  "next": ("open_url" if hit.name else "not_found"),
                  "warnings": hit.warnings})


def _opened(name: str, kind: str, url: str, handle: str, warnings: list[str], **extra) -> dict:
    full, more = api.absolute_or_relative(url)
    opened = C.navigate(full)
    return _text({"opened": opened, "url": full, "id": handle, "name": name, "kind": kind,
                  **extra, "note": "" if opened else "no CDP browser; give the URL to the user",
                  "warnings": warnings + more})


def open_(args: dict) -> dict:
    index = shared_index()
    if args.get("id"):
        try:
            e = R.entry_for(args["id"], index)
        except LookupError as err:
            return _text({"opened": False, "reason": str(err)}, is_error=True)
        return _opened(e["name"], e["kind"], e["url"], args["id"], [])
    hit = R.resolve(args["phrase"], index=index, verbose=False)
    if not hit.name:
        return _text({"opened": False, "reason": hit.reason or "no match",
                      "alternatives": hit.alternatives[:3],
                      "warnings": hit.warnings}, is_error=True)
    if hit.confirm:
        url, more = api.absolute_or_relative(hit.url)
        return _text({"opened": False, "ambiguous": True,
                      "candidates": [{"id": hit.ref, "name": hit.name, "url": url,
                                      "confidence": hit.confidence}] + hit.alternatives[:3],
                      "next": "ask the user which one, then call celonis_open with its id",
                      "warnings": hit.warnings + more})
    return _opened(hit.name, hit.kind, hit.url, hit.ref, hit.warnings, confidence=hit.confidence)


def read(args: dict) -> dict:
    index = shared_index()
    warnings: list[str] = []
    if args.get("id"):
        try:
            entry = R.entry_for(args["id"], index)
        except LookupError as err:
            return _text({"answered": False, "reason": str(err)}, is_error=True)
        try:
            k, km = P.kpi_definition(entry, index)
        except LookupError as err:
            return _text({"answered": False, "reason": str(err),
                          "url": api.absolute_or_relative(entry["url"])[0]}, is_error=True)
        pql = args.get("pql") or k["pql"]
    elif args.get("pql"):
        pql = args["pql"]
        km = R.km_for_ask(args["phrase"], index)
    else:
        hit = R.resolve(args["phrase"], index=index, verbose=False)
        warnings = hit.warnings
        if (hit.kind or "") != "kpi":
            return _text({"answered": False,
                          "reason": f'"{args["phrase"]}" resolved to '
                                    f'{hit.kind or "nothing"}; read needs a KPI',
                          "url": hit.url}, is_error=True)
        found = P.find_kpi(hit.name, index)
        if not found:
            return _text({"answered": False, "reason": "no KPI definition matched"}, is_error=True)
        k, km = found
        pql = k["pql"]
    tab = _tab()
    if not tab:
        return _text({"answered": False, "pql": pql, "reason": "no signed-in tab on the sandbox "
                      "(run `celonis doctor`)"}, is_error=True)
    out = P.execute(pql, km, index, tab)
    if out.get("error"):
        return _text({"answered": False, "pql": pql, "error": out["error"][:300]}, is_error=True)
    return _text({"answered": True, "pql": P.pql_table(pql),
                  "columns": out["columns"], "rows": out["rows"][:20],
                  "query_ms": out["ms"], "warnings": warnings})


def doctor(args: dict) -> dict:
    lines = C.doctor(report=False)
    return _text("\n".join(lines), is_error=any(l.startswith("FAIL") for l in lines))


HANDLERS = {"celonis_search": search, "celonis_open": open_, "celonis_read": read,
            "celonis_resolve": resolve, "celonis_doctor": doctor}


def call(name: str, args: dict) -> dict:
    run = HANDLERS.get(name)
    if run is None:
        return _text(f"unknown tool: {name}", is_error=True)
    args, bad = parse_args(name, args)
    if bad:
        return _text(bad, is_error=True)
    try:
        return run(args)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return _text(f"{type(e).__name__}: {e}", is_error=True)


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL,
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "celonis-jev", "version": VERSION}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        result = call(params.get("name", ""), params.get("arguments") or {})
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
