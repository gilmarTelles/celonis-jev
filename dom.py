"""Live-DOM page model: the same candidate shape as `page.py`, read from a real page.

`page.py` parses HTML for a fetch-based agent. A browser-based agent needs the
rendered DOM instead - same ids, same fields, so `agent.navigate` and
`page.select_text` work unchanged. That is the whole difference between the two
agents: which function produces `{title, url, candidates, text}`.

Both front-ends apply the same extraction rules: the JS literals in
`SNAPSHOT_JS` are generated from `page_rules` at import time, so the rules are
stated once and the two cannot drift apart.

`SNAPSHOT_JS` runs inside the page (see README notes: pass it to `tab.evaluate`),
and `snapshot_to_page` maps its JSON onto the page dict the loop expects.
"""

from __future__ import annotations

import json

import page_rules as rules


def _js_list(values, upper: bool = False) -> str:
    """JS array literal, in the given order."""
    return "[" + ", ".join(json.dumps(v.upper() if upper else v) for v in values) + "]"


def _js_set(values, upper: bool = False) -> str:
    """JS Set literal, sorted so the generated script is stable across runs.
    `upper` renders tag names the way the DOM reports them (`el.tagName`)."""
    return "new Set(" + _js_list(sorted(v.upper() if upper else v for v in values)) + ")"


def _js_table(rows) -> str:
    """JS literal for an ordered (key, flag) table."""
    return "[" + ", ".join(f"[{json.dumps(k)}, {str(ok).lower()}]" for k, ok in rows) + "]"


SNAPSHOT_JS = rf"""
(() => {{
  const SKIP = {_js_set(rules.SKIP, upper=True)};
  const INTERACTIVE = {_js_set(rules.INTERACTIVE, upper=True)};
  const TEXTY = {_js_set(rules.TEXTY, upper=True)};
  const ROLES = {_js_set(rules.ROLES)};
  const LABEL_PRECEDENCE = {_js_table(rules.LABEL_PRECEDENCE)};
  const LABEL_MAX = {rules.LABEL_MAX};
  const LANDMARK = {json.dumps(",".join(sorted(rules.LANDMARK)))};
  const REGION_ORDER = {_js_list(rules.REGION_ORDER)};
  const REGION_CAP = {json.dumps(rules.REGION_CAP)};
  const TEXT_REGIONS = {_js_set(rules.TEXT_REGIONS)};
  const PROSE = {_js_set(rules.PROSE, upper=True)};
  const VALUES = {_js_set(rules.VALUES, upper=True)};
  const LABELS = {_js_set(rules.LABELS, upper=True)};
  const MIN_PROSE = {rules.MIN_PROSE};
  const MIN_VALUE = {rules.MIN_VALUE};
  const MAX_TEXT_CHARS = {rules.MAX_TEXT_CHARS};
  const MAX_TEXT_POOL = {rules.MAX_TEXT_POOL};
  const clean = s => (s || "").replace(/\s+/g, " ").trim();
  const canon = r => (REGION_CAP[r] === undefined ? "body" : r);
  const seen = new Set();

  function visible(el) {{
    if (el.hasAttribute("hidden") || el.getAttribute("aria-hidden") === "true") return false;
    for (let p = el; p && p.nodeType === 1; p = p.parentElement) {{
      if (SKIP.has(p.tagName) || p.hasAttribute("hidden") || p.getAttribute("aria-hidden") === "true") return false;
    }}
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none" && cs.opacity !== "0";
  }}
  function region(el) {{
    const r = el.closest(LANDMARK);
    return r ? r.tagName.toLowerCase() : "body";
  }}
  const slug = href => {{
    if (!href) return "";
    let path = href;
    try {{ path = new URL(href, location.href).pathname; }} catch (e) {{}}
    return clean(path.replace(/^\/+|\/+$/g, "").split("/").join(" ").split("-").join(" ")) || href;
  }};
  function label(el, text) {{
    const a = k => clean(el.getAttribute(k) || "");
    let lab = "";
    for (const [k, wins] of LABEL_PRECEDENCE) {{
      const v = a(k);
      if (v && (wins || !text)) {{ lab = v; break; }}
    }}
    return (lab || text || slug(a("href"))).slice(0, LABEL_MAX);
  }}

  const candidates = [];
  for (const el of document.querySelectorAll(
      "a,button,input,select,textarea,summary,[role],[contenteditable=true],[onclick]")) {{
    if (!visible(el)) continue;
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (!INTERACTIVE.has(el.tagName) && !ROLES.has(role) && !el.hasAttribute("onclick")
        && el.getAttribute("contenteditable") !== "true") continue;
    let text = "";
    for (const n of el.childNodes) if (n.nodeType === 3) text += n.nodeValue;
    if (!text.trim()) text = el.innerText || "";
    const href = el.getAttribute("href") || "";
    const bad = !href || href === "#" || /^(javascript|mailto|tel):/i.test(href);
    if (el.tagName === "A" && bad) continue;
    const kind = el.tagName === "INPUT" ? "input:" + (el.type || "text").toLowerCase()
               : ROLES.has(role) && el.tagName !== "A" && el.tagName !== "INPUT" ? "role:" + role
               : el.tagName.toLowerCase();
    const row = {{
      kind, region: region(el), label: label(el, clean(text)),
      href: bad ? "" : href,
      url: bad ? "" : new URL(href, location.href).href.split("#")[0],
      name: el.getAttribute("name") || "",
      form_action: (el.closest("form") || {{}}).action || "",
      box: (b => [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)])
           (el.getBoundingClientRect()),
    }};
    const dedupe = row.label.toLowerCase() + "|" + (row.url || row.kind);
    if (seen.has(dedupe)) continue;
    seen.add(dedupe);
    candidates.push(row);
  }}
  const kept = [], perRegion = {{}};
  for (const c of candidates) {{
    const r = canon(c.region);
    if ((perRegion[r] || 0) >= REGION_CAP[r]) continue;
    perRegion[r] = (perRegion[r] || 0) + 1;
    kept.push(c);
  }}
  kept.sort((a, b) => REGION_ORDER.indexOf(canon(a.region)) - REGION_ORDER.indexOf(canon(b.region)));

  const text = [];
  const seenText = new Set();
  let hold = "";
  for (const el of document.querySelectorAll([...TEXTY].join(","))) {{
    if (!visible(el)) continue;
    const raw = clean(el.innerText || el.textContent || "");
    if (LABELS.has(el.tagName)) {{ hold = raw; continue; }}
    let body = raw;
    if (VALUES.has(el.tagName) && hold) {{
      body = hold + ": " + raw;
      hold = "";
    }} else {{
      hold = "";
    }}
    body = body.slice(0, MAX_TEXT_CHARS);
    if (body.length < (PROSE.has(el.tagName) ? MIN_PROSE : MIN_VALUE)) continue;
    const r = canon(region(el));
    if (!TEXT_REGIONS.has(r)) continue;
    const key = body.slice(0, 80).toLowerCase();
    if (seenText.has(key)) continue;
    seenText.add(key);
    text.push({{tag: el.tagName.toLowerCase(), region: r, text: body}});
    if (text.length >= MAX_TEXT_POOL) break;
  }}
  return JSON.stringify({{url: location.href, title: document.title,
                         candidates: kept, text: text}});
}})()
"""


def snapshot_to_page(snapshot: str | dict) -> dict:
    """Browser snapshot -> the page dict `agent.navigate` and `page.select_text` use."""
    snap = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
    for i, c in enumerate(snap["candidates"]):
        c["id"] = f"e{i:02d}"
    for i, t in enumerate(snap["text"]):
        t["id"] = f"t{i:02d}"
    return {"title": snap.get("title", ""), "url": snap.get("url", ""),
            "candidates": snap["candidates"], "text": snap["text"]}
