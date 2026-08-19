"""ADR-002 / `CONTEXT.md` §5 row 12b: the k=2 exploration-frontier half of gate G2 / I4.

I4, in full: "`research_fanout` yields an empty race-findings set across all 100 determinism
replays **and** the k=2 exploration frontier." `test_research_fanout.py` in this same directory
proves the 100-replay half against the real fixture; this file proves the k=2 half using
`_k2_frontier.py`'s minimal, test-only enumerator (see its docstring for exactly what "k=2"
means here and why it cannot depend on a real, not-yet-built scheduler) over a research-fanout
-shaped synthetic log - the four workers' concurrent, reducer-declared writes to `findings`,
reordered every way the frontier allows.

If any reordering ever produces a finding, that is real evidence of a **new**, order-dependent
false positive the fixed default-order test cannot see - the mission's own third STOP
CONDITION ("research_fanout produces any finding") applies here exactly as it does to the real
fixture, since this synthetic log is that fixture's own documented shape.
"""

from __future__ import annotations

import pytest

from agentdx.analysis.race import detect_conflicts
from agentdx.events.schema import Event
from tests.analysis.race._causal_log import CausalLog
from tests.analysis.race._events import state_read, state_write
from tests.false_positives._k2_frontier import MAX_TRANSPOSITIONS, k2_frontier

_WORKERS: tuple[str, ...] = ("worker_1", "worker_2", "worker_3", "worker_4")


def _build_log_for_schedule(schedule: tuple[str, ...]) -> list[Event]:
    """Build a research-fanout-shaped log with the four workers' writes in `schedule`'s order.

    Every write still carries `reducer="operator.add"` and is still mutually concurrent with
    every other (no causal_parents linking them) - only their relative *seq* order changes
    across schedules, exactly the dimension a real scheduler's interleaving choice would move.
    `synthesiser`'s read comes last, causally after all four (a real fan-in join), so the read
    never becomes part of what is being permuted.

    **Rewritten 2026-08-19 (OP-2 finding, `CONTEXT.md` §13) to build the log through
    `CausalLog`** rather than hand-setting `vclock={}` alongside `causal_parents=()` - see
    `tests/analysis/race/_causal_log.py`'s module docstring for why that combination was never
    a shape the real scheduler produces.
    """
    log = CausalLog()
    for worker in schedule:
        log.add(
            state_write,
            agent_id=worker,
            span_id="s",
            key="findings",
            value=f"{worker}-contribution",
            reducer="operator.add",
        )
    log.add(
        state_read,
        causes=list(range(len(schedule))),
        agent_id="synthesiser",
        span_id="s",
        key="findings",
        value="joined",
    )
    return log.events


@pytest.mark.false_positives
def test_frontier_is_bounded_deterministic_and_includes_the_default_order() -> None:
    frontier = k2_frontier(_WORKERS)
    assert _WORKERS in frontier
    assert len(frontier) == len(set(frontier))  # duplicate-free
    assert frontier == k2_frontier(_WORKERS)  # deterministic - same input, same output
    # every member is a genuine permutation of the four workers, nothing invented or dropped
    assert all(sorted(schedule) == sorted(_WORKERS) for schedule in frontier)
    # bounded: not exploring beyond MAX_TRANSPOSITIONS is what "k=2" means (PRD §15.1)
    assert MAX_TRANSPOSITIONS == 2


@pytest.mark.false_positives
def test_every_schedule_in_the_k2_frontier_yields_zero_findings() -> None:
    """The gate itself: every one of research_fanout's k=2-reachable schedules is clean."""
    frontier = k2_frontier(_WORKERS)
    assert len(frontier) > 1, "frontier collapsed to the default order alone - not exploring"

    offending: list[tuple[str, ...]] = []
    for schedule in frontier:
        events = _build_log_for_schedule(schedule)
        if detect_conflicts(events) != ():
            offending.append(schedule)

    assert offending == ([]), (
        f"gate G2 FAILED: {len(offending)}/{len(frontier)} k=2 schedule(s) produced a finding"
    )
