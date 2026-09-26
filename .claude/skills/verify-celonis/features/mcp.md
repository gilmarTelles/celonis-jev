# MCP server

`celonis_mcp.py` is the one agent interface. It exposes five stdio MCP tools: `celonis_search`, `celonis_open`, `celonis_read`, `celonis_resolve`, and `celonis_doctor`. The flow an agent follows is search, pick a candidate, then open or read it by `id`. Search and every by-id call work with no TypeSafe key; only `celonis_resolve` and phrase-based open/read can need Jev. Hosts start the server from `.mcp.json`. Failures come back as `isError: true` tool results, not as crashes.

## Sub-features

- `mcp-init` answers `initialize` with `serverInfo.name` `celonis-jev` and protocol `2024-11-05`.
- `mcp-list` lists exactly the five tools with input schemas.
- `mcp-search-nokey` returns one candidate per (`kind`, `name`) with `id`, `name`, `kind`, `container`, `url`, `score`, `instances`, and `copies` (the other instances as `{id, container}`), with no key and no tenant config.
- `mcp-open-id-nokey` returns the candidate's url with `opened: false` when there is no key, no tenant config, and no Chrome.
- `mcp-read-not-kpi` refuses a non-KPI id with `isError` and the entry's url.
- `mcp-unknown-id` refuses an id the index does not hold with `isError` and a hint to search.
- `mcp-bad-args` validates arguments at the tool boundary: `phrase`, `id`, `pql` are strings (stripped, empty means absent), `k` is an integer from 1 to 50 and not a boolean, search and resolve need `phrase`, open and read need exactly one of `id` or `phrase`. A bad call returns a one-line `isError`; an unexpected exception returns `<Type>: <message>` and its traceback goes to stderr only.
- `mcp-resolve` returns `hit` (with its `id`), `confidence`, `confirm`, `alternatives` (each with an `id`), and `next`.
- `mcp-doctor` returns the doctor lines, with `isError` set when any line is FAIL.
- `mcp-open-ambiguous` refuses to open a weak pick and returns candidates with ids.
- `mcp-read` returns KPI rows, or `isError` explaining why it could not.

## How to get to it (user POV)

- A host that reads `.mcp.json` (Claude Code, Cursor, omp) starts `python3 celonis_mcp.py` and calls the tools.
- Pipe JSON-RPC lines into `python3 celonis_mcp.py` by hand.

## Driving it with verify-celonis

Preconditions:

- Baseline preconditions from the README hold. The `mcp-bare` steps need only the local index: no key, no tenant config, no browser.

- **List tools.** Run `$V mcp mcp-list tools/list`. There are two JSON-RPC replies: `id 1` has `serverInfo.name: "celonis-jev"`, and `id 2` lists `celonis_search`, `celonis_open`, `celonis_read`, `celonis_resolve`, and `celonis_doctor`.
- **Search with no key.** Run `$V mcp-bare mcp-search-nokey celonis_search '{"phrase":"the operations dashboard","k":5}'`. `isError: false`. The result text has at most five `candidates`, each with a non-empty `id`, an integer `instances` of at least 1, and a `copies` list of `{id, container}` holding `min(10, instances - 1)` entries, none equal to the candidate's own `id`, no two candidates sharing a (`kind`, `name`) pair, and `warnings` names the missing tenant config because the urls are tenant-relative. Stderr is empty.
- **Open by id with no key.** Take the first candidate's `id` from the search above. Run `$V mcp-bare mcp-open-id-nokey celonis_open '{"id":"<that id>"}'`. `isError: false`, `opened: false`, `url` equals that candidate's `url`, and `note` says to give the URL to the user. With Chrome up and a tenant configured, the plain `$V mcp` form navigates instead and reads `opened: true`.
- **Read a non-KPI id.** Take a candidate whose `kind` is not `kpi`. Run `$V mcp-bare mcp-read-not-kpi celonis_read '{"id":"<that id>"}'`. `isError: true`, `reason` starts `read needs a KPI`, and `url` is set.
- **Unknown id.** Run `$V mcp-bare mcp-unknown-id celonis_open '{"id":"no-such-id"}'`, then the same with `celonis_read`. Both `isError: true` with `reason` starting `unknown id 'no-such-id'` and naming `celonis_search`.
- **Neither or both.** Run `$V mcp-bare mcp-one-of celonis_open '{}'`. `isError: true` with `pass exactly one of id (from celonis_search) or phrase`.
- **Bad arguments.** Run `$V mcp-bare mcp-bad-id celonis_open '{"id":["x"]}'`, `$V mcp-bare mcp-no-phrase celonis_search '{}'`, and `$V mcp-bare mcp-bad-k celonis_search '{"phrase":"x","k":true}'`. Each is `isError: true` with exactly one short line (`id must be a string`, `phrase is required`, `k must be an integer from 1 to 50`) and no traceback or file path in the result text.
- **Resolve.** Run `$V mcp mcp-resolve celonis_resolve '{"phrase":"show the KPI for order cycle time"}'`. The `id 2` result text is JSON with `hit.kind: "kpi"`, a non-empty `hit.id`, `next: "open_url"`, and `isError: false`.
- **Doctor.** Run `$V mcp mcp-doctor celonis_doctor`. The result text holds the same lines as `doctor.txt`, and `isError` is `true` exactly when those lines contain FAIL (no browser counts as FAIL).
- **Ambiguous open.** Run `$V mcp mcp-open-ambiguous celonis_open '{"phrase":"open the operations dashboard"}'`. The result text has `opened: false`, `ambiguous: true`, a `candidates` list where every entry has an `id`, and `next` says to ask the user and call `celonis_open` with the chosen id.
- **Read without a browser.** With `browser   no`, search for a KPI with `$V mcp mcp-search-kpi celonis_search '{"phrase":"order cycle time KPI"}'`, take a candidate whose `kind` is `kpi` (its id reads `<model id>/<kpi id>`), and run `$V mcp mcp-read-nobrowser celonis_read '{"id":"<that id>"}'`. `isError: true`, `pql` holds the KPI's PQL (so the definition was found), and `reason` reads `no signed-in tab on the sandbox (run `celonis doctor`)`.
- **Proof.** Each `.state` reads `vocabulary unchanged`. The MCP `celonis_open` never learns phrases.

## Gotchas

- The server exits when stdin closes. The helper sends `initialize` and one call per process, so there is no session state to rely on between calls. Ids are index handles, so they stay valid across processes until the index is rebuilt.
- A KPI's id is `<model id>/<kpi id>`: package copies repeat the bare KPI id, and only the pair is unique.
- Search collapses entries by (`kind`, `name`). They are not always copies: a table name repeats across pools with different data, and a KPI name across packages with different PQL. `copies` names each other instance's container and id, so pick the one in the right container. A translated copy has a different name, so it stays its own candidate.
- Tool results nest JSON as a string inside `result.content[0].text`. Parse twice.
- Unknown JSON lines are silently skipped. A malformed request yields no reply, not an error reply.
- A host may launch it with a different working directory than the repo root. The server finds the index next to its own file, so that is fine, but credentials still come from `~/.celonis` and the environment.
