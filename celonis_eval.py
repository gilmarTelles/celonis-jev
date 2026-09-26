"""How often the resolver lands on the right entry, measured the same way every run.

The labelled set lives in celonis-eval.json (ignored: its phrases name tenant
things). Each case says which (kind, name) pairs are an acceptable answer; the
builder turns those into every index id that carries one, so any package's copy
of an entry counts. A case with no acceptable pair is an ask for something the
tenant does not hold, and the right answer is a refusal.

Each case runs two ways: code alone (`resolve_code_only`) and the full resolver
with Jev (`resolve`). Recall@10/@30 says whether BM25 put a right entry where the
judgment could see it. The resolver is driven from outside and never edited.

  --live      the tenant's name search and Jev over the network; records the name
              search into bench/eval-cassette.json and every Jev answer into cache/
  --offline   (default) replays both; no credentials, no network. A miss is an
              error on that case, never a silent empty answer.

Run:  python3 celonis_eval.py --build-cases        labels -> ids, fails on a stale label
      python3 celonis_eval.py --live --baseline    record, score, keep as the baseline
      python3 celonis_eval.py --offline            replay and compare to the baseline
      python3 celonis_eval.py --only e001,e002
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import time
from pathlib import Path

import celonis_api
import celonis_resolve as R
import jev

HERE = Path(__file__).parent
CASES = HERE / "celonis-eval.json"
BENCH = HERE / "bench"
CASSETTE = BENCH / "eval-cassette.json"
BASELINE = BENCH / "eval-baseline.json"
BUCKETS = ("exact", "overlap", "paraphrase")
DETERMINISTIC = ("alias", "literal", "column")



class ReplayMiss(RuntimeError):
    pass


class CassetteMiss(RuntimeError):
    pass


# --- the labelled set ---------------------------------------------------------

def _pair(kind: str, name: str) -> tuple[str, str]:
    return kind, name.strip().lower()


def build_cases(index: R.Index) -> dict:
    """Rewrite each case's expect_ids from its expect pairs. Fails on any pair no entry has."""
    blob = json.loads(CASES.read_text())
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
    CASES.write_text(json.dumps(blob, indent=1, ensure_ascii=False) + "\n")
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


# --- scoring ------------------------------------------------------------------

def correct(res: R.Resolution | None, case: dict) -> bool:
    if res is None:
        return False
    if not case["expect"]:
        return res.name is None
    # A named answer with no entity id cannot be opened, so it scores wrong.
    return bool(res.entity_id) and res.entity_id in case["expect_ids"]


def pick(res: R.Resolution | None) -> dict:
    if res is None:
        return {}
    return {"name": res.name, "kind": res.kind, "path": res.path, "confirm": res.confirm}


def run_case(case: dict, index: R.Index, client) -> dict:
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
    row |= {"code_ok": correct(code, case), "code_pick": pick(code),
            "jev_ok": correct(hit, case), "jev_pick": pick(hit),
            "jev_confirm": bool(hit and hit.confirm), "path": hit.path if hit else None,
            "calls": hit.calls if hit else 0, "tokens": hit.tokens if hit else 0,
            "deterministic": bool(hit and hit.path in DETERMINISTIC),
            "jev_net_s": round(sum(p.get("latency_s", 0.0) for p in passes), 3),
            "jev_cached": any(p.get("cached") for p in passes),
            "warnings": sorted({w for r in (code, hit) if r for w in r.warnings})}
    return row


def summarize(rows: list[dict]) -> dict:
    out = {}
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
    return out


# --- report -------------------------------------------------------------------

def _frac(k: int, n: int) -> str:
    return f"{k:>2}/{n:<2} {100 * k / n:3.0f}%" if n else "   n/a     "


def print_table(summary: dict, mode: str) -> None:
    ms = "median ms (replay)" if mode == "offline" else "median ms"
    print(f"\n{'bucket':<11} {'n':>3}  {'code-only':<11} {'jev':<11} {'jev no-confirm':<14} "
          f"{'recall@10':<11} {'recall@30':<11} {ms + ' code/jev':<28} {'calls':>5} {'tokens':>7}")
    for b, s in summary.items():
        print(f"{b:<11} {s['n']:>3}  {_frac(s['code_ok'], s['n']):<11} {_frac(s['jev_ok'], s['n']):<11} "
              f"{_frac(s['jev_ok_no_confirm'], s['n']):<14} {_frac(s['recall10'], s['n_present']):<11} "
              f"{_frac(s['recall30'], s['n_present']):<11} "
              f"{str(s['median_code_ms']) + ' / ' + str(s['median_jev_ms']):<28} "
              f"{s['calls']:>5} {s['tokens']:>7,}")


def print_rows(rows: list[dict]) -> None:
    for r in rows:
        mark = lambda ok: "ok" if ok else "--"
        rec = "" if r["absent"] else f" r10={int(r['recall10'])} r30={int(r['recall30'])}"
        print(f"{r['id']} {r['bucket'][:5]:<5} code {mark(r['code_ok'])} jev {mark(r['jev_ok'])}"
              f"{'?' if r['jev_confirm'] else ' '} {str(r['path']):<12} calls={r['calls']}{rec} "
              f"{r['phrase'][:48]!r} -> {r['jev_pick'].get('name')!r}"
              + (f"  ERROR {r['error']}" if r.get("error") else ""))


def print_disagreements(rows: list[dict]) -> None:
    diff = [r for r in rows if r["code_ok"] != r["jev_ok"]]
    print(f"\ndisagreements: {len(diff)}")
    for r in diff:
        side = "code right, jev wrong" if r["code_ok"] else "jev right, code wrong"
        c, j = r["code_pick"], r["jev_pick"]
        print(f"  {r['id']} {side}: {r['phrase']!r}\n"
              f"      code [{c.get('kind')}] {c.get('name')!r} ({c.get('path')})   "
              f"jev [{j.get('kind')}] {j.get('name')!r} ({j.get('path')})")


def print_delta(summary: dict, base: dict) -> None:
    print(f"\nvs baseline {base.get('at', '?')} ({base.get('mode', '?')}):")
    pct = lambda s, k, n: 100 * s[k] / s[n] if s.get(n) else 0.0
    for b, s in summary.items():
        o = base["summary"].get(b)
        if not o:
            continue
        print(f"  {b:<11} code {pct(s, 'code_ok', 'n') - pct(o, 'code_ok', 'n'):+5.1f}pp  "
              f"jev {pct(s, 'jev_ok', 'n') - pct(o, 'jev_ok', 'n'):+5.1f}pp  "
              f"recall@10 {pct(s, 'recall10', 'n_present') - pct(o, 'recall10', 'n_present'):+5.1f}pp")


# --- entry --------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Offline, rerunnable resolver eval.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--offline", action="store_true")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--build-cases", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    live = args.live

    index = R.Index()
    if args.build_cases:
        print_case_counts(build_cases(index))
        return
    blob = json.loads(CASES.read_text())
    if blob.get("index_built") != index.built:
        print(f"! labels built against index {blob.get('index_built')}, index is {index.built}; "
              f"rerun --build-cases")
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    cases = [c for c in blob["cases"] if not only or c["id"] in only]

    BENCH.mkdir(exist_ok=True)
    cassette = json.loads(CASSETTE.read_text()) if CASSETTE.exists() else {"named_lookup": {}}
    if live:
        R.named_lookup = recording_lookup(cassette["named_lookup"], R.named_lookup)
        client = RecordingJev()
    else:
        celonis_api.base = celonis_api.headers = _no_network
        R.named_lookup = replaying_lookup(cassette["named_lookup"])
        client = ReplayJev()

    rows = [run_case(c, index, client) for c in cases]
    if live:
        CASSETTE.write_text(json.dumps(cassette, indent=1, ensure_ascii=False) + "\n")

    name = "live" if live else "offline"
    summary = summarize(rows)
    result = {"mode": name, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "index_built": index.built,
              "cases_built": blob.get("built"), "summary": summary, "rows": rows}
    out = BENCH / f"eval-{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")

    print_rows(rows)
    print_table(summary, name)
    if summary["ALL"]["errors"]:
        print(f"\n! {summary['ALL']['errors']} case(s) errored and are scored wrong; see ERROR above")
    if summary["ALL"]["degraded"]:
        print(f"\n! {summary['ALL']['degraded']} case(s) ran degraded (tenant name search "
              f"unavailable); offline replay cannot reproduce them")
    print_disagreements(rows)
    if BASELINE.exists():
        print_delta(summary, json.loads(BASELINE.read_text()))
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
