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

`open` and `read` need a signed-in Chrome session with remote debugging. The
workbench is the only path that sends SQL and its guard allows one `SELECT` or
`WITH` statement only. Keep that boundary narrow.

## Host integrations

`omp/celonis.ts` exposes the CLI as omp tools. `celonis_mcp.py` exposes the same
read-only surface over MCP. `python3 celonis_agent_setup.py --check` audits
local wiring; the installer writes machine-specific configuration to ignored
locations.

## Verification

There is no permanent test suite. Run the changed command or a focused smoke
scenario, then run `python3 -m compileall -q .`. Do not commit generated tenant
data merely to make a check pass.
