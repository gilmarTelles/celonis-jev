"""A local page that shows the resolver deciding.

    python3 celonis_cli.py view            # http://127.0.0.1:8766
    python3 celonis_view.py --port 9000 --no-open

Same pipeline as `celonis resolve --json` - vocabulary, tenant name search, BM25,
one Jev call - but it shows what a terminal hides: the signals code read from the
ask, the candidates BM25 scored and what boosted them, the JSON of every Jev
request and every Jev answer with its probabilities, and the threshold each branch
of the decision was taken with. The verdict card is the result dict itself, the one
`celonis resolve --json` prints.

`Open` navigates the signed-in Chrome exactly like `celonis open` does, and learns
the phrase the same way (a judged resolution a human accepted joins the vocabulary).

Loopback only, read-only, and the API key stays on the server: the page is handed
URLs, never credentials.
"""

from __future__ import annotations

import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cdp
import celonis_api as api
import celonis_resolve as R
import cliargs
import jev

PAGE = Path(__file__).with_name("celonis_view.html")
DEFAULT_PORT = 8766
_lock = threading.Lock()
_state: dict = {}


class NoJev:
    """Stands in for the model when there is no key: the deterministic layers still run."""

    def __init__(self, why: str) -> None:
        self.why = why

    def ask(self, state, questions, cache=None):
        raise RuntimeError(self.why)


def index() -> R.Index:
    """One index for the whole server: loading it is the only slow part of a start."""
    with _lock:
        if "index" not in _state:
            _state["index"] = R.Index()
        return _state["index"]


def client():
    with _lock:
        if "client" not in _state:
            try:
                _state["client"] = jev.Jev()
            except jev.JevError as e:            # no key: the judgment cannot run
                _state["client"] = NoJev(str(e))
        return _state["client"]


def base() -> str:
    return api.base()


def resolve_traced(phrase: str) -> dict:
    """The result and the reasoning, or the reasoning alone when a step failed.

    A missing key or a dropped connection must not cost the page its trail: the
    steps that ran are still the answer to "what did it do before it stopped".
    """
    trail = R.Trail()
    try:
        result = R.resolve(phrase, client=client(), index=index(), verbose=False, trace=trail)
    except Exception as e:
        return {"result": None, "trail": trail.data, "error": f"{type(e).__name__}: {e}"}
    return {"result": result.to_dict(), "trail": trail.data}


def open_in_chrome(payload: dict) -> dict:
    """Navigate the signed-in tab, and learn the phrase - the CLI's `open`."""
    result = payload.get("result") or {}
    if not result.get("url"):
        return {"error": "nothing to open"}
    url = result["url"] if result["url"].startswith("http") else base() + result["url"]
    try:
        tab = cdp.find_tab("celonis.cloud") or cdp.new_tab(url)
        cdp.navigate(tab, url)
    except Exception as e:
        return {"url": url, "error": f"no browser to open ({type(e).__name__})"}
    learned = False
    if result.get("path") == "ranked" and result.get("entity_id"):
        index().vocab.remember(result["phrase"], {"id": result["entity_id"],
                                                  "kind": result.get("kind", "")})
        learned = True
    return {"url": url, "opened": url, "learned": learned}


def status() -> dict:
    idx = index()
    try:
        key = bool(jev.api_key())
    except jev.JevError:
        key = False
    try:
        tabs = [t["url"] for t in cdp.targets()]
    except Exception:
        tabs = []
    try:
        host = base()
    except Exception:
        host = ""
    return {"index": {"built": idx.built, "entries": len(idx.entries),
                      "columns_built": idx.columns.built},
            "vocabulary": {"phrases": len(idx.vocab.items),
                           "curated": sum(1 for a in idx.vocab.items if a["source"] == "curated"),
                           "learned": sum(1 for a in idx.vocab.items if a["source"] == "learned")},
            "jev": {"model": jev.MODEL, "key": key},
            "chrome": {"tabs": tabs[:10], "count": len(tabs)},
            "base": host,
            "thresholds": R.Trail().data["thresholds"],
            "asks": R.DEMO}


class Handler(BaseHTTPRequestHandler):
    server_version = "celonis-view"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:                     # noqa: N802 - http.server's spelling
        route = urlparse(self.path)
        ask = (parse_qs(route.query).get("q") or [""])[0].strip()
        try:
            if route.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif route.path == "/api/status":
                self._json(status())
            elif route.path in ("/api/resolve", "/api/open") and ask:
                payload = resolve_traced(ask)
                if route.path == "/api/open" and payload.get("result"):
                    payload["open"] = open_in_chrome(payload)
                self._json(payload)
            elif route.path in ("/api/resolve", "/api/open"):
                self._json({"error": "?q= takes the ask"}, 400)
            else:
                self._json({"error": f"no route {route.path}"}, 404)
        except Exception as e:                    # one bad ask must not kill the server
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def log_message(self, fmt: str, *args) -> None:
        pass                                      # a line per request would drown the banner


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"  celonis view  {url}   (ctrl-c to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("  stopped")
    finally:
        httpd.server_close()


def main() -> int:
    _args, opts = cliargs.parse_argv(sys.argv[1:])
    port = int(cliargs.flag(opts, "--port") or DEFAULT_PORT)
    serve(port, open_browser="--no-open" not in opts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
