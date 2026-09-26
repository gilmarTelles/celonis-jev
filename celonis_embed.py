"""Optional semantic ranking for `search()`: a local sentence-embedding model.

A paraphrase shares meaning with an entry's name but not its words, which BM25
cannot bridge. When a local Ollama serves an embedding model, every index entry
is embedded once by `python3 celonis_index.py --embed` (cached in
cache/embeddings-<model>.json, keyed by entry text) and the ask is embedded at
search time; `search()` fuses that ranking with BM25.
An entry described by `celonis_index.py --describe` embeds its description too:
a code or terse name alone gives a paraphrase nothing to match.

Search never embeds the index. It ranks the entries that already have a vector
and leaves the rest to BM25. A query embed that fails or stalls (5 s budget)
pauses semantic ranking for the whole process for BREAKER_S, with one warning,
so a broken server costs one slow search, not every search.

    CELONIS_EMBED_URL    default http://localhost:11434; "off" disables
    CELONIS_EMBED_MODEL  default mxbai-embed-large
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import sys
import tempfile
import time
from array import array
from collections import Counter
from pathlib import Path

import requests

URL = os.environ.get("CELONIS_EMBED_URL", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("CELONIS_EMBED_MODEL", "mxbai-embed-large")
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "  # mxbai's retrieval prompt
CACHE = Path(__file__).with_name("cache")
VERSION_TIMEOUT = (0.5, 2.0)
QUERY_TIMEOUT = (1.0, 5.0)
BATCH_TIMEOUT = (1.0, 120.0)   # a cold model load takes ~50 s
BATCH = 64
BREAKER_S = 300.0
EMBED_HINT = "run `python3 celonis_index.py --embed`"

KIND_TEXT = {"board_v2": "view (dashboard)", "kpi": "KPI", "semantic_model": "knowledge model",
             "section": "settings page", "task_type_v2": "task type", "datajob": "data job"}

_paused_until = 0.0
_paused_why = ""


def disabled() -> bool:
    return URL.lower() in ("", "off", "none")


def paused() -> str | None:
    """Why semantic ranking is paused for this process, or None."""
    return _paused_why if time.monotonic() < _paused_until else None


def _trip(why: str) -> None:
    global _paused_until, _paused_why
    _paused_until, _paused_why = time.monotonic() + BREAKER_S, why
    print(f"! semantic search paused {BREAKER_S / 60:.0f} min ({URL}, {MODEL}): {why}; "
          f"search is BM25 only", file=sys.stderr)


def cache_path(model: str | None = None) -> Path:
    """One file per model. Escaping `_` too keeps distinct names distinct ('a:b' vs 'a_b')."""
    safe = re.sub(r"[^A-Za-z0-9.-]", lambda m: f"_{ord(m.group()):02x}", model or MODEL)
    return CACHE / f"embeddings-{safe}.json"


def _legacy_paths() -> list[Path]:
    """Build-stamped caches from before the model-keyed file, newest first."""
    pat = re.compile(r"embeddings-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-" + re.escape(MODEL.replace(":", "-"))
                     + r"\.json")
    found = [p for p in CACHE.glob("embeddings-*.json") if pat.fullmatch(p.name)]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def words(name: str) -> str:
    """CustomObjectName / SNAKE_CASE / kebab-case -> plain words, stopwords kept."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    return re.sub(r"[_\-./]+", " ", s).strip()


def descriptions_path(built: str) -> Path:
    """Generated descriptions for the index built at `built`; a rebuilt index drops stale ones."""
    return CACHE / f"descriptions-{built.replace(':', '-')}.json"


def load_descriptions(built: str) -> dict[tuple[str, str], str]:
    """What each (kind, name) is for, from `celonis_index.py --describe`; empty when absent."""
    try:
        items = json.loads(descriptions_path(built).read_text())["items"]
    except (OSError, ValueError, TypeError, KeyError):
        return {}
    out = {}
    for key, d in items.items():
        kind, _, name = key.partition("\t")
        if isinstance(d, dict) and isinstance(d.get("description"), str) and d["description"].strip():
            out[(kind, name)] = d["description"].strip()
    return out


def entry_text(e: dict, descriptions: dict[tuple[str, str], str] | None = None) -> str:
    kind = KIND_TEXT.get(e["kind"], e["kind"].replace("_", " ").replace("-", " "))
    text = f"{kind}: {words(e['name'])}"
    said = (descriptions or {}).get((e["kind"], e["name"]))
    if said:
        return f"{text}. {said}"
    where = e.get("package") or e.get("space") or e.get("pool")
    if where and e["kind"] not in ("package", "space", "pool"):
        text += f" (in {where})"
    if e["kind"] == "object" and e.get("fields"):
        text += "; fields: " + ", ".join(words(f) for f in e["fields"][:12])
    if e.get("hint"):
        text += f"; {str(e['hint'])[:160]}"
    return text


def _dot(a: array, b: array) -> float:
    return math.sumprod(a, b) if hasattr(math, "sumprod") else sum(map(float.__mul__, a, b))


def _decode(raw: dict) -> dict[str, array]:
    """text -> vector, keeping only the dimension most vectors share."""
    out: dict[str, array] = {}
    for t, b64 in raw.items():
        try:
            v = array("f")
            v.frombytes(base64.b64decode(b64))
        except (TypeError, ValueError, binascii.Error):
            continue
        out[t] = v
    if not out:
        return out
    dim = Counter(len(v) for v in out.values()).most_common(1)[0][0]
    return {t: v for t, v in out.items() if len(v) == dim and dim}


def _read(path: Path) -> dict[str, array]:
    blob = json.loads(path.read_text())
    raw = blob.get("vectors") if isinstance(blob, dict) else None
    if not isinstance(raw, dict):
        raise TypeError("no vectors object")
    return _decode(raw)


def load_cache() -> dict[str, array]:
    """This model's vectors by entry text. Unreadable or absent reads as empty, with a warning."""
    path = cache_path()
    src = path if path.exists() else next(iter(_legacy_paths()), None)
    if src is None:
        return {}
    try:
        return _read(src)
    except (OSError, ValueError, TypeError) as err:
        print(f"! {src.name} is unreadable ({type(err).__name__}); semantic search skips it "
              f"until you {EMBED_HINT}", file=sys.stderr)
        return {}


def _embed(texts: list[str], timeout: tuple[float, float]) -> list[array]:
    r = requests.post(f"{URL}/api/embed", timeout=timeout,
                      json={"model": MODEL, "input": texts, "keep_alive": "30m"})
    r.raise_for_status()
    got = r.json()["embeddings"]
    if not isinstance(got, list) or len(got) != len(texts):
        raise TypeError(f"expected {len(texts)} embeddings, got {type(got).__name__}")
    return [array("f", v) for v in got]


class Embeddings:
    """Semantic ranking over the cached vectors. `ok` is False until a query embed succeeds."""

    def __init__(self, entries: list[dict], built: str = "") -> None:
        self.entries = entries
        self.built = built
        self.ok = False
        self.vectors: list[tuple[array, dict]] | None = None   # loaded on the first rank()
        self.dim = 0

    def _load(self) -> None:
        stored, said = load_cache(), load_descriptions(self.built)
        self.vectors = [(stored[t], e) for e in self.entries if (t := entry_text(e, said)) in stored]
        self.dim = len(self.vectors[0][0]) if self.vectors else 0

    @property
    def warning(self) -> str | None:
        """What a search report should say about semantic ranking, or None when it is fully on."""
        if disabled():
            return "semantic search off (CELONIS_EMBED_URL=off); BM25 only"
        if paused():
            return f"semantic search paused ({paused()}); BM25 only"
        if self.vectors is None:
            return None
        if not self.vectors:
            return f"semantic search has no embeddings for {MODEL}; BM25 only. To add it, {EMBED_HINT}"
        if len(self.vectors) < len(self.entries):
            return (f"semantic search covers {len(self.vectors)} of {len(self.entries)} entries; "
                    f"to cover the rest, {EMBED_HINT}")
        return None

    def rank(self, phrase: str) -> list[tuple[float, dict]] | None:
        """Every embedded entry by cosine similarity to the ask, best first; None when unavailable.

        Never raises: any failure here leaves search on BM25.
        """
        self.ok = False
        if disabled() or paused():
            return None
        try:
            if self.vectors is None:
                requests.get(f"{URL}/api/version", timeout=VERSION_TIMEOUT).raise_for_status()
                self._load()
            if not self.vectors:
                return None
            q = _embed([QUERY_PREFIX + phrase], QUERY_TIMEOUT)[0]
            if len(q) != self.dim:
                raise ValueError(f"query has {len(q)} dimensions, the cache {self.dim}")
            sims = [(_dot(q, v), e) for v, e in self.vectors]
        except Exception as err:        # the semantic layer is optional: degrade, never propagate
            _trip(f"{type(err).__name__}: {str(err)[:120]}")
            return None
        sims.sort(key=lambda x: -x[0])
        self.ok = True
        return sims


# --- precompute: `celonis_index.py --embed` --------------------------------------

def _save(path: Path, stored: dict[str, array], wanted: set[str]) -> None:
    """Merge with whatever another run wrote meanwhile, keep only `wanted`, replace atomically."""
    try:
        on_disk = _read(path) if path.exists() else {}
    except (OSError, ValueError, TypeError):
        on_disk = {}
    dim = len(next(iter(stored.values()))) if stored else 0
    for t, v in on_disk.items():
        if t in wanted and t not in stored and len(v) == dim:
            stored[t] = v
    vectors = {t: base64.b64encode(v.tobytes()).decode() for t, v in stored.items() if t in wanted}
    path.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"model": MODEL, "dim": dim, "vectors": vectors}, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def embed_index(entries: list[dict], built: str = "", required: bool = True) -> None:
    """Embed every entry the cache lacks, checkpointing after each batch.

    Resumable and idempotent: a killed run keeps its finished batches, a rerun
    embeds only what is still missing, and a run with nothing missing just
    prunes texts the index no longer has. With `required` False an unreachable
    model is a silent no-op (the end of a normal index build).
    """
    if disabled():
        if required:
            raise SystemExit("semantic search is off (CELONIS_EMBED_URL=off); nothing to embed")
        return
    try:
        requests.get(f"{URL}/api/version", timeout=VERSION_TIMEOUT).raise_for_status()
    except requests.RequestException as err:
        if required:
            raise SystemExit(f"no embedding server at {URL} ({type(err).__name__}); "
                             f"start Ollama and `ollama pull {MODEL}`")
        return
    path = cache_path()
    said = load_descriptions(built)
    wanted = {entry_text(e, said) for e in entries}
    stored = {t: v for t, v in load_cache().items() if t in wanted}
    missing = sorted(wanted - set(stored))
    batches = math.ceil(len(missing) / BATCH)
    print(f"embeddings: {len(stored)}/{len(wanted)} cached, {len(missing)} to embed with {MODEL} "
          f"in {batches} batches -> {path.relative_to(path.parent.parent)}", file=sys.stderr)
    started = time.perf_counter()
    for n, i in enumerate(range(0, len(missing), BATCH), 1):
        chunk = missing[i:i + BATCH]
        try:
            got = _embed(chunk, BATCH_TIMEOUT)
        except (requests.RequestException, KeyError, TypeError, ValueError) as err:
            why = f"embedding stopped at batch {n}/{batches} ({type(err).__name__}); rerun to resume"
            if required:
                raise SystemExit(why)
            print(f"! {why}", file=sys.stderr)
            return
        stored.update(zip(chunk, got))
        if len({len(v) for v in stored.values()}) > 1:
            raise SystemExit(f"{MODEL} returned vectors of a different size than the cache; "
                             f"delete {path.name} and rerun")
        _save(path, stored, wanted)
        print(f"  batch {n}/{batches}: {len(stored)}/{len(wanted)} embedded "
              f"({time.perf_counter() - started:.0f} s)", file=sys.stderr)
    if not missing:
        _save(path, stored, wanted)
    for old in _legacy_paths():
        old.unlink(missing_ok=True)
