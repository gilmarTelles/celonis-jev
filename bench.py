"""Where the per-step milliseconds and tokens actually go.

Same state, same questions, two batching strategies: one call carrying every
question, versus one call per question. Also times the code-side work (fetch,
parse, shortlist) that surrounds the model call.
"""

from __future__ import annotations

import json
import time

import agent
import jev
import page

URL = "https://en.wikipedia.org/wiki/Burkina_Faso"
GOAL = "the capital city of Burkina Faso"


def _value(answer: dict):
    if answer["type"] == "noul":
        return round(answer["noul"], 2)
    if answer["type"] == "choice":
        return answer["choice"]
    return answer["score"]


def main() -> None:
    client = jev.Jev(use_cache=False)
    html_cache: dict[str, str] = {}

    t0 = time.perf_counter()
    pg = page.parse(page.fetch(URL, html_cache), URL)
    t_fetch = time.perf_counter() - t0
    t1 = time.perf_counter()
    pg["text"] = page.select_text(pg["text"], GOAL)
    state = agent.build_state(GOAL, pg, [], set(), [])
    qs = {k: v for k, v in agent.questions(pg, set(), []).items()}
    t_shape = time.perf_counter() - t1
    blob = len(json.dumps(state, ensure_ascii=False))

    print(f"page: {URL}")
    print(f"  fetch+parse {t_fetch * 1000:6.0f}ms   shape+shortlist {t_shape * 1000:5.0f}ms"
          f"   state {blob:,} chars   {len(pg['candidates'])} candidates, {len(pg['text'])} text blocks")
    print(f"  questions per step: {len(qs)} ({', '.join(qs)})")

    started = time.perf_counter()
    batched, usage_b, ms_b, _ = client.ask(state, qs, cache=False)
    t_batched = time.perf_counter() - started
    print(f"\n  one call / {len(qs)} questions : {t_batched * 1000:6.0f}ms  "
          f"{usage_b['input_tokens']:,} tok  ${usage_b['input_tokens'] / 1e6 * jev.PRICE_IN:.5f}")

    singles, t_single, tok_single = {}, 0.0, 0
    for key in qs:
        started = time.perf_counter()
        ans, usage, _, _ = client.ask(state, {key: qs[key]}, cache=False)
        t_single += time.perf_counter() - started
        tok_single += usage["input_tokens"]
        singles[key] = ans[key]
    print(f"  {len(qs)} calls / 1 question   : {t_single * 1000:6.0f}ms  "
          f"{tok_single:,} tok  ${tok_single / 1e6 * jev.PRICE_IN:.5f}")
    print(f"  -> {t_single / t_batched:.1f}x slower, "
          f"{tok_single / usage_b['input_tokens']:.1f}x the tokens")

    same = sum(_value(singles[k]) == _value(batched[k]) for k in qs)
    print(f"  identical answers: {same}/{len(qs)}")
    for key in qs:
        a, b = _value(batched[key]), _value(singles[key])
        flag = "" if a == b else "   <-- differs"
        extra = f" p={batched[key]['probabilities'][b]:.2f}" if key == "target" and isinstance(b, str) else ""
        print(f"    {key:<9} batched={a}{extra}  single={b}{flag}")

    enc = jev.Jev(use_cache=True)
    enc.ask(state, qs)                      # populate
    started = time.perf_counter()
    _, _, _, cached = enc.ask(state, qs)
    print(f"\n  replay of the same step       : {(time.perf_counter() - started) * 1000:.1f}ms "
          f"(from cache: {cached})")


if __name__ == "__main__":
    try:
        main()
    except jev.JevError as e:
        raise SystemExit(str(e))
