"""The writer's contract: no stamping, gapless order, batching, chaining, sealing."""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from agentdx.events.canonical import CHAIN_GENESIS, verify_chain
from agentdx.events.migrations import SchemaVersionError, migrate
from agentdx.events.schema import SCHEMA_VERSION, EventType
from agentdx.events.writer import (
    ChainedEvent,
    EventValidationError,
    EventWriter,
    WriterStateError,
)
from tests.unit.events import factories


class RecordingSink:
    """An in-memory `EventSink`, so the writer is testable with no database (P03 is later)."""

    def __init__(self) -> None:
        """Build the fixture sink."""
        self.batches: list[tuple[ChainedEvent, ...]] = []
        self.sealed: list[tuple[str, str]] = []

    def append(self, batch: tuple[ChainedEvent, ...] | list[ChainedEvent]) -> None:
        """Append."""
        self.batches.append(tuple(batch))

    def seal(self, run_id: str, final_hash: str) -> None:
        """Seal."""
        self.sealed.append((run_id, final_hash))

    @property
    def events(self) -> list[ChainedEvent]:
        """Events."""
        return [chained for batch in self.batches for chained in batch]


class TestStampingBoundary:
    """Design constraint 6: the writer cannot assign seq or vclock because it has neither."""

    def test_write_accepts_only_a_stamped_event(self) -> None:
        """Write accepts only a stamped event."""
        signature = inspect.signature(EventWriter.write)
        assert list(signature.parameters) == ["self", "event"]
        assert signature.parameters["event"].annotation == "Event"

    def test_the_event_reaching_the_sink_is_the_very_object_that_was_written(self) -> None:
        """OP-3 F2 regression: the old test asserted three attribute spellings, not a property.

        It checked that `_seq`, `_clock` and `_vclock` were absent from `vars(writer)` — while
        the writer legitimately holds `_last_seq`, a counter. A refactor that added
        `event = replace(event, seq=self._last_seq + 1)` would have stamped the event, killed
        design constraint 6, and still passed. Identity is the property that actually matters:
        whatever the writer hands the sink must be the same object it was given, unmodified.
        """
        sink = RecordingSink()
        writer = EventWriter("r_f2a91", sink, batch_size=1)
        event = factories.make_event(EventType.RUN_START, seq=0, vclock={"planner": 3})
        writer.write(event)
        assert sink.events[0].event is event

    def test_the_writer_module_cannot_construct_or_modify_an_event(self) -> None:
        """A structural guarantee, checked on the AST rather than trusted.

        If `writer.py` never calls `Event(...)` and never calls `replace(...)`, it has no way
        to produce an event different from the one handed to it — regardless of what state it
        keeps. This is the mechanism behind design constraint 6, and unlike a name check it
        cannot be defeated by renaming a variable.
        """
        import ast
        import pathlib

        import agentdx.events.writer as writer_module

        tree = ast.parse(pathlib.Path(writer_module.__file__).read_text(encoding="utf-8"))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        } | {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "Event" not in called, "writer.py constructs an Event — it must only pass through"
        assert "replace" not in called, "writer.py calls replace() — it must not modify events"
        assert "from_draft" not in called, "stamping belongs to the runtime, not the writer"

    def test_the_writer_does_not_repair_a_gap_it_could_have_repaired(self) -> None:
        """It holds `_last_seq`, so it *could* assign seq. It must refuse instead."""
        writer = EventWriter("r_f2a91", RecordingSink())
        writer.write(factories.make_event(EventType.RUN_START, seq=0, vclock={}))
        with pytest.raises(WriterStateError):
            writer.write(factories.make_event(EventType.STATE_READ, seq=7))


class TestOrdering:
    """Gapless seq is enforced at the writer, not discovered at seal."""

    def test_a_gap_is_rejected_with_e_event_022(self) -> None:
        """A gap is rejected with e event 022."""
        writer = EventWriter("r_f2a91", RecordingSink())
        writer.write(factories.make_event(EventType.RUN_START, seq=0, vclock={}))
        with pytest.raises(WriterStateError) as excinfo:
            writer.write(factories.make_event(EventType.STATE_READ, seq=2))
        assert excinfo.value.error.code == "E-EVENT-022"

    def test_the_first_event_must_be_seq_zero(self) -> None:
        """The first event must be seq zero."""
        writer = EventWriter("r_f2a91", RecordingSink())
        with pytest.raises(WriterStateError) as excinfo:
            writer.write(factories.make_event(seq=1))
        assert excinfo.value.error.code == "E-EVENT-022"

    def test_an_event_from_another_run_is_rejected_with_e_event_051(self) -> None:
        """An event from another run is rejected with e event 051."""
        writer = EventWriter("r_f2a91", RecordingSink())
        stray = dataclasses.replace(
            factories.make_event(EventType.RUN_START, seq=0, vclock={}), run_id="r_other"
        )
        with pytest.raises(WriterStateError) as excinfo:
            writer.write(stray)
        assert excinfo.value.error.code == "E-EVENT-051"

    def test_a_rejected_event_leaves_no_trace(self) -> None:
        """The seq check on the next call is only meaningful if a rejection is a no-op."""
        writer = EventWriter("r_f2a91", RecordingSink())
        writer.write(factories.make_event(EventType.RUN_START, seq=0, vclock={}))
        head = writer.last_hash
        with pytest.raises(WriterStateError):
            writer.write(factories.make_event(EventType.STATE_READ, seq=5))
        assert writer.last_hash == head
        writer.write(factories.make_event(EventType.STATE_READ, seq=1))


class TestValidationOnWrite:
    """PRD §9.8: validation happens on write, always."""

    def test_an_invalid_event_never_reaches_the_sink(self) -> None:
        """An invalid event never reaches the sink."""
        sink = RecordingSink()
        writer = EventWriter("r_f2a91", sink, batch_size=1)
        broken = dataclasses.replace(
            factories.make_event(EventType.RUN_START, seq=0, vclock={}), schema_version=99
        )
        with pytest.raises(EventValidationError):
            writer.write(broken)
        assert sink.events == []


class TestBatching:
    """Events are handed over in batches, one transaction each (PRD §9.7)."""

    def test_nothing_is_flushed_before_the_batch_is_full(self) -> None:
        """Nothing is flushed before the batch is full."""
        sink = RecordingSink()
        writer = EventWriter("r_f2a91", sink, batch_size=4)
        writer.write(factories.make_event(EventType.RUN_START, seq=0, vclock={}))
        writer.write(factories.make_event(EventType.STATE_READ, seq=1))
        assert sink.batches == []

    def test_a_full_batch_is_flushed(self) -> None:
        """A full batch is flushed."""
        sink = RecordingSink()
        writer = EventWriter("r_f2a91", sink, batch_size=2)
        writer.write(factories.make_event(EventType.RUN_START, seq=0, vclock={}))
        writer.write(factories.make_event(EventType.STATE_READ, seq=1))
        assert len(sink.batches) == 1
        assert len(sink.batches[0]) == 2

    def test_context_manager_flushes_on_clean_exit(self) -> None:
        """Context manager flushes on clean exit."""
        sink = RecordingSink()
        with EventWriter("r_f2a91", sink, batch_size=100) as writer:
            writer.write(factories.make_event(EventType.RUN_START, seq=0, vclock={}))
        assert len(sink.events) == 1

    def test_batch_size_must_be_positive(self) -> None:
        """Batch size must be positive."""
        with pytest.raises(ValueError, match="batch_size"):
            EventWriter("r_f2a91", RecordingSink(), batch_size=0)


class TestSealing:
    """After run_end the log is closed (PRD §9.7)."""

    def test_run_end_seals_the_writer(self) -> None:
        """Run end seals the writer."""
        sink = RecordingSink()
        writer = EventWriter("r_f2a91", sink)
        writer.write(factories.make_event(EventType.RUN_START, seq=0, vclock={}))
        writer.write(factories.make_event(EventType.RUN_END, seq=1, causal_parents=[0]))
        assert writer.sealed
        assert sink.sealed == [("r_f2a91", writer.last_hash)]

    def test_writing_after_seal_is_e_event_050(self) -> None:
        """Writing after seal is e event 050."""
        writer = EventWriter("r_f2a91", RecordingSink())
        writer.write(factories.make_event(EventType.RUN_START, seq=0, vclock={}))
        writer.write(factories.make_event(EventType.RUN_END, seq=1, causal_parents=[0]))
        with pytest.raises(WriterStateError) as excinfo:
            writer.write(factories.make_event(EventType.STATE_READ, seq=2))
        assert excinfo.value.error.code == "E-EVENT-050"

    def test_run_end_forces_a_flush_before_sealing(self) -> None:
        """Run end forces a flush before sealing."""
        sink = RecordingSink()
        writer = EventWriter("r_f2a91", sink, batch_size=1000)
        writer.write(factories.make_event(EventType.RUN_START, seq=0, vclock={}))
        writer.write(factories.make_event(EventType.RUN_END, seq=1, causal_parents=[0]))
        assert len(sink.events) == 2


class TestChaining:
    """The writer maintains the tamper-evident chain as it goes."""

    def test_the_chain_starts_at_genesis_and_verifies(self) -> None:
        """The chain starts at genesis and verifies."""
        sink = RecordingSink()
        writer = EventWriter("r_f2a91", sink, batch_size=1)
        log = factories.make_log()
        for event in log:
            writer.write(event)
        writer.flush()
        assert sink.events[0].prev_hash == CHAIN_GENESIS
        chain = [(c.prev_hash, c.this_hash) for c in sink.events]
        assert verify_chain([c.event for c in sink.events], chain) is None

    def test_the_chain_is_blind_to_volatile_fields(self) -> None:
        """Two machines running the same log must agree on the chain (PRD §9.7, §31)."""

        def chain_for(offset: int) -> list[str]:
            sink = RecordingSink()
            writer = EventWriter("r_f2a91", sink, batch_size=1)
            for event in factories.make_log():
                writer.write(dataclasses.replace(event, wall_ts_ms=event.wall_ts_ms + offset))
            writer.flush()
            return [c.this_hash for c in sink.events]

        assert chain_for(0) == chain_for(5000)


class TestMigrationHarness:
    """The version policy (PRD §9.9). The registry carries its first step, v1->v2."""

    def test_a_current_version_record_passes_through_unchanged(self) -> None:
        """A current version record passes through unchanged."""
        record = {"schema_version": SCHEMA_VERSION, "seq": 0}
        assert migrate(record) is record

    def test_a_future_version_is_rejected_naming_both_versions(self) -> None:
        """A future version is rejected naming both versions."""
        with pytest.raises(SchemaVersionError) as excinfo:
            migrate({"schema_version": SCHEMA_VERSION + 1})
        assert "E-EVENT-060" in str(excinfo.value)
        assert excinfo.value.found == SCHEMA_VERSION + 1

    def test_a_missing_version_is_rejected(self) -> None:
        """A missing version is rejected."""
        with pytest.raises(SchemaVersionError):
            migrate({"seq": 0})

    def test_the_registry_carries_the_v1_to_v2_step(self) -> None:
        """The registry carries exactly the v1->v2 step added at P09 OP-3 repair (D-45).

        Was `assert MIGRATIONS == {}` before `SCHEMA_VERSION` went 1 -> 2 — this is the
        deliberate update that finding required, not drift: the harness existed empty
        specifically so its first real entry would land here.
        """
        from agentdx.events.migrations import MIGRATIONS

        assert set(MIGRATIONS) == {1}
        assert callable(MIGRATIONS[1])
