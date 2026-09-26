# Ask, open, and read

`ask` and `open` resolve a phrase and take the user there: they navigate the signed-in Chrome tab to the tenant URL, or print the link when no browser is available. A weak pick stops at a confirm step instead of opening. An accepted Jev-judged pick is learned into the vocabulary. `read` resolves to a KPI and runs its PQL through the signed-in session to show rows.

## Sub-features

- `ask-link` prints the absolute tenant link when no CDP browser is running.
- `ask-confirm` refuses to open an ambiguous pick, lists candidates with URLs, and exits `3`.
- `ask-open` navigates the tenant tab on `127.0.0.1:9222`.
- `ask-learn` writes an accepted `ranked` phrase to `cache/aliases-learned.json`. `--no-learn` suppresses this.
- `read-kpi` runs a KPI's PQL and prints columns and rows.
- `read-not-kpi` explains that a non-KPI resolution has no value to run.
- `open-id` opens one `celonis search` candidate by `--id`, or prints its link with no browser.
- `read-id` runs one KPI candidate's PQL by `--id`, fetching the definition from that KPI's own model.

## How to get to it (user POV)

- Run `python3 celonis_cli.py ask "<phrase>"` or `python3 celonis_cli.py open "<phrase>"`.
- Run `python3 celonis_cli.py read "<KPI phrase>"`.
- Run `python3 celonis_cli.py search "<phrase>"`, then `open --id <id>` or `read --id <id>` with a candidate's id.
- Choose Open on the trace page, or call MCP `celonis_open` / `celonis_read`.

## Driving it with verify-celonis

Preconditions:

- Baseline preconditions from the README hold.
- `ask-link` and `ask-confirm` need no browser.
- `ask-open`, `ask-learn`, and `read-kpi` need `browser   yes` from `$V doctor`, meaning a Chrome window on `:9222` that the user signed in to the tenant. Otherwise report them as skipped.

- **Link without a browser.** Ask for a clear KPI with no browser. Run `$V cli ask-link -- ask --no-learn "show the KPI for order cycle time"`. Exit `0`, stdout shows `[kpi] ...` and `no browser to open; link: https://<tenant>/...`.
- **Confirm gate.** Ask a loose phrase. Run `$V cli ask-confirm -- ask --no-learn "open the operations dashboard"`. Exit `3`, stdout reads `ambiguous (top ...)`, lists each candidate with a full URL, and contains no `opened:` line.
- **Open in Chrome.** With a signed-in tab, run `$V cli ask-open -- ask --no-learn "show the KPI for order cycle time"`. Exit `0`, stdout reads `opened: https://...`, and `python3 celonis_cli.py tabs` lists that URL.
- **Learn a phrase.** With a signed-in tab, pick a phrase whose `resolve --json` shows `path: "ranked"` and `confirm: false`, then run `$V cli ask-learn -- ask "<that phrase>"`. Stdout reads `learned: ...`, `.state` reads `VOCABULARY CHANGED`, and `$V cli ask-learn-after -- aliases` lists the phrase as `learned`. `$V cleanup` restores the file.
- **Read a KPI.** With a signed-in tab, run `$V cli read-kpi -- read "show the KPI for order cycle time"`. Exit `0`, and stdout shows `PQL`, `cols`, at least one row, and a `query ...ms` line.
- **Read a non-KPI.** Run `$V cli read-not-kpi -- read "the data jobs in the main pool"`. Exit `0`, and stdout says it resolved to a non-KPI and suggests `celonis open`.
- **Open by id.** Take an `id` from `$V cli open-id-search -- search --json "the operations dashboard"`. Run `$V cli open-id -- open --id <that id>`. Exit `0`, stdout shows `[<kind>] <name>` and either `opened: https://...` or `no browser to open; link: https://...`. An unknown id exits `2` with `unknown id`.
- **Read by id.** Take a `kpi` candidate's id (`<model id>/<kpi id>`) from `search --json`. Run `$V cli read-id -- read --id <that id>`. With `browser   yes`: exit `0` and rows as in `read-kpi`. With `browser   no`: exit `1`, the KPI title and its `PQL` line print, then `no CDP browser to run it in`. A non-KPI id exits `2` with `read needs a KPI` and suggests `open --id`. An unknown id exits `2` with `unknown id` and no `open --id` hint. With no tenant configured (`HOME=$(mktemp -d)`), a KPI id prints one `NoTenant: no tenant configured ...` line on stderr, no traceback, and exits `1`.

## Gotchas

- Without `--no-learn`, an accepted `ranked` pick rewrites `cache/aliases-learned.json` and changes later resolves. Always run `$V cleanup` afterwards.
- `--force` bypasses the confirm gate. Never use it in a proof of the gate.
- `ask-link` still calls the tenant and Jev. "No browser" does not mean offline.
- A tab parked on `id.celonis.cloud` is a login page. `doctor` reports it as FAIL, and `read` fails there.
- Only learnable resolutions are learned (`path` is `ranked`). Literal and alias hits never write the vocabulary, so they cannot prove `ask-learn`.
