"""Unit tests for `runtime.faults.taint` — PRD §9.4 fault taint propagation.

Every causal graph here is hand-authored and the expected taint map is hand-computed from
PRD §9.4's three rules, not derived from running any fault-class module — this is the
independent check `compute_causal_taint`'s own docstring describes it as.
"""

from __future__ import annotations

from agentdx.events.schema import EventType
from agentdx.runtime.faults.taint import FaultTaintTracker, compute_causal_taint, taint_summary
from tests.unit.events.factories import make_event


def _fault_event(
    event_type: EventType, *, seq: int, fault_id: str, causal_parents: list[int]
) -> object:
    return make_event(
        event_type,
        seq=seq,
        causal_parents=causal_parents,
        payload={
            "fault_id": fault_id,
            "fault_type": "agent_crash",
            "target": "reviewer",
            "params": {},
            "trigger": {"kind": "at_virtual_ts", "value": 1000},
        },
    )


def test_causally_downstream_events_carry_taint_and_unrelated_branch_does_not() -> None:
    # seq0: fault_injected(f_00), no parents.
    # seq1: an ordinary event caused by seq0 (e.g. the crashed agent's own next event) —
    #       inherits f_00 via rule 2.
    # seq2: fault_effect(f_00), itself caused by seq0 — direct taint via rule 1.
    # seq3: an ordinary event caused by seq1 — inherits f_00 transitively via rule 2.
    # seq4: an *independent*, concurrent event with no causal link to the fault at all —
    #       must carry no taint.
    events = [
        _fault_event(EventType.FAULT_INJECTED, seq=0, fault_id="f_00", causal_parents=[]),
        make_event(EventType.TOOL_CALL, seq=1, causal_parents=[0], agent_id="reviewer"),
        _fault_event(EventType.FAULT_EFFECT, seq=2, fault_id="f_00", causal_parents=[0]),
        make_event(EventType.STATE_WRITE, seq=3, causal_parents=[1], agent_id="reviewer"),
        make_event(EventType.STATE_WRITE, seq=4, causal_parents=[], agent_id="planner"),
    ]
    taint = compute_causal_taint(events)  # type: ignore[arg-type]

    assert taint[0] == "f_00"
    assert taint[1] == "f_00"
    assert taint[2] == "f_00"
    assert taint[3] == "f_00"
    assert 4 not in taint  # the concurrent, causally-unrelated branch carries no taint


def test_earliest_injected_fault_wins_when_two_faults_both_reach_an_event() -> None:
    events = [
        _fault_event(EventType.FAULT_INJECTED, seq=0, fault_id="f_00", causal_parents=[]),
        make_event(EventType.TOOL_CALL, seq=1, causal_parents=[0]),
        _fault_event(EventType.FAULT_INJECTED, seq=2, fault_id="f_01", causal_parents=[]),
        # seq3 is downstream of *both* the f_00 branch (via seq1) and the f_01 branch (via
        # seq2) — PRD §9.4: "if multiple faults contribute, the earliest wins". f_00 was
        # injected at seq0, f_01 at seq2, so f_00 (the earlier) must win.
        make_event(EventType.STATE_WRITE, seq=3, causal_parents=[1, 2]),
    ]
    taint = compute_causal_taint(events)  # type: ignore[arg-type]
    assert taint[3] == "f_00"


def test_taint_summary_counts_events_per_fault_id() -> None:
    summary = taint_summary({0: "f_00", 1: "f_00", 2: "f_01"})
    assert summary == {"f_00": 2, "f_01": 1}


# ---------------------------------------------------------------------------------------
# FaultTaintTracker (live, incremental — rules 1 + 2 + 3)
# ---------------------------------------------------------------------------------------


def test_tracker_rule_1_direct_fault_id_takes_precedence_over_everything() -> None:
    tracker = FaultTaintTracker()
    resolved = tracker.resolve(agent_id="reviewer", causal_parents=(), direct_fault_id="f_00")
    assert resolved == "f_00"


def test_tracker_rule_2_inherits_taint_from_a_recorded_causal_parent() -> None:
    tracker = FaultTaintTracker()
    tracker.record(seq=0, fault_id="f_00", is_fault_injected=True)
    resolved = tracker.resolve(agent_id=None, causal_parents=(0,))
    assert resolved == "f_00"


def test_tracker_rule_2_prefers_the_earliest_injected_fault_among_parents() -> None:
    tracker = FaultTaintTracker()
    tracker.record(seq=0, fault_id="f_00", is_fault_injected=True)
    tracker.record(seq=2, fault_id="f_01", is_fault_injected=True)
    tracker.record(seq=1, fault_id="f_00", is_fault_injected=False)
    tracker.record(seq=3, fault_id="f_01", is_fault_injected=False)
    resolved = tracker.resolve(agent_id=None, causal_parents=(1, 3))
    assert resolved == "f_00"  # f_00 injected at seq0, f_01 injected at seq2 — f_00 earlier


def test_tracker_rule_3_uses_agent_context_when_no_causal_taint_reaches() -> None:
    tracker = FaultTaintTracker()
    tracker.mark_agent_tainted("reviewer", "f_00")
    resolved = tracker.resolve(agent_id="reviewer", causal_parents=())
    assert resolved == "f_00"


def test_tracker_rule_3_does_not_apply_to_a_different_agent() -> None:
    tracker = FaultTaintTracker()
    tracker.mark_agent_tainted("reviewer", "f_00")
    resolved = tracker.resolve(agent_id="tester", causal_parents=())
    assert resolved is None


def test_tracker_clear_agent_removes_rule_3_taint() -> None:
    tracker = FaultTaintTracker()
    tracker.mark_agent_tainted("reviewer", "f_00")
    tracker.clear_agent("reviewer")
    resolved = tracker.resolve(agent_id="reviewer", causal_parents=())
    assert resolved is None


def test_tracker_mark_agent_tainted_keeps_the_first_fault_on_repeat_calls() -> None:
    tracker = FaultTaintTracker()
    tracker.mark_agent_tainted("reviewer", "f_00")
    tracker.mark_agent_tainted("reviewer", "f_01")  # should not overwrite
    resolved = tracker.resolve(agent_id="reviewer", causal_parents=())
    assert resolved == "f_00"


def test_tracker_record_with_no_fault_id_does_not_pollute_seq_taint() -> None:
    tracker = FaultTaintTracker()
    tracker.record(seq=0, fault_id=None, is_fault_injected=False)
    resolved = tracker.resolve(agent_id=None, causal_parents=(0,))
    assert resolved is None
