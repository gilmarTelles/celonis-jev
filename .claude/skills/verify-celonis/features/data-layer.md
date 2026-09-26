# Data layer

The data-layer commands show what the tenant's data pools hold without a browser. `tables` lists indexed pool tables and flags double uploads. `table` describes one table's columns, row count, and last load. `query` runs one read statement through a pool's transformation workbench. A guard refuses anything other than a single `SELECT` or `WITH` before it reaches the tenant.

## Sub-features

- `tables-list` lists indexed tables, optionally filtered by text.
- `tables-twins` lists tables that sit beside their own double upload (`X` and `X_X`).
- `table-describe` prints the workbench location, row count, last load, and columns for one table.
- `table-ambiguous-pool` refuses a `--pool` name that matches more than one pool.
- `query-select` runs one `SELECT` and prints status, columns, and rows.
- `query-guard` refuses writes and multi-statement SQL with exit `1` and no workbench call.

## How to get to it (user POV)

- Run `python3 celonis_cli.py tables [text]` or `python3 celonis_cli.py tables --twins`.
- Run `python3 celonis_cli.py table <name> --pool <pool name|id|phrase>`.
- Run `python3 celonis_cli.py query "<SQL>" --pool <pool>`.

## Driving it with verify-celonis

Preconditions:

- Baseline preconditions from the README hold. No browser is needed.
- Get a pool ID from this run: `$V cli tables-list -- tables T001` prints the pools that hold `T001`, and `resolve --json "the data jobs in the main pool"` returns a URL containing a pool ID.

- **Twins.** Run `$V cli tables-twins -- tables --twins`. Exit `0`, and the first line reads `N table(s) sitting next to their own double upload`, followed by one `pool kept + extra` line per pair.
- **Ambiguous pool.** Run `$V cli table-ambiguous-pool -- table T001 --pool "<pool name>"`, where `<pool name>` is a pool name that prefixes another pool's name (take both from `tables` output in this run). Exit `1`, and the output reads `'<pool name>' matches [...]` with at least two pool names.
- **Describe.** Run `$V cli table-describe -- table T001 --pool <pool id>`. Exit `0`, and the output shows `T001  in <pool>  [<job> / <task>]`, a `rows N   last load <timestamp>` line, and `N columns: MANDT, BUKRS, ...`.
- **Select.** Run `$V cli query-select -- query "SELECT COUNT(*) AS n FROM T001" --pool <pool id>`. Exit `0`, `status SUCCESS`, `cols n`, and one row whose count equals `rows` from describe.
- **Guard.** Run `$V cli query-guard-delete -- query "DELETE FROM T001" --pool <pool id>`, then `$V cli query-guard-multi -- query "SELECT 1; SELECT 2" --pool <pool id>`. Both exit `1` and print no `status` line, which shows the SQL never reached the workbench. The delete prints `read-only: delete is not a query`; the pair prints `one statement at a time`.
- **Proof.** Every `.state` reads `vocabulary unchanged`. Rerun describe after the guard cases and confirm that `rows` is unchanged.

## Gotchas

- `--pool` takes a name, an ID, or a vocabulary phrase. Names that prefix other names (`<pool>` vs `<pool> Testing`) fail closed, so prefer the ID.
- `table`, `query`, and `columns` each take several seconds (a workbench execution plus polling), and a stuck workbench task times out after 45 s.
- `columns` writes `cache/table-columns.json`, a local cache and not a tenant write. It is fine to run, but it is slow across a whole pool, so use `--limit`.
- The workbench is the tenant's own execution surface. Keep proofs to `SELECT`/`WITH`; the guard is the boundary under test, not something to probe with real writes elsewhere.
