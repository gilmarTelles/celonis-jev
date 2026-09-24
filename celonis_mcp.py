"""celonis as an MCP server: use it from any chat without leaving the conversation.

    {"command": "python3", "args": ["celonis_mcp.py"], "cwd": "<this repo>"}

Stdio transport, newline-delimited JSON-RPC, no dependencies beyond this repo
(the resolver, the index, cdp.py). Tools:

    celonis_resolve(phrase)  -> where a phrase points, with alternatives
    celonis_open(phrase)     -> resolve, then navigate the signed-in tab
    celonis_read(phrase)     -> resolve to a KPI, run its PQL, return the rows
    celonis_doctor()         -> what is missing, and the one command that fixes it
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

TOOLS = [
    {
        "name": "celonis_resolve",
        "description": ("Resolve a plain-language request to a Celonis location: which space, "
                        "package, asset, object, event, KPI or app page it refers to, plus a "
                        "ready-to-use deep link and the runner-up candidates with "
                        "probabilities. Read-only; no browser needed."),
        "inputSchema": {"type": "object", "properties": {
            "phrase": {"type": "string", "description": "What the user wants, in their words."}},
            "required": ["phrase"]},
    },
    {
        "name": "celonis_open",
        "description": ("Resolve a phrase and navigate the signed-in browser tab to it. Use when "
                        "the user wants to be taken somewhere. Returns the URL either way."),
        "inputSchema": {"type": "object", "properties": {
            "phrase": {"type": "string"}}, "required": ["phrase"]},
    },
    {
        "name": "celonis_read",
        "description": ("Answer a data question: resolve the phrase to a KPI, run its PQL through "
                        "the signed-in session and return the resulting rows."),
        "inputSchema": {"type": "object", "properties": {
            "phrase": {"type": "string"},
            "pql": {"type": "string", "description": "Optional PQL to run instead of resolving."}},
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


def call(name: str, args: dict) -> dict:
    try:
        if name == "celonis_resolve":
            index = R.Index()
            hit = R.resolve(args["phrase"], index=index, verbose=False)
            return _text({"hit": {"name": hit.name, "kind": hit.kind,
                                  "url": hit.url} if hit.name else None,
                          "reason": hit.reason,
                          "confidence": hit.confidence, "confirm": hit.confirm,
                          "intent": hit.intent,
                          "alternatives": hit.alternatives[:3],
                          "next": ("open_url" if hit.name else "not_found")})
        if name == "celonis_open":
            index = R.Index()
            hit = R.resolve(args["phrase"], index=index, verbose=False)
            if not hit.name:
                return _text({"opened": False, "reason": hit.reason or "no match",
                              "alternatives": hit.alternatives[:3]}, is_error=True)
            if hit.confirm:
                return _text({"opened": False, "ambiguous": True,
                              "candidates": [{"name": hit.name, "url": api.absolute(hit.url),
                                              "confidence": hit.confidence}] + hit.alternatives[:3],
                              "next": "ask the user which one"})
            url = api.absolute(hit.url)
            opened = False
            try:
                tab = cdp.find_tab("celonis.cloud") or cdp.new_tab(url)
                cdp.navigate(tab, url)
                opened = True
            except Exception:
                pass
            return _text({"opened": opened, "url": url, "name": hit.name,
                          "kind": hit.kind, "confidence": hit.confidence,
                          "note": "" if opened else "no CDP browser; give the URL to the user"})
        if name == "celonis_read":
            index = R.Index()
            if args.get("pql"):
                pql = args["pql"]
                km = R.km_for_ask(args["phrase"], index)
            else:
                hit = R.resolve(args["phrase"], index=index, verbose=False)
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
            tab = cdp.find_tab("celonis.cloud")
            if not tab:
                return _text({"answered": False, "reason": "no signed-in tab on the sandbox "
                              "(run `celonis doctor`)"}, is_error=True)
            out = P.execute(pql, km, index, tab)
            if out.get("error"):
                return _text({"answered": False, "pql": pql, "error": out["error"][:300]}, is_error=True)
            return _text({"answered": True, "pql": P.pql_table(pql),
                          "columns": out["columns"], "rows": out["rows"][:20],
                          "query_ms": out["ms"]})
        if name == "celonis_doctor":
            lines = C.doctor(report=False)
            return _text("\n".join(lines), is_error=any(l.startswith("FAIL") for l in lines))
        return _text(f"unknown tool: {name}", is_error=True)
    except Exception as e:
        return _text(f"{type(e).__name__}: {e}\n{traceback.format_exc()[-400:]}", is_error=True)


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
