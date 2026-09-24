"""Browser-backed driver: same questions, same `agent.decide`, async I/O.

The HTTP loop cannot see a JS app; this one asks the live DOM for the page model
(`dom.SNAPSHOT_JS`) and navigates the tab. Everything between snapshot and action
is the shared code path, so thresholds, questions and decisions stay identical.

Run from a session that owns a browser tab:

    tab = await browser.open({"name": "site", "url": start})
    run = await navigate_browser(goal, start, tab)
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse

import agent
import dom
import jev
import page

SETTLE_S = 1.5            # grace period after a navigation, before looking at the DOM
QUIET_MS = 800            # DOM must stop mutating this long to count as rendered
QUIET_MAX_MS = 9000

QUIET_JS = """
new Promise(r => {
  let timer;
  const finish = () => { obs.disconnect(); clearTimeout(timer); r(true); };
  const obs = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(finish, %d); });
  obs.observe(document.documentElement, {subtree: true, childList: true, attributes: true});
  timer = setTimeout(finish, %d);
  setTimeout(finish, %d);
})
""" % (QUIET_MS, QUIET_MS, QUIET_MAX_MS)


async def snapshot(tab, settle: float = 0.0, samples: int = 4) -> dict:
    """SPAs render in waves: wait until the DOM stops changing, then take the
    richest of a few samples. A heavy editor (Knowledge Model) has a quiet moment
    mid-boot that a single read would mistake for the finished page."""
    if settle:
        await asyncio.sleep(settle)
    await tab.evaluate(QUIET_JS)
    best = dom.snapshot_to_page(await tab.evaluate(dom.SNAPSHOT_JS))
    for _ in range(samples - 1):
        await asyncio.sleep(0.7)
        pg = dom.snapshot_to_page(await tab.evaluate(dom.SNAPSHOT_JS))
        if len(pg["candidates"]) > len(best["candidates"]):
            best = pg
    for _ in range(6):                      # empty shell: give it another moment
        if best["candidates"]:
            break
        await asyncio.sleep(0.5)
        best = dom.snapshot_to_page(await tab.evaluate(dom.SNAPSHOT_JS))
    return best


async def navigate_browser(goal: str, start_url: str, tab, query: str = "",
                           max_steps: int = 6, client: jev.Jev | None = None,
                           trace: bool = True) -> agent.Run:
    client = client or jev.Jev()
    run = agent.Run(goal=goal, found=False)
    await tab.goto(start_url, wait_until="domcontentloaded")
    pg = await snapshot(tab, SETTLE_S)
    history: list[dict] = []
    visited = {urllib.parse.urldefrag(pg["url"])[0]}
    prev_sig = None

    for step_no in range(1, max_steps + 1):
        sig = agent.page_sig(pg)
        if sig == prev_sig:
            run.status = "no progress: the page did not change"
            break
        prev_sig = sig
        pg["text"] = page.select_text(pg["text"], goal)
        here = urllib.parse.urldefrag(pg["url"])[0]
        skip = agent.skip_seen(pg, visited, here)
        answers, usage, ms, cached = client.ask(
            agent.build_state(goal, pg, history, skip, []),
            agent.questions(pg, skip, []))
        act = agent.decide(pg, answers, pg["url"], query, skip, [], visited)
        agent.record_step(run, step_no, pg, act, usage, ms, cached)
        nxt = agent.dispatch(run, act, step_no, here, pg["url"], history,
                             visited, nowhere="led nowhere on a JS app")
        if nxt is None:
            break
        await tab.goto(nxt, wait_until="domcontentloaded")
        pg = await snapshot(tab, SETTLE_S)
    else:
        run.status = f"step budget {max_steps} exhausted"

    if trace:
        agent._print_run(run)
    return run


# --- site map -------------------------------------------------------------

T_NAV = 0.50
ASSET = (".png", ".jpg", ".jpeg", ".svg", ".gif", ".css", ".js", ".ico",
         ".woff", ".woff2", ".pdf", ".json", ".xml")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-", re.I)


def url_key(url: str) -> str:
    """Page identity for the map: scheme + host + path. Query strings here carry
    tab and node state, and would otherwise make one page look like many."""
    s = urllib.parse.urlsplit(url)
    return f"{s.scheme}://{s.netloc}{s.path}"


def is_area(url: str) -> bool:
    """Structural pages only: /ui/team, /store/ui, ... Instance URLs carry entity
    ids (/spaces/<uuid>/packages/<uuid>/nodes/<uuid>) and are content, not map."""
    return not UUID_RE.search(urllib.parse.urlsplit(url).path)


def section(url: str) -> str:
    """First two path segments: the app whose pages these are."""
    parts = [p for p in urllib.parse.urlsplit(url).path.split("/") if p]
    return "/".join(parts[:2])


def crawlable(url: str, origin: str, seen: set[str], blocked: tuple,
              prefix: str) -> bool:
    """A link the map can walk: same site, not yet seen, a page rather than an
    asset or catalog instance, outside the `blocked` prefixes and - when given
    - inside `prefix`."""
    s = urllib.parse.urlsplit(url)
    return (s.netloc == origin and url_key(url) not in seen
            and not url.lower().endswith(ASSET) and is_area(url)
            and not any(s.path.startswith(b) for b in blocked)
            and (not prefix or s.path.startswith(prefix)))


def nav_questions(pg: dict) -> dict:
    """One Noul per link: does it lead somewhere else, or is it page-local? The
    model judges meaning; code still does the counting, the queue and the dedupe."""
    q = {}
    for c in pg["candidates"]:
        if not c["url"]:
            continue
        label = c["label"][:60]
        q[f"nav_{c['id']}"] = {
            "type": "noul",
            "instructions": (
                f"Does clicking element {c['id']} ({label!r}) open a different page or "
                "application area, instead of changing something on this page "
                "(a filter, tab, dialog, sort, checkbox or menu)?"),
            "criteria": {"true": "the click leads to another page or area",
                         "false": "the click only changes this page"},
        }
    return q


def nav_state(pg: dict) -> dict:
    return {"task": "map the application so a reader can find each area",
            "page": {"url": pg["url"], "title": pg["title"],
                     "candidates": [{"id": c["id"], "kind": c["kind"],
                                     "region": c["region"], "label": c["label"],
                                     "href": c["href"][:90]}
                                    for c in pg["candidates"] if c["url"]]}}


async def crawl_nodes(tab, package_base: str, nodes: list[dict],
                      trace: bool = True) -> list[dict]:
    """Visit every asset of one package by URL. The ids come from the package API,
    so nothing has to be clicked: code builds `/nodes/<id>`, the browser opens it,
    and the editor's own surface is recorded. This is the shape a real assistant
    wants - addressable content, not guesswork."""
    out = []
    for n in nodes:
        url = f"{package_base}/nodes/{n['id']}"
        try:
            await tab.goto(url, wait_until="domcontentloaded")
            pg = await snapshot(tab, SETTLE_S)
        except Exception as e:
            out.append({**n, "error": repr(e)[:80]})
            continue
        tabs = [c["label"] for c in pg["candidates"] if c["kind"].startswith("role:tab")]
        buttons = [c["label"] for c in pg["candidates"]
                   if c["kind"] == "button" and c["label"]][:28]
        entry = {"id": n["id"], "name": n.get("name"), "asset": n.get("assetType"),
                 "key": n.get("key"), "url": pg["url"], "title": pg["title"],
                 "tabs": tabs, "buttons": buttons,
                 "headings": [t["text"][:90] for t in pg["text"]
                              if t["tag"].startswith("h")][:6],
                 "candidates": len(pg["candidates"])}
        out.append(entry)
        if trace:
            print(f"  {str(n.get('assetType')):<14} {str(n.get('name'))[:32]:<34} "
                  f"{len(pg['candidates'])} elements, {len(tabs)} tabs | {pg['title'][:60]}")
    return out


async def crawl(tab, start: str, client: jev.Jev | None = None, max_pages: int = 24,
                max_depth: int = 2, section_cap: int = 4,
                trace: bool = True, blocked: tuple = (), prefix: str = "") -> dict:
    """Breadth-first over navigable pages, because a site map that dives into one
    catalog subtree and never reaches the other apps is not a map. Pages are
    deduped by path, catalog instances skipped, each app capped, `blocked` path
    prefixes left out, and `prefix` (when given) keeps the walk inside one app."""
    client = client or jev.Jev()
    origin = urllib.parse.urlsplit(start).netloc
    seen, queue, pages = {url_key(start)}, [(start, 0)], []
    per_section: dict[str, int] = {}
    capped: dict[str, list[str]] = {}

    while queue and len(pages) < max_pages:
        url, depth = queue.pop(0)
        await tab.goto(url, wait_until="domcontentloaded")
        pg = await snapshot(tab, SETTLE_S)
        here = url_key(pg["url"])
        if any(p["key"] == here for p in pages):
            if trace:
                print(f"  {url[:70]} -> same page as an earlier one, skipped")
            continue
        if per_section.get(section(here), 0) >= section_cap:
            capped.setdefault(section(here), []).append(here)
            if trace:
                print(f"  {here[:88]}\n    section already covered, skipped")
            continue
        per_section[section(here)] = per_section.get(section(here), 0) + 1
        qs = nav_questions(pg)
        links = []
        if qs:
            answers, usage, ms, cached = client.ask(nav_state(pg), qs)
            keep = {k[4:] for k, a in answers.items() if a["noul"] >= T_NAV}
            links = [c for c in pg["candidates"] if c["id"] in keep and c["url"]]
            if trace:
                print(f"  {here[:88]}\n    {len(pg['candidates'])} candidates, "
                      f"{len(qs)} asked, {len(links)} navigate "
                      f"({usage['input_tokens']:,} tok{', cached' if cached else ''})")
        pages.append({
            "key": here, "url": pg["url"], "title": pg["title"], "depth": depth,
            "section": section(here),
            "headings": [t["text"][:80] for t in pg["text"] if t["tag"].startswith("h")][:8],
            "links": [{"label": c["label"][:60], "url": c["url"]} for c in links],
        })
        if depth >= max_depth:
            continue
        for c in links:
            nxt = c["url"]
            if not crawlable(nxt, origin, seen, blocked, prefix):
                continue
            k = url_key(nxt)
            if per_section.get(section(k), 0) >= section_cap:
                capped.setdefault(section(k), []).append(k)
                continue
            seen.add(k)
            queue.append((k, depth + 1))
    return {"pages": pages,
            "capped": [{"section": s, "urls": sorted(set(u))}
                       for s, u in sorted(capped.items())]}
