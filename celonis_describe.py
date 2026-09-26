"""Model-written descriptions of index items, for semantic search.

`python3 celonis_index.py --describe` asks the claude CLI for one sentence per
item from the item's own metadata; celonis_embed embeds that sentence with the
item, so an item named by a code or a terse label can match a paraphrase.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CACHE = Path(__file__).with_name("cache")


PATH = CACHE / "descriptions.json"


class Unreadable(Exception):
    """The descriptions file exists but does not parse; nothing may overwrite or prune it."""


def _legacy() -> Path | None:
    """The newest file from when descriptions were keyed by index build stamp."""
    found = [p for p in CACHE.glob("descriptions-*.json")
             if re.fullmatch(r"descriptions-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d\.json", p.name)]
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def read_store() -> dict:
    """The descriptions file; a stamped file from an earlier version is adopted when it is the only one."""
    src = PATH if PATH.exists() else _legacy()
    if src is None:
        return {"cost_usd": 0.0, "seconds": 0.0, "items": {}}
    try:
        store = json.loads(src.read_text())
        if not isinstance(store.get("items"), dict):
            raise ValueError("no items object")
    except (OSError, ValueError, AttributeError) as err:
        raise Unreadable(f"{src} is unreadable ({type(err).__name__}: {err}). It holds paid "
                         f"descriptions: restore it from {PATH.name}.bak or fix it, then rerun") from err
    store.pop("built", None)
    return store


def write_store(store: dict) -> None:
    """Replace the file atomically, so a killed run leaves the previous version whole."""
    PATH.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=PATH.parent, prefix=PATH.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(store, f, indent=1, ensure_ascii=False)
        os.replace(tmp, PATH)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_descriptions() -> dict[tuple[str, str], str]:
    """What each (kind, name) is for; empty when never described. Raises Unreadable."""
    out = {}
    for key, d in read_store()["items"].items():
        kind, _, name = key.partition("\t")
        if isinstance(d, dict) and isinstance(d.get("description"), str) and d["description"].strip():
            out[(kind, name)] = d["description"].strip()
    return out


def _key(item: dict) -> str:
    return f"{item['kind']}\t{item['name']}"


def _digest(item: dict) -> str:
    """Short hash of the metadata sent, so an item whose metadata changed is described again."""
    return hashlib.sha1(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def _stale(store: dict, item: dict) -> bool:
    """Absent, or described from different metadata. Adopted entries carry no hash and are kept."""
    got = store["items"].get(_key(item))
    return got is None or got.get("h", _digest(item)) != _digest(item)


SYSTEM = ("You describe items in a Celonis process-mining tenant for a search index. "
          "Answer with strict JSON only, no prose, no code fences.")
ASK = """For each item below, write what a business user would say when looking for it.
Use ONLY the item's own metadata. Return a JSON array with one object per item:
{"i": <the item's i>, "d": "<one plain-English sentence: what it is and what it is for>",
 "k": [8 to 15 search keywords or short phrases a business user might type]}
Keywords: synonyms, the business concept, English glosses of non-English names, and the
expansion of abbreviations (GL, AP, AR, PO, SAP table codes such as BKPF or BSEG) only
when you are confident. Do not invent facts the metadata does not support.

Items:
"""
BATCH = 50
BUDGET_USD = 3.0


def _describe_items(entries: list[dict]) -> list[dict]:
    """One compact metadata record per (kind, name): the only thing the model sees."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in entries:
        groups.setdefault((e["kind"], e["name"]), []).append(e)
    items = []
    for (kind, name), es in groups.items():
        e = es[0]
        where = sorted({c for x in es for c in (x.get("package"), x.get("pool"), x.get("space"),
                                                 x.get("app")) if c})[:3]
        item = {"kind": kind, "name": name, "in": where}
        detail = e.get("columns") or e.get("fields")
        if detail:
            item["columns"] = list(detail)[:30]
        for k in ("key", "pql", "hint"):
            if e.get(k) and e[k] != name:
                item[k] = str(e[k])[:200]
        items.append(item)
    return items


def _describe_batch(batch: list[dict], model: str) -> tuple[dict, float]:
    """Ask the claude CLI for one batch; the answer keyed by (kind, name), and its cost.

    The CLI runs in an empty directory with no tools and no settings, so the
    item metadata in the prompt is all it can read.
    """
    payload = [dict(it, i=i) for i, it in enumerate(batch)]
    with tempfile.TemporaryDirectory() as cwd:
        run = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
             "--no-session-persistence", "--setting-sources", "", "--system-prompt", SYSTEM],
            input=ASK + json.dumps(payload, ensure_ascii=False), cwd=cwd,
            capture_output=True, text=True, timeout=600)
    out = json.loads(run.stdout)
    cost = float(out.get("total_cost_usd") or 0)
    text = out.get("result") or ""
    rows = json.loads(text[text.index("["):text.rindex("]") + 1])
    got = {}
    for r in rows:
        it = batch[int(r["i"])]
        if isinstance(r.get("d"), str) and isinstance(r.get("k"), list):
            got[_key(it)] = {"description": r["d"], "keywords": [str(k) for k in r["k"]][:15],
                             "h": _digest(it)}
    return got, cost


def describe(entries: list[dict], model: str = "haiku", workers: int = 6) -> None:
    """Describe every index item the file lacks, or whose metadata changed, into cache/descriptions.json.

    Resumable: items already in the file are skipped, so a rerun after a failed
    or over-budget run only pays for what is missing.
    """
    try:
        store = read_store()
    except Unreadable as err:
        raise SystemExit(str(err))
    store.setdefault("model", model)
    todo = [it for it in _describe_items(entries) if _stale(store, it)]
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    started, failed = time.time(), 0
    print(f"{len(todo)} items to describe in {len(batches)} batches with {model}")
    if PATH.exists():
        shutil.copy2(PATH, PATH.with_name(PATH.name + ".bak"))

    def one(batch: list[dict]) -> tuple[dict, float]:
        if store["cost_usd"] >= BUDGET_USD:
            return {}, 0.0
        for _ in range(2):
            try:
                return _describe_batch(batch, model)
            except Exception:
                continue
        return {}, 0.0

    with ThreadPoolExecutor(workers) as pool:
        for got, cost in pool.map(one, batches):
            failed += not got
            store["items"].update(got)
            store["cost_usd"] = round(store["cost_usd"] + cost, 4)
            write_store(store)
    store["seconds"] = round(store["seconds"] + time.time() - started, 1)
    write_store(store)
    print(f"{len(store['items'])} described -> {PATH} (${store['cost_usd']:.2f}, "
          f"{store['seconds']:.0f} s, {failed} batches failed or skipped)")
