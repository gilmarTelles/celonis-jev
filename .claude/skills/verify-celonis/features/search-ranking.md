# Search ranking, semantic on and off

`search` ranks index entries with BM25 and, when a local Ollama serves the embedding model and `celonis_index.py --embed` has run, fuses that with an embedding ranking (reciprocal-rank fusion). Semantic ranking lets a paraphrase that shares no word with an entry's name still find it. `CELONIS_EMBED_URL=off` turns it off. Turning it on or off may change which candidates appear and in what order. It must not change the deterministic lead, container ordering, the report's shape, or what an ask with no content words returns.

## Sub-features

- `rank-on` fuses BM25 with the embedding ranking. A candidate only the embedding found carries `score` `0`, and `warnings` holds no semantic line when every entry has a vector.
- `rank-off` is BM25 alone. `warnings` holds `semantic search off (CELONIS_EMBED_URL=off); BM25 only`, and every non-exact candidate has `score` above `0`.
- `rank-exact` keeps a deterministic answer (alias, spelled-out name, named column) first with `exact: true` in both modes.
- `rank-container` keeps the entries of a container the vocabulary names (an alias with a `container`) ahead of the rest in both modes. A container named only by its index name adds a score boost but does not reorder.
- `rank-field` gives each candidate `rank`, its position in the ranking the mode used. Non-exact candidates appear in ascending `rank`. `score` is BM25's and is not the order.
- `rank-partial` warns `semantic search covers N of M entries` when only some entries have a vector.
- `rank-stopwords` answers an ask made only of stopwords (`the`, `show me the`) with no candidates and a `reason`, in both modes. The CLI exits `2`. MCP returns `isError: false`. An empty MCP phrase is still `phrase is required`.

## How to get to it (user POV)

- Run `python3 celonis_cli.py search --json "<phrase>"`, then the same with `CELONIS_EMBED_URL=off` in front.
- MCP `celonis_search` ranks the same way. The server reads `CELONIS_EMBED_URL` at startup.

## Driving it with verify-celonis

Preconditions:

- Baseline preconditions from the README hold. No browser is needed.
- Ollama answers on `localhost:11434` with `mxbai-embed-large` pulled, and the embedding cache covers the index. Check with `$V cli rank-cover -- search --json "order cycle time"` and `$V check rank-cover '"semantic" not in " ".join(r["warnings"])'`. On `FAIL`, run the fresh-build step of [embed-index.md](./embed-index.md) first, or record the run as `rank-partial`.

- **Paraphrase, off.** Pick a paraphrase whose words the index barely uses, so BM25 runs out before ten candidates. `who has access to what in the team` usually works. Run `CELONIS_EMBED_URL=off $V cli rank-para-off -- search --json "<paraphrase>"`. Then run `$V check rank-para-off 'len(r["candidates"]) < 10 and any("CELONIS_EMBED_URL=off" in w for w in r["warnings"]) and all(c["score"] > 0 for c in r["candidates"] if not c.get("exact"))'`. On `FAIL` because of the count, pick rarer words and rerun. Exit `0` with BM25 hits, or `2` with a `reason` when BM25 finds nothing. The `.cmd` file records the `# env CELONIS_EMBED_URL=off` line.
- **Paraphrase, on.** Run `$V cli rank-para-on -- search --json "<the same paraphrase>"`. Exit `0`. Then run `$V check rank-para-on 'len(r["candidates"]) == 10 and any(c["score"] == 0 for c in r["candidates"])'`. `PASS` shows that the embedding filled the list with candidates BM25 never found.
- **Exact lead, both modes.** Run `$V cli rank-exact-on -- search --json "show the KPI for order cycle time"` and `CELONIS_EMBED_URL=off $V cli rank-exact-off -- search --json "show the KPI for order cycle time"`. For each label, `$V check <label> 'r["candidates"][0].get("exact") is True'`. Both `id`s of the first candidate are equal. Check with `$V check rank-exact-off 'r["candidates"][0]["id"] == "<id from rank-exact-on>"'`.
- **Container first, both modes.** This step needs a vocabulary phrase with a container. Take one from `$V cli aliases`, or, when `celonis-aliases.json` is absent, write a temporary one. Take a pool's `id` and `name` from `$V cli rank-pools -- search --json "data pool"` (a candidate with `kind` `pool`), then run `python3 -c 'import json,sys; json.dump({"aliases":[{"phrase":"zebra stripes","container":{"kind":"pool","id":sys.argv[1]},"why":"verify-celonis probe"}]}, open("celonis-aliases.json","w"))' <pool id>`. `$V cleanup` removes the file because it was absent at `start`. Run `$V cli rank-cont-on -- search --json "data jobs in the zebra stripes pool"` and the same with `CELONIS_EMBED_URL=off` as `rank-cont-off`. For each label, run `$V check <label> '(lambda f: f[0] and f == sorted(f, reverse=True))([x["id"] == "<pool id>" or x["container"] == "<pool name>" for x in r["candidates"]])'`. `PASS` means the pool's own entries lead and nothing from another container comes before them.
- **Rank order, both modes.** For `rank-para-on`, `rank-exact-on`, and their `-off` twins, run `$V check <label> '(lambda rk: rk == sorted(rk))([c["rank"] for c in r["candidates"] if not c.get("exact")])'` and `$V check <label> 'all(isinstance(r[k], t) for k, t in (("candidates", list), ("warnings", list), ("next", str)))'`. All `PASS`.
- **Stopwords only, both modes.** Run `$V cli rank-stop-on -- search --json "show me the"` and `CELONIS_EMBED_URL=off $V cli rank-stop-off -- search --json "show me the"`. Both exit `2`. Then `$V check <label> 'r["candidates"] == [] and "no words to search for" in r["reason"]'`. The text form, `$V cli rank-stop-text -- search "the"`, prints that `reason` and exits `2`.
- **Stopwords only, MCP.** Run `$V mcp-bare rank-stop-mcp celonis_search '{"phrase":"show me the"}'`. Then `$V check rank-stop-mcp 'r["candidates"] == [] and "no words to search for" in r["reason"] and replies[-1]["result"]["isError"] is False'`. Run `$V mcp-bare rank-empty-mcp celonis_search '{"phrase":"  "}'`. It is `isError: true` with `phrase is required`.
- **Proof.** Every `.state` reads `vocabulary unchanged`, because the temporary alias was written before the calls, not by them. Every `rank-*-off` `.cmd` begins with `# env CELONIS_EMBED_URL=off`. After `$V cleanup`, `celonis-aliases.json` is gone again (cleanup prints `removed celonis-aliases.json`).

## Gotchas

- `score` is BM25's even when semantic ranking is on, so a `score` of `0` next to a `rank` of `1` is expected. Sort by `rank`, never by `score`.
- The exact candidate keeps its own `rank` from the fused order, which can be any number. That is why the rank-order check skips it.
- `CELONIS_EMBED_URL` is read when `celonis_embed` is imported. Set it on the command, not after the MCP server has started.
- A query embed that fails trips a 5-minute breaker for the process. A CLI call is one process, so each CLI step starts fresh. See [embed-down.md](./embed-down.md).
- Embedding results depend on the model and on the index text. Assert on the shape (an embedding-only candidate exists, the warning is there or not), not on which entry the paraphrase lands.
