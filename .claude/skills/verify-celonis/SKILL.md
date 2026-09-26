---
name: verify-celonis
description: Drive the celonis-jev resolver the way a user does (the stdio MCP server agents call, the `celonis_cli.py` CLI, and the loopback trace page `celonis view`) and capture proof outside the checkout. Use after changing resolution, CLI output, the workbench guard, the view page, or the MCP tools, or whenever a claim like "resolve now picks X" or "the guard refuses Y" needs evidence against the real tenant.
---

# Verify celonis-jev

celonis-jev turns a plain-language request into a Celonis tenant location. What a user touches:

- **MCP server:** `celonis_mcp.py`, newline-delimited JSON-RPC over stdio (wired by `.mcp.json`). The one agent interface: `celonis_search`, `celonis_open`, `celonis_read`, `celonis_resolve`, `celonis_doctor`.
- **CLI:** `python3 celonis_cli.py <command>` in the repo root, for anything that can only run shell commands. Short-lived; no server.
- **Trace page:** `celonis view` serves `celonis_view.html` plus a JSON API on `127.0.0.1`.

The MCP server and the CLI share their functions but not their entry points. Proving one does not prove the other.

Everything talks to one real tenant (the `sandbox` entry in `~/.celonis/environments.json`) and to TypeSafe Jev. There is no mock tenant. Every path this skill drives is read-only against the tenant. Never run Deploy, package push, KPI create/update, or any other mutation, and never add `--force` to a proof unless the feature file asks for it.

All commands below run from the repo root. The helper is `.claude/skills/verify-celonis/verify-celonis`; shorten it with `V=.claude/skills/verify-celonis/verify-celonis`.

## Launch

There is nothing to keep alive for the CLI. A run is:

```bash
V=.claude/skills/verify-celonis/verify-celonis
$V start          # prints the evidence dir; takes the checkout lock; snapshots the vocabulary files
$V doctor         # must end in "verdict   drivable"
```

One-time setup, only when `doctor` says so:

- `FAIL no index`: run `python3 celonis_index.py`. It makes GET requests only, takes about 100 s, and writes `celonis-index.json` and `celonis-columns.json` (both gitignored). The index is ready when it prints `N entries -> celonis-index.json`.
- `FAIL no TypeSafe key` / `environments.json missing`: stop and ask the user. Credentials live outside the repo; never create or print them.
- `WARN index ... min old` (over a day): rebuild the index before trusting a resolve result.

The trace page runs only when a feature needs it: `$V view-start` picks a free port, starts `celonis_view.py --no-open`, and returns once `GET /api/status` answers. The server runs in its own session, so it outlives the shell call that started it (agent shells kill their process group on return). Only `$V cleanup` stops it. Do not start the page with a plain `python3 celonis_cli.py view &`, because it will die between calls.

**Isolation.** The index, Jev cache (`cache/*.json`), and vocabulary (`celonis-aliases.json`, `cache/aliases-learned.json`) sit at fixed paths inside this checkout. Two runs in one checkout share them, so `start` refuses while another run holds `cache/.verify-celonis.lock`. Do not delete that lock unless the run named in it is truly dead. The debug Chrome on `:9222` is the user's signed-in session, so never drive it from two runs at once.

## Doctor

`$V doctor` wraps `python3 celonis_cli.py doctor`, saves it as `doctor.txt`, and adds a verdict:

- `verdict   drivable`: key, tenant API, and index are fine. Browser-free features are provable.
- `verdict   NOT DRIVABLE` + the failing lines: fix those first (see Launch) or report them.
- `browser   yes|no`: whether `open`, `read`, and the page's Open button can be proven. `no` is normal. Those paths need a Chrome that the **user** has signed in to (`doctor` prints the exact `open -na "Google Chrome" ... --remote-debugging-port=9222` command). Launching it is fine, but signing in is the user's job. Without it, report those entry points as skipped, not verified.

## Drive

- **CLI:** `$V cli <label> -- <celonis_cli args...>`, for example `$V cli resolve-kpi -- resolve --json "show the KPI for order cycle time"`. Prefer `--json` wherever the command offers it and assert on fields. Exit codes are part of the contract: `resolve`/`ask` return `0` on a hit, `2` on no match, and `3` on an ambiguous `ask` (which lists candidates and opens nothing); `query`/`table` return `1` on a refusal or error.
- **Trace page API:** `$V view-get <label> '/api/resolve?q=<url-encoded phrase>'`. Also `/api/status`. `/api/open` navigates Chrome **and** learns the phrase, so use it only for the open feature.
- **Trace page UI (optional, for a visual proof):** open the `view.url` from the run dir in a browser the harness can drive, type into `#q`, submit with `#go` ("Resolve"), and wait until `#go` reads `Resolve` again. The verdict renders in `#out` and the status pills in `#pills`.
- **MCP:** `$V mcp <label> tools/list` or `$V mcp <label> celonis_resolve '{"phrase":"..."}'`. This sends `initialize` plus one call and captures both JSON-RPC replies.
- **MCP with no credentials:** `$V mcp-bare <label> <tool> '<json args>'` is the same call with `TYPESAFE_API_KEY` unset and `HOME` pointed at an empty temp dir, so neither the Jev key nor `~/.celonis/environments.json` is reachable. Use it to prove what works with no key.

Stable handles: CLI subcommands and flags from the docstring at the top of `celonis_cli.py`; the JSON keys of `Resolution` in `celonis_types.py` (`path`, `name`, `kind`, `url`, `confidence`, `confirm`, `alternatives`, `reason`, `calls`); the view routes in `celonis_view.py`; the tool names in `celonis_mcp.py`.

Tenant content (names, counts, which phrase maps where) changes whenever the tenant does. Before asserting on a specific name, get it from a fresh read in the same run (`tables`, `resolve --json`), rather than from a feature file.

## Evidence

The evidence dir is `~/.cache/verify-celonis/runs/<run-id>/` (override the root with `VERIFY_CELONIS_HOME`). It stays **outside the repo** on purpose: the output holds tenant names, IDs, and URLs, which must never reach Git. Each helper call writes `<label>.cmd`, `.stdout`, `.stderr`, `.exit`, and `.state`. `.state` records hashes of the vocabulary files before and after the call and ends in `vocabulary unchanged` or `VOCABULARY CHANGED`. `run.txt` records the git revision, the dirty-file count, and the state before and after cleanup.

Proof standards:

- Drive the real entry point (the CLI command, the HTTP route, the MCP call). Never call `celonis_resolve.resolve()` from a Python one-liner as a stand-in.
- Record the action together with the result: the command, stdout, and exit code, not just a line you liked.
- Check side effects alongside the output. A read-only claim needs `vocabulary unchanged`. A learning claim needs `VOCABULARY CHANGED` plus a second read (`$V cli aliases-after -- aliases`) that shows the phrase.
- For "it refuses" claims, show that nothing happened: the guard message, exit `1`, no `status` line (the SQL never reached the workbench), and an unchanged vocabulary.
- Jev answers are cached in `cache/<hash>.json`. A repeat ask can be served from that cache (`calls` stays the same and `ms` drops). When a change touches the Jev prompt, say whether the proof ran cold or cached.
- The `--no-open` flag and "no browser" paths still reach the tenant API and Jev. They are not offline modes.

## Cleanup

```bash
$V cleanup
```

This stops only the view process this run started (by the PID in `view.pid`), restores `celonis-aliases.json` and `cache/aliases-learned.json` to their state at `start` (or removes them if they did not exist then), releases the lock, and lists the evidence it kept. It never touches the index or the Jev cache, never kills by process name, and never deletes the evidence dir. Run it after every attempt, including failed ones.

## Feature map

`features/README.md` indexes the recipes. When proving a change, drive every entry point that the relevant feature file lists, or report each skipped one with its unmet precondition.
