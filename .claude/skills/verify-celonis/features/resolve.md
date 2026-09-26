# Resolve a phrase

Resolve turns a plain-language request into one tenant location, with a kind, a tenant-relative URL, a confidence, and runner-up candidates. It asks the user to confirm when the pick is weak and refuses when nothing fits. Search is its local, judgment-free sibling: ranked candidates with ids, one per kind and name, each listing its other instances as `copies`. Neither opens a browser or changes the vocabulary.

## Sub-features

- `resolve-literal` answers a phrase that names an entity, with no Jev call (`path` is `literal`, `alias`, or `named_lookup`; `calls` is `0`).
- `resolve-judged` sends an ambiguous phrase to one Jev judgment (`calls` is `1`; `path` is `ranked` or `fallback`).
- `resolve-confirm` flags a weak pick with `confirm: true` and lists `alternatives`.
- `resolve-refuse` returns no name for a phrase that matches nothing, with exit `2`.
- `resolve-baseline` runs the code-only path with `--no-jev` for comparison.
- `resolve-verbose` prints the human-readable trace when `--json` is omitted.
- `resolve-search` lists candidates with ids from the local index alone, leading with the deterministic answer marked `exact` when there is one.
- `search-bad-limit` rejects a `--limit` that is not a positive whole number with one line and exit `2`. `columns --limit` and `view --port` follow the same rule.
- `search-stopwords` answers an ask with no content words with no candidates and a reason. See [search-ranking.md](./search-ranking.md).

## How to get to it (user POV)

- Run `python3 celonis_cli.py resolve "<phrase>"` (verbose trace) or add `--json`.
- Run `python3 celonis_cli.py resolve --no-jev "<phrase>"` for the baseline.
- Run `python3 celonis_cli.py search "<phrase>"` (or `--json`) for candidates with ids.
- The same resolver answers through the trace page and MCP; see those files.

## Driving it with verify-celonis

Preconditions:

- Baseline preconditions from the README hold. No browser is needed.

- **Literal hit.** Ask for a KPI by a name the index holds. Run `$V cli resolve-literal -- resolve --json "show the KPI for order cycle time"`. Exit `0`, `path` is `literal`, `kind` is `kpi`, `calls` is `0`, and `url` starts with `/package-manager/`.
- **Judged hit.** Ask something only a judgment can settle. Run `$V cli resolve-judged -- resolve --json "the data jobs in the main pool"`. Exit `0`, `calls` is `1`, `name` is set, and `alternatives` is non-empty. `path` is `ranked` when the judgment clears the threshold and `fallback` (with `confirm: true`) when it does not. Record which one ran.
- **Confirm flag.** Ask a loose phrase. Run `$V cli resolve-confirm -- resolve --json "open the operations dashboard"`. Exit `0`, `confirm` is `true`, `calls` is `1`, and `alternatives` lists three runners-up.
- **Refusal.** Ask for something absent from the tenant. Run `$V cli resolve-refuse -- resolve --json "xylophone recital for penguins"`. Exit `2`, `name` is `null`, `path` is `no_candidates`, and `reason` is `no match`.
- **Baseline.** Run the code-only path on the judged phrase. Run `$V cli resolve-baseline -- resolve --no-jev "the data jobs in the main pool"`. Exit `0`, the output is a JSON `Resolution`, and `calls` is `0`.
- **Verbose trace.** Run `$V cli resolve-verbose -- resolve "the data jobs in the main pool"`. Exit `0`, and stdout shows the steps before the verdict.
- **Search.** Run `$V cli resolve-search -- search --json "show the KPI for order cycle time"`. Exit `0`; stdout is one JSON object whose `candidates` each carry `id`, `kind`, `name`, `container`, `url`, `rank`, `score`, `instances`, and `copies` (up to ten `{id, container}` for the other instances, best first), and no two share a (`kind`, `name`) pair; `next` names `celonis open --id` and `celonis read --id` (MCP's names the tools). The text form prints the copies' containers on an `also in:` line. When `resolve-literal` above answered deterministically, the first candidate has `exact: true`, `why: "literal"`, and the same `url` as that resolve (search prefixes the tenant base when a tenant is configured, so compare the path). Run it again as `env -u TYPESAFE_API_KEY $V cli resolve-search-nokey -- search --json "show the KPI for order cycle time"` and the output is identical: search never calls Jev.
- **Search with no hit.** Run `CELONIS_EMBED_URL=off $V cli resolve-search-none -- search "xylophone recital for penguins"`. Exit `2`, and stdout says nothing in the index shares a word with it. With semantic ranking on, the embedding always offers its nearest entries, so this step needs it off.
- **Bad `--limit`.** Run `$V cli search-bad-limit -- search --limit abc "order cycle time"` and `$V cli search-zero-limit -- search --limit 0 "order cycle time"`. Each exits `2` with empty stdout and one stderr line, `--limit must be a positive whole number, got 'abc'` (or `'0'`), and no traceback. Also run `$V cli columns-bad-limit -- columns --limit abc --pool x` and `$V cli view-bad-port -- view --port abc --no-open`. Each exits `2` with the matching one-line message and starts nothing. `$V cli search-limit-2 -- search --json --limit 2 "order cycle time"` returns at most two candidates.
- **Proof.** Every `.state` file above reads `vocabulary unchanged`.

## Gotchas

- `resolve` exits `0` even when `confirm` is `true`. Only `ask` turns ambiguity into exit `3`.
- A repeated phrase can be answered from the Jev cache in `cache/`. `calls` still reads `1`, but `ms` drops sharply. Say so when timing matters.
- The example phrases are generic and the picks they land on depend on the tenant. Assert on shape (path, calls, confirm, exit), and take names from this run's output.
- `--json` wraps nothing else. Parse stdout as one JSON object; any other line on stdout is a regression.
