"""TypeSafe /v1/systemone client.

One call carries every question the step might need; the state is sent once. The
answers come back keyed exactly as asked. Responses are content-addressed onto
disk so reruns and evals replay for free.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
PRICE_IN = 0.042  # USD per 1M input tokens, Jev as of 2026-09
CACHE_DIR = Path(__file__).with_name("cache")


class JevError(RuntimeError):
    """No key, a refused request, or retries exhausted: the judgment cannot run."""


def api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    env = Path.home() / ".config/typesafe/env.sh"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("TYPESAFE_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise JevError("TYPESAFE_API_KEY not set and ~/.config/typesafe/env.sh missing")


def _digest(state, questions, model: str) -> str:
    blob = json.dumps({"s": state, "q": questions, "m": model},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class Jev:
    def __init__(self, model: str = MODEL, use_cache: bool = True) -> None:
        self.model = model
        self.use_cache = use_cache
        self.key = api_key()
        self.calls = 0
        self.replays = 0
        self.input_tokens = 0
        self.seconds = 0.0

    @property
    def cost_usd(self) -> float:
        return self.input_tokens / 1e6 * PRICE_IN

    def ask(self, state, questions: dict, cache: bool | None = None) -> tuple[dict, dict, float, bool]:
        """-> (answers, usage, latency_s, from_cache)"""
        use_cache = self.use_cache if cache is None else cache
        CACHE_DIR.mkdir(exist_ok=True)
        path = CACHE_DIR / f"{_digest(state, questions, self.model)}.json"

        if use_cache and path.exists():
            payload = json.loads(path.read_text())
            self.replays += 1
            self.calls += 1
            self.input_tokens += payload["usage"]["input_tokens"]
            return payload["answers"], payload["usage"], 0.0, True

        body = json.dumps({"state": state, "model": self.model,
                           "questions": questions}, ensure_ascii=False).encode()
        last = None
        for attempt in range(4):
            req = urllib.request.Request(
                API, data=body,
                headers={"Authorization": f"Bearer {self.key}",
                         "Content-Type": "application/json"})
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    payload = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
                if e.code < 500 and e.code != 429:
                    raise JevError(last)
            except Exception as e:  # transient network
                last = repr(e)
            time.sleep(1.5 * (attempt + 1))
        else:
            raise JevError(f"typesafe call failed: {last}")

        latency = time.perf_counter() - started
        usage = payload["usage"]
        self.calls += 1
        self.input_tokens += usage["input_tokens"]
        self.seconds += latency
        if use_cache:
            path.write_text(json.dumps({"answers": payload["answers"],
                                        "usage": usage}, indent=1))
        return payload["answers"], usage, latency, False
