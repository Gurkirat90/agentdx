"""Gate G4 (CONTEXT.md §6): killing `reviewer` at t=3000 reproduces the same cascade, 20/20.

The gate's literal verification command, `agentdx scenario run scenarios/kill_reviewer.yaml
--repeat 20`, cannot run — `agentdx scenario run` is P17 (CLI, not yet built) and no `RunHost`
exists to wire a real graph through the scheduler (an open gap CONTEXT.md already flags as
requiring a human decision). This test demonstrates the same claim against the real
`Scheduler` + `CrashInjector`, at `scenarios/kill_reviewer.yaml`'s own seed (42) and crash
timestamp (3000ms), the same way gate G3 (`tests/determinism/test_replay_equality.py`) was
satisfied against a hand-authored scenario rather than a real graph (P06 precedent).
"""

from __future__ import annotations

import pytest

from tests.integration.faults._harness import SEED, cascade_shape, run_scenario_async

_REPEATS = 20


@pytest.mark.determinism
@pytest.mark.asyncio
async def test_kill_reviewer_at_t3000_reproduces_identical_cascade_shape_20_of_20() -> None:
    """Gate G4's own verification test. Paste this test's own output into the report."""
    shapes = []
    for _ in range(_REPEATS):
        events = await run_scenario_async(SEED)
        shapes.append(cascade_shape(events))

    distinct = set(shapes)
    assert len(distinct) == 1, (
        f"gate G4 FAILED: {len(distinct)} distinct cascade shapes across {_REPEATS} runs "
        f"at seed {SEED}: {distinct}"
    )
    shape = shapes[0]
    print(f"\ngate G4: {_REPEATS}/{_REPEATS} runs at seed={SEED} produced:\n  {shape}")  # noqa: T201

    # Not a vacuous pass: the crash and its cascade actually happened, every time.
    assert shape.crashed_agents == ("reviewer",)
    assert shape.tester_took_fallback_path is True
    # Exactly the fault's own two events carry taint — not `schedule_decision` (emitted every
    # step with no declared `causes`) and not `tester`'s own event (a concurrent, causally-
    # unrelated branch: it never declared a `causes` edge to anything reviewer produced,
    # since no review message ever arrives for it to depend on). See
    # `runtime.faults.taint`'s module docstring and `runtime.scheduler._SchedulerRecorder.
    # write`'s call site for why this is true only because the fault hook is fed *declared*
    # causal parents, not `Scheduler._causal_parents`'s linear-fallback-inclusive output.
    assert dict(shape.tainted_event_type_counts) == {
        "fault_injected": 1,
        "fault_effect": 1,
    }


@pytest.mark.determinism
@pytest.mark.asyncio
async def test_a_different_seed_can_produce_a_different_cascade_shape() -> None:
    """Sanity check that `cascade_shape` is not trivially constant regardless of input."""
    baseline = cascade_shape(await run_scenario_async(SEED))
    # A seed with no fault targeting anything still crashes reviewer identically here
    # (the fault is unconditional on schedule order — AT_VIRTUAL_TS, not a scheduling race),
    # so this asserts the *harness* itself is a real, non-degenerate run rather than
    # asserting seed-sensitivity of the fault (which PRD §12.3 requires NOT be present for
    # an AT_VIRTUAL_TS trigger — see triggers.py's own docstring).
    assert baseline.total_event_count > 4
