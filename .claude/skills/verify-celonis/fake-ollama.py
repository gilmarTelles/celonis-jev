"""A broken embedding server for verify-celonis: `python3 fake-ollama.py <port> <hang|500>`.

/api/version answers like Ollama, so search gets as far as the query embed.
/api/embed then never answers (hang) or answers HTTP 500. Each request is
logged as one stdout line, which is how a proof counts re-probes.
"""

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, MODE = int(sys.argv[1]), sys.argv[2]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        print(f"GET {self.path}", flush=True)
        self._send(200, b'{"version":"0.0.0-fake"}') if self.path == "/api/version" else self._send(404, b"{}")

    def do_POST(self):
        print(f"POST {self.path}", flush=True)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if MODE == "hang":
            threading.Event().wait()
        self._send(500, b'{"error":"fake-ollama: model failed"}')


ThreadingHTTPServer.daemon_threads = True
ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
