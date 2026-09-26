"""The two shapes everything here is made of: an index entry, and a resolution.

entry     one addressable thing in the tenant. Entries round-trip through
          celonis-index.json, so they stay plain dicts at runtime; the builders
          below are the single definition of what each kind holds, and the id
          encoders are the only place its composite ids are spelled. They produce
          exactly the dicts celonis_index.build used to inline.

resolution  what `celonis_resolve.resolve` answers with. One schema every time:
          a field is present whether or not a path set it, so no consumer has to
          guess which shape it got. `to_dict()` is the JSON form (CLI --json, the
          view page, MCP).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

CONTAINER_KINDS = ("pool", "space", "package")


def section_id(pool_id: str, path: str) -> str:
    """A section's id is where it lives: the pool plus the path under it."""
    return f"{pool_id}/{path}"


def table_id(pool_id: str, schema: str, name: str) -> str:
    """A table's id is the triple that says which copy of the name this is."""
    return f"{pool_id}:{schema}:{name}"


# --- entry builders: one per kind the index publishes ------------------------

def space_entry(name: str, entry_id: str) -> dict:
    return {"kind": "space", "name": name, "id": entry_id, "url": "/ui/team/settings"}


def package_entry(name: str, entry_id: str, key: str, space: str,
                  space_id: str | None, url: str) -> dict:
    return {"kind": "package", "name": name, "id": entry_id, "key": key,
            "space": space, "spaceId": space_id, "url": url}


def asset_entry(kind: str, name: str, entry_id: str, key: str, package: str,
                space_id: str | None, package_id: str | None, url: str) -> dict:
    """A studio asset node (a knowledge model, a view, an app, ...)."""
    return {"kind": kind, "name": name, "id": entry_id, "key": key, "package": package,
            "asset": True, "spaceId": space_id, "packageId": package_id, "url": url}


def pool_entry(name: str, entry_id: str, url: str) -> dict:
    return {"kind": "pool", "name": name, "id": entry_id, "url": url}


def section_entry(name: str, entry_id: str, pool: str, pool_id: str, url: str) -> dict:
    """A navigable page of a pool or of the Objects and Events workspace."""
    return {"kind": "section", "name": name, "id": entry_id,
            "pool": pool, "poolId": pool_id, "url": url}


def oem_entry(kind: str, name: str, key: str, entry_id: str | None,
              pool: str, pool_id: str, fields: list[str], url: str) -> dict:
    """An object, event or perspective of the object-centric model."""
    return {"kind": kind, "name": name, "key": key, "id": entry_id,
            "pool": pool, "poolId": pool_id, "fields": fields, "url": url}


def table_entry(name: str, schema: str, pool: str, pool_id: str, url: str) -> dict:
    return {"kind": "table", "name": name, "id": table_id(pool_id, schema, name),
            "schema": schema, "pool": pool, "poolId": pool_id, "url": url}


def datajob_entry(name: str, entry_id: str, pool: str, pool_id: str,
                  status: str, transformations: int, last_run: str, url: str) -> dict:
    return {"kind": "datajob", "name": name, "id": entry_id,
            "pool": pool, "poolId": pool_id, "status": status,
            "transformations": transformations, "lastRun": last_run, "url": url}


def kpi_entry(name: str, entry_id: str, package: str, model: str,
              pql: str, url: str) -> dict:
    return {"kind": "kpi", "name": name, "id": entry_id, "package": package,
            "pool": "", "model": model, "pql": pql, "url": url}


@dataclass
class Resolution:
    """One answer shape for every path that resolves a phrase.

    Paths differ in what they know, not in what they return: a deterministic hit
    carries no `p_none`, a refusal carries no `name`, but the keys are always the
    same ones.
    """

    phrase: str
    path: str                      # alias | literal | column | named_lookup |
                                   # no_candidates | bm25_top1 | ranked | fallback | ...
    calls: int = 0
    name: str | None = None
    kind: str | None = None
    key: str | None = None
    package: str | None = None
    pool: str | None = None
    url: str | None = None
    entity_id: str | None = None
    ref: str | None = None         # the id celonis_open / celonis_read take back
    intent: str = "open"
    exists: float = 1.0
    p_none: float = 0.0
    confidence: float = 0.0
    margin: float | None = None
    confirm: bool = False
    weak: bool = False
    grounded: bool = True
    pinned_to: str | None = None
    reason: str | None = None
    hint: str | None = None
    why: str | None = None
    alternatives: list = field(default_factory=list)
    tokens: int = 0
    cost: float = 0.0
    cached: bool = False
    ms: float = 0.0
    kpi: dict | None = None        # the definition, when the path ran one
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
