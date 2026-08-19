"""explore/reduce.py — partial-order reduction (PRD §15.4).

v1 ships **independence-based reduction**, not full DPOR (Q-43.2.4, `agentdx.toml`
`[explore] upgrade_reduction_if_redundancy_over`; sleep sets / full DPOR only if measured
redundancy exceeds that threshold — measured, not guessed, see `generate.py`'s
`ReductionStats`). A scheduling point (one `Turn`, `schedule.py`) is *interesting* — worth
branching `generate.py`'s BFS on — unless it can be proven not to matter:

1. Only one task was runnable (`Turn.choices_at == 1`): no choice exists, trivially not
   interesting.
2. Every runnable candidate's next operation is *independent* of every other's (PRD §15.4's
   `independent(op_a, op_b)`, reproduced here verbatim): touching disjoint state keys, not
   sending on the same edge, not contending the same lock or the same tool call.
3. A runnable candidate whose next operation set is empty (pure local computation, no
   observable event) is independent of everything by definition — guard 3 falls out of guard
   2 rather than needing its own code path.

**The empirical gap, stated once here rather than scattered as comments (this is the
soundness caveat PRD §15.4 itself requires be stated, not hidden):** this codebase's only
scheduler-visible yield point around a state/message/lock/tool operation is the LLM-call one
(`sdk/providers/openai_compatible.py`); state/message/lock/tool operations are not individually
preemptible. A "turn" — the observable unit `schedule.py` reconstructs — is therefore
everything one chosen task does between two yield points, which may be several operations, not
one. Guard 2 needs to know a *candidate that was not chosen*'s operation set to compare against
the chosen one's — information no static analysis in this codebase currently exposes (no
`sdk/`/`runtime/` change is in this prompt's `DELIVERABLES`). This module resolves that
honestly rather than guessing: a candidate's operation set is read from **the most recent
other turn in the same executed run where that same task id was the one chosen** — a real,
observed operation set, never invented. If a candidate has never been observed taking a turn
anywhere else in the run, its operation set is unknown and guard 2 **fails open**: the point is
treated as interesting (not reduced away). Soundness beats speed, always — an unproven
"independent" is never assumed; only a proven one is used to reduce.

Consequence, stated plainly: on a graph where every task takes exactly one turn ever (a
single-shot fan-out, `research_fanout`'s own shape), guard 2 has no "elsewhere" to look at and
never fires — every multi-way point stays interesting, and reduction's measured effectiveness
on that shape is genuinely low. That is a true number, not a defect in the guard.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agentdx.events.schema import Event, EventType
from agentdx.explore.schedule import Turn

# EventType kinds PRD §15.4's `independent()` reasons about. `"state"` in the PRD's own
# pseudocode covers both a read and a write on the same key — a write racing a prior read is
# exactly what race detection (P12) cares about, so both STATE_READ and STATE_WRITE map here.
_STATE_KINDS: frozenset[EventType] = frozenset({EventType.STATE_READ, EventType.STATE_WRITE})


@dataclass(frozen=True, slots=True)
class Op:
    """One observable operation.

    Exactly the shape PRD §15.4's `independent()` reasons about: `kind`, `key` (state), `edge`
    (message), `lock_id` (lock or a locked write), `tool` + `args_hash` (tool call).
    """

    kind: EventType
    key: str | None = None
    edge: str | None = None
    lock_id: str | None = None
    tool: str | None = None
    args_hash: str | None = None


def op_from_event(event: Event) -> Op | None:
    """Return the `Op` an observable event represents, or None for a non-observable event.

    Guarantees: only event types PRD §15.4 gives independence rules for produce an `Op` —
    everything else (spans, schedule_decision, run boundaries, assertions, ...) is treated as
    not observable for reduction purposes, matching guard 3's "no observable event" wording.
    """
    payload = event.payload
    if event.type in _STATE_KINDS:
        key = payload.get("key")
        lock_id = payload.get("lock_id") if event.type is EventType.STATE_WRITE else None
        return Op(
            kind=event.type,
            key=str(key) if isinstance(key, str) else None,
            lock_id=str(lock_id) if isinstance(lock_id, str) else None,
        )
    if event.type in (EventType.MESSAGE_SEND, EventType.MESSAGE_RECV):
        edge = payload.get("edge")
        return Op(kind=event.type, edge=str(edge) if isinstance(edge, str) else None)
    if event.type in (EventType.LOCK_ACQUIRE, EventType.LOCK_RELEASE):
        lock_id = payload.get("lock_id")
        return Op(kind=event.type, lock_id=str(lock_id) if isinstance(lock_id, str) else None)
    if event.type is EventType.TOOL_CALL:
        tool = payload.get("tool")
        args_hash = payload.get("args_hash")
        return Op(
            kind=event.type,
            tool=str(tool) if isinstance(tool, str) else None,
            args_hash=str(args_hash) if isinstance(args_hash, str) else None,
        )
    return None


def ops_of(events: Sequence[Event]) -> tuple[Op, ...]:
    """Return every observable `Op` in `events`, in order. Non-observable events are dropped."""
    ops = (op_from_event(event) for event in events)
    return tuple(op for op in ops if op is not None)


def independent(op_a: Op, op_b: Op) -> bool:
    """Return whether two operations cannot affect each other's outcome (PRD §15.4).

    Reproduced from the PRD's own pseudocode, unmodified in substance — only the event-type
    names are this codebase's real `EventType` members rather than the PRD's illustrative
    `"state"`/`"tool"` strings (see the module docstring's `_STATE_KINDS` note).
    """
    if op_a.kind in _STATE_KINDS and op_b.kind in _STATE_KINDS and op_a.key == op_b.key:
        return False
    if op_a.edge is not None and op_a.edge == op_b.edge:
        return False
    if op_a.lock_id is not None and op_a.lock_id == op_b.lock_id:
        return False
    if op_a.tool is not None and op_a.tool == op_b.tool and op_a.args_hash == op_b.args_hash:
        return False
    return True


def op_sets_independent(ops_a: Sequence[Op], ops_b: Sequence[Op]) -> bool:
    """Return whether every op in `ops_a` is independent of every op in `ops_b`.

    Guarantees: an empty sequence is vacuously independent of anything — this is guard 3
    (purely local computation, no observable event) falling out of guard 2, not a special case.
    """
    return all(independent(a, b) for a in ops_a for b in ops_b)


def interesting_steps(turns: Sequence[Turn]) -> tuple[int, ...]:
    """Return the `sched_step` values worth branching `generate.py`'s BFS on.

    A step is interesting unless guard 1 (only one runnable task) or guard 2 (every runnable
    candidate's operation set is *provably* independent of the chosen task's own, via the
    empirical same-task-elsewhere lookup the module docstring describes) rules it out. See the
    module docstring for the full soundness account, including guard 2's honest failure mode.

    Guarantees: deterministic — iterates `turns` in the order given (already `sched_step`
    order, `schedule.turns_from_events`'s own guarantee) and every lookup is a dict keyed by
    task id, never a bare set. The "last observed turn per task" table is built incrementally,
    one turn ahead of the point being evaluated, so it only ever reflects turns strictly
    *before* the current one — never a later turn in the same run.
    """
    last_turn_before_here: dict[str, Turn] = {}
    interesting: list[int] = []
    for turn in turns:
        if turn.choices_at > 1:
            chosen_ops = ops_of(turn.events)
            all_independent = True
            for candidate in turn.ready_task_ids:
                observed = last_turn_before_here.get(candidate)
                if observed is None:
                    # Never observed taking a turn earlier in this run: fails open.
                    all_independent = False
                    break
                if not op_sets_independent(chosen_ops, ops_of(observed.events)):
                    all_independent = False
                    break
            if not all_independent:
                interesting.append(turn.sched_step)
        last_turn_before_here[turn.chosen_task_id] = turn
    return tuple(interesting)


@dataclass(frozen=True, slots=True)
class ReductionStats:
    """What `generate.py` reports for Design Constraint 3: measured, not assumed.

    `redundancy_fraction` is `reduced_points / total_multi_choice_points` — the fraction of
    real branching points guard 2 was able to prove independent, i.e. how much of the
    "redundant exploration" Q-43.2.4 asks about this reduction actually caught. `0.0` when
    there were no multi-choice points at all (nothing to measure, not "perfectly reduced").
    """

    total_multi_choice_points: int
    reduced_points: int

    @property
    def redundancy_fraction(self) -> float:
        """Return the measured fraction of multi-choice points guard 2 proved independent."""
        if self.total_multi_choice_points == 0:
            return 0.0
        return self.reduced_points / self.total_multi_choice_points


def reduction_stats(turns: Sequence[Turn], interesting: Sequence[int]) -> ReductionStats:
    """Return the measured reduction effectiveness for one executed run's turns.

    `interesting` is `interesting_steps(turns)`'s own return value — passed in rather than
    recomputed so a caller that already has it (every real caller does) never pays for the
    same walk twice.
    """
    multi_choice = tuple(turn.sched_step for turn in turns if turn.choices_at > 1)
    # frozenset, not set() — pure membership testing, never iterated (AGENTS.md §4.1).
    interesting_set = frozenset(interesting)
    reduced = tuple(step for step in multi_choice if step not in interesting_set)
    return ReductionStats(total_multi_choice_points=len(multi_choice), reduced_points=len(reduced))


__all__ = [
    "Op",
    "ReductionStats",
    "independent",
    "interesting_steps",
    "op_from_event",
    "op_sets_independent",
    "ops_of",
    "reduction_stats",
]
