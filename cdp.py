"""Minimal Chrome DevTools Protocol client: stdlib only.

Enough CDP for this job: list tabs, open a tab on a URL, and evaluate JavaScript
in a tab (which is how the CLI borrows the signed-in browser session for reads).
No websocket dependency - just the handshake and the two frame shapes CDP uses.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import urllib.parse
import urllib.request

DEFAULT_PORT = int(os.environ.get("CELONIS_CDP_PORT", "9222"))


def _http(port: int, path: str, method: str = "GET", timeout: float = 10.0):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def alive(port: int = DEFAULT_PORT) -> bool:
    try:
        _http(port, "/json/version", timeout=3)
        return True
    except Exception:
        return False


def targets(port: int = DEFAULT_PORT) -> list[dict]:
    return [t for t in _http(port, "/json/list") if t.get("type") == "page"]


def find_tab(match: str = "", port: int = DEFAULT_PORT) -> dict | None:
    for t in targets(port):
        if match and match not in t.get("url", ""):
            continue
        return t
    return None


def new_tab(url: str, port: int = DEFAULT_PORT) -> dict:
    """Chrome >= 111 requires PUT on /json/new."""
    query = urllib.parse.quote(url, safe="")
    try:
        return _http(port, f"/json/new?{query}", method="PUT")
    except Exception:
        return _http(port, f"/json/new?{query}", method="GET")


class WS:
    """One websocket connection, text frames, client-side masking."""

    def __init__(self, url: str, timeout: float = 60.0) -> None:
        u = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake closed")
            buf += chunk
        if b"101" not in buf.split(b"\r\n")[0]:
            raise RuntimeError(f"websocket handshake failed: {buf.split(b'\r\n')[0]!r}")
        self._buf = buf.split(b"\r\n\r\n", 1)[1]

    def _send(self, payload: bytes) -> None:
        header = bytearray([0x81])                      # FIN + text
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self) -> str:
        data = b""
        while True:
            b0, b1 = self._recv_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if (b1 & 0x80) else None
            payload = self._recv_exact(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("websocket closed by peer")
            if opcode in (0x1, 0x2, 0x0):
                data += payload
                if fin:
                    return data.decode("utf-8", "replace")

    def call(self, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
        self._send(json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode())
        while True:
            msg = json.loads(self.recv())
            if msg.get("id") == msg_id:
                return msg

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


def evaluate(tab: dict, expression: str, await_promise: bool = True, timeout: float = 60.0):
    ws = WS(tab["webSocketDebuggerUrl"], timeout=timeout)
    try:
        res = ws.call("Runtime.evaluate",
                      {"expression": expression, "awaitPromise": await_promise,
                       "returnByValue": True, "userGesture": True})
        result = res.get("result", {})
        if "exceptionDetails" in result:
            raise RuntimeError(str(result["exceptionDetails"])[:400])
        return result.get("result", {}).get("value")
    finally:
        ws.close()


def navigate(tab: dict, url: str, timeout: float = 30.0) -> None:
    ws = WS(tab["webSocketDebuggerUrl"], timeout=timeout)
    try:
        ws.call("Page.navigate", {"url": url})
    finally:
        ws.close()
