# AGENTS.md

Working notes for changing this repository.

## What this is

A read-oriented resolver that turns a natural-language request into a Celonis
location, plus a browser agent for actions that cannot be expressed as a URL.
TypeSafe Jev supplies narrow judgments; code supplies retrieval, arithmetic,
thresholds, URL construction, and validation.

## Start here

```bash
python3 celonis_cli.py doctor
python3 celonis_index.py
python3 celonis_cli.py resolve "<request>"
python3 celonis_cli.py view --no-open
```

The local tenant index, aliases, column snapshot, crawl maps, benchmark output,
and project MCP configuration are generated artifacts. They stay outside Git;
`.gitignore` protects their standard paths. A fresh clone must be configured
with a tenant before it can resolve tenant-specific names.

## Ground rules

1. **Code computes, Jev judges.** Never ask Jev to count, add, compare dates, or
   produce text. Keep each judgment narrow; keep thresholds in code.
2. **Never write to the tenant.** No Deploy, package push, KPI create/update, or
   other mutation from this repository. Browser edit mode is out of scope unless
   a caller explicitly needs to inspect it, and changes must not be saved.
3. **No secrets or tenant exports in Git.** Credentials come from
   `~/.celonis/environments.json` and `~/.config/typesafe/env.sh`. Never inline
   keys, cookies, bearer tokens, screenshots, customer data, or tenant exports.
4. **Treat generated data as private.** Keep `celonis-index.json`,
   `celonis-columns.json`, `celonis-aliases.json`, crawl maps, benchmark output,
   and local MCP files ignored. Regenerate them from the configured tenant.
5. **Read before editing.** Reuse existing patterns. Before changing an
   exported symbol, inspect its callers and preserve the public contract.
6. **Fail closed.** Refusals, missing credentials, missing indexes, and
   ambiguous resolutions must be explicit; never guess or silently fall back to
   a different tenant.

## Commands

```bash
python3 celonis_cli.py doctor
python3 celonis_index.py
python3 celonis_cli.py search "<request>"
python3 celonis_cli.py open --id <id>
python3 celonis_cli.py read --id <KPI id>
python3 celonis_cli.py resolve "<request>"
python3 celonis_cli.py open "<request>"
python3 celonis_cli.py view --no-open
python3 celonis_cli.py read "<KPI request>"
python3 celonis_cli.py table "<table>"
python3 celonis_cli.py query "SELECT ..."
python3 celonis_cli.py tables --twins
python3 celonis_cli.py uses "<field or phrase>"
python3 celonis_cli.py columns [text]
python3 celonis_cli.py verify
python3 celonis_cli.py aliases
python3 celonis_map.py --json
```

`read` needs a signed-in Chrome session with remote debugging; `open` drives
that session when it exists and prints the link when it does not. The
workbench is the only path that sends SQL and its guard allows one `SELECT` or
`WITH` statement only. Keep that boundary narrow.

## Agent interface

MCP is the one agent interface. `celonis_mcp.py` is the server and `.mcp.json`
wires it for any host that reads it. Five tools:

| tool | what it does | Jev key |
|---|---|---|
| `celonis_search(phrase, k?)` | candidates with ids, one per kind and name; `copies` lists the other instances by container | no |
| `celonis_open(id \| phrase)` | navigate the signed-in tab; the URL either way | only for a phrase that needs judgment |
| `celonis_read(id \| phrase, pql?)` | run a KPI's PQL, return the rows | only for a phrase that needs judgment |
| `celonis_resolve(phrase)` | one location, confidence, runner-ups with ids | when the phrase is ambiguous |
| `celonis_doctor()` | what is missing and the command that fixes it | no |

The flow is search, pick a candidate, then open or read it by id. An id is an
index handle: a KPI's is `<model id>/<kpi id>` because package copies repeat
the KPI id; every other kind uses its entry id. Search and open by id need
only the local index, not even a tenant config. Read by id needs the tenant
and a signed-in tab.

A harness that can only run shell commands gets the same surface from the CLI:
`search`, `open --id`, `read --id`, `resolve`. Both call the same functions in
`celonis_resolve.py`, `celonis_pql.py` and `celonis_cli.py`; add behaviour
there, not in one entry point.

`python3 celonis_agent_setup.py --check` audits the MCP wiring for omp and
Claude; the installer writes machine-specific configuration to ignored
locations. `.claude/skills/verify-celonis` drives every entry point against the
real tenant and keeps the evidence outside the checkout.

## Evaluation

```bash
python3 celonis_eval.py --build-cases        # labels -> index ids; fails on a stale label
python3 celonis_eval.py --live --baseline    # tenant + Jev; records, scores, keeps a baseline
python3 celonis_eval.py --offline            # replay only: no credentials, no network
python3 celonis_eval.py --only e001,e002
python3 celonis_eval.py --live --judges code,jev,agent   # add the calling agent as a judge
```

`celonis_eval.py` scores the resolver with and without Jev per bucket (exact,
overlap, paraphrase), with recall@10/@30 of the BM25 shortlist, and prints the
delta against `bench/eval-baseline.json`. The labelled set `celonis-eval.json`
names tenant entities, so it is private and ignored: write it per tenant, with
every acceptable (kind, name) pair per case, then rerun `--build-cases` after
each index refresh. A `--live` run records the tenant name search into
`bench/eval-cassette.json` and Jev answers into `cache/`; `--offline` replays
both and reports a miss as an error on that case. Results land in
`bench/eval-<mode>-<timestamp>.json`. The harness drives the resolver from
outside; it never changes resolver behaviour.

`--judges code,jev,agent` adds a third judge: the calling agent picking from
`celonis_search` candidates, as a host LLM sees them over MCP. Each case sends
the request and the candidate list (names, kinds, containers) to Anthropic
through the user's own Claude account via headless `claude -p` (no tools, no
project settings, `--agent-model`, default `sonnet`), so it costs money: about
one cent a case with sonnet (measured: $0.68 for 62 cases). It is opt-in for that reason; the default stays
`code,jev`. Answers are recorded in `bench/agent-cache/`; `--live` reuses them
unless `--fresh-agent` asks again, and `--offline` replays them without ever
starting the CLI. A reply that is not bare JSON (a fenced ```json block
included) scores as a malformed answer. Agent latency is reported as search plus the model turn, and
separately as wall time including CLI startup.

## Verification

There is no permanent test suite. Run the changed command or a focused smoke
scenario (the verify-celonis skill has one recipe per feature), then run
`python3 -m compileall -q .`. Do not commit generated tenant
data merely to make a check pass.
