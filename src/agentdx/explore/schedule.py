"""explore/schedule.py — schedule representation (PRD §15.2).

A **delay schedule** is a compact record of where an executed run deviated from the
scheduler's default choice: `{decision_step: chosen_index}`, at most `k` entries. It is what
a caller passes to `Scheduler(delay_schedule=...)` to reproduce one specific interleaving, and
it is exactly what a minimal reproduction ships (PRD §14.8, §15.2). `decision_step` is
deliberately *not* the `sched_step` a `Turn` is keyed by below — see `DelaySchedule`'s own
docstring for the one-step offset between "when `_choose()` ran" and "what `sched_step` its
turn's events were stamped with", and `Turn.decision_step` for the conversion.

A **turn** is the unit this module reconstructs from an already-executed run's event log: the
`schedule_decision` that started it, plus every event the chosen task emitted before its next
yield or completion. `runtime/scheduler.py::_SchedulerRecorder.write` stamps every event a
task emits during one turn with the *same* `sched_step` (`self._sched._step`, incremented once
per `_choose()` call, not once per event) — so grouping events by `sched_step` recovers turn
boundaries exactly, with no separate bookkeeping of "when did this task last yield".

This module holds the pure representation only — no execution, no reduction policy, no
detector calls. `generate.py` drives execution; `reduce.py` decides which turns are worth
branching on; `dedup.py` and this module's own `signature()` are what makes a schedule never
run twice (§15.5).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from agentdx.events.schema import Event, EventType

_DOCS: Final = "docs/exploration.md"


class ExploreError(RuntimeError):
    """Base class for every error `explore/` raises. Carries an `E-EXPL-NNN` code.

    `code` is set per-instance in `__init__`, never redeclared as a class attribute by a
    subclass — the same pattern `runtime.scheduler.SchedulerError` uses, and for the same
    reason: overriding a writable class attribute with a `Final` one in a subclass is a
    type-checker error (`mypy --strict` catches this; the base class holds the single
    writable declaration and every subclass passes its own code through the constructor).
    """

    code: Final = "E-EXPL-000"

    def __init__(self, detail: str, *, code: str = "E-EXPL-000") -> None:
        """Build the error from a plain-English description of what went wrong."""
        self.code = code  # type: ignore[misc]
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


class MalformedRunError(ExploreError):
    """A run's event log has no usable `schedule_decision` structure to explore from.

    Raised when a log contains zero `schedule_decision` events (nothing to branch on — the
    caller likely handed `explore()` a log from a non-scheduler-backed execution, PRD §15.3's
    precondition) or when two `schedule_decision` events claim the same `sched_step` with a
    different `chosen_task_id` (the event log is not from one coherent, replayable run).
    """

    def __init__(self, detail: str) -> None:
        """Build the error, fixed to code `E-EXPL-001`."""
        super().__init__(detail, code="E-EXPL-001")


# ---------------------------------------------------------------------------------------
# DelaySchedule and Signature (PRD §15.2)
# ---------------------------------------------------------------------------------------

DelaySchedule = Mapping[int, int]
"""`{decision_step: chosen_index}` — deviations from the scheduler's default choice.

`chosen_index` is passed straight through to `Scheduler._choose`, which returns
`runnable[chosen_index % len(runnable)]` (`runtime/scheduler.py`, built for this feature and
otherwise unused until now). The empty mapping is the default schedule. `len(keys) <= k` is a
generation-time invariant (`generate.py`), not enforced by this type itself — a `DelaySchedule`
with more than `k` entries is a valid *value*, just not one `generate.py`'s BFS ever produces
or that a caller should pass to `execute()` inside an exploration run.

**`decision_step` is `Scheduler._step` at the moment `_choose()` was called for that turn —
NOT the `sched_step` stamped on the resulting `schedule_decision` event.** `_scheduler_loop`
calls `_choose(runnable)` while `self._step` still holds the *previous* turn's post-increment
value, only then does `self._step += 1`, and only after that does `_resume_task` stamp the
`schedule_decision` with the now-incremented `self._step`. So the turn whose event carries
`sched_step=N` was decided by the `_choose()` call make when `self._step == N - 1`. This
module's own `Turn.decision_step` property (`= sched_step - 1`) is the only sanctioned way to
turn a reconstructed `Turn` into a `DelaySchedule` key — verified empirically against a live
`Scheduler` in `tests/unit/explore/test_schedule.py::test_decision_step_matches_live_scheduler`,
not merely reasoned about from the source. Using `sched_step` directly here silently controls
the *next* turn instead of the intended one — this was caught and fixed during P13's own build,
not a decision left implicit.
"""


def canonical_delay_schedule_text(delay_schedule: DelaySchedule) -> str:
    """Return the canonical text form of a delay schedule: sorted `[step, index]` pairs.

    Guarantees: two mappings with identical `(step, index)` content produce byte-identical
    text regardless of dict insertion order (AGENTS.md §4.1 — the mapping analogue of
    `agentdx.sorted_set()`). This is the text `signature()` hashes; it is also the exact form
    `minimal_repro`-style tooling would want to print or ship in a scenario file.
    """
    items: list[list[int]] = [[step, index] for step, index in sorted(delay_schedule.items())]
    return json.dumps(items, separators=(",", ":"))


def signature(delay_schedule: DelaySchedule) -> str:
    """Return the PRD §15.2 `Signature`: `blake2b(canonical_json(sorted(items)))`.

    Guarantees: identical delay-schedule content always produces the same signature, on any
    machine, in any process (I1) — this is the sole mechanism `dedup.py` relies on to
    guarantee a schedule is never executed twice (§15.5). Two *different* delay schedules
    collide only in the cryptographic-hash sense (never observed, not treated as a real risk
    at this digest size — see `docs/exploration.md`).
    """
    digest = hashlib.blake2b(
        canonical_delay_schedule_text(delay_schedule).encode("utf-8"), digest_size=16
    ).hexdigest()
    return f"blake2b:{digest}"


# ---------------------------------------------------------------------------------------
# Turn reconstruction (the executed-run side of PRD §15.2's `Schedule`)
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Turn:
    """One reconstructed scheduling decision from an executed run's own event log.

    `sched_step` is PRD §15.2's `Schedule` key. `chosen_task_id`/`ready_task_ids` are read
    directly from the `schedule_decision` event's payload (`runtime/scheduler.py`'s own
    `ready_task_ids` names every OTHER runnable task, not the chosen one — see
    `_resume_task`'s docstring — so `choices_at == len(ready_task_ids) + 1`, not
    `len(ready_task_ids)`). `events` is every event stamped with this `sched_step` other than
    the `schedule_decision` itself, in `seq` order — the observable effect of this one turn.
    """

    sched_step: int
    chosen_task_id: str
    ready_task_ids: tuple[str, ...]
    events: tuple[Event, ...]

    @property
    def choices_at(self) -> int:
        """Return PRD §15.3's `run.choices_at(step)`: how many tasks were runnable here."""
        return len(self.ready_task_ids) + 1

    @property
    def decision_step(self) -> int:
        """Return the `DelaySchedule` key that controls this turn's choice.

        `sched_step - 1` — see `DelaySchedule`'s own docstring for why. This is the only
        value `generate.py` ever writes into a child `DelaySchedule`; never `sched_step`
        itself.
        """
        return self.sched_step - 1

    @property
    def candidate_task_ids(self) -> tuple[str, ...]:
        """Return every runnable task id at this turn, chosen one first, then sorted rest.

        `alt=0` is always the scheduler's own default choice (`runnable[0]`, `_choose`'s
        `policy="priority"`/fallback path) — but delay-schedule indices are taken **modulo**
        the runnable count (`_choose`: `runnable[delay_schedule[step] % len(runnable)]`), so
        `alt=0` does not always mean "the chosen one" once other steps in the same schedule
        have already reordered the runnable list. This property exists for reduction's
        empirical lookup (`reduce.py`), which only needs *the set* of candidates, not their
        index order — index order is `generate.py`'s concern (`range(1, choices_at)`).
        """
        return (self.chosen_task_id, *sorted(self.ready_task_ids))


def turns_from_events(events: Sequence[Event]) -> tuple[Turn, ...]:
    """Reconstruct every turn from a completed run's event log, in `sched_step` order.

    Guarantees: deterministic — grouping is by the stamped `sched_step` field, never by dict
    or set iteration (AGENTS.md §4.1); ties do not occur because `sched_step` is unique per
    turn by construction (`Scheduler._step` increments exactly once per `_choose()` call).

    Raises:
        MalformedRunError: two `schedule_decision` events share a `sched_step` with different
            `chosen_task_id`s, or a `schedule_decision`'s payload is missing a required field.
    """
    decisions: dict[int, Event] = {}
    by_step: dict[int, list[Event]] = {}
    for event in events:
        by_step.setdefault(event.sched_step, []).append(event)
        if event.type is EventType.SCHEDULE_DECISION:
            existing = decisions.get(event.sched_step)
            if existing is not None and existing.payload.get("chosen_task_id") != event.payload.get(
                "chosen_task_id"
            ):
                detail = (
                    f"sched_step {event.sched_step} has two schedule_decision events "
                    f"naming different chosen_task_id values — not one coherent run"
                )
                raise MalformedRunError(detail)
            decisions[event.sched_step] = event

    turns: list[Turn] = []
    for step in sorted(decisions):
        decision = decisions[step]
        chosen = decision.payload.get("chosen_task_id")
        ready = decision.payload.get("ready_task_ids")
        if not isinstance(chosen, str) or not isinstance(ready, list | tuple):
            detail = f"schedule_decision at sched_step {step} has a malformed payload"
            raise MalformedRunError(detail)
        rest = tuple(
            sorted(
                (event for event in by_step[step] if event.type is not EventType.SCHEDULE_DECISION),
                key=lambda event: event.seq,
            )
        )
        turns.append(
            Turn(
                sched_step=step,
                chosen_task_id=chosen,
                ready_task_ids=tuple(str(t) for t in ready),
                events=rest,
            )
        )
    return tuple(turns)


__all__ = [
    "DelaySchedule",
    "ExploreError",
    "MalformedRunError",
    "Turn",
    "canonical_delay_schedule_text",
    "signature",
    "turns_from_events",
]
