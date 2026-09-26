# Describe entries (placeholder, not on master)

`celonis_index.py --describe` is not on master. It lives in open PR #5 and is being reworked there, so its flags, file names, and output are not settled. This file reserves the feature and names what its recipe must prove once the PR merges. Until then there is nothing to drive. Report it as `not on this revision`, never as verified or failed.

## Sub-features

- `describe-budget` stops at a spending cap and says how far it got.
- `describe-no-claude` fails fast with one line and no traceback when the `claude` CLI is not on `PATH`.
- `describe-resume` continues after an interruption without describing an entry twice.
- `describe-no-spend` proves every step above without a paid model call.

## How to get to it (user POV)

- Not available on master. Take the command and flags from the merged PR's docstring in `celonis_index.py`, not from this file.

## Driving it with verify-celonis

Preconditions:

- `grep -n -- '--describe' celonis_index.py` prints a match. If it prints nothing, stop and report `describe: not on this revision`.

- **Write the recipe when the PR merges.** Replace this file with the four-section recipe, following the style of [embed-index.md](./embed-index.md): an interrupted run, a resume, and a no-op rerun, each against a separate cache file so the real one is never written.
- **Never spend in verification.** Put a fake `claude` first on `PATH` for every step, for example a script in the evidence dir that prints a fixed description and appends each call to a log. Count the calls from that log. The budget and resume claims rest on those counts. Proving `describe-no-claude` means running with a `PATH` that has no `claude` at all.

## Gotchas

- Do not invent flags from this file. It deliberately names none.
- A real `claude` on `PATH` spends money. Check `command -v claude` inside the step's environment before any run.
