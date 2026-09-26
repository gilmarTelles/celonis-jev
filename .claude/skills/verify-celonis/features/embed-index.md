# Embed the index

`python3 celonis_index.py --embed` (or `python3 celonis_cli.py index --embed`) embeds every index entry the cache lacks with the local Ollama model and writes `cache/embeddings-<model>.json`, one file per model. It saves after every 64-entry batch, so an interrupted run keeps its finished batches and a rerun embeds only what is still missing. A run with nothing missing changes nothing. An unreadable cache (a truncated file) is reported and rebuilt from scratch. Search never embeds. It only reads this file.

## Sub-features

- `embed-fresh` builds the cache from nothing and prints `embeddings: 0/N cached, N to embed ... in B batches`, then one `batch i/B` line per batch.
- `embed-resume` continues after an interruption. The rerun's first line reports the finished batches as cached (`64·k/N cached`), and it runs only the remaining batches.
- `embed-noop` finds nothing missing (`N/N cached, 0 to embed ... in 0 batches`), exits `0` in under a second, and leaves the file byte-identical.
- `embed-repair` sees an unreadable cache, warns `<file> is unreadable (JSONDecodeError)`, and re-embeds every entry into a fresh, readable file.
- `embed-refuse` exits non-zero with one line and no traceback when semantic search is off (`semantic search is off (CELONIS_EMBED_URL=off); nothing to embed`) or no server answers (`no embedding server at <url> (ConnectionError); start Ollama and ...`).

## How to get to it (user POV)

- Run `ollama pull mxbai-embed-large` once, then `python3 celonis_index.py --embed`.
- A normal `python3 celonis_index.py` refresh runs the same step at the end when Ollama answers, and skips it silently when not.

## Driving it with verify-celonis

Preconditions:

- Baseline preconditions from the README hold. No browser is needed. Ollama answers on `localhost:11434` with `mxbai-embed-large` pulled (`curl -s localhost:11434/api/tags` lists it).
- **Never touch the real cache.** Every step below runs with `export CELONIS_EMBED_MODEL=mxbai-embed-large:latest`. Ollama serves the same model under that tag, but `cache_path()` escapes the colon, so the steps write a separate file, `cache/embeddings-mxbai-embed-large_3alatest.json` (call it `$ALT`). Set `REAL=cache/embeddings-mxbai-embed-large.json` and `D=` the evidence dir from `$V start`. Before the first step, run `shasum -a 256 $REAL > $D/embed-real.before` and `cp $REAL $D/embed-real.backup`. `$ALT` must not exist yet. If it does, move it into `$D`.
- A full build of 3,500 entries took 2 to 6 minutes in testing, depending on how busy Ollama is. The whole recipe takes 5 to 12 minutes.

- **Fresh build, interrupted.** Run `$V run embed-cut -- perl -e 'alarm shift; exec @ARGV' 30 python3 celonis_index.py --embed`. Exit `142` (SIGALRM) after about 30 s. `.stderr` starts `embeddings: 0/N cached, N to embed with mxbai-embed-large:latest in B batches -> cache/embeddings-mxbai-embed-large_3alatest.json` and shows a few `batch i/B` lines. `$ALT` exists.
- **Resume.** Run `$V cli embed-resume -- index --embed`. Exit `0`. The first `.stderr` line reads `embeddings: K/N cached` with `K` equal to 64 times the last finished batch of `embed-cut`, `B'` batches equal `ceil((N-K)/64)`, and the last line reads `batch B'/B': N/N embedded`. Nothing before `K` was embedded again.
- **No-op.** Run `shasum -a 256 $ALT`, then `$V cli embed-noop -- index --embed`, then `shasum -a 256 $ALT` again. Exit `0` in under a second, `.stderr` reads `embeddings: N/N cached, 0 to embed ... in 0 batches`, and the two hashes are equal.
- **Search reads it.** Run `$V cli embed-search -- search --json "order cycle time"`, then `$V check embed-search 'not any("semantic" in w for w in r["warnings"])'`. `PASS`: every entry has a vector.
- **Truncated cache.** Put a copy of the real cache in place and cut it: run `cp $REAL $ALT` and `python3 -c 'import os,sys; f=sys.argv[1]; os.truncate(f, os.path.getsize(f)//2)' $ALT`. Run `$V cli embed-trunc-search -- search --json "order cycle time"`. Exit `0` with BM25 candidates. `.stderr` reads `! embeddings-mxbai-embed-large_3alatest.json is unreadable (JSONDecodeError); semantic search skips it until you run ...`. Then `$V check embed-trunc-search 'r["candidates"] and any("has no embeddings" in w for w in r["warnings"])'`.
- **Repair.** Run `$V cli embed-repair -- index --embed`. Exit `0`. `.stderr` starts with the same `unreadable` warning, then `embeddings: 0/N cached, N to embed`, and ends `N/N embedded`. Run `python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["vectors"]))' $ALT`. It prints `N`.
- **Refusals.** Run `CELONIS_EMBED_URL=off $V cli embed-off -- index --embed` and `CELONIS_EMBED_URL=http://127.0.0.1:9 $V cli embed-nosrv -- index --embed`. Each exits `1` with the one line from `embed-refuse` on stderr and no traceback.
- **Restore and proof.** Run `rm -f $ALT` and `shasum -a 256 -c $D/embed-real.before`. It prints `OK`: the real cache was never written. If it fails, restore it with `cp $D/embed-real.backup $REAL` and report the failure. Every `.state` reads `vocabulary unchanged`.

## Gotchas

- `N` counts distinct entry texts, not entries. Copies of one KPI in several packages share a text, so `N` is below the index's entry count.
- `export CELONIS_EMBED_MODEL` lasts for the shell. Unset it before any other feature, or later searches read `$ALT` (or find no cache at all after the restore step).
- The interruption lands mid-batch. That batch is lost and re-embedded on resume. Only whole batches are checkpointed.
- The first batch after Ollama loads the model can take up to a minute (`BATCH_TIMEOUT` read is 120 s). A slow first line is not a hang.
- A normal index refresh (`celonis_index.py` without `--embed`) calls the Celonis API for about 100 s. This recipe never needs it.
