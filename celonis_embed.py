"""Optional semantic ranking for `search()`: a local sentence-embedding model.

A paraphrase shares meaning with an entry's name but not its words, which BM25
cannot bridge. When a local Ollama serves an embedding model, every index entry
is embedded once (cached under cache/, keyed by the index's build stamp) and the
ask is embedded at search time; `search()` fuses that ranking with BM25.

Optional by construction: when Ollama is unreachable (1 s connect timeout) this
warns once and `rank()` returns None, and `search()` is plain BM25 again.

    CELONIS_EMBED_URL    default http://localhost:11434; "off" disables
    CELONIS_EMBED_MODEL  default mxbai-embed-large
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import sys
import time
from array import array
from pathlib import Path

import requests

URL = os.environ.get("CELONIS_EMBED_URL", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("CELONIS_EMBED_MODEL", "mxbai-embed-large")
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "  # mxbai's retrieval prompt
CACHE = Path(__file__).with_name("cache")
TIMEOUT = (1.0, 180.0)   # connect fast-fails when nothing listens; a cold model load takes ~50 s
BATCH = 64

KIND_TEXT = {"board_v2": "view (dashboard)", "kpi": "KPI", "semantic_model": "knowledge model",
             "section": "settings page", "task_type_v2": "task type", "datajob": "data job"}


def words(name: str) -> str:
    """CustomObjectName / SNAKE_CASE / kebab-case -> plain words, stopwords kept."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    return re.sub(r"[_\-./]+", " ", s).strip()


def entry_text(e: dict) -> str:
    kind = KIND_TEXT.get(e["kind"], e["kind"].replace("_", " ").replace("-", " "))
    text = f"{kind}: {words(e['name'])}"
    where = e.get("package") or e.get("space") or e.get("pool")
    if where and e["kind"] not in ("package", "space", "pool"):
        text += f" (in {where})"
    if e["kind"] == "object" and e.get("fields"):
        fields = e["fields"] if isinstance(e["fields"], list) else re.findall(r"'([^']+)'", str(e["fields"]))
        text += "; fields: " + ", ".join(words(f) for f in fields[:12])
    if e.get("hint"):
        text += f"; {str(e['hint'])[:160]}"
    return text


def _dot(a: array, b: array) -> float:
    return math.sumprod(a, b) if hasattr(math, "sumprod") else sum(map(float.__mul__, a, b))


class Embeddings:
    """Unit vectors per entry, or `ok = False` and a single warning."""

    def __init__(self, entries: list[dict], built: str) -> None:
        self.ok = False
        self.entries = entries
        self.vectors: list[array] = []
        self.stats: dict = {}
        if URL.lower() in ("", "off", "none"):
            return
        try:
            self._load(built)
            self.ok = True
        except (requests.RequestException, KeyError, ValueError) as err:
            print(f"! semantic search off ({URL}, {MODEL}): {type(err).__name__}; "
                  f"search is BM25 only", file=sys.stderr)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        r = requests.post(f"{URL}/api/embed", timeout=TIMEOUT,
                          json={"model": MODEL, "input": texts, "keep_alive": "30m"})
        r.raise_for_status()
        return r.json()["embeddings"]

    def _load(self, built: str) -> None:
        requests.get(f"{URL}/api/version", timeout=TIMEOUT).raise_for_status()
        path = CACHE / f"embeddings-{re.sub(r'[^A-Za-z0-9]+', '-', built)}-{MODEL.replace(':', '-')}.json"
        stored: dict[str, str] = {}
        if path.exists():
            stored = json.loads(path.read_text()).get("vectors", {})
        texts = [entry_text(e) for e in self.entries]
        missing = sorted({t for t in texts if t not in stored})
        started = time.perf_counter()
        if missing:
            print(f"embedding {len(missing)} index entries with {MODEL} once "
                  f"(cached in {path.name}; minutes on a laptop)", file=sys.stderr)
        for i in range(0, len(missing), BATCH):
            chunk = missing[i:i + BATCH]
            for t, v in zip(chunk, self._embed(chunk)):
                stored[t] = base64.b64encode(array("f", v).tobytes()).decode()
        if missing:
            CACHE.mkdir(exist_ok=True)
            path.write_text(json.dumps({"model": MODEL, "built": built, "vectors": stored}))
        decoded: dict[str, array] = {}
        for t in set(texts):
            v = array("f")
            v.frombytes(base64.b64decode(stored[t]))
            decoded[t] = v
        self.vectors = [decoded[t] for t in texts]
        self.stats = {"embedded": len(missing), "unique": len(decoded),
                      "precompute_s": round(time.perf_counter() - started, 1), "cache": str(path)}

    def rank(self, phrase: str) -> list[tuple[float, dict]] | None:
        """Every entry by cosine similarity to the ask, best first; None when off."""
        if not self.ok:
            return None
        try:
            q = array("f", self._embed([QUERY_PREFIX + phrase])[0])
        except (requests.RequestException, KeyError, ValueError) as err:
            print(f"! semantic search failed for this ask: {type(err).__name__}", file=sys.stderr)
            return None
        sims = [(_dot(q, v), e) for v, e in zip(self.vectors, self.entries)]
        sims.sort(key=lambda x: -x[0])
        return sims
