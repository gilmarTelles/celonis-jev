"""Navigation loop: one Jev call per step, many speculative questions per call.

Division of labour
  code  : fetch, parse, shortlist candidates, prefetch destinations, copy the
          answer verbatim, keep the URL graph, decide with thresholds
  Jev   : which element to act on, whether the page already answers, which text
          block holds the answer, which prefetched destination is the one

Per step the questions are sent together, so a wrong branch costs nothing: the
retry target is already in the same probability distribution, no second call.
"""

from __future__ import annotations

import enum
import urllib.parse
from dataclasses import dataclass, field

import jev
import page

T_ON_PAGE = 0.80     # Noul: page text already answers the goal
T_ANSWER = 0.20      # Choice confidence: the selected text block is the answer
T_BLOCKED = 0.60     # Noul: overlay / banner / login wall covers the content
T_AHEAD = 0.50       # Choice probability: a prefetched destination answers already
T_TARGET = 0.30      # Choice confidence: act on the chosen element
T_NONE = 0.50        # p(none): no element on this page advances the goal
T_DISMISS = 0.30     # Choice confidence: act on the chosen overlay closer


def _cands(pg: dict, skip: set[str] | None = None) -> list[dict]:
    skip = skip or set()
    return [c for c in pg["candidates"] if c["id"] not in skip]


def build_state(goal: str, pg: dict, history: list[dict], skip: set[str],
                ahead: list[dict]) -> dict:
    cands = []
    for c in _cands(pg, skip):
        row = {"id": c["id"], "kind": c["kind"], "region": c["region"],
               "label": c["label"]}
        if c["href"]:
            row["href"] = c["href"][:90]
        cands.append(row)
    state = {
        "goal": goal,
        "step": len(history) + 1,
        "page": {"url": pg["url"], "title": pg["title"],
                 "candidates": cands,
                 "text": [{"id": t["id"], "tag": t["tag"], "text": t["text"]}
                          for t in pg["text"]]},
        "did": history[-4:],
    }
    if skip:
        state["skip"] = sorted(skip)
    if ahead:
        state["page"]["ahead"] = [
            {"id": f"a{i}", "url": a["url"], "title": a["title"],
             "headings": a["headings"], "lead": a["lead"]}
            for i, a in enumerate(ahead)]
    return state


def questions(pg: dict, skip: set[str], ahead: list[dict]) -> dict:
    ids = [c["id"] for c in _cands(pg, skip)]
    tids = [t["id"] for t in pg["text"]]
    q: dict = {
        "target": {
            "type": "choice",
            "instructions": (
                "Which single element in `page.candidates` most directly advances "
                "`goal`? Pick the element to click or type into next. When a link "
                "appears in `page.ahead`, its destination headings are shown, so "
                "judge the link by where it leads. Prefer content links in the "
                "`main` region over site chrome, and never pick an element listed "
                "in `skip`."),
            "criteria": {**{i: None for i in ids},
                         "none": "no element on this page advances the goal"},
        },
        "kind": {
            "type": "choice",
            "instructions": "What is the next action at `page` for `goal`?",
            "criteria": {
                "follow_link": "follow the link of the chosen element in `page.candidates`",
                "use_search": "submit the site search box with a query for the goal",
                "dismiss_overlay": "close a banner, modal, cookie or login notice that covers the content first",
                "answer_ready": "`page.text` already answers the goal, so nothing needs clicking",
                "give_up": "the goal cannot be reached from this page",
            },
        },
        "on_page": {
            "type": "noul",
            "instructions": (
                "Does `page.text` already state the answer to `goal`, in words? "
                "Answer yes only when a reader could answer from `page.text` alone."),
            "criteria": {"true": "an entry of `page.text` states the answer",
                         "false": "no entry of `page.text` states the answer"},
        },
        "answer": {
            "type": "choice",
            "instructions": (
                "Which entry of `page.text` contains the answer to `goal`? The "
                "entry's text is copied verbatim, so pick only an entry that "
                "states the answer."),
            "criteria": {**{i: None for i in tids},
                         "none": "no entry states the answer"},
        },
        "blocked": {
            "type": "noul",
            "instructions": (
                "Does `page` show an overlay, cookie banner, modal, login wall or "
                "captcha that covers the content a reader wants?"),
            "criteria": {"true": "something covers the content",
                         "false": "the content is readable"},
        },
        "dismiss": {
            "type": "choice",
            "instructions": (
                "Which element in `page.candidates` would close an overlay, cookie "
                "banner, modal or login prompt?"),
            "criteria": {**{i: None for i in ids},
                         "none": "nothing on the page blocks the content"},
        },
    }
    if ahead:
        q["ahead"] = {
            "type": "choice",
            "instructions": (
                "Do the headings or lead text of a prefetched destination in "
                "`page.ahead` show that it already answers `goal`? Pick that "
                "destination so the reader can skip the page in between."),
            "criteria": {**{f"a{i}": None for i in range(len(ahead))},
                         "none": "none of the prefetched destinations answers it yet"},
        }
    return q


@dataclass
class Step:
    n: int
    url: str
    title: str
    ncand: int
    ntext: int
    action: str
    detail: str
    probs: dict = field(default_factory=dict)
    tokens: int = 0
    ms: float = 0.0
    cached: bool = False


@dataclass
class Run:
    goal: str
    found: bool
    answer: str = ""
    url: str = ""
    status: str = ""
    steps: list[Step] = field(default_factory=list)


def _rank(p: dict, skip: set[str]) -> list[tuple[str, float]]:
    return sorted(((k, v) for k, v in p.items() if k != "none" and k not in skip),
                  key=lambda kv: kv[1], reverse=True)


def _search_url(base: str, pg: dict, query: str) -> str | None:
    for c in pg["candidates"]:
        if c["kind"].startswith("input") and c["form_action"] and c["name"]:
            action = urllib.parse.urljoin(base, c["form_action"])
            sep = "&" if "?" in action else "?"
            return f"{action}{sep}{urllib.parse.quote(c['name'])}={urllib.parse.quote(query)}"
    return None


class ActionKind(enum.Enum):
    """What the loop does next; `Action.display` gives the reported action."""
    ANSWER = "answer"
    FOLLOW_LINK = "follow_link"
    USE_SEARCH = "use_search"
    DISMISS_OVERLAY = "dismiss_overlay"
    JUMP_AHEAD = "jump_ahead"
    ESCALATE = "escalate"


@dataclass
class Action:
    kind: ActionKind
    detail: str = ""
    probs: dict = field(default_factory=dict)
    next_url: str = ""
    answer: str = ""
    label: str = ""              # chosen element's label, or the search URL
    low_confidence: bool = False

    def display(self) -> str:
        return self.kind.value + (" low-conf" if self.low_confidence else "")


def decide(pg: dict, answers: dict, url: str, query: str = "",
           skip: set[str] | None = None, ahead: list[dict] | None = None,
           visited: set[str] | None = None) -> Action:
    """Turn one set of answers into one action. No I/O, no model: the same logic
    drives the HTTP loop and the browser loop."""
    skip, ahead, visited = skip or set(), ahead or [], visited or set()
    target, kind, ans = answers["target"], answers["kind"], answers["answer"]

    if answers["blocked"]["noul"] >= T_BLOCKED:
        dis = answers["dismiss"]
        if dis["choice"] != "none" and dis["confidence"] >= T_DISMISS:
            cid = dis["choice"]
            cand = next(c for c in pg["candidates"] if c["id"] == cid)
            nxt = urllib.parse.urldefrag(cand["url"] or _search_url(url, pg, query) or "")[0]
            return Action(ActionKind.DISMISS_OVERLAY,
                          f"{cid} p={dis['probabilities'][cid]:.2f}",
                          dis["probabilities"], nxt,
                          label=cand["label"][:34] or cid)

    if answers["on_page"]["noul"] >= T_ON_PAGE and ans["choice"] != "none" \
            and ans["confidence"] >= T_ANSWER:
        i = next(i for i, t in enumerate(pg["text"]) if t["id"] == ans["choice"])
        text = pg["text"][i]["text"]
        if i + 1 < len(pg["text"]):
            text += " || " + pg["text"][i + 1]["text"]
        return Action(ActionKind.ANSWER,
                      f"{ans['choice']} conf={ans['confidence']:.2f}",
                      ans["probabilities"], answer=text)

    if kind["choice"] == "use_search" and query:
        surl = _search_url(url, pg, query)
        if surl:
            return Action(ActionKind.USE_SEARCH, surl, kind["probabilities"],
                          surl, label=surl)

    if ahead and "ahead" in answers:
        ah = answers["ahead"]
        if ah["choice"] != "none" and ah["probabilities"][ah["choice"]] >= T_AHEAD:
            aid = ah["choice"]
            dest = ahead[int(aid[1:])]
            return Action(ActionKind.JUMP_AHEAD,
                          f"{dest['url']} p={ah['probabilities'][aid]:.2f}",
                          ah["probabilities"], dest["url"])

    ranked = _rank(target["probabilities"], skip)
    p_none = target["probabilities"].get("none", 0.0)
    while ranked and p_none < T_NONE:
        cid, p = ranked.pop(0)
        cand = next(c for c in pg["candidates"] if c["id"] == cid)
        raw = cand["url"] or (_search_url(url, pg, query)
                              if cand["kind"].startswith("input") else "")
        nxt = urllib.parse.urldefrag(raw)[0] if raw else ""
        if not nxt or nxt in visited:
            continue                 # in-page anchor, or a page already seen
        label = cand["label"][:34] or cid
        return Action(ActionKind.FOLLOW_LINK if cand["url"] else ActionKind.USE_SEARCH,
                      f"{cid} {label!r} p={p:.2f} none={p_none:.2f} -> {nxt[:60]}",
                      target["probabilities"], nxt, label=label,
                      low_confidence=target["confidence"] < T_TARGET)

    why = (f"no usable element: p(none)={p_none:.2f} conf={target['confidence']:.2f}"
           if ranked or p_none < T_NONE else f"p(none)={p_none:.2f}, target={target['choice']}")
    return Action(ActionKind.ESCALATE, why, target["probabilities"])


def _load(url: str, loader, html_cache: dict) -> dict:
    return loader(url) if loader else page.parse(page.fetch(url, html_cache), url)


def page_sig(pg: dict) -> str:
    """Identity of a rendered page: same url and same elements means an action
    changed nothing (a dead SPA click), so the loop should stop instead of retry."""
    import hashlib
    labels = "|".join(f'{c["kind"]}:{c["label"]}' for c in pg["candidates"])
    return hashlib.sha1((pg["url"] + labels).encode()).hexdigest()[:16]


def skip_seen(pg: dict, visited: set[str], here: str) -> set[str]:
    """Ids of candidates whose destination is already seen: following one of
    them would loop instead of advancing."""
    return {c["id"] for c in pg["candidates"]
            if c["url"] and urllib.parse.urldefrag(c["url"])[0] in visited | {here}}


def record_step(run: Run, step_no: int, pg: dict, act: Action, usage: dict,
                ms: float, cached: bool) -> Step:
    st = Step(step_no, pg["url"], pg["title"], len(pg["candidates"]),
              len(pg["text"]), act.display(), act.detail, act.probs,
              tokens=usage["input_tokens"], ms=ms * 1000, cached=cached)
    run.steps.append(st)
    return st


def dispatch(run: Run, act: Action, step_no: int, here: str, url: str,
             history: list[dict], visited: set[str], *,
             submits_search: bool = False,
             nowhere: str = "led nowhere") -> str | None:
    """Apply one action to the run and return the url to load next, or None
    when the loop must stop. `url` is the page the action was decided on (the
    request url on the fetch front-end, the rendered url in a browser).
    `submits_search` marks a front-end that can submit the search box by GET;
    a browser cannot, so typing is left to the caller. `nowhere` completes the
    status of an action that led nowhere."""
    if act.kind is ActionKind.ANSWER:
        run.found, run.answer, run.url = True, act.answer, url
        run.status = "answered without generating text"
        return None
    if act.kind is ActionKind.ESCALATE:
        run.answer, run.url = act.answer, url
        run.status = "no element clears the bar - hand to a reasoning model"
        return None
    if act.kind is ActionKind.USE_SEARCH and not submits_search:
        run.status = (f"needs input: the next step is typing into {act.label!r}, "
                      "which belongs to the caller (credentials), not the loop")
        return None
    if not act.next_url or act.next_url == here:
        run.status = f"{act.display()} {nowhere}"
        return None
    history.append({"step": step_no, "did": f"{act.display()} {act.detail[:40]}"})
    visited.add(act.next_url)
    return act.next_url


def navigate(goal: str, start_url: str, query: str = "", max_steps: int = 6,
             client: jev.Jev | None = None, prefetch: bool = True,
             trace: bool = True, loader=None) -> Run:
    """`loader(url) -> page dict` defaults to HTTP fetch + parse. A browser loader
    (see `dom.py`) drives a JS app instead; the decision logic is unchanged."""
    client = client or jev.Jev()
    if loader is not None:
        prefetch = False          # a browser would need extra tabs to look ahead
    html_cache: dict[str, str] = {}
    run = Run(goal=goal, found=False)
    url = start_url
    history: list[dict] = []
    visited = {urllib.parse.urldefrag(start_url)[0]}
    prev_sig = None

    for step_no in range(1, max_steps + 1):
        try:
            pg = _load(url, loader, html_cache)
        except Exception as e:
            run.status = f"load failed: {e!r}"
            break
        sig = page_sig(pg)
        if sig == prev_sig:
            run.status = "no progress: the page did not change"
            break
        prev_sig = sig
        pg["text"] = page.select_text(pg["text"], goal)
        here = urllib.parse.urldefrag(url)[0]
        skip = skip_seen(pg, visited, here)
        ahead = (page.prefetch_ahead(goal, pg["candidates"], url, html_cache,
                                     visited) if prefetch else [])
        answers, usage, ms, cached = client.ask(build_state(goal, pg, history,
                                                            skip, ahead),
                                                questions(pg, skip, ahead))
        act = decide(pg, answers, url, query, skip, ahead, visited)
        record_step(run, step_no, pg, act, usage, ms, cached)
        nxt = dispatch(run, act, step_no, here, url, history, visited,
                       submits_search=True)
        if nxt is None:
            break
        url = nxt
    else:
        run.status = f"step budget {max_steps} exhausted"

    if trace:
        _print_run(run)
    return run


def _print_run(run: Run) -> None:
    print(f'goal: {run.goal}')
    for s in run.steps:
        tag = "cache" if s.cached else f"{s.ms:5.0f}ms"
        top = sorted(((k, v) for k, v in s.probs.items() if k != "none"),
                     key=lambda kv: kv[1], reverse=True)[:3]
        rank = "  ".join(f"{k}:{v:.2f}" for k, v in top)
        print(f"  {s.n} {tag:>7} {s.tokens:>6}tok {s.action:<14} {s.detail[:78]}")
        if rank:
            print(f"      ranked: {rank}")
    print(f"  -> {'FOUND' if run.found else 'NOT FOUND'}: {run.status}")
    if run.answer:
        print(f"     {run.url}\n     {run.answer[:400]}")
