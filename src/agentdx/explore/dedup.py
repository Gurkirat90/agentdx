"""explore/dedup.py — duplicate elimination (PRD §15.5).

"A schedule is never executed twice." The mechanism is `schedule.signature()`: two delay
schedules with identical `(decision_step, chosen_index)` content hash identically, so a `set`
of seen signatures is a complete, deterministic duplicate filter. This module is that set, wrapped
just enough to make "did I already enqueue this one" and "did I already execute this one" two
distinct, separately-answerable questions — `generate.py`'s BFS needs both: a schedule can be
*seen* (already in the frontier or already run) well before it is *executed*.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentdx.explore.schedule import DelaySchedule, signature


@dataclass
class SeenSchedules:
    """Tracks every delay-schedule signature `generate.py` has already enqueued or run.

    Guarantees: `mark` is idempotent — marking an already-seen signature is a no-op, not an
    error, since `generate.py`'s BFS calls `already_seen` and `mark` together at the point a
    child is considered, and a duplicate child is exactly the case this class exists to make
    cheap and correct rather than exceptional.
    """

    _signatures: set[str] = field(default_factory=set)

    def already_seen(self, delay_schedule: DelaySchedule) -> bool:
        """Return whether this exact delay schedule has already been marked seen."""
        return signature(delay_schedule) in self._signatures

    def mark(self, delay_schedule: DelaySchedule) -> bool:
        """Record this delay schedule as seen.

        Returns:
            True if this was the first time (the caller should enqueue/execute it), False if
            it was already seen (the caller should discard it as a duplicate).
        """
        sig = signature(delay_schedule)
        if sig in self._signatures:
            return False
        self._signatures.add(sig)
        return True

    def __len__(self) -> int:
        """Return how many distinct signatures have been marked."""
        return len(self._signatures)


__all__ = ["SeenSchedules"]
