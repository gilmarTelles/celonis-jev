# celonis-jev

Resolve plain-language requests to locations in a Celonis tenant. TypeSafe Jev
judges the ambiguous part; Python computes retrieval, thresholds, URLs, and
read-only data access. A browser is used only when the API cannot express the
requested action.

## Security boundary

This repository contains code and documentation only. Tenant indexes, aliases,
column snapshots, crawl maps, evaluation results, credentials, cookies, and
local MCP configuration are intentionally not committed. They are generated
locally and ignored by `.gitignore`.

Credentials stay outside the repository:

- `TYPESAFE_API_KEY` in the environment or `~/.config/typesafe/env.sh`.
- Celonis URL and team key in `~/.celonis/environments.json`.
- Browser session in the caller's signed-in Chrome profile.

The resolver and workbench are read-only with respect to the tenant. The
workbench accepts only one `SELECT` or `WITH` statement.

## Quickstart

```bash
python3 -m pip install -r requirements.txt

# Configure credentials outside this checkout, then build a local index.
python3 celonis_cli.py doctor
python3 celonis_index.py

python3 celonis_cli.py search "the dashboard for monthly credits"
python3 celonis_cli.py open --id <an id from search>
python3 celonis_cli.py resolve "the dashboard for monthly credits"
python3 celonis_cli.py view --no-open
```

`search` needs only the local index: no Jev key, no tenant, no browser.
`resolve` needs no browser after the local index exists. `read` needs a
signed-in Chrome tab with remote debugging enabled; `open` uses one when it
exists and prints the link otherwise. `celonis_index.py` refreshes
the ignored tenant snapshot from the configured APIs.

## Architecture

```text
phrase
  -> optional local vocabulary and column lookup
  -> tenant name search
  -> BM25 shortlist over the local index
  -> one narrow Jev judgment when ambiguity remains
  -> thresholded decision and tenant-relative URL
  -> browser navigation only when required
```

The core modules are:

| file | role |
|---|---|
| `celonis_resolve.py` | retrieval, judgment, thresholds, and URL composition |
| `celonis_types.py` | index entry builders and the `Resolution` schema |
| `celonis_api.py` | external credential loading and read-only HTTP access |
| `celonis_index.py` | builds the local tenant index and column snapshot |
| `celonis_cli.py` | CLI commands for resolving, opening, reading, and diagnostics |
| `celonis_pql.py` | KPI definitions and PQL execution through a signed-in session |
| `workbench.py` | guarded read-only SQL against a configured pool |
| `celonis_view.py` / `celonis_view.html` | loopback-only decision trace |
| `agent.py`, `page.py`, `dom.py`, `browser_nav.py` | generic browser navigation support |
| `celonis_mcp.py` | the MCP server: the one agent interface |

## Agents

MCP is the one agent interface. `.mcp.json` starts `celonis_mcp.py`, which
offers `celonis_search`, `celonis_open`, `celonis_read`, `celonis_resolve` and
`celonis_doctor`. An agent searches, picks a candidate, then opens or reads it
by id. Search and open by id work with no TypeSafe key; only `celonis_resolve`
and a phrase passed to open or read reach Jev, and only when judgment is
needed. Anything that can only run shell commands uses the same surface through
the CLI (`search`, `open --id`, `read --id`).

```bash
python3 celonis_agent_setup.py --check
python3 celonis_agent_setup.py
```

The installer adds the MCP server to omp and Claude using paths on the local
machine. The generated project MCP file is ignored; `.mcp.json` is the portable
repository configuration.

## Development checks

```bash
python3 -m compileall -q .
python3 celonis_cli.py doctor
python3 celonis_eval.py --offline    # resolver accuracy from a recorded run; see AGENTS.md
```

Do not add tenant exports, screenshots, benchmark captures, credentials, or
session material to Git. If a tenant artifact is needed for a local run, keep it
in the ignored paths and regenerate it from the configured environment.
