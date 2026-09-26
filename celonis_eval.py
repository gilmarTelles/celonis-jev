"""How often the resolver lands on the right entry, measured the same way every run.

The labelled set lives in celonis-eval.json (ignored: its phrases name tenant
things). Each case says which (kind, name) pairs are an acceptable answer; the
builder turns those into every index id that carries one, so any package's copy
of an entry counts. A case with no acceptable pair is an ask for something the
tenant does not hold, and the right answer is a refusal.

Each case runs two ways: code alone (`resolve_code_only`) and the full resolver
with Jev (`resolve`). Recall@10/@30 says whether BM25 put a right entry where the
judgment could see it. The resolver is driven from outside and never edited.

`--judges code,jev,agent` adds a third way: the calling agent itself, shown the
request and the `celonis_search` candidates exactly as the MCP tool returns them,
picks one id or none. It runs headless Claude Code (`claude -p`) with no tools
and no project settings, so it costs money on the user's Claude account.

  --live      the tenant's name search and Jev over the network; records the name
              search into bench/eval-cassette.json and every Jev answer into cache/
  --offline   (default) replays both; no credentials, no network. A miss is an
              error on that case, never a silent empty answer.
  Agent answers are recorded in bench/agent-cache/; --live reuses a recorded
  answer for the same model and prompt unless --fresh-agent asks for a new one.

Run:  python3 celonis_eval.py --build-cases        labels -> ids, fails on a stale label
      python3 celonis_eval.py --live --baseline    record, score, keep as the baseline
      python3 celonis_eval.py --offline            replay and compare to the baseline
      python3 celonis_eval.py --only e001,e002
      python3 celonis_eval.py --recall-only        BM25 and search recall only, in seconds
      python3 celonis_eval.py --recall-only --semantic off   search without the embedding model
      python3 celonis_eval.py --live --judges code,jev,agent --fresh-agent
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

import celonis_api
import celonis_embed
import celonis_resolve as R
import jev

HERE = Path(__file__).parent
CASES = HERE / "celonis-eval.json"
BENCH = HERE / "bench"
CASSETTE = BENCH / "eval-cassette.json"
BASELINE = BENCH / "eval-baseline.json"
BUCKETS = ("exact", "overlap", "paraphrase")
DETERMINISTIC = ("alias", "literal", "column")
AGENT_CACHE = BENCH / "agent-cache"
JUDGES = ("code", "jev", "agent")


class ReplayMiss(RuntimeError):
    pass


class CassetteMiss(RuntimeError):
    pass


class AgentMiss(RuntimeError):
    pass


# --- the labelled set ---------------------------------------------------------

def _pair(kind: str, name: str) -> tuple[str, str]:
    return kind, name.strip().lower()


def build_cases(index: R.Index, path: Path) -> dict:
    """Rewrite each case's expect_ids from its expect pairs. Fails on any pair no entry has."""
    blob = json.loads(path.read_text())
    ids_by_pair: dict[tuple[str, str], list[str]] = {}
    for e in index.entries:
        ids_by_pair.setdefault(_pair(e["kind"], e["name"]), []).append(e["id"])
    seen, stale = set(), []
    for c in blob["cases"]:
        if c["id"] in seen or c["bucket"] not in BUCKETS:
            raise SystemExit(f"{c['id']}: duplicate id or unknown bucket {c['bucket']!r}")
        seen.add(c["id"])
        ids: list[str] = []
        for p in c["expect"]:
            found = ids_by_pair.get(_pair(p["kind"], p["name"]))
            if not found:
                stale.append(f"  {c['id']} [{p['kind']}] {p['name']!r}")
            ids += [i for i in found or [] if i not in ids]
        c["expect_ids"] = ids
    if stale:
        raise SystemExit("expect pairs no index entry matches (fix the label or refresh the index):\n"
                         + "\n".join(stale))
    blob["built"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    blob["index_built"] = index.built
    path.write_text(json.dumps(blob, indent=1, ensure_ascii=False) + "\n")
    return blob


def print_case_counts(blob: dict) -> None:
    print(f"{len(blob['cases'])} cases against index {blob['index_built']}")
    for b in BUCKETS:
        cs = [c for c in blob["cases"] if c["bucket"] == b]
        kinds = sorted({p["kind"] for c in cs for p in c["expect"]})
        absent = sum(1 for c in cs if not c["expect"])
        print(f"  {b:<11} {len(cs):>3}  absent {absent}  kinds: {', '.join(kinds)}")


# --- record and replay --------------------------------------------------------

class RecordingJev(jev.Jev):
    """Jev over the network every time (honest latency), still writing the replay file."""

    def ask(self, state, questions: dict, cache: bool | None = None):
        answers, usage, secs, _ = super().ask(state, questions, cache=False)
        path = jev.CACHE_DIR / f"{jev._digest(state, questions, self.model)}.json"
        path.write_text(json.dumps({"answers": answers, "usage": usage}, indent=1))
        return answers, usage, secs, False


class ReplayJev:
    """Duck-types jev.Jev from cache/ alone; needs no key and opens no socket."""

    def ask(self, state, questions: dict, cache: bool | None = None):
        path = jev.CACHE_DIR / f"{jev._digest(state, questions, jev.MODEL)}.json"
        if not path.exists():
            raise ReplayMiss(f"no recorded Jev answer {path.name}; rerun with --live")
        payload = json.loads(path.read_text())
        return payload["answers"], payload["usage"], 0.0, True


def recording_lookup(cassette: dict, real):
    def lookup(phrase: str) -> list[dict]:
        got = real(phrase)
        if phrase in cassette and cassette[phrase] != got:
            print(f"  ! name search changed between calls for {phrase!r}; keeping the latest")
        cassette[phrase] = got
        return got
    return lookup


def replaying_lookup(cassette: dict):
    def lookup(phrase: str) -> list[dict]:
        if phrase not in cassette:
            raise CassetteMiss(f"no recorded name search for {phrase!r}; rerun with --live")
        return cassette[phrase]
    return lookup


def _no_network(*_a, **_k):
    raise RuntimeError("offline eval reached celonis_api")


def _no_tenant_search(phrase: str) -> list[dict]:
    raise RuntimeError(f"recall-only eval reached the tenant name search for {phrase!r}")


# --- the calling agent as a judge ---------------------------------------------

AGENT_SYSTEM = ("You pick which Celonis item a user means from search candidates. "
                "Answer with JSON only.")


def candidates_for_agent(cands: list[dict]) -> list[dict]:
    """A candidate as the MCP celonis_search tool shows it, minus the url."""
    return [{k: v for k, v in c.items() if k != "url"} for c in cands]


def agent_prompt(phrase: str, cands: list[dict]) -> str:
    listed = json.dumps({"candidates": candidates_for_agent(cands)}, indent=1, ensure_ascii=False)
    return (f"The user asked: {json.dumps(phrase, ensure_ascii=False)}\n\n"
            f"celonis_search returned:\n{listed}\n\n"
            'Reply with one JSON object: {"pick": "<candidate id>"} for the candidate the user '
            'means, or {"pick": "none"} when none of the candidates is what they asked for.')


def call_claude(prompt: str, model: str) -> dict:
    """One headless Claude Code turn: no tools, no settings, an empty cwd."""
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
           "--no-session-persistence", "--setting-sources", "", "--system-prompt", AGENT_SYSTEM]
    with tempfile.TemporaryDirectory() as cwd:
        t = time.perf_counter()
        done = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd,
                              timeout=180)
        wall_ms = (time.perf_counter() - t) * 1000
    try:
        out = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise AgentMiss(f"claude exited {done.returncode}: {(done.stderr or done.stdout)[:200]}")
    if out.get("is_error"):
        raise AgentMiss(f"claude reported an error: {str(out.get('result'))[:200]}")
    return {"model": model, "result": out.get("result", ""),
            "duration_api_ms": out.get("duration_api_ms"), "cost_usd": out.get("total_cost_usd", 0.0),
            "usage": out.get("usage"), "wall_ms": round(wall_ms, 1)}


def agent_answer(prompt: str, model: str, mode: str) -> dict:
    """The recorded answer (offline, live) or a new one (live on a miss, fresh)."""
    path = AGENT_CACHE / f"{hashlib.sha256((model + prompt).encode()).hexdigest()[:32]}.json"
    if mode != "fresh" and path.exists():
        return json.loads(path.read_text()) | {"replayed": True}
    if mode == "offline":
        raise AgentMiss(f"no recorded agent answer {path.name}; rerun with --live")
    got = call_claude(prompt, model)
    got["pick_raw"] = got["result"]
    AGENT_CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(got, indent=1, ensure_ascii=False) + "\n")
    return got | {"replayed": False}


def parse_pick(text: str, cands: list[dict]) -> str:
    """The picked id, or 'none'. Anything else is a malformed answer."""
    try:
        got = json.loads(text.strip())
    except json.JSONDecodeError:
        raise ValueError(f"not JSON: {text[:80]!r}")
    pick = got.get("pick") if isinstance(got, dict) else None
    ids = {c["id"] for c in cands} | {x["id"] for c in cands for x in c["copies"]}
    if pick != "none" and pick not in ids:
        raise ValueError(f"pick {pick!r} is not a candidate id")
    return pick


def agent_resolution(phrase: str, pick: str, index: R.Index) -> R.Resolution:
    if pick == "none":
        return R.Resolution(phrase=phrase, path="agent")
    e = index.by_ref[pick]
    return R.Resolution(phrase=phrase, path="agent", name=e["name"], kind=e["kind"],
                        entity_id=e["id"])


def run_agent(case: dict, index: R.Index, model: str, mode: str) -> dict:
    phrase = case["phrase"]
    t = time.perf_counter()
    cands = R.search(phrase, index, k=10)
    row = {"search_ms": (time.perf_counter() - t) * 1000, "semantic": index.semantic.ok}
    if case["expect"]:
        row["search_recall10"] = any(index.by_ref[c["id"]]["id"] in case["expect_ids"]
                                     for c in cands)
    else:
        row["search_recall10"] = None
    res = None
    try:
        ans = agent_answer(agent_prompt(phrase, cands), model, mode)
        row |= {"agent_raw": ans["result"], "agent_api_ms": ans["duration_api_ms"],
                "agent_wall_ms": ans["wall_ms"], "agent_cost": ans["cost_usd"],
                "agent_replayed": ans["replayed"],
                "agent_ms": row["search_ms"] + (ans["duration_api_ms"] or 0)}
        pick = parse_pick(ans["result"], cands)
        row["agent_id"] = pick
        res = agent_resolution(phrase, pick, index)
    except (AgentMiss, ValueError, TypeError, OSError, subprocess.TimeoutExpired) as e:
        row["agent_error"] = f"{type(e).__name__}: {e}"
    row |= {"agent_ok": correct(res, case), "agent_pick": pick_of(res)}
    return row


# --- scoring ------------------------------------------------------------------

def correct(res: R.Resolution | None, case: dict) -> bool:
    if res is None:
        return False
    if not case["expect"]:
        return res.name is None
    # A named answer with no entity id cannot be opened, so it scores wrong.
    return bool(res.entity_id) and res.entity_id in case["expect_ids"]


def pick_of(res: R.Resolution | None) -> dict:
    if res is None:
        return {}
    return {"name": res.name, "kind": res.kind, "path": res.path, "confirm": res.confirm}


def run_case(case: dict, index: R.Index, client, agent: tuple[str, str] | None) -> dict:
    phrase, row = case["phrase"], {"id": case["id"], "bucket": case["bucket"],
                                   "phrase": case["phrase"], "absent": not case["expect"]}
    if case["expect"]:
        top = [e["id"] for e in index.top(phrase, 30)]
        row["recall10"] = any(i in case["expect_ids"] for i in top[:10])
        row["recall30"] = any(i in case["expect_ids"] for i in top)
    else:
        row["recall10"] = row["recall30"] = None
    code = hit = None
    trail = R.Trail()
    try:
        t = time.perf_counter()
        code = R.resolve_code_only(phrase, index)
        row["code_ms"] = (time.perf_counter() - t) * 1000
        t = time.perf_counter()
        hit = R.resolve(phrase, client=client, index=index, verbose=False, trace=trail)
        row["jev_ms"] = (time.perf_counter() - t) * 1000
    except (ReplayMiss, CassetteMiss) as e:
        row["error"] = str(e)
    passes = trail.data["passes"]
    row |= {"code_ok": correct(code, case), "code_pick": pick_of(code),
            "jev_ok": correct(hit, case), "jev_pick": pick_of(hit),
            "jev_confirm": bool(hit and hit.confirm), "path": hit.path if hit else None,
            "calls": hit.calls if hit else 0, "tokens": hit.tokens if hit else 0,
            "deterministic": bool(hit and hit.path in DETERMINISTIC),
            "jev_net_s": round(sum(p.get("latency_s", 0.0) for p in passes), 3),
            "jev_cached": any(p.get("cached") for p in passes),
            "warnings": sorted({w for r in (code, hit) if r for w in r.warnings})}
    if agent:
        row |= run_agent(case, index, *agent)
    return row


def summarize(rows: list[dict]) -> dict:
    out, agent = {}, any("agent_ok" in r for r in rows)
    for b in BUCKETS + ("ALL",):
        rs = [r for r in rows if b == "ALL" or r["bucket"] == b]
        present = [r for r in rs if not r["absent"]]
        med = lambda k: round(statistics.median([r[k] for r in rs if k in r]), 1) \
            if any(k in r for r in rs) else None
        out[b] = {"n": len(rs), "n_present": len(present),
                  "code_ok": sum(r["code_ok"] for r in rs),
                  "jev_ok": sum(r["jev_ok"] for r in rs),
                  "jev_ok_no_confirm": sum(r["jev_ok"] and not r["jev_confirm"] for r in rs),
                  "recall10": sum(bool(r["recall10"]) for r in present),
                  "recall30": sum(bool(r["recall30"]) for r in present),
                  "median_code_ms": med("code_ms"), "median_jev_ms": med("jev_ms"),
                  "calls": sum(r["calls"] for r in rs), "tokens": sum(r["tokens"] for r in rs),
                  "errors": sum(1 for r in rs if r.get("error")),
                  "degraded": sum(1 for r in rs if r.get("warnings"))}
        if agent:
            out[b] |= {"agent_ok": sum(r["agent_ok"] for r in rs),
                       "search_recall10": sum(bool(r["search_recall10"]) for r in present),
                       "median_agent_ms": med("agent_ms"),
                       "median_agent_wall_ms": med("agent_wall_ms"),
                       "agent_cost_usd": round(sum(r.get("agent_cost") or 0 for r in rs), 4),
                       "agent_errors": sum(1 for r in rs if r.get("agent_error"))}
    return out


# --- report -------------------------------------------------------------------

def _frac(k: int, n: int) -> str:
    return f"{k:>2}/{n:<2} {100 * k / n:3.0f}%" if n else "   n/a     "


def print_table(summary: dict, mode: str) -> None:
    ms = "median ms (replay)" if mode == "offline" else "median ms"
    agent = "agent_ok" in summary["ALL"]
    head = (f"\n{'bucket':<11} {'n':>3}  {'code-only':<11} {'jev':<11} {'jev no-confirm':<14} "
            f"{'recall@10':<11} {'recall@30':<11} {ms + ' code/jev':<28} {'calls':>5} {'tokens':>7}")
    if agent:
        head += (f"  {'agent':<11} {'search@10':<11} {'agent ms (search+api)':>21} "
                 f"{'agent wall ms':>13}")
    print(head)
    for b, s in summary.items():
        line = (f"{b:<11} {s['n']:>3}  {_frac(s['code_ok'], s['n']):<11} {_frac(s['jev_ok'], s['n']):<11} "
                f"{_frac(s['jev_ok_no_confirm'], s['n']):<14} {_frac(s['recall10'], s['n_present']):<11} "
                f"{_frac(s['recall30'], s['n_present']):<11} "
                f"{str(s['median_code_ms']) + ' / ' + str(s['median_jev_ms']):<28} "
                f"{s['calls']:>5} {s['tokens']:>7,}")
        if agent:
            line += (f"  {_frac(s['agent_ok'], s['n']):<11} "
                     f"{_frac(s['search_recall10'], s['n_present']):<11} "
                     f"{str(s['median_agent_ms']):>21} {str(s['median_agent_wall_ms']):>13}")
        print(line)
    if agent:
        a = summary["ALL"]
        print(f"\nagent total cost ${a['agent_cost_usd']:.4f}"
              + (" (recorded, not spent this run)" if mode == "offline" else "")
              + f", {a['agent_errors']} agent error(s)")


def print_rows(rows: list[dict]) -> None:
    for r in rows:
        mark = lambda ok: "ok" if ok else "--"
        rec = "" if r["absent"] else f" r10={int(r['recall10'])} r30={int(r['recall30'])}"
        agent = f" agent {mark(r['agent_ok'])}" if "agent_ok" in r else ""
        print(f"{r['id']} {r['bucket'][:5]:<5} code {mark(r['code_ok'])} jev {mark(r['jev_ok'])}"
              f"{'?' if r['jev_confirm'] else ' '}{agent} {str(r['path']):<12} calls={r['calls']}{rec} "
              f"{r['phrase'][:48]!r} -> {r['jev_pick'].get('name')!r}"
              + (f"  ERROR {r['error']}" if r.get("error") else "")
              + (f"  AGENT ERROR {r['agent_error']}" if r.get("agent_error") else ""))


def print_disagreements(rows: list[dict]) -> None:
    judges = [j for j in JUDGES if rows and f"{j}_ok" in rows[0]]
    diff = [r for r in rows if len({r[f"{j}_ok"] for j in judges}) > 1]
    print(f"\ndisagreements: {len(diff)}")
    for r in diff:
        right = [j for j in judges if r[f"{j}_ok"]]
        picks = "   ".join(f"{j} [{r[f'{j}_pick'].get('kind')}] {r[f'{j}_pick'].get('name')!r} "
                           f"({r[f'{j}_pick'].get('path')})" for j in judges)
        print(f"  {r['id']} right: {', '.join(right) or 'none'}: {r['phrase']!r}\n      {picks}")


def print_delta(summary: dict, base: dict, semantic: str) -> None:
    print(f"\nvs baseline {base.get('at', '?')} ({base.get('mode', '?')}):")
    if semantic != "unused" and base.get("semantic", "unrecorded") != semantic:
        print(f"  ! baseline search ran with semantic={base.get('semantic', 'unrecorded')}, this run "
              f"with semantic={semantic}: agent and search@10 deltas compare different rankings")
    pct = lambda s, k, n: 100 * s[k] / s[n] if s.get(n) else 0.0
    for b, s in summary.items():
        o = base["summary"].get(b)
        if not o:
            continue
        print(f"  {b:<11} code {pct(s, 'code_ok', 'n') - pct(o, 'code_ok', 'n'):+5.1f}pp  "
              f"jev {pct(s, 'jev_ok', 'n') - pct(o, 'jev_ok', 'n'):+5.1f}pp  "
              f"recall@10 {pct(s, 'recall10', 'n_present') - pct(o, 'recall10', 'n_present'):+5.1f}pp"
              + (f"  agent {pct(s, 'agent_ok', 'n') - pct(o, 'agent_ok', 'n'):+5.1f}pp"
                 if "agent_ok" in s and "agent_ok" in o else ""))
    if "agent_ok" in summary["ALL"] and "agent_ok" not in base["summary"].get("ALL", {}):
        print("  agent: no baseline")


# --- recall only ---------------------------------------------------------------

def semantic_mode(rows: list[dict]) -> str:
    """Did celonis_search rank with embeddings: on, off, or mixed (the layer dropped mid-run)."""
    used = {r["semantic"] for r in rows if "semantic" in r}
    return "mixed" if len(used) > 1 else "on" if used == {True} else "off"


def recall_row(case: dict, index: R.Index) -> dict:
    """Where BM25 and celonis_search put a right entry. Pure and local."""
    phrase, want = case["phrase"], set(case["expect_ids"])
    ranked = [e["id"] for _, e in index.top_scored(phrase, k=len(index.entries))]
    first = next((n for n, i in enumerate(ranked, 1) if i in want), None)
    hit = any(index.by_ref[r]["id"] in want
              for c in R.search(phrase, index, k=10)
              for r in [c["id"]] + [x["id"] for x in c["copies"]])
    return {"id": case["id"], "bucket": case["bucket"], "phrase": phrase, "semantic": index.semantic.ok,
            "bm25_rank": first, "bm25_10": bool(first and first <= 10),
            "bm25_30": bool(first and first <= 30), "search_10": hit}


def run_recall(cases: list[dict], index: R.Index, cases_path: Path) -> None:
    rows = [recall_row(c, index) for c in cases if c["expect"]]
    summary = {}
    print(f"\n{'bucket':<11} {'n_present':>9}  {'bm25@10':<11} {'bm25@30':<11} {'search@10':<11}")
    for b in BUCKETS + ("ALL",):
        rs = [r for r in rows if b == "ALL" or r["bucket"] == b]
        s = summary[b] = {"n_present": len(rs), **{k: sum(r[k] for r in rs)
                                                   for k in ("bm25_10", "bm25_30", "search_10")}}
        print(f"{b:<11} {s['n_present']:>9}  {_frac(s['bm25_10'], len(rs)):<11} "
              f"{_frac(s['bm25_30'], len(rs)):<11} {_frac(s['search_10'], len(rs)):<11}")
    missed = [r for r in rows if not r["search_10"]]
    print(f"\nmissed by search@10: {len(missed)}")
    for r in missed:
        print(f"  {r['id']} {r['bucket']:<10} bm25 rank {r['bm25_rank'] or 'unranked'}: {r['phrase']!r}")
    semantic = semantic_mode(rows)
    print("\nMETRIC " + " ".join(f"{b}_search@10={summary[b]['search_10']}/{summary[b]['n_present']}"
                                 for b in ("paraphrase", "exact", "overlap")) + f" semantic={semantic}")
    BENCH.mkdir(exist_ok=True)
    out = BENCH / f"recall-{cases_path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"mode": "recall", "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "cases": cases_path.name, "index_built": index.built,
                               "semantic": semantic, "summary": summary, "rows": rows}, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {out.relative_to(HERE)}")


# --- entry --------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Offline, rerunnable resolver eval.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--offline", action="store_true")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--build-cases", action="store_true")
    ap.add_argument("--cases", type=Path, default=CASES, help="labelled set (default celonis-eval.json)")
    ap.add_argument("--recall-only", action="store_true",
                    help="BM25 and celonis_search recall only: no judges, no Jev, no network")
    ap.add_argument("--semantic", choices=("auto", "on", "off"), default="auto",
                    help="embedding ranking in celonis_search: auto uses it when a local model "
                         "answers; on fails without one")
    ap.add_argument("--only", default="")
    ap.add_argument("--judges", default="code,jev",
                    help="code and jev always run; add agent for the calling-agent judge")
    ap.add_argument("--agent-model", default="sonnet")
    ap.add_argument("--fresh-agent", action="store_true",
                    help="with --live: ask the agent again instead of reusing its recorded answer")
    args = ap.parse_args()
    live = args.live
    judges = {j.strip() for j in args.judges.split(",") if j.strip()}
    if not {"code", "jev"} <= judges <= set(JUDGES):
        ap.error(f"--judges takes code,jev optionally with agent; got {args.judges!r}")
    if args.recall_only and (live or "agent" in judges):
        ap.error("--recall-only runs no judges and no network")
    if args.fresh_agent and not live:
        ap.error("--fresh-agent needs --live")
    agent = None
    if "agent" in judges:
        agent = (args.agent_model, "fresh" if args.fresh_agent else "live" if live else "offline")
        if live and shutil.which("claude") is None:
            ap.error("the agent judge runs `claude -p`, and no `claude` is on PATH; install "
                     "Claude Code or drop agent from --judges")

    if args.semantic == "off":
        celonis_embed.URL = "off"
    index = R.Index()
    if args.semantic == "on" and index.semantic.rank("semantic probe") is None:
        ap.error(f"--semantic on: {index.semantic.warning or 'no embedding model answered'}")
    if args.build_cases:
        print_case_counts(build_cases(index, args.cases))
        return
    blob = json.loads(args.cases.read_text())
    if blob.get("index_built") != index.built:
        print(f"! labels built against index {blob.get('index_built')}, index is {index.built}; "
              f"rerun --build-cases")
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    cases = [c for c in blob["cases"] if not only or c["id"] in only]
    if args.recall_only:
        celonis_api.base = celonis_api.headers = _no_network
        R.named_lookup = _no_tenant_search
        run_recall(cases, index, args.cases)
        return

    BENCH.mkdir(exist_ok=True)
    cassette = json.loads(CASSETTE.read_text()) if CASSETTE.exists() else {"named_lookup": {}}
    if live:
        R.named_lookup = recording_lookup(cassette["named_lookup"], R.named_lookup)
        client = RecordingJev()
    else:
        celonis_api.base = celonis_api.headers = _no_network
        R.named_lookup = replaying_lookup(cassette["named_lookup"])
        client = ReplayJev()

    try:
        rows = [run_case(c, index, client, agent) for c in cases]
    finally:
        # A crash mid-run keeps the name searches it already paid for.
        if live:
            CASSETTE.write_text(json.dumps(cassette, indent=1, ensure_ascii=False) + "\n")

    name = "live" if live else "offline"
    summary = summarize(rows)
    semantic = semantic_mode(rows) if agent else "unused"
    result = {"mode": name, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "index_built": index.built,
              "cases_built": blob.get("built"), "semantic": semantic, "summary": summary, "rows": rows}
    out = BENCH / f"eval-{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")

    print_rows(rows)
    print_table(summary, name)
    if agent:
        print(f"celonis_search for the agent judge: semantic={semantic}")
    if summary["ALL"]["errors"]:
        print(f"\n! {summary['ALL']['errors']} case(s) errored and are scored wrong; see ERROR above")
    if summary["ALL"]["degraded"]:
        print(f"\n! {summary['ALL']['degraded']} case(s) ran degraded (tenant name search "
              f"unavailable); offline replay cannot reproduce them")
    print_disagreements(rows)
    if BASELINE.exists():
        print_delta(summary, json.loads(BASELINE.read_text()), semantic)
    if args.baseline and summary["ALL"]["degraded"]:
        print("\nnot saved as the baseline: a degraded run is not the path offline replays")
        args.baseline = False
    if args.baseline:
        shutil.copyfile(out, BASELINE)
    print(f"\nwrote {out.relative_to(HERE)}" + (f" and {BASELINE.relative_to(HERE)}" if args.baseline else ""))


if __name__ == "__main__":
    try:
        main()
    except jev.JevError as e:
        raise SystemExit(str(e))
