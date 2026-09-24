"""One flag parser for the entrypoints.

`--pool X` and `--pool=X` both work, flags may sit anywhere among the positionals,
and a flag with no value is True. Every command-line entrypoint in the repo uses
this, so adding a flag does not add a parsing idiom.
"""

from __future__ import annotations

VALUE_FLAGS = ("--pool", "--job", "--task", "--limit", "--promote", "--why", "--port", "--label")


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
    """A value flag's content, or None. `True` (a bare `--name`) reads as None."""
    v = opts.get(name)
    return v if isinstance(v, str) and v else None
