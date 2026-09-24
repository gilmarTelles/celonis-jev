"""Page -> candidate model. Code owns the DOM; Jev owns the judgment.

Turns raw HTML into a compact, addressable state:
  candidates: every interactive element, id-labelled (e00, e01, ...)
  text:       every meaningful text block, id-labelled (t00, t01, ...)

No third-party deps. This is the candidate-coverage step: Jev can only choose an
element this file emits, so what is reachable is a code property, not a model one.
Region quotas keep site chrome (search box, nav) in the list beside content links,
and keep boilerplate text out of the state.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

# Candidate and text extraction rules, shared with the browser front-end.
from page_rules import (INTERACTIVE, LABELS, LABEL_MAX, LABEL_PRECEDENCE,
                        LANDMARK, MAX_TEXT_CHARS, MAX_TEXT_POOL, MIN_PROSE,
                        MIN_VALUE, PROSE, REGION_CAP, REGION_ORDER, ROLES, SKIP,
                        TEXT_REGIONS, TEXTY, VALUES, VOID)

UA = "Mozilla/5.0 (compatible; jev-nav/0.1; +local)"
TIMEOUT = 25

MAX_HEADINGS = 12
MAX_PROSE = 28
MAX_VALUES = 22
PREFETCH_K = 3

TEXT_STOP = set("""a an the of to in on for and or is are was were be been being what
which who whom whose how when where why do does did can could should would will
shall may might must i me my we our you your it its this that these those with
from at by as into about over after before under again more most some such no
not only own same so than too very just now""".split())


def fetch(url: str, cache: dict[str, str] | None = None) -> str:
    if cache is not None and url in cache:
        return cache[url]
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
        charset = r.headers.get_content_charset() or "utf-8"
    html = raw.decode(charset, "replace")
    if cache is not None:
        cache[url] = html
    return html


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


class _Extract(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.regions: list[str] = []
        self.suppress = 0
        self.ints: list[dict] = []
        self.blocks: list[dict] = []
        self.candidates: list[dict] = []
        self.texts: list[dict] = []
        self.forms: list[str] = []
        self.hold = ""
        self.title = ""
        self._in_title = False

    @property
    def region(self) -> str:
        return self.regions[-1] if self.regions else "body"

    def _label(self, a: dict, text: str) -> str:
        for key, wins_over_text in LABEL_PRECEDENCE:
            if a.get(key, "").strip() and (wins_over_text or not text):
                return clean(a[key])
        if text:
            return text
        href = a.get("href", "")
        if href:
            path = urllib.parse.urlsplit(urllib.parse.urljoin(self.base, href)).path
            slug = clean(path.strip("/").replace("/", " ").replace("-", " "))
            return slug or href
        return ""

    def _flush_int(self) -> None:
        cap = self.ints.pop()
        a = cap["attrs"]
        label = self._label(a, clean(" ".join(cap["text"])))[:LABEL_MAX]
        href = clean(a.get("href", ""))
        if href.startswith(("javascript:", "mailto:", "tel:")) or href == "#":
            href = ""
        kind = cap["tag"]
        if kind == "input":
            itype = (a.get("type") or "text").lower()
            if itype == "hidden":
                return
            kind = f"input:{itype}"
        elif kind == "a" and not href:
            return
        elif cap.get("role") in ROLES and kind not in ("a", "input"):
            kind = f"role:{cap['role']}"
        if not label and not href:
            return
        self.candidates.append({
            "tag": cap["tag"], "kind": kind, "region": cap["region"],
            "label": label, "href": href, "name": a.get("name", ""),
            "form_action": self.forms[-1] if self.forms else "",
            "url": urllib.parse.urljoin(self.base, href) if href else "",
        })

    def _flush_block(self) -> None:
        t = self.blocks.pop()
        body = clean(" ".join(t["text"]))
        tag = t.get("as", t["tag"])
        if tag in LABELS:                      # hold a label until its value cell
            self.hold = body
            return
        if tag in VALUES and self.hold:
            body, self.hold = f"{self.hold}: {body}", ""
        else:
            self.hold = ""
        body = body[:MAX_TEXT_CHARS]
        if len(body) < (MIN_PROSE if tag in PROSE else MIN_VALUE):
            return
        self.texts.append({"tag": tag, "region": t["region"], "text": body})

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
            return
        a = {k: (v or "") for k, v in attrs}
        hidden = "hidden" in a or a.get("aria-hidden", "").lower() == "true"
        if self.suppress or tag in SKIP or hidden:
            if tag not in VOID:
                self.suppress += 1
            return
        if tag in LANDMARK:
            self.regions.append(tag)
        if tag == "form":
            self.forms.append(a.get("action", ""))
        role = a.get("role", "").lower()
        if (tag in INTERACTIVE or role in ROLES or "onclick" in a
                or a.get("contenteditable") == "true"):
            self.ints.append({"tag": tag, "attrs": a, "role": role,
                              "region": self.region, "text": []})
        texty = a.get("data-as", tag)          # <span data-as="p"> is a paragraph
        if texty in TEXTY:
            self.blocks.append({"tag": tag, "as": texty, "region": self.region,
                                "text": []})
        if tag in VOID:
            if self.ints and self.ints[-1]["tag"] == tag:
                self._flush_int()
            if self.blocks and self.blocks[-1]["tag"] == tag:
                self._flush_block()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.suppress:
            return
        for frame in self.ints:
            frame["text"].append(data)
        for block in self.blocks:
            block["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if self.suppress:
            self.suppress -= 1
            return
        if self.ints and self.ints[-1]["tag"] == tag:
            self._flush_int()
        if self.blocks and self.blocks[-1]["tag"] == tag:
            self._flush_block()
        if tag in LANDMARK and self.regions and self.regions[-1] == tag:
            self.regions.pop()
        if tag == "form" and self.forms:
            self.forms.pop()

    def close(self):
        super().close()
        while self.ints:
            self._flush_int()
        while self.blocks:
            self._flush_block()


def _region(r: str) -> str:
    return r if r in REGION_CAP else "body"


def text_tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{2,}", s.lower()) if w not in TEXT_STOP}


def parse(html: str, base: str = "about:blank") -> dict:
    p = _Extract(base)
    p.feed(html)
    p.close()

    seen: set[tuple[str, str]] = set()
    by_region: dict[str, list[dict]] = {}
    for c in p.candidates:
        key = (c["label"].lower(), c["url"] or c["kind"])
        if key in seen:
            continue
        seen.add(key)
        r = _region(c["region"])
        if len(by_region.get(r, ())) >= REGION_CAP[r]:
            continue
        by_region.setdefault(r, []).append(c)
    kept = [c for r in REGION_ORDER for c in by_region.get(r, [])]
    for i, c in enumerate(kept):
        c["id"] = f"e{i:02d}"

    body = [t for t in p.texts if _region(t["region"]) in TEXT_REGIONS]
    texts = []
    seen_t = set()
    for t in body:
        key = t["text"][:80].lower()
        if key in seen_t:
            continue
        seen_t.add(key)
        texts.append(t)
        if len(texts) >= MAX_TEXT_POOL:
            break
    return {"title": clean(p.title)[:160], "url": base,
            "candidates": kept, "text": texts}


def _score(goal_tokens: set[str], text: str) -> float:
    tt = text_tokens(text)
    if not tt:
        return 0.0
    return len(goal_tokens & tt) / len(tt) ** 0.5


def select_text(texts: list[dict], goal: str, max_heads: int = MAX_HEADINGS,
                max_prose: int = MAX_PROSE, max_values: int = MAX_VALUES,
                keep_first: int = 6) -> list[dict]:
    """Goal-aware cut of the text blocks: code narrows by lexical relevance and by
    document position, Jev then judges the survivors. Unrelated text in `state`
    costs accuracy, so the cut happens before the request, not after."""
    heads = [t for t in texts if t["tag"].startswith("h")][:max_heads]
    prose = [t for t in texts if t["tag"] in PROSE]
    values = [t for t in texts if t["tag"] in VALUES]
    g = text_tokens(goal)

    def pick(seq: list[dict], k: int) -> list[dict]:
        ranked = sorted(range(len(seq)), key=lambda i: (-_score(g, seq[i]["text"]), i))
        keep = set(ranked[:k]) | set(range(min(keep_first, len(seq))))
        return [seq[i] for i in sorted(keep)]

    picked = heads + pick(prose, max_prose) + pick(values, max_values)
    for i, t in enumerate(picked):
        t["id"] = f"t{i:02d}"
    return picked


def candidate_lines(cands: list[dict]) -> list[str]:
    out = []
    for c in cands:
        bits = [c["id"], c["kind"], f'region={c["region"]}']
        if c.get("label"):
            bits.append(f'label="{c["label"]}"')
        if c.get("href"):
            bits.append(f'href={c["href"][:70]}')
        out.append(" | ".join(bits))
    return out


def _overlap(goal_tokens: set[str], c: dict) -> float:
    hay = text_tokens(c.get("label", "") + " " + c.get("href", ""))
    if not hay or not goal_tokens:
        return 0.0
    return len(goal_tokens & hay) / (len(goal_tokens) ** 0.5 * len(hay) ** 0.5)


def prefetch_ahead(goal: str, cands: list[dict], base_url: str,
                   cache: dict[str, str], avoid: set[str] | None = None,
                   k: int = PREFETCH_K) -> list[dict]:
    """Code shortlists destinations by lexical overlap, fetches them concurrently,
    and returns only their title + headings: a cheap look one hop ahead."""
    host = urllib.parse.urlsplit(base_url).netloc
    avoid = avoid or set()
    g = text_tokens(goal)
    pool = [c for c in cands
            if c.get("url", "").startswith("http")
            and c["url"] not in avoid
            and urllib.parse.urlsplit(c["url"]).netloc == host]
    pool = sorted(pool, key=lambda c: _overlap(g, c), reverse=True)[:k]
    pool = [c for c in pool if _overlap(g, c) > 0]

    def grab(c: dict):
        try:
            return c["url"], parse(fetch(c["url"], cache), c["url"])
        except Exception:
            return c["url"], None

    ahead = []
    with ThreadPoolExecutor(max_workers=max(1, k)) as ex:
        for url, page in ex.map(grab, pool):
            if not page:
                continue
            heads = [t["text"][:90] for t in page["text"]
                     if t["tag"] in ("h1", "h2", "h3")][:6]
            lead = next((t["text"] for t in page["text"]
                         if t["tag"] in ("p", "li", "td")), "")
            if not heads and not lead:
                continue
            ahead.append({"url": url, "title": page["title"],
                          "headings": heads, "lead": lead[:200]})
    return ahead
