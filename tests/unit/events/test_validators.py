"""Every validation rule, with a passing case and a failing case asserting the exact code.

Error codes are a public contract (analysers, CI gates and bundle import branch on them),
so each failing case pins the code rather than merely asserting that *something* failed.
A test that only checks "it raised" would let a renumbering pass silently.
"""

from __future__ import annotations

import dataclasses

import pytest

from agentdx.events.canonical import canonical_bytes
from agentdx.events.schema import Event, EventType
from agentdx.events.validators import (
    EventValidationError,
    check_cross_event,
    check_semantic,
    check_structural,
    validate_event,
    validate_log,
)
from tests.unit.events import factories


def codes(errors: tuple[object, ...]) -> set[str]:
    """Return the set of error codes in a validation result."""
    return {getattr(e, "code", "") for e in errors}


class TestStructural:
    """Layer (a): types, required fields, closed enum, floats."""

    def test_a_valid_event_of_every_type_passes(self) -> None:
        """A valid event of every type passes."""
        for event_type in EventType:
            assert check_structural(factories.make_event(event_type)) == ()

    def test_missing_required_payload_field_is_e_event_001(self) -> None:
        """Missing required payload field is e event 001."""
        event = factories.make_event(EventType.STATE_WRITE)
        payload = {k: v for k, v in event.payload.items() if k != "key"}
        broken = dataclasses.replace(event, payload=payload)
        assert "E-EVENT-001" in codes(check_structural(broken))

    def test_unknown_event_type_is_e_event_002(self) -> None:
        """The enum is closed: an unknown type is an error, never a pass-through."""
        broken = dataclasses.replace(factories.make_event(), type="totally_new_type")
        assert codes(check_structural(broken)) == {"E-EVENT-002"}

    def test_wrong_field_type_is_e_event_003(self) -> None:
        """Wrong field type is e event 003."""
        broken = dataclasses.replace(factories.make_event(), seq="0")
        assert "E-EVENT-003" in codes(check_structural(broken))

    def test_bool_does_not_satisfy_an_int_field(self) -> None:
        """`bool` is an `int` subclass in Python; the contract does not accept it as one."""
        broken = dataclasses.replace(factories.make_event(), virtual_ts_ms=True)
        assert "E-EVENT-003" in codes(check_structural(broken))

    def test_null_in_a_non_nullable_field_is_e_event_004(self) -> None:
        """Null in a non nullable field is e event 004."""
        broken = dataclasses.replace(factories.make_event(), vclock=None)
        assert codes(check_structural(broken)) & {"E-EVENT-001", "E-EVENT-004"}

    def test_value_outside_a_closed_enum_is_e_event_005(self) -> None:
        """Value outside a closed enum is e event 005."""
        event = factories.make_event(EventType.SPAN_END)
        broken = dataclasses.replace(event, payload={**event.payload, "status": "fine"})
        assert "E-EVENT-005" in codes(check_structural(broken))

    def test_unknown_payload_field_is_e_event_006(self) -> None:
        """Unknown payload field is e event 006."""
        event = factories.make_event(EventType.STATE_READ)
        broken = dataclasses.replace(event, payload={**event.payload, "surprise": 1})
        assert "E-EVENT-006" in codes(check_structural(broken))

    def test_wrong_schema_version_is_e_event_008(self) -> None:
        """Wrong schema version is e event 008."""
        broken = dataclasses.replace(factories.make_event(), schema_version=99)
        assert "E-EVENT-008" in codes(check_structural(broken))

    def test_non_object_payload_is_e_event_012(self) -> None:
        """Non object payload is e event 012."""
        broken = dataclasses.replace(factories.make_event(), payload=[1, 2])
        assert "E-EVENT-012" in codes(check_structural(broken))

    def test_float_in_payload_is_e_event_013(self) -> None:
        """Ruling R4. PRD §12.2's P1 `agent_slow.factor` will need a per-mille integer."""
        event = factories.make_event(EventType.FAULT_INJECTED)
        broken = dataclasses.replace(event, payload={**event.payload, "params": {"factor": 1.5}})
        assert "E-EVENT-013" in codes(check_structural(broken))

    def test_nested_float_is_found(self) -> None:
        """Nested float is found."""
        event = factories.make_event(EventType.FAULT_INJECTED)
        broken = dataclasses.replace(
            event, payload={**event.payload, "params": {"a": {"b": [0.25]}}}
        )
        assert "E-EVENT-013" in codes(check_structural(broken))


class TestSemantic:
    """Layer (b): ordering, scope and taint, one event against its predecessor."""

    def test_a_valid_pair_passes(self) -> None:
        """A valid pair passes."""
        first = factories.make_event(EventType.RUN_START, seq=0, vclock={})
        second = factories.make_event(EventType.STATE_READ, seq=1, causal_parents=[0])
        assert check_semantic(second, first) == ()

    def test_causal_parent_not_less_than_seq_is_e_event_020(self) -> None:
        """Causal parent not less than seq is e event 020."""
        first = factories.make_event(EventType.RUN_START, seq=0, vclock={})
        broken = factories.make_event(EventType.STATE_READ, seq=1, causal_parents=[1])
        assert "E-EVENT-020" in codes(check_semantic(broken, first))

    def test_virtual_ts_going_backwards_is_e_event_021(self) -> None:
        """Virtual ts going backwards is e event 021."""
        first = factories.make_event(EventType.RUN_START, seq=0, vclock={})
        later = factories.make_event(EventType.STATE_READ, seq=1)
        broken = dataclasses.replace(later, virtual_ts_ms=first.virtual_ts_ms - 1)
        assert "E-EVENT-021" in codes(check_semantic(broken, first))

    def test_a_seq_gap_is_e_event_022(self) -> None:
        """A seq gap is e event 022."""
        first = factories.make_event(EventType.RUN_START, seq=0, vclock={})
        broken = factories.make_event(EventType.STATE_READ, seq=2)
        assert "E-EVENT-022" in codes(check_semantic(broken, first))

    def test_first_event_must_be_seq_zero(self) -> None:
        """First event must be seq zero."""
        assert "E-EVENT-022" in codes(check_semantic(factories.make_event(seq=1), None))

    def test_missing_span_id_on_a_span_scoped_type_is_e_event_023(self) -> None:
        """Missing span id on a span scoped type is e event 023."""
        broken = dataclasses.replace(factories.make_event(EventType.STATE_WRITE), span_id=None)
        assert "E-EVENT-023" in codes(check_semantic(broken, None))

    def test_span_id_on_a_run_scoped_type_is_e_event_024(self) -> None:
        """Span id on a run scoped type is e event 024."""
        broken = dataclasses.replace(factories.make_event(EventType.RUN_START), span_id="x")
        assert "E-EVENT-024" in codes(check_semantic(broken, None))

    def test_duplicate_causal_parent_is_e_event_025(self) -> None:
        """Duplicate causal parent is e event 025."""
        first = factories.make_event(EventType.RUN_START, seq=0, vclock={})
        broken = factories.make_event(EventType.STATE_READ, seq=1, causal_parents=[0, 0])
        assert "E-EVENT-025" in codes(check_semantic(broken, first))

    def test_negative_timestamp_is_e_event_026(self) -> None:
        """Negative timestamp is e event 026."""
        broken = dataclasses.replace(factories.make_event(), virtual_ts_ms=-1)
        assert "E-EVENT-026" in codes(check_semantic(broken, None))

    def test_vclock_regression_for_the_same_slot_is_e_event_027(self) -> None:
        """Vclock regression for the same slot is e event 027."""
        first = factories.make_event(EventType.STATE_READ, seq=0, vclock={"coder": 5})
        broken = factories.make_event(EventType.STATE_WRITE, seq=1, vclock={"coder": 3})
        assert "E-EVENT-027" in codes(check_semantic(broken, first))

    def test_vclock_holding_steady_for_the_slot_is_allowed(self) -> None:
        """PRD §9.8 says >= for the slot; §14.2 implies >. The looser §9.8 rule is enforced."""
        first = factories.make_event(EventType.STATE_READ, seq=0, vclock={"coder": 5})
        held = factories.make_event(EventType.STATE_WRITE, seq=1, vclock={"coder": 5})
        assert "E-EVENT-027" not in codes(check_semantic(held, first))


class TestCrossEvent:
    """Layer (c): whole-log invariants."""

    def test_a_valid_log_passes(self) -> None:
        """A valid log passes."""
        assert check_cross_event(factories.make_log()) == ()

    def test_dangling_causal_parent_is_e_event_040(self) -> None:
        """Dangling causal parent is e event 040."""
        log = factories.make_log()
        broken = [*log[:3], dataclasses.replace(log[3], causal_parents=[99]), *log[4:]]
        assert "E-EVENT-040" in codes(check_cross_event(broken))

    def test_parent_vclock_ahead_of_child_is_e_event_041(self) -> None:
        """Parent vclock ahead of child is e event 041."""
        parent = factories.make_event(EventType.RUN_START, seq=0, vclock={"planner": 9})
        child = factories.make_event(
            EventType.STATE_READ, seq=1, causal_parents=[0], vclock={"planner": 2}
        )
        assert "E-EVENT-041" in codes(check_cross_event([parent, child]))

    def test_uninherited_fault_taint_is_e_event_042(self) -> None:
        """PRD §9.4 rule 2: taint descends through causal_parents, unconditionally."""
        parent = factories.make_event(EventType.RUN_START, seq=0, vclock={}, fault_id="f_01")
        child = factories.make_event(EventType.STATE_READ, seq=1, causal_parents=[0])
        assert "E-EVENT-042" in codes(check_cross_event([parent, child]))

    def test_inherited_fault_taint_passes(self) -> None:
        """Inherited fault taint passes."""
        parent = factories.make_event(EventType.RUN_START, seq=0, vclock={}, fault_id="f_01")
        child = factories.make_event(
            EventType.STATE_READ, seq=1, causal_parents=[0], fault_id="f_01"
        )
        assert "E-EVENT-042" not in codes(check_cross_event([parent, child]))

    def test_taint_without_a_tainted_parent_is_accepted(self) -> None:
        """PRD §9.4 rule 3 taints via agent context, an edge the log does not record.

        The 'iff' in the schema is therefore only half-checkable, and this test pins the
        deliberate gap so that nobody later 'fixes' it into a false positive.
        """
        parent = factories.make_event(EventType.RUN_START, seq=0, vclock={})
        child = factories.make_event(
            EventType.STATE_READ, seq=1, causal_parents=[0], fault_id="f_09"
        )
        assert check_cross_event([parent, child]) == ()

    def test_two_run_ids_in_one_log_is_e_event_043(self) -> None:
        """Two run ids in one log is e event 043."""
        log = factories.make_log()
        broken = [*log[:2], dataclasses.replace(log[2], run_id="r_other"), *log[3:]]
        assert "E-EVENT-043" in codes(check_cross_event(broken))


class TestComposedEntryPoints:
    """`validate_event` and `validate_log` raise with the full error list."""

    def test_validate_event_accepts_a_valid_event(self) -> None:
        """Validate event accepts a valid event."""
        assert validate_event(factories.make_event(EventType.RUN_START, vclock={})) is None

    def test_validate_event_raises_with_the_code_attached(self) -> None:
        """Validate event raises with the code attached."""
        broken = dataclasses.replace(factories.make_event(), schema_version=99)
        with pytest.raises(EventValidationError) as excinfo:
            validate_event(broken)
        assert excinfo.value.errors[0].code == "E-EVENT-008"

    def test_error_renders_with_a_docs_anchor(self) -> None:
        """Error renders with a docs anchor."""
        broken = dataclasses.replace(factories.make_event(), schema_version=99)
        with pytest.raises(EventValidationError) as excinfo:
            validate_event(broken)
        assert "docs/event-schema.md#e-event-008" in str(excinfo.value)

    def test_validate_log_accepts_the_generated_log(self) -> None:
        """Validate log accepts the generated log."""
        assert validate_log(factories.make_log()) is None

    def test_validate_log_reports_the_earliest_problem_first(self) -> None:
        """Validate log reports the earliest problem first."""
        log = factories.make_log()
        broken = [
            *log[:2],
            dataclasses.replace(log[2], schema_version=99),
            dataclasses.replace(log[3], schema_version=98),
            *log[4:],
        ]
        with pytest.raises(EventValidationError) as excinfo:
            validate_log(broken)
        assert excinfo.value.errors[0].seq == 2


class TestStampingBoundary:
    """The `DraftEvent` -> `Stamp` -> `Event` boundary (PRD §9.6, design constraint 6)."""

    def test_from_draft_carries_every_stamped_field(self) -> None:
        """From draft carries every stamped field."""
        from agentdx.events.schema import DraftEvent, Stamp

        draft = DraftEvent(
            type=EventType.STATE_WRITE,
            payload=factories.sample_payload(EventType.STATE_WRITE),
            agent_id="coder",
            clock_slot="coder",
            span_id="a3f19c22b0d1",
        )
        stamp = Stamp(
            seq=7,
            sched_step=4,
            virtual_ts_ms=2418,
            wall_ts_ms=191,
            vclock={"coder": 8},
            causal_parents=[6],
            fault_id=None,
        )
        event = Event.from_draft(draft, stamp, "r_f2a91")
        assert (event.seq, event.sched_step, event.virtual_ts_ms) == (7, 4, 2418)
        assert event.vclock == {"coder": 8}
        assert event.payload == draft.payload
        assert check_structural(event) == ()

    def test_a_draft_carries_no_ordering_information(self) -> None:
        """A DraftEvent must not be able to express seq, vclock or timestamps."""
        from agentdx.events.schema import DraftEvent

        forbidden = {"seq", "sched_step", "virtual_ts_ms", "wall_ts_ms", "vclock", "fault_id"}
        assert forbidden.isdisjoint({f.name for f in dataclasses.fields(DraftEvent)})

    def test_canonical_bytes_ignore_the_stamped_wall_clock(self) -> None:
        """Canonical bytes ignore the stamped wall clock."""
        from agentdx.events.schema import DraftEvent, Stamp

        draft = DraftEvent(
            type=EventType.RUN_START,
            payload=factories.sample_payload(EventType.RUN_START),
        )
        base = Stamp(0, 0, 0, 1, {}, [])
        other = dataclasses.replace(base, wall_ts_ms=999_999)
        assert canonical_bytes(Event.from_draft(draft, base, "r_a")) == canonical_bytes(
            Event.from_draft(draft, other, "r_a")
        )
