"""Live demo: Jev steering an HTTP navigation agent, one call per step.

Run: python3 demo.py
"""

from __future__ import annotations

import json
import time

import agent
import jev

TASKS = [
    ("on-page: answer already here", "https://en.wikipedia.org/wiki/Burkina_Faso",
     "the capital city of Burkina Faso", "", "Ouagadougou"),
    ("search: site search then article", "https://en.wikipedia.org/wiki/Main_Page",
     "who won the 2006 FIFA World Cup final", "2006 FIFA World Cup final", "Italy"),
    ("nav: two hops through a docs nav", "https://docs.typesafe.ai/",
     "the price of Jev per million tokens", "", "0.042"),
    ("nav: another nav trail", "https://docs.typesafe.ai/",
     "what low confidence tells the code to do", "", "route to a human"),
]


def main() -> None:
    client = jev.Jev()
    summary = []
    for label, url, goal, query, expect in TASKS:
        print(f"\n=== {label}\ntask: {goal}")
        started = time.perf_counter()
        before = (client.input_tokens, client.seconds, len(client.__dict__))
        run = agent.navigate(goal, url, query=query, max_steps=6, client=client)
        wall = time.perf_counter() - started
        steps = run.steps
        tok = sum(s.tokens for s in steps)
        jev_ms = sum(s.ms for s in steps)
        hit = expect.lower() in run.answer.lower()
        summary.append((label, len(steps), tok, jev_ms, wall, run.found, hit))
        print(f"  steps={len(steps)} tokens={tok} "
              f"jev={jev_ms:.0f}ms wall={wall:.1f}s "
              f"answer_match={'yes' if hit else 'NO'}")

    print("\n=== summary")
    print(f"{'task':<34}{'steps':>6}{'tokens':>8}{'jev ms':>8}{'wall s':>8}{'found':>7}{'match':>7}")
    for label, n, tok, ms, wall, found, hit in summary:
        print(f"{label:<34}{n:>6}{tok:>8}{ms:>8.0f}{wall:>8.1f}"
              f"{'yes' if found else 'no':>7}{'yes' if hit else 'NO':>7}")
    print(f"\ncalls={client.calls} (replays={client.replays}) "
          f"tokens={client.input_tokens} cost=${client.cost_usd:.4f} "
          f"jev_time={client.seconds:.1f}s")


if __name__ == "__main__":
    try:
        main()
    except jev.JevError as e:
        raise SystemExit(str(e))
