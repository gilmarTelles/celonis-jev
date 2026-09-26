# celonis-jev verification map

This directory is the maintained source for verifying what a user of celonis-jev can do. Read this index before driving anything, then use the matching feature file as the recipe.

## Baseline preconditions

- Work from the repo root with `V=.claude/skills/verify-celonis/verify-celonis`.
- `$V start` succeeded and printed an evidence dir under `~/.cache/verify-celonis/runs/`.
- `$V doctor` ends in `verdict   drivable`, and the index is under a day old.
- The `browser` line from `doctor` decides whether browser entry points (`open`, `read`, the page's Open, MCP `celonis_open`/`celonis_read`) can be proven in this run. Their no-browser answers (a printed link, `opened: false`, the no-tab reason) are provable either way.
- Never drive a trace page or Chrome session that this run did not start or that the user did not hand over.

## Driving conventions

- Run CLI actions through `$V cli <label> -- <args>`, page API calls through `$V view-get`, and MCP calls through `$V mcp` (or `$V mcp-bare` for a call with no Jev key and no tenant config). Run anything else a recipe needs from the repo root through `$V run <label> -- <command>`.
- Assert on a captured JSON result with `$V check <label> '<python expression>'`. It prints `PASS` or `FAIL` and appends to `<label>.check`.
- Set `CELONIS_EMBED_URL` or `CELONIS_EMBED_MODEL` in front of `$V`. The helper records them at the top of `<label>.cmd`.
- Use labels shaped `<feature-id>-<entry>`, for example `resolve-json-kpi`, so the evidence names itself.
- Add `--no-learn` to every `ask`/`open` unless the recipe proves learning.
- Take tenant-specific names from a read in the same run. The names in these files were true when written and can drift.
- Every path here is read-only against the tenant. Nothing in this map deploys, pushes, or edits tenant objects.

## Proof and skip reporting

- CLI and MCP proof is the helper's captured `.cmd`, `.stdout`, `.exit`, and `.state`. `.state` must read `vocabulary unchanged` unless the recipe proves learning.
- Page proof is the captured API response. Add a screenshot only when the claim is visual.
- Refusal proof includes the refusal text, the non-zero exit, and evidence that the action never happened.
- Report an unreachable entry point with the command attempted and the unmet precondition (usually `browser   no`). Never report it as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing user-visible behavior, then exactly four H2 sections in this order: `Sub-features`, `How to get to it (user POV)`, `Driving it with verify-celonis`, and `Gotchas`. The driving section starts with `Preconditions:` and pairs each action with an exact command and its observable result.

## Features

- [Resolve a phrase](./resolve.md) covers deterministic hits, Jev-judged hits, refusals, the code-only baseline, and `search` from the CLI.
- [Ask, open, and read](./ask-open-read.md) covers the confirm-before-open gate, browser navigation, phrase learning, KPI reads, and `open --id` / `read --id`.
- [Trace page](./view.md) covers `celonis view`: status, traced resolve, and the page UI.
- [MCP server](./mcp.md) covers the five stdio tools agents call, including search and open/read by id with no key.
- [Data layer](./data-layer.md) covers `tables`, `table`, `query`, and the read-only SQL guard.
- [Search ranking, semantic on and off](./search-ranking.md) covers what `CELONIS_EMBED_URL=off` changes in `search` and what it must not: the exact lead, container order, `rank`, `warnings`, and stopword-only asks.
- [Embed the index](./embed-index.md) covers `celonis_index.py --embed`: a fresh build, resume after an interruption, a no-op rerun, and repair of a truncated cache, all on a separate cache file.
- [Search with the embedding server down](./embed-down.md) covers an unreachable, hung, or failing Ollama, and the breaker that stops one MCP process from asking again.
- [Describe entries](./describe.md) is a placeholder. `--describe` is not on master yet.
