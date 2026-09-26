# Search with the embedding server down

Semantic ranking is optional, so a broken Ollama must never break search. When the server is unreachable, hangs, or answers an error, search returns its BM25 candidates, the report's `warnings` says semantic search is paused and why, and stderr carries one `! semantic search paused 5 min` line. A query embed has a 5 s read budget. The first failure trips a breaker that pauses semantic ranking for the rest of the process (5 minutes), so an MCP server pays the timeout once, not on every call.

## Sub-features

- `down-unreach` handles a closed port: the version probe fails at once, and `warnings` holds `semantic search paused (ConnectionError: ...); BM25 only`.
- `down-hang` handles a server that accepts the embed request and never answers: search returns after the 5 s budget with `semantic search paused (ReadTimeout: ...)`.
- `down-500` handles a server whose embed answers HTTP 500: search returns at once with `semantic search paused (HTTPError: 500 ...)`.
- `down-breaker` keeps one MCP process from asking the broken server again: three `celonis_search` calls send one version probe and one embed request in total, and all three replies carry the paused warning.

## How to get to it (user POV)

- Stop Ollama (or point `CELONIS_EMBED_URL` at a dead or broken server) and run `python3 celonis_cli.py search "<phrase>"`.
- An agent host keeps one `celonis_mcp.py` process alive across calls, which is where the breaker matters.

## Driving it with verify-celonis

Preconditions:

- Baseline preconditions from the README hold. No browser is needed. The real Ollama may be up or down. Every step points `CELONIS_EMBED_URL` somewhere else.
- `$V fake-ollama hang` or `$V fake-ollama 500` starts `fake-ollama.py` (stdlib only, shipped beside the helper) on a free loopback port and prints its URL. `/api/version` answers like Ollama, so search gets as far as the query embed. `/api/embed` never answers (`hang`) or answers 500 (`500`). Each request is one line in `<evidence dir>/fake-ollama.log`, which starts empty. A second `fake-ollama` call replaces the first, and `$V cleanup` stops it.

- **Unreachable.** Run `CELONIS_EMBED_URL=http://127.0.0.1:9 $V cli down-unreach -- search --json "order cycle time"`. Exit `0` in about a second. Then run `$V check down-unreach 'r["candidates"] and all(c["score"] > 0 for c in r["candidates"] if not c.get("exact")) and any(w.startswith("semantic search paused (ConnectionError") for w in r["warnings"])'`. `PASS`. `.stderr` holds one `! semantic search paused 5 min (http://127.0.0.1:9, ...)` line.
- **Hung.** Run `U=$($V fake-ollama hang)`, then `CELONIS_EMBED_URL=$U $V cli down-hang -- search --json "order cycle time"`. Exit `0`, and the `.time` file reads about 5 to 6 s (the 5 s budget plus the index load). Then `$V check down-hang 'r["candidates"] and any(w.startswith("semantic search paused (ReadTimeout") for w in r["warnings"])'`. `fake-ollama.log` reads `GET /api/version` then `POST /api/embed`.
- **Breaker in one MCP process.** With the hung server still up, run `: > <evidence dir>/fake-ollama.log`, then `CELONIS_EMBED_URL=$U $V mcp-bare down-breaker celonis_search '{"phrase":"order cycle time"}' celonis_search '{"phrase":"vendor master data"}' celonis_search '{"phrase":"open invoices"}'`. The `.time` file reads about 5 to 6 s, not 15. Then `$V check down-breaker 'len(rs) == 3 and all(x["candidates"] and any("semantic search paused" in w for w in x["warnings"]) for x in rs)'`. `fake-ollama.log` holds exactly two lines, `GET /api/version` and `POST /api/embed`. `.stderr` holds exactly one `! semantic search paused` line.
- **HTTP 500.** Run `U=$($V fake-ollama 500)`, then `CELONIS_EMBED_URL=$U $V cli down-500 -- search --json "order cycle time"`. Exit `0` in under a second. Then `$V check down-500 'r["candidates"] and any(w.startswith("semantic search paused (HTTPError: 500") for w in r["warnings"])'`.
- **Text form.** Run `CELONIS_EMBED_URL=http://127.0.0.1:9 $V cli down-text -- search "order cycle time"`. The first stdout line is `  ! semantic search paused (ConnectionError: ...); BM25 only`, followed by `#<rank> bm25 <score>` candidate lines.
- **Proof.** Every `.state` reads `vocabulary unchanged`. Every `.cmd` begins with the `# env CELONIS_EMBED_URL=...` line it ran with. Run `$V cleanup` and check that it prints `stopped fake-ollama pid <n>`.

## Gotchas

- The breaker lives in process memory. Each CLI call is a new process and pays the failure again: about 5 s for a hung server, nothing noticeable for a dead port or a 500. Prove the breaker with one `$V mcp` call that carries several tool calls, never with repeated CLI calls.
- A hung version probe (`/api/version` itself stalls) costs 2 s, not 5. `fake-ollama.py` answers the probe on purpose, so the steps exercise the query embed's 5 s budget.
- The paused warning truncates the error text at 120 characters, so match on its start (`semantic search paused (ReadTimeout`), not on the whole line.
- These steps never embed the index and never write the cache. `index --embed` against a dead server is the refusal step in [embed-index.md](./embed-index.md).
