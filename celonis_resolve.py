"""Natural language -> a Celonis location (and only when needed, an action).

    resolve("the view that shows tax credits by month")
      -> {kind: board_v2, name: 'IBS/CBS Creditability Map', url: ..., confidence, alternatives}

Pipeline, cheapest first:

  1. vocabulary     curated and learned aliases, spelled-out names, a named column
  2. named lookup   /package-manager/api/search   (no model, ~0.3 s)
  3. shortlist      BM25 over the local index     (no model, ~10 ms)
  4. judgment       one Jev call: which candidate, what intent, does it exist at all
  5. compose        URL from the index entry (the id is already known)

The judgment is asked the cheap question first (SHORTLIST candidates) and re-asked
with the full list only when it refuses or is not sure (SHORTLIST_FULL). Measured
17 Sep: the second call fires on 4-5 of 23 judged asks, tokens per judged call fall
43%, and the accuracy is the same as asking with the full list every time.

Run:  python3 celonis_resolve.py "your ask"        one resolution
      python3 celonis_resolve.py --demo            the evaluation set

`resolve(..., trace=Trail())` fills the trail a page can show: the signals code read,
the candidates and their scores, the JSON of every Jev call, and the threshold each
branch was taken with. `celonis_view.py` is that page. With no trail the recording
is free: the payload builders only run when a trail is on.
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

import celonis_api
import jev
from celonis_types import CONTAINER_KINDS, Resolution

INDEX = Path(__file__).with_name("celonis-index.json")
COLUMNS = Path(__file__).with_name("celonis-columns.json")
SHORTLIST = 10        # the first prompt: candidates are what a judgment costs
SHORTLIST_FULL = 30   # re-asked only when the first pass refuses or is not sure


class T:
    """Every threshold the decision is taken with. Numbers live here, never in a
    prompt; `THRESHOLDS` mirrors the class for the page that shows them, so a new
    threshold appears there by existing here."""

    EXISTS = 0.40
    MATCH = 0.50        # above: act. below: return it, but ask the user to confirm
    MATCH_WEAK = 0.15   # the floor - below this the top candidate is noise
    MARGIN = 0.15       # top minus runner-up: below this the ask is genuinely ambiguous
    PROMOTE = 0.25      # a refusal is overruled only by a candidate this strong
    NONE = 0.50         # p(match = none) at or above this: the thing is not in the index


THRESHOLDS = {"T_" + k: v for k, v in vars(T).items() if not k.startswith("_")}
THRESHOLDS |= {"SHORTLIST": SHORTLIST, "SHORTLIST_FULL": SHORTLIST_FULL}

STOP = set("the a an of to in on for and or is are was were be what which who how "
           "when where why do does can i me my we our you your it its this that with "
           "from at by as show me please open go take".split())

CURATED_ALIASES = Path(__file__).with_name("celonis-aliases.json")
LEARNED_ALIASES = Path(__file__).with_name("cache") / "aliases-learned.json"

CONTAINER_HIT = 2.5     # the ask names the pool/space/package this entry lives in
KIND_HIT = 2.0          # the ask names the kind this entry is
MIN_ALIAS_TOKENS = 2    # a one-word alias may boost, never answer on its own


def tokens(s: str) -> list[str]:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s or "")     # CustomObjectName -> Custom Object Name
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 1]


# The words people use for a kind, longest phrase first. Container words (pool,
# space, package) are deliberately absent: "the tax package" locates a thing, it
# does not name a kind, and boosting `package` there picks the wrong entry.
KIND_WORDS = (
    ("knowledge model", "semantic_model"),
    ("data job", "section"),
    ("sql editor", "section"),
    ("task type", "task_type_v2"),
    ("view", "board_v2"),
    ("dashboard", "board_v2"),
    ("kpi", "kpi"),
    ("indicator", "kpi"),
    ("object", "object"),
    ("event", "event"),
    ("perspective", "perspective"),
    ("scenario", "scenario"),
    ("setting", "section"),
    ("table", "table"),
)


def _sing(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _contains(q: list[str], w: list[str]) -> bool:
    """Does the token list `q` say the token list `w` in one piece?"""
    return bool(w) and any(q[i:i + len(w)] == w for i in range(len(q) - len(w) + 1))


KINDS = tuple((tuple(_sing(t) for t in w.split()), k) for w, k in KIND_WORDS)


def implied_kind(phrase: str) -> str | None:
    """The kind the ask says in words, or None. Deterministic - no model.

    One kind, the first the ask says (longest phrase first: "knowledge model"
    beats "model"). Measured 17 Sep: boosting every kind an ask names changed no
    rank, so the list stays a single value.
    """
    q = [_sing(t) for t in tokens(phrase)]
    return next((k for w, k in KINDS if _contains(q, list(w))), None)


def in_container(entry: dict, ids: list[str] | tuple[str, ...]) -> bool:
    """Does this entry sit in one of those pools, spaces or packages (or is it one)?"""
    return any(c in (entry.get("id"), entry.get("poolId"), entry.get("spaceId"),
                     entry.get("packageId")) for c in ids)


def phrase_key(phrase: str) -> str:
    return " ".join(tokens(phrase))


class Vocab:
    """Phrases people use for an entity, and for the container it lives in.

    Curated entries are tenant facts no sentence derives. Learned entries are
    written back when a human navigates to an accepted resolution; both stores
    are local configuration, not repository data. The layer lets a repeated
    phrase answer without another model call.
    """

    def __init__(self, curated: Path = CURATED_ALIASES, learned: Path = LEARNED_ALIASES) -> None:
        self.items: list[dict] = []
        self.by_phrase: dict[str, dict] = {}
        for path, source in ((curated, "curated"), (learned, "learned")):
            try:
                blob = json.loads(Path(path).read_text())
            except Exception:            # absent or unreadable: the layer is optional
                continue
            for a in blob.get("aliases", []):
                tok = tokens(a.get("phrase", ""))
                if not tok or not (a.get("target") or a.get("container")):
                    continue
                item = {"phrase": a.get("phrase", ""), "key": " ".join(tok), "tokens": tok,
                        "source": source, "target": a.get("target"),
                        "container": a.get("container"), "why": a.get("why", "")}
                if item["key"] in self.by_phrase:       # curated wins over learned
                    continue
                self.by_phrase[item["key"]] = item
                self.items.append(item)
        self.items.sort(key=lambda a: -len(a["tokens"]))

    def hits(self, phrase: str) -> list[dict]:
        """The aliases this ask says, longest phrase first."""
        q = f" {phrase_key(phrase)} "
        return [a for a in self.items if f" {a['key']} " in q]

    def remember(self, phrase: str, entry: dict) -> None:
        """Write `phrase -> entity` back after a human accepted a resolution."""
        key = phrase_key(phrase)
        if not key or key in self.by_phrase:
            return
        LEARNED_ALIASES.parent.mkdir(exist_ok=True)
        try:
            blob = json.loads(LEARNED_ALIASES.read_text())
        except Exception:
            blob = {"aliases": []}
        blob["aliases"].append({"phrase": key, "target": entry["id"], "kind": entry["kind"],
                                "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        LEARNED_ALIASES.write_text(json.dumps(blob, indent=1) + "\n")
        self.__init__()                  # the store is small; reload instead of patching


def _is_copy(name: str, names: set[str]) -> bool:
    """Is this table a copy of another one in its pool?

    `celonis tables --twins` reports the X_X pairs a repeated upload leaves behind;
    zzbak_* and blackbox_* are the same failure under another name. None of them
    should win a question over the table they copy.
    """
    low = name.lower()
    if low.startswith(("zzbak_", "blackbox_", "_bak")):
        return True
    return any(other != name and low == f"{other}_{other}".lower() for other in names)


class Columns:
    """Which pool tables hold a column.

    `celonis columns` scans tables through the workbench into cache/ - a scan
    artifact, not in git, 15 minutes for a pool. The index builder folds that scan
    into `celonis-columns.json`, which is committed, so a clone answers a column
    question with no cache and no tenant call. Exact knowledge, so code answers it.
    """

    def __init__(self, path: Path = COLUMNS) -> None:
        self.built = ""
        self.pools: dict[str, dict[str, list[str]]] = {}
        self.by_column: dict[str, list[tuple[str, str]]] = {}
        try:
            blob = json.loads(Path(path).read_text())
        except Exception:                # absent: the column layer is optional
            return
        self.built = blob.get("built", "")
        self.pools = blob.get("pools", {})
        for pool, tables in self.pools.items():
            for table, cols in tables.items():
                for c in cols:
                    self.by_column.setdefault(str(c).lower(), []).append((pool, table))

    def holders(self, column: str) -> list[tuple[str, str]]:
        """(poolId, table) pairs holding this column, case-insensitive."""
        return self.by_column.get(column.lower(), [])


def named_containers(phrase: str, entries: list[dict]) -> list[dict]:
    """Pools, spaces and packages the ask names, by index name, longest first."""
    q = tokens(phrase)
    out = [e for e in entries
           if e["kind"] in CONTAINER_KINDS and _contains(q, tokens(e["name"]))]
    out.sort(key=lambda e: -len(tokens(e["name"])))
    return out[:2] if len(out) > 1 else out


class Index:
    def __init__(self, path: Path = INDEX, vocab: "Vocab | None" = None,
                 columns: "Columns | None" = None) -> None:
        blob = json.loads(path.read_text()) if path.exists() else {"entries": [], "built": "missing"}
        self.entries = blob["entries"]
        self.built = blob.get("built", "unknown")
        self.by_id = {e["id"]: e for e in self.entries}
        self.vocab = vocab if vocab is not None else Vocab()
        self.columns = columns if columns is not None else Columns()
        self.df: dict[str, int] = {}
        for e in self.entries:
            for t in set(tokens(e["name"]) + tokens(e.get("key", ""))):
                self.df[t] = self.df.get(t, 0) + 1
        self.n = max(1, len(self.entries))
        self._boosts: dict[str, dict] = {}

    def idf(self, t: str) -> float:
        return math.log(1 + self.n / (1 + self.df.get(t, 0)))

    def boosts(self, phrase: str) -> dict:
        """What the ask already tells us, per entry id: alias text, containers, kind.

        Computed once per phrase, from the index and the vocabulary - no model.
        """
        if phrase in self._boosts:
            return self._boosts[phrase]
        text: dict[str, set[str]] = {}
        flat: dict[str, float] = {}
        said: list[str] = []                     # containers the vocabulary names: facts
        for a in self.vocab.hits(phrase):
            if a["target"]:
                text.setdefault(a["target"], set()).update(a["tokens"])
            if (a.get("container") or {}).get("id"):
                said.append(a["container"]["id"])
        named = [e["id"] for e in named_containers(phrase, self.entries)]
        containers = said + [c for c in named if c not in said]
        kind = implied_kind(phrase)
        for e in self.entries:
            b = 0.0
            if in_container(e, containers):
                b += CONTAINER_HIT
            if kind and e["kind"] == kind:
                b += KIND_HIT
            if b:
                flat[e["id"]] = flat.get(e["id"], 0.0) + b
        self._boosts[phrase] = {"text": text, "flat": flat, "said": said}
        return self._boosts[phrase]

    def score(self, phrase: str, e: dict) -> float:
        q = tokens(phrase)
        if not q:
            return 0.0
        name = set(tokens(e["name"]) + tokens(e.get("key", "")))
        ctx = set(tokens(f'{e.get("package","")} {e.get("space","")} {e.get("pool","")}'))
        s = sum(self.idf(t) * (2.0 if t in name else 0.5 if t in ctx else 0.0) for t in q)
        if e["name"].lower() in phrase.lower():
            s += 4.0
        if e["kind"] == "table" and not set(q) & name:
            # A table is its name plus its columns. Container context alone is
            # not evidence: large pools contain many unrelated tables.
            held = {str(c).lower() for c in (e.get("columns") or [])}
            if not set(q) & held:
                return 0.0
        b = self.boosts(phrase)
        s += b["flat"].get(e["id"], 0.0)
        alias_text = b["text"].get(e["id"])
        if alias_text:
            s += sum(self.idf(t) for t in q if t in alias_text)
        return s

    def top_scored(self, phrase: str, k: int = SHORTLIST) -> list[tuple[float, dict]]:
        """The shortlist with the score that put each entry there, best first.

        When the ask said where to look ('the tax pool'), that pool's own entries
        lead the candidate list. Not a filter: the rest still follows, so a
        question about something outside the container can still be answered.
        """
        b = self.boosts(phrase)
        scored = [(self.score(phrase, e), e) for e in self.entries]
        scored = [x for x in scored if x[0] > 0]
        scored.sort(key=lambda x: -x[0])
        if b["said"]:
            said = b["said"]
            inside = [x for x in scored if in_container(x[1], said)]
            scored = inside + [x for x in scored if not in_container(x[1], said)]
        return scored[:k]

    def top(self, phrase: str, k: int = SHORTLIST) -> list[dict]:
        return [e for _, e in self.top_scored(phrase, k)]

    def models(self) -> list[dict]:
        """The knowledge models, the vocabulary's designated package first.

        Several packages can carry models with the same name. A local vocabulary
        may designate one package; otherwise the first model in the local index
        is used. The index is tenant-specific and generated outside Git.
        """
        ms = [e for e in self.entries if e["kind"] == "semantic_model"]
        said = [m for a in self.vocab.items
                if (a.get("container") or {}).get("kind") == "package"
                for m in ms if m.get("packageId") == (a["container"] or {}).get("id")]
        picked = {id(m) for m in said}
        return said + [m for m in ms if id(m) not in picked]


def named_lookup(phrase: str) -> list[dict]:
    """The tenant's own name search, used when the request includes a literal name."""
    try:
        r = requests.get(celonis_api.base() + "/package-manager/api/search",
                         headers=celonis_api.headers(), timeout=20,
                         params={"searchTerm": phrase, "draftMode": "false", "flavor": "STUDIO"})
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else r.json().get("results", [])
    except Exception:
        return []


def containers_named(phrase: str, index: "Index") -> list[dict]:
    """The pools, spaces and packages the ask names: vocabulary first, then index names.

    A request can name a container such as a pool, space, or package. The
    container narrows the local index without turning a container into the
    requested object.
    """
    out = []
    for a in index.vocab.hits(phrase):
        c = a.get("container") or {}
        if c.get("id") and index.by_id.get(c["id"]):
            out.append(index.by_id[c["id"]])
    for e in named_containers(phrase, index.entries):
        if e["id"] not in {x["id"] for x in out}:
            out.append(e)
    return out


def km_for_ask(phrase: str, index: "Index") -> dict:
    """The knowledge model an ask means: the one in a package the ask names (and
    which holds exactly one), else the one the vocabulary designates (see
    `Index.models`), else the first the index holds."""
    ms = index.models()
    if not ms:
        raise LookupError("the index holds no knowledge model; refresh it (celonis_index.py)")
    for c in containers_named(phrase, index):
        in_pkg = [m for m in ms if m.get("packageId") == c["id"]] or \
                 [m for m in ms if m.get("package") == c["name"]]
        if len(in_pkg) == 1:
            return in_pkg[0]
    return ms[0]


def same_thing_in_container(phrase: str, entry: dict, index: "Index") -> tuple[dict, dict] | None:
    """The same-named, same-kind entry inside a container the phrase names.

    The model judges which thing is meant; the container picks which instance of
    it. Only swapped when that container holds exactly one, so this never guesses
    between clones. Nothing happens when the ask names no container or when the
    chosen entry already lives in the first one.
    """
    for container in containers_named(phrase, index):
        key = "poolId" if container["kind"] == "pool" else \
              "spaceId" if container["kind"] == "space" else "packageId"
        if entry.get(key) == container["id"]:
            return None
        twins = [e for e in index.entries
                 if e["kind"] == entry["kind"] and e["name"] == entry["name"]
                 and e.get(key) == container["id"]]
        if len(twins) == 1:
            return twins[0], container
    return None


def column_route(phrase: str, index: "Index") -> tuple[dict, str] | None:
    """The table that holds a column the ask names, when evidence supports it.

    This fires only when the ask names a table or says field/column and the
    local column snapshot identifies a holder. Copies and backup names never
    win over the table they copy; multiple honest holders remain a judgment.
    """
    if implied_kind(phrase) != "table" and not set(tokens(phrase)) & {"field", "column"}:
        return None
    holders: dict[str, list[tuple[str, str]]] = {}
    for t in dict.fromkeys(tokens(phrase)):
        found = index.columns.holders(t)
        if found:
            holders[t] = found
    if len(holders) != 1:
        return None
    column, found = next(iter(holders.items()))
    ids = [c["id"] for c in containers_named(phrase, index)]
    if ids:                                    # the ask said where: that pool's tables win
        pinned = [h for h in found if h[0] in ids]
        found = pinned or found
    names = {p: {e["name"] for e in index.entries
                 if e["kind"] == "table" and e.get("poolId") == p}
             for p, _ in found}
    found = [h for h in found if not _is_copy(h[1], names[h[0]])] or found
    pair = set(found)
    entries = [e for e in index.entries
               if e["kind"] == "table" and (e.get("poolId"), e["name"]) in pair]
    if len(entries) == 1:
        return entries[0], f"column {column.upper()}"
    return None


def path_for(why: str) -> str:
    """The deterministic layer that answered: alias, literal or column."""
    if why == "literal":
        return "literal"
    return "column" if why.startswith("column ") else "alias"


def decide(phrase: str, index: "Index") -> tuple[dict, str] | None:
    """Answer from the vocabulary, a column the snapshot knows, or the literal name.

    Three ways an ask is already answered before any judgment:

    * a vocabulary phrase of two tokens or more that is not contradicted by a kind
      word in the ask ('the view that shows tax credits by month' -> the alias
      'credits by month', for a view);
    * a word the column snapshot knows as a column, when the ask names a table and
      that column picks exactly one of them ('the table with the KTOSL field');
    * an entry whose name the ask spells out in full, when it is the only name the
      ask contains ('tax jurisdiction map').

    Everything else needs the judgment, and gets it.
    """
    kind = implied_kind(phrase)
    for a in index.vocab.hits(phrase):
        entry = index.by_id.get(a.get("target") or "")
        if entry and len(a["tokens"]) >= MIN_ALIAS_TOKENS and (kind is None or kind == entry["kind"]):
            return entry, a["source"]

    by_column = column_route(phrase, index)
    if by_column:
        return by_column

    q = tokens(phrase)
    spelled = [e for e in index.entries
               if e["kind"] not in CONTAINER_KINDS            # naming a pool/space/package locates, it does not answer
               and (kind is None or e["kind"] == kind)
               and len(tokens(e["name"])) >= 2
               and len(tokens(e["name"])) / max(1, len(set(q))) >= 0.5
               and _contains(q, tokens(e["name"]))]
    if len({tuple(tokens(e["name"])) for e in spelled}) == 1:
        return max(spelled, key=lambda e: index.score(phrase, e)), "literal"
    return None


def questions(shortlist: list[dict], phrase: str = "") -> dict:
    """The one call: which candidate, what kind, what intent, does it exist at all.

    `phrase` is the ask. It is used for one thing only: telling a candidate that
    holds a word the ask names as a column. The column snapshot supplies that
    evidence when it is available.
    """
    said = set(tokens(phrase))
    opts = {}
    for i, e in enumerate(shortlist):
        ctx = " / ".join(x for x in (e.get("space"), e.get("package"), e.get("pool")) if x)
        holds = sorted({str(c) for c in (e.get("columns") or []) if str(c).lower() in said})
        hint = (f" ({e['hint']})" if e.get("hint") else "") + \
               (f" (holds {', '.join(holds)})" if holds else "")
        opts[f"c{i:02d}"] = f"{e['name']} — {e['kind']}" + (f" in {ctx}" if ctx else "") + hint
    opts["none"] = "nothing in this list is what the request refers to"
    kinds = sorted({e["kind"] for e in shortlist})
    return {
        "kind": {
            "type": "choice",
            "instructions": ("What kind of thing does the request refer to? Name the thing itself — "
                             "if the request says KPI, pick `kpi`; if it says object or event, pick "
                             "that; a view that merely shows the value is not the KPI."),
            "criteria": {**{k: None for k in kinds}, "any": "the request does not name a kind"},
        },
        "match": {
            "type": "choice",
            "instructions": ("Which entry in `candidates` does the request refer to? Pick the entry "
                             "that IS the thing asked about, not one that merely relates to the same "
                             "subject or contains it. Match on meaning: 'the view that shows tax "
                             "credits by month' may be named 'IBS/CBS Creditability Map'. Choose "
                             "`none` when no entry is the thing being asked about."),
            "criteria": opts,
        },
        "intent": {
            "type": "choice",
            "instructions": "What does the request want done with that entry?",
            "criteria": {
                "open": "show or go to it",
                "edit": "change it",
                "read_data": "get the number or content it produces",
                "explain": "understand what it is or how it is built",
                "list": "see the set of related entries",
            },
        },
        "exists": {
            "type": "noul",
            "instructions": ("Does `candidates` contain the thing the request is about, even under "
                             "a different name?"),
            "criteria": {"true": "one entry is that thing",
                         "false": "the thing is not in this list"},
        },
    }


def _place(out: Resolution, entry: dict) -> None:
    """Copy the addressable fields of an index entry into a result."""
    out.name, out.kind, out.key = entry["name"], entry["kind"], entry.get("key")
    out.package, out.pool = entry.get("package"), entry.get("pool")
    out.url, out.entity_id = entry["url"], entry.get("id")


def answer_from_entry(phrase: str, entry: dict, path: str, started: float, index: "Index",
                      why: str) -> Resolution:
    """A resolution made without a model call, container-pinned like the judged one."""
    out = Resolution(phrase=phrase, path=path, why=why,
                     confidence=0.92 if path == "literal" else 1.0)
    _place(out, entry)
    pinned = same_thing_in_container(phrase, entry, index)
    if pinned:
        chosen, container = pinned
        _place(out, chosen)
        out.pinned_to = container["name"]
    out.ms = (time.perf_counter() - started) * 1000
    return out


def state_for(phrase: str, short: list[dict]) -> dict:
    """The state every question in one call reads."""
    return {"request": phrase,
            "candidates": [{"id": f"c{i:02d}", "name": e["name"], "kind": e["kind"],
                            "context": " / ".join(x for x in (e.get("space"), e.get("package"),
                                                              e.get("pool")) if x),
                            **({"hint": e["hint"]} if e.get("hint") else {})}
                           for i, e in enumerate(short)]}


def thin(answers: dict) -> bool:
    """Was the candidate list too thin to judge?

    Yes when the model refused and no candidate stands out, or when its best score is
    under the noise floor: one more call with the full list is then cheaper than being
    wrong. No when a candidate is strong enough to be promoted past the refusal
    (`T.PROMOTE`) - the wider list would only add worse options.

    Measured 17 Sep: this fires on 4-5 of 23 judged asks.
    """
    probs = answers["match"]["probabilities"]
    best = max((v for k, v in probs.items() if k != "none"), default=0.0)
    if best >= T.PROMOTE:
        return False
    return answers["match"]["choice"] == "none" or best < T.MATCH_WEAK


class Trail:
    """What the resolver did, for a page that shows the decision.

    The result is one URL; this is the reasoning behind it - the signals code read
    from the ask, the candidates BM25 scored and what boosted them, the exact JSON
    of every Jev call (state in, answers out), and the threshold each branch of the
    decision was taken with. The payload builders run only while the trail is on
    (they are the expensive copy); `NO_TRACE` is the default and does no work.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.data: dict = {"steps": [], "passes": [], "post": [],
                           "thresholds": dict(THRESHOLDS)}

    def begin(self, phrase: str, index: "Index") -> None:
        if not self.enabled:
            return
        self.data.update({"phrase": phrase,
                          "index": {"built": index.built, "entries": len(index.entries),
                                    "columns_built": index.columns.built}})

    def signals(self, phrase: str, index: "Index") -> None:
        if not self.enabled:
            return
        self.data["steps"].append({"layer": "signals", **_signals(phrase, index)})

    def step(self, layer: str, **fields) -> None:
        if not self.enabled:
            return
        self.data["steps"].append({"layer": layer, **fields})

    def shortlist(self, phrase: str, scored: list[tuple[float, dict]],
                  index: "Index", wanted: int) -> None:
        if not self.enabled:
            return
        self.data["steps"].append({"layer": "shortlist", "k": len(scored), "wanted": wanted,
                                   "candidates": _trail_candidates(phrase, scored, index)})

    def judgment(self, phrase: str, scored: list[tuple[float, dict]], index: "Index",
                 **fields) -> None:
        if not self.enabled:
            return
        self.data["passes"].append(
            {**fields, "candidates": _trail_candidates(phrase, scored, index)})

    def post(self, rule: str, **fields) -> None:
        if not self.enabled:
            return
        self.data["post"].append({"rule": rule, **fields})

    def result(self, out: Resolution) -> None:
        if not self.enabled:
            return
        self.data["result"] = out.to_dict()


NO_TRACE = Trail(enabled=False)


def _trail_entry(entry: dict) -> dict:
    """One index entry, as the page shows it."""
    return {"id": entry["id"], "name": entry["name"], "kind": entry["kind"], "url": entry["url"],
            "container": " / ".join(x for x in (entry.get("space"), entry.get("package"),
                                                entry.get("pool")) if x)}


def _trail_candidates(phrase: str, scored: list[tuple[float, dict]],
                      index: "Index") -> list[dict]:
    """The shortlist the model was shown, with the score and the boost that put it there."""
    b = index.boosts(phrase)
    out = []
    for i, (score, e) in enumerate(scored):
        row = {"slot": f"c{i:02d}", "name": e["name"], "kind": e["kind"],
               "context": " / ".join(x for x in (e.get("space"), e.get("package"),
                                                 e.get("pool")) if x),
               "url": e["url"], "score": round(score, 2),
               "boost": round(b["flat"].get(e["id"], 0.0), 2)}
        if e["id"] in b["text"]:
            row["alias"] = sorted(b["text"][e["id"]])
        if e.get("hint"):
            row["hint"] = e["hint"]
        out.append(row)
    return out


def _signals(phrase: str, index: "Index") -> dict:
    """What the ask says before any scoring: its tokens, its kind word, its containers."""
    return {"tokens": tokens(phrase), "implied_kind": implied_kind(phrase),
            "containers": [_trail_entry(e) for e in containers_named(phrase, index)],
            "aliases": [{"phrase": a["phrase"], "source": a["source"],
                         "target": (index.by_id.get(a["target"]) or {}).get("name")}
                        for a in index.vocab.hits(phrase)]}


@dataclass
class Match:
    """What the answers say after code applied the thresholds. Pure data: the
    branch taken, the entry it points at, and everything a trail needs to say why."""

    branch: str = ""                             # matched | fallback | refused (decide_match sets it)
    entry: dict | None = None
    model_pick: str = "none"                     # the slot the model chose
    model_conf: float = 0.0
    conf: float = 0.0                            # the confidence the branch acts on
    runner_up: float = 0.0
    margin: float = 0.0
    confirm: bool = False
    weak: bool = False
    reason: str | None = None
    best: float = 0.0                            # the fallback/refused floor's candidate
    p_none: float = 0.0
    exists: float = 1.0
    intent: str = "open"
    hit_slot: str = "none"
    ranked: list = field(default_factory=list)   # (slot, prob, entry), kind applied
    pre_kind: list = field(default_factory=list) # (slot, prob, entry), containers dropped
    dropped: list = field(default_factory=list)  # entries dropped for being containers
    kind: str = "any"
    kind_conf: float = 0.0
    kind_case: str = "unsettled"                 # applied | skipped | unsettled
    kept: list = field(default_factory=list)     # entries left after the kind filter


def decide_match(phrase: str, answers: dict, short: list[dict], index: "Index") -> Match:
    """Rank the candidates and take the branch. No I/O, no model: every threshold
    is applied here and nowhere else.

    Ranking drops the containers the ask named (they say where to look, not what is
    looked for); the kind filter then keeps only what the model says the thing is;
    the branch is the model's pick above the floor, else the one candidate that
    stands out past a refusal (`T.PROMOTE`), else nothing.
    """
    probs = answers["match"]["probabilities"]
    want = answers["kind"]["choice"]
    want_conf = answers["kind"]["confidence"]
    m = Match(model_pick=answers["match"]["choice"], model_conf=answers["match"]["confidence"],
              conf=answers["match"]["confidence"],
              p_none=probs.get("none", 0.0), exists=answers["exists"]["noul"],
              intent=answers["intent"]["choice"], kind=want, kind_conf=want_conf)
    containers = {c["id"] for c in containers_named(phrase, index)}
    m.dropped = [e for k, e in ((k, short[int(k[1:])]) for k in probs if k != "none")
                 if e["id"] in containers]
    pairs = sorted(((k, v, short[int(k[1:])]) for k, v in probs.items()
                    if k != "none" and short[int(k[1:])]["id"] not in containers),
                   key=lambda kv: -kv[1])
    m.pre_kind = pairs
    hit_id = answers["match"]["choice"]
    if hit_id not in {k for k, _v, _e in pairs}:
        hit_id = "none"
    conf = m.model_conf

    if want != "any" and want_conf >= 0.5:       # code applies the kind, the model only names it
        same = [x for x in pairs if x[2]["kind"] == want]
        if same:
            m.kind_case = "applied"
            m.ranked = same
            hit_id, conf = same[0][0], same[0][1]
        else:
            m.kind_case = "skipped"
            m.ranked = pairs
    else:
        m.ranked = pairs
    m.kept = [e for _k, _v, e in m.ranked]
    m.hit_slot = hit_id

    promotable = [x for x in m.ranked if x[1] >= T.PROMOTE]
    if hit_id != "none" and max(conf, m.ranked[0][1] if m.ranked else 0.0) >= T.MATCH_WEAK:
        m.branch = "matched"
        m.entry = short[int(hit_id[1:])]
        m.runner_up = m.ranked[1][1] if len(m.ranked) > 1 else 0.0
        m.margin = conf - m.runner_up
        m.confirm = conf < T.MATCH or (m.margin < T.MARGIN and conf < 0.70)
    elif promotable:
        # The model refused, but one candidate stands out anyway - the case this
        # fallback is for: a thin index entry that little else in the list resembles.
        # It used to promote whatever came first, whatever its score, and dress it up
        # as `confirm: True`, which is how an ask for an alert that does not exist
        # answered "Alert Control Manager" (the model had given it 0.05).
        #
        # Measured 17 Sep over the labelled set: the candidate a real ask needs never
        # scored below 0.26, and the best candidate of an ask with no target never
        # scored above 0.27 - which is a coin flip either side of `T.PROMOTE`, and
        # `confirm` is set so the caller still checks. A wider labelled set is what
        # would move it (see okf/next-steps.md, P2).
        m.branch = "fallback"
        m.entry, m.best = promotable[0][2], promotable[0][1]
        m.confirm, m.weak = True, True
    else:
        m.branch = "refused"
        m.reason = "not in the index" if m.p_none >= T.NONE else "no match"
        m.best = m.ranked[0][1] if m.ranked else 0.0
    m.conf = conf
    return m


def resolve(phrase: str, client: jev.Jev | None = None, index: Index | None = None,
            verbose: bool = True, trace: "Trail | None" = None) -> Resolution:
    trace = trace or NO_TRACE
    client = client or jev.Jev()
    index = index or Index()
    started = time.perf_counter()
    trace.begin(phrase, index)
    trace.signals(phrase, index)

    quick = decide(phrase, index)
    if quick:                        # vocabulary, a named column, or a spelled-out name
        entry, why = quick
        out = answer_from_entry(phrase, entry, path_for(why), started, index, why)
        trace.step("deterministic", path=out.path, why=why, entry=_trail_entry(entry),
                   pinned_to=out.pinned_to)
        trace.result(out)
        if verbose:
            print(f'"{phrase}"\n  -> {why}: [{out.kind}] {out.name} (no model call)\n'
                  f'     {out.url}')
        return out

    named = named_lookup(phrase)
    trace.step("named_lookup", count=len(named),
               results=[{"name": n.get("name"),
                         "kind": n.get("assetType", n.get("type", "")),
                         "url": n.get("url") or n.get("link")} for n in named[:8]])
    if len(named) == 1 and named[0].get("name"):
        out = Resolution(phrase=phrase, path="named_lookup", name=named[0]["name"],
                         kind=named[0].get("assetType", named[0].get("type", "")),
                         url=None, confidence=1.0, grounded=True,
                         ms=(time.perf_counter() - started) * 1000)
        trace.result(out)
        if verbose:
            print(f'"{phrase}"\n  -> named lookup: {out.name} (no model call){named}')
        return out

    scored = index.top_scored(phrase)
    short = [e for _, e in scored]
    trace.shortlist(phrase, scored, index, wanted=SHORTLIST)
    if not short:
        # Nothing in the index shares a word with the ask. Columns and PQL are not
        # indexed, so the phrase may still name something real - `uses` searches those.
        out = Resolution(phrase=phrase, path="no_candidates", name=None, url=None,
                         reason="no match",
                         hint=f"nothing in the index shares a word with that; "
                              f"celonis uses {phrase!r} searches PQL and object fields",
                         ms=(time.perf_counter() - started) * 1000)
        trace.result(out)
        return out

    state, prompt = state_for(phrase, short), questions(short, phrase)
    answers, usage, secs, cached = client.ask(state, prompt)
    tokens_used, calls = usage["input_tokens"], 1
    trace.judgment(phrase, scored, index, n=1, k=len(short), reason="first pass", state=state,
                   questions=prompt, answers=answers, usage=usage,
                   latency_s=round(secs, 3), cached=cached)
    if SHORTLIST_FULL > SHORTLIST and thin(answers):
        wide_scored = index.top_scored(phrase, k=SHORTLIST_FULL)
        wide = [e for _, e in wide_scored]
        if len(wide) > len(short):
            probs = answers["match"]["probabilities"]
            trace.post("escalate", because="the model refused, or no candidate stood out",
                       model_pick=answers["match"]["choice"],
                       best=round(max((v for k, v in probs.items() if k != "none"), default=0.0), 3),
                       threshold=T.PROMOTE, k_from=len(short), k_to=len(wide))
            wide_state, wide_prompt = state_for(phrase, wide), questions(wide, phrase)
            wide_answers, wide_usage, secs, wide_cached = client.ask(wide_state, wide_prompt)
            answers, usage = wide_answers, wide_usage
            cached, short = cached and wide_cached, wide
            scored = wide_scored
            tokens_used += usage["input_tokens"]
            calls = 2
            trace.judgment(phrase, scored, index, n=2, k=len(wide),
                           reason="re-ask with the wider list", state=wide_state,
                           questions=wide_prompt, answers=wide_answers, usage=wide_usage,
                           latency_s=round(secs, 3), cached=wide_cached)

    m = decide_match(phrase, answers, short, index)
    trace.post("rank", model_pick=m.model_pick, model_confidence=round(m.model_conf, 2),
               p_none=round(m.p_none, 3),
               dropped_containers=[e["name"] for e in m.dropped],
               ranked=[{"slot": k, "name": e["name"], "kind": e["kind"], "prob": round(v, 3)}
                       for k, v, e in m.pre_kind])
    if m.kind_case == "applied":
        trace.post("kind", model=m.kind, confidence=round(m.kind_conf, 2), applied=True,
                   kept=[e["name"] for e in m.kept])
    elif m.kind_case == "unsettled":
        trace.post("kind", model=m.kind, confidence=round(m.kind_conf, 2), applied=False,
                   because="the model named no kind, or was not sure of it")

    result = Resolution(phrase=phrase, path="ranked", calls=calls, intent=m.intent,
                        exists=round(m.exists, 2), p_none=round(m.p_none, 2),
                        confidence=round(m.conf, 2),
                        ms=(time.perf_counter() - started) * 1000, tokens=tokens_used,
                        cost=tokens_used / 1e6 * jev.PRICE_IN, cached=cached)
    chosen: dict | None = m.entry
    if m.branch == "matched":
        result.confirm, result.margin = m.confirm, round(m.margin, 3)
        trace.post("decision", branch="matched", confidence=round(m.conf, 2),
                   runner_up=round(m.runner_up, 3), margin=round(m.margin, 3),
                   confirm=m.confirm,
                   because=("the model picked a candidate" if m.conf >= T.MATCH
                            else "the model picked it, below the confidence to act on"))
    elif m.branch == "fallback":
        result.path, result.confirm, result.weak = "fallback", True, True
        trace.post("decision", branch="fallback", model_pick="none", best=round(m.best, 3),
                   threshold=T.PROMOTE,
                   because="the model refused, but one candidate cleared T_PROMOTE anyway")
    else:
        result.name = None
        result.reason = m.reason
        trace.post("decision", branch="refused", p_none=round(m.p_none, 3),
                   best=round(m.best, 3),
                   threshold=T.NONE if m.p_none >= T.NONE else T.PROMOTE,
                   because="nothing cleared the floor a refusal is overruled by")

    pinned = same_thing_in_container(phrase, chosen, index) if chosen else None
    if pinned:                     # the ask named a container, so that picks the instance
        chosen, container = pinned
        result.pinned_to, result.confirm = container["name"], False
        trace.post("pin", container=container["name"], to=_trail_entry(chosen),
                   because="the ask named a container, and it holds exactly one of these")
    if chosen:
        _place(result, chosen)
        # `exists` is the model's judgment about the candidate LIST. A confident
        # match is evidence the list did hold it, so a thin index entry no longer
        # marks a correct resolution ungrounded.
        result.grounded = m.exists >= T.EXISTS or m.conf >= T.MATCH
        trace.post("grounded", exists=round(m.exists, 2), confidence=round(m.conf, 2),
                   verdict=result.grounded,
                   because=f"exists >= {T.EXISTS}" if m.exists >= T.EXISTS
                   else f"match confidence >= {T.MATCH}")

    result.alternatives = [
        {"name": e["name"], "kind": e["kind"], "prob": round(p, 2), "url": e["url"]}
        for k, p, e in m.ranked
        if k != m.hit_slot and e["id"] != (chosen or {}).get("id")][:3]
    trace.result(result)

    if verbose:
        print(f'"{phrase}"')
        print(f"  exists={result.exists} p_none={result.p_none} intent={result.intent} "
              f"conf={result.confidence} calls={result.calls} "
              f"tokens={result.tokens} {result.ms:.0f}ms")
        if result.name:
            print(f"  -> [{result.kind}] {result.name}\n     {result.url}")
            if result.pinned_to:
                print(f"     (the ask names {result.pinned_to}; took that one's copy)")
        else:
            print(f"  -> {result.reason}")
        for a in result.alternatives:
            print(f"     alt {a['prob']:.2f} [{a['kind']}] {a['name']}")
    return result


def resolve_code_only(phrase: str, index: "Index") -> Resolution:
    """Baseline: what code alone reaches, with no model anywhere.

    The vocabulary, a spelled-out asset name and the tenant's own name search are
    all lookups, so they come first; BM25's top entry is the fallback.
    """
    started = time.perf_counter()
    quick = decide(phrase, index)
    if quick:
        entry, why = quick
        return answer_from_entry(phrase, entry, path_for(why), started, index, why)
    named = named_lookup(phrase)
    if named:
        n = named[0]
        return Resolution(phrase=phrase, path="named_lookup", name=n.get("name"),
                          kind=n.get("assetType", n.get("type", "")),
                          url=n.get("url") or n.get("link") or "", intent="open",
                          confidence=1.0,
                          ms=(time.perf_counter() - started) * 1000)
    short = index.top(phrase, k=1)
    e = short[0] if short else None
    return Resolution(phrase=phrase, path="bm25_top1",
                      name=e["name"] if e else None, kind=e["kind"] if e else None,
                      url=e["url"] if e else "", confidence=0.0,
                      ms=(time.perf_counter() - started) * 1000)


DEMO = [
    "open the operations dashboard",
    "show the KPI for order cycle time",
    "where do I define the invoice object",
    "the data jobs in the main pool",
    "the SQL editor for the analytics pool",
]


def main() -> None:
    if "--demo" in sys.argv:
        client = jev.Jev()
        idx = Index()
        rows = []
        for phrase in DEMO:
            r = resolve(phrase, client=client, index=idx, verbose=False)
            rows.append(r)
            mark = "OK " if r.name else "-- "
            print(f"{mark}{phrase:<46} -> "
                  f"{(r.name or r.reason or '?')[:40]:<42} "
                  f"{('[' + (r.kind or '-') + ']')[:14]:<15} conf={r.confidence} "
                  f"tok={r.tokens:>5} {r.ms:>6.0f}ms")
            if r.name:
                print(f"      {r.url}")
            elif r.alternatives:
                print("      alts: " + "; ".join(f"{a['name'][:30]} {a['prob']}" for a in r.alternatives))
        ok = sum(1 for r in rows if r.name)
        tok = sum(r.tokens for r in rows)
        ms = sum(r.ms for r in rows)
        print(f"\n{ok}/{len(rows)} resolved | {tok:,} tokens | ${tok / 1e6 * jev.PRICE_IN:.4f} | "
              f"{ms / 1000:.1f}s total ({ms / len(rows):.0f}ms per ask)")
        return
    phrase = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
    if not phrase:
        print(__doc__)
        return
    resolve(phrase)


if __name__ == "__main__":
    try:
        main()
    except jev.JevError as e:
        raise SystemExit(str(e))
