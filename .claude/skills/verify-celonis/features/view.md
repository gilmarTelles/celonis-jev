# Trace page

`celonis view` serves a loopback-only page that runs the same resolve as the CLI and shows what the terminal hides: the signals, the BM25 candidates, every Jev request and answer, and the threshold behind the decision. The page gets URLs, never credentials.

## Sub-features

- `view-status` reports the index, vocabulary, Jev model and key presence, Chrome tabs, and thresholds at `GET /api/status`.
- `view-resolve` returns `{result, trail}` at `GET /api/resolve?q=` without opening anything.
- `view-bad-request` answers `400` when `q` is missing and `404` for unknown routes.
- `view-ui` renders status pills, demo chips, and a verdict card for a submitted phrase.
- `view-open` navigates Chrome and learns the phrase at `GET /api/open?q=` (needs a browser).

## How to get to it (user POV)

- Run `python3 celonis_cli.py view` (opens the default browser) or `python3 celonis_cli.py view --no-open --port <port>`.
- Type a phrase into the page's text box and choose `Resolve`, or choose a demo chip.

## Driving it with verify-celonis

Preconditions:

- Baseline preconditions from the README hold.
- `$V view-start` printed `view ready  http://127.0.0.1:<port>/`.

- **Status.** Run `$V view-get view-status /api/status`. Stdout ends `http 200`, and the JSON has `index.entries` matching doctor's entry count, `jev.key: true`, and a `thresholds` object.
- **Traced resolve.** Run `$V view-get view-resolve '/api/resolve?q=the%20data%20jobs%20in%20the%20main%20pool'`. Stdout ends `http 200`, `result` has the same keys as `resolve --json`, and `trail` is non-empty.
- **Parity with the CLI.** Run `$V cli view-parity -- resolve --json "the data jobs in the main pool"`. Its `name`, `kind`, and `url` equal `result` from the traced resolve.
- **Missing query.** Run `$V view-get view-noq /api/resolve`. Stdout ends `http 400` with `?q= takes the ask`.
- **Page UI.** Open `$(cat <evidence dir>/view.url)` in a browser the harness can drive, in a new tab. Fill the textbox named `the view that shows tax credits by month` (`#q`) with `the data jobs in the main pool`, then click the button `Resolve` (`#go`). It turns into a disabled `Resolving…` and changes back when done. `#out` then shows the verdict card (name, path chip, `MATCH CONFIDENCE`, and `Confirm first.` with an `Open anyway` button when `confirm` is true), and `#pills` shows the entry count. Take a screenshot and copy it into the evidence dir.
- **Open.** Only with `browser   yes`: run `$V view-get view-open '/api/open?q=show%20the%20KPI%20for%20order%20cycle%20time'`. `open.opened` is the tenant URL.
- **Proof.** Run `$V cleanup` and check it prints `stopped view pid <pid>`. `view.log` and the captured responses remain in the evidence dir.

## Gotchas

- The server binds `127.0.0.1` only. A browser on this machine reaches it; a remote browser cannot.
- `python3 celonis_cli.py view` without `--no-open` launches the system browser. Start it through `$V view-start` instead.
- The index loads on the first request, so the first `/api/*` call is the slow one. `view-start` waits for `/api/status`, which absorbs that delay.
- `/api/open` learns the phrase exactly as the CLI's `open` does. Never hit it in a read-only proof.
- `Open anyway` on the verdict card calls `/api/open`. Never click it in a read-only proof.
- Some browser tools save screenshots under a temp dir whatever `filename` says. Copy from the path the tool prints.
- Request logging is off (`log_message` is silenced), so `view.log` only shows the banner and tracebacks.
