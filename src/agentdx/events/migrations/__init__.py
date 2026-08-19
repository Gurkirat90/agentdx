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

**The registry carried its first entry at P09 OP-3 repair (D-45).** `SCHEMA_VERSION` went
1 -> 2 to add `"aborted_guard"` to `run_end.payload.status`'s enum (`events/schema.py`).
The harness existed empty since before the end-of-week-1 freeze specifically so that this
first real migration would slot into an already-designed, already-tested seam rather than
be designed under pressure at the moment it was first needed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final, TypeAlias

from agentdx.events.schema import SCHEMA_VERSION, PayloadValue

EventRecord: TypeAlias = Mapping[str, PayloadValue]  # noqa: UP040  # D-08
Migration: TypeAlias = Callable[[EventRecord], EventRecord]  # noqa: UP040  # D-08


def _migrate_v1_to_v2(record: EventRecord) -> EventRecord:
    """Stamp `schema_version=2`. Purely additive: v2 only widens `run_end.status`'s enum.

    A v1 record was written before `"aborted_guard"` existed as a legal status value, so no
    v1 record can already hold it — there is nothing for this step to translate, only the
    version marker itself to advance. Returns a new mapping; the input is never mutated
    (`migrate`'s own contract).
    """
    return {**record, "schema_version": 2}


MIGRATIONS: Final[Mapping[int, Migration]] = {1: _migrate_v1_to_v2}
"""`n -> migrate_v{n}_to_v{n+1}`. See the module docstring for what v1->v2 changes.

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
