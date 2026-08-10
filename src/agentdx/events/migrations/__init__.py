"""Ordered, forward-only event-schema migrations and the version policy (PRD §9.9).

A migration never rewrites history in place: the event log is append-only and immutable
(I2). Migrations project an old log into the current schema **on read**, leaving the stored
bytes untouched — which is also what keeps a bundle's hash chain verifiable after import,
since the chain covers the bytes as recorded, not as interpreted.

**Policy (PRD §9.9).**

* Writing is always at `SCHEMA_VERSION`. There is no path that writes an old version.
* Reading supports the current version and one previous. Older than that is a hard failure
  naming both versions, not a best-effort guess.
* Adding an optional field or a new event type is a minor change and needs no migration.
  Removing a field, changing a field's meaning, or removing an event type requires a
  version bump *and* a migration registered here.

**This registry is intentionally empty.** `SCHEMA_VERSION` is 1 and no version 0 was ever
written. The harness exists now, before the end-of-week-1 freeze, because the first time it
is needed will be the worst time to design it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final, TypeAlias

from agentdx.events.schema import SCHEMA_VERSION, PayloadValue

EventRecord: TypeAlias = Mapping[str, PayloadValue]  # noqa: UP040  # D-08
Migration: TypeAlias = Callable[[EventRecord], EventRecord]  # noqa: UP040  # D-08

MIGRATIONS: Final[Mapping[int, Migration]] = {}
"""`n -> migrate_v{n}_to_v{n+1}`. Empty by design; see the module docstring.

A migration takes a decoded record and returns a decoded record. It runs *before* the
record becomes an `Event`, because an old record need not satisfy the current dataclass.
"""

OLDEST_READABLE_VERSION: Final = SCHEMA_VERSION - 1 if SCHEMA_VERSION > 1 else SCHEMA_VERSION
"""The lowest `schema_version` this build will migrate on read (current and one previous)."""


class SchemaVersionError(Exception):
    """Raised when a log's `schema_version` cannot be brought to the current version.

    Carries the error code `E-EVENT-060` and names both versions, per PRD §9.9's rule that
    a version failure states what it found and what it wanted.
    """

    def __init__(self, found: int, wanted: int) -> None:
        """Build the exception from the version found and the version required."""
        self.found = found
        self.wanted = wanted
        super().__init__(
            f"[E-EVENT-060] event schema_version {found} cannot be read by this build, "
            f"which supports {OLDEST_READABLE_VERSION}..{wanted} "
            f"(docs/event-schema.md#e-event-060)"
        )


def migrate(record: EventRecord, *, to_version: int = SCHEMA_VERSION) -> EventRecord:
    """Upgrade one decoded event record to `to_version`, applying each step in order.

    Guarantees: a record already at `to_version` is returned unchanged, and the input is
    never mutated. Steps are applied in ascending version order with no gaps; a missing
    step is `SchemaVersionError` rather than a silent skip.

    Raises:
        SchemaVersionError: the record is older than `OLDEST_READABLE_VERSION`, newer than
            this build, or a required migration step is not registered.
    """
    version = record.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise SchemaVersionError(-1, to_version)
    if version > to_version or version < OLDEST_READABLE_VERSION:
        raise SchemaVersionError(version, to_version)

    current: EventRecord = record
    while version < to_version:
        step = MIGRATIONS.get(version)
        if step is None:
            raise SchemaVersionError(version, to_version)
        current = step(current)
        version += 1
    return current


__all__ = [
    "MIGRATIONS",
    "OLDEST_READABLE_VERSION",
    "EventRecord",
    "Migration",
    "SchemaVersionError",
    "migrate",
]
