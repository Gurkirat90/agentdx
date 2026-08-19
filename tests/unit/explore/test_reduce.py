"""Unit tests for `explore/reduce.py` — hand-computed expected outputs (AGENTS.md §5)."""

from __future__ import annotations

from agentdx.events.schema import EventType
from agentdx.explore.reduce import (
    Op,
    independent,
    interesting_steps,
    op_from_event,
    op_sets_independent,
    ops_of,
    reduction_stats,
)
from agentdx.explore.schedule import turns_from_events
from tests.unit.explore._factories import schedule_decision, state_read, state_write, tool_call

# ---------------------------------------------------------------------------------------
# op_from_event / ops_of
# ---------------------------------------------------------------------------------------


def test_op_from_event_state_write_carries_key() -> None:
    event = state_write(seq=0, sched_step=1, agent_id="a", key="k1", value_hash="vh1")
    op = op_from_event(event)
    assert op == Op(kind=EventType.STATE_WRITE, key="k1", lock_id=None)


def test_op_from_event_non_observable_returns_none() -> None:
    event = schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=[])
    assert op_from_event(event) is None


def test_ops_of_drops_non_observable_events() -> None:
    events = (
        schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=[]),
        state_write(seq=1, sched_step=1, agent_id="a", key="k", value_hash="vh1"),
    )
    ops = ops_of(events)
    assert ops == (Op(kind=EventType.STATE_WRITE, key="k", lock_id=None),)


# ---------------------------------------------------------------------------------------
# independent
# ---------------------------------------------------------------------------------------


def test_independent_disjoint_state_keys() -> None:
    a = Op(kind=EventType.STATE_WRITE, key="k1")
    b = Op(kind=EventType.STATE_WRITE, key="k2")
    assert independent(a, b) is True


def test_independent_same_state_key_is_false() -> None:
    a = Op(kind=EventType.STATE_WRITE, key="k1")
    b = Op(kind=EventType.STATE_READ, key="k1")
    assert independent(a, b) is False


def test_independent_same_edge_is_false() -> None:
    a = Op(kind=EventType.MESSAGE_SEND, edge="planner->coder")
    b = Op(kind=EventType.MESSAGE_RECV, edge="planner->coder")
    assert independent(a, b) is False


def test_independent_different_edge_is_true() -> None:
    a = Op(kind=EventType.MESSAGE_SEND, edge="planner->coder")
    b = Op(kind=EventType.MESSAGE_SEND, edge="reviewer->tester")
    assert independent(a, b) is True


def test_independent_same_lock_id_is_false() -> None:
    a = Op(kind=EventType.STATE_WRITE, key="k1", lock_id="L1")
    b = Op(kind=EventType.STATE_WRITE, key="k2", lock_id="L1")
    assert independent(a, b) is False


def test_independent_same_tool_same_args_is_false() -> None:
    a = Op(kind=EventType.TOOL_CALL, tool="search", args_hash="h1")
    b = Op(kind=EventType.TOOL_CALL, tool="search", args_hash="h1")
    assert independent(a, b) is False


def test_independent_same_tool_different_args_is_true() -> None:
    a = Op(kind=EventType.TOOL_CALL, tool="search", args_hash="h1")
    b = Op(kind=EventType.TOOL_CALL, tool="search", args_hash="h2")
    assert independent(a, b) is True


def test_op_sets_independent_vacuously_true_for_empty() -> None:
    assert op_sets_independent((), (Op(kind=EventType.STATE_WRITE, key="k"),)) is True
    assert op_sets_independent((), ()) is True


# ---------------------------------------------------------------------------------------
# interesting_steps — guard 1 and guard 2 (including the fail-open case)
# ---------------------------------------------------------------------------------------


def test_interesting_steps_empty_when_no_turn_has_a_choice() -> None:
    """Guard 1: every turn has `choices_at == 1` — nothing is ever interesting."""
    events = (
        schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=[]),
        schedule_decision(seq=1, sched_step=2, chosen_task_id="a", ready_task_ids=[]),
    )
    turns = turns_from_events(events)
    assert interesting_steps(turns) == ()


def test_interesting_steps_fails_open_when_candidate_never_observed() -> None:
    """A candidate that has never taken an earlier turn: guard 2 cannot rule it out."""
    events = (
        schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=["b"]),
        state_write(seq=1, sched_step=1, agent_id="a", key="k1", value_hash="vh1"),
    )
    turns = turns_from_events(events)
    # `b` has never been observed taking a turn anywhere earlier in this run -> fails open.
    assert interesting_steps(turns) == (1,)


def test_interesting_steps_reduces_when_candidate_observed_independent() -> None:
    """Both candidates have taken an earlier turn, and their ops touch disjoint keys."""
    events = (
        # Turn 1: `a` writes k1 (nothing to compare against yet, choices_at==1: not
        # interesting by guard 1, so this doesn't even reach guard 2).
        schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=[]),
        state_write(seq=1, sched_step=1, agent_id="a", key="k1", value_hash="vh1"),
        # Turn 2: `b` writes k2 (also solo-runnable here).
        schedule_decision(seq=2, sched_step=2, chosen_task_id="b", ready_task_ids=[]),
        state_write(seq=3, sched_step=2, agent_id="b", key="k2", value_hash="vh2"),
        # Turn 3: a two-way choice between `a` and `b`. Both have now taken an earlier
        # turn (k1 vs k2 -- disjoint keys, independent) -> guard 2 proves it uninteresting.
        schedule_decision(seq=4, sched_step=3, chosen_task_id="a", ready_task_ids=["b"]),
        state_write(seq=5, sched_step=3, agent_id="a", key="k1", value_hash="vh3"),
    )
    turns = turns_from_events(events)
    assert interesting_steps(turns) == ()


def test_interesting_steps_stays_interesting_when_same_key_observed() -> None:
    """Same setup, but both candidates' earlier turns touch the *same* key -> interesting."""
    events = (
        schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=[]),
        state_write(seq=1, sched_step=1, agent_id="a", key="shared", value_hash="vh1"),
        schedule_decision(seq=2, sched_step=2, chosen_task_id="b", ready_task_ids=[]),
        state_write(seq=3, sched_step=2, agent_id="b", key="shared", value_hash="vh2"),
        schedule_decision(seq=4, sched_step=3, chosen_task_id="a", ready_task_ids=["b"]),
        state_write(seq=5, sched_step=3, agent_id="a", key="shared", value_hash="vh3"),
    )
    turns = turns_from_events(events)
    assert interesting_steps(turns) == (3,)


def test_interesting_steps_only_looks_strictly_earlier_never_future() -> None:
    """A candidate's *later* turn in the same run must never leak into an earlier lookup."""
    events = (
        # Turn 1: two-way choice a/b. Neither has an earlier turn -> fails open -> interesting.
        schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=["b"]),
        state_write(seq=1, sched_step=1, agent_id="a", key="k1", value_hash="vh1"),
        # Turn 2: `b` runs, writing a *different* key than turn 1's `a` did.
        schedule_decision(seq=2, sched_step=2, chosen_task_id="b", ready_task_ids=[]),
        state_write(seq=3, sched_step=2, agent_id="b", key="k2", value_hash="vh2"),
    )
    turns = turns_from_events(events)
    # Turn 1 must be interesting because of the fail-open (b was never observed *before*
    # turn 1) -- even though b's only turn in the whole run (turn 2, AFTER turn 1) happens
    # to be on a disjoint key. If turn 2's data leaked backwards, turn 1 would wrongly be
    # reduced away.
    assert interesting_steps(turns) == (1,)


# ---------------------------------------------------------------------------------------
# reduction_stats
# ---------------------------------------------------------------------------------------


def test_reduction_stats_hand_computed() -> None:
    events = (
        schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=[]),
        state_write(seq=1, sched_step=1, agent_id="a", key="k1", value_hash="vh1"),
        schedule_decision(seq=2, sched_step=2, chosen_task_id="b", ready_task_ids=[]),
        state_write(seq=3, sched_step=2, agent_id="b", key="k2", value_hash="vh2"),
        schedule_decision(seq=4, sched_step=3, chosen_task_id="a", ready_task_ids=["b"]),
        state_write(seq=5, sched_step=3, agent_id="a", key="k1", value_hash="vh3"),
    )
    turns = turns_from_events(events)
    interesting = interesting_steps(turns)
    stats = reduction_stats(turns, interesting)
    # Only turn 3 has choices_at > 1; it is proven independent (disjoint keys) -> reduced.
    assert stats.total_multi_choice_points == 1
    assert stats.reduced_points == 1
    assert stats.redundancy_fraction == 1.0


def test_reduction_stats_zero_multi_choice_points_is_zero_not_error() -> None:
    events = (schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=[]),)
    turns = turns_from_events(events)
    stats = reduction_stats(turns, ())
    assert stats.total_multi_choice_points == 0
    assert stats.reduced_points == 0
    assert stats.redundancy_fraction == 0.0


def test_op_from_event_lock_and_tool_shapes_for_completeness() -> None:
    """Sanity check the two Op-producing branches not otherwise exercised above."""
    tc = tool_call(seq=0, sched_step=1, agent_id="a", tool="search", args_hash="h1")
    assert op_from_event(tc) == Op(kind=EventType.TOOL_CALL, tool="search", args_hash="h1")
    sr = state_read(seq=1, sched_step=1, agent_id="a", key="k1", value_hash="vh1")
    assert op_from_event(sr) == Op(kind=EventType.STATE_READ, key="k1")
