"""The `Migration` record, in a leaf module so numbered migrations can import it.

Split out of `migrations/__init__.py` purely to break the import cycle: the package
`__init__` is the runner and must import every numbered migration, so a numbered migration
cannot import the package `__init__` to get the type it is an instance of.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Migration:
    """One numbered, forward-only schema change.

    Guarantees: `statements` are applied in order inside a single transaction, so a
    migration either lands completely or not at all. `major` and `rewrites_events` are
    declarations the runner enforces; they are not documentation.
    """

    version: int
    name: str
    statements: tuple[str, ...]
    major: bool = False
    """True when an older build could not read the result. Requires `agentdx migrate`
    rather than running automatically on open (PRD §27.5)."""

    rewrites_events: bool = False
    """True when the migration must modify existing `events` rows. Forces the runner to
    drop and reinstate the append-only triggers explicitly, in the same transaction."""

    triggers: tuple[str, ...] = field(default=())
    """The append-only trigger DDL this migration is responsible for, if any.

    Held on the migration that creates them so the runner can reinstate a byte-identical
    trigger after a rewriting migration, rather than keeping a second copy that could drift
    from the one actually created.
    """


__all__ = ["Migration"]
