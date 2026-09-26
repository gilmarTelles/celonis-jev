"""One flag parser for the entrypoints.

`--pool X` and `--pool=X` both work, flags may sit anywhere among the positionals,
and a flag with no value is True. Every command-line entrypoint in the repo uses
this, so adding a flag does not add a parsing idiom.
"""

from __future__ import annotations

import sys
from typing import NoReturn

VALUE_FLAGS = ("--pool", "--job", "--task", "--limit", "--promote", "--why", "--port", "--label", "--id")


def parse_argv(argv: list[str]) -> tuple[list[str], dict]:
    """Split positionals from flags: -> (["table", "orders"], {"--pool": "main"})."""
    rest: list[str] = []
    opts: dict = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key, eq, val = a.partition("=")
            if key in VALUE_FLAGS:
                if not val and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                    val, i = argv[i + 1], i + 1
                opts[key] = val
            else:
                opts[key] = True
        else:
            rest.append(a)
        i += 1
    return rest, opts


def flag(opts: dict, name: str) -> str | None:
    """A value flag's content, or None when absent. Given with no value, a usage error."""
    v = opts.get(name)
    if v is None:
        return None
    if not isinstance(v, str) or not v:
        usage_error(f"{name} needs a value")
    return v


def usage_error(message: str) -> NoReturn:
    """A bad command line: one line on stderr and exit 2, never a traceback."""
    print(message, file=sys.stderr)
    raise SystemExit(2)


def number(opts: dict, name: str, default: int | None) -> int | None:
    """A value flag that must be a positive whole number, or `default` when absent."""
    v = flag(opts, name)
    if v is None:
        return default
    if not v.isdigit() or int(v) < 1:
        usage_error(f"{name} must be a positive whole number, got {v!r}")
    return int(v)
