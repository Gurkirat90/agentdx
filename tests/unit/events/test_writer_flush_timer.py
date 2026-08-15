"""The wall-clock flush trigger (PRD §27.3, D-16).

`writer.py`'s module docstring names this file as the proof that flush timing is invisible
to I1: the injected `wall_time_fn` changes only *when* buffered bytes reach the sink, never
what those bytes are.
"""

from __future__ import annotations

from agentdx.events.canonical import canonical_log_hash
from agentdx.events.writer import ChainedEvent, EventWriter
from tests.unit.events import factories


class FakeWallClock:
    """A `wall_time_fn`-shaped callable driven by the test, never the real clock."""

    def __init__(self, start_ms: int = 0) -> None:
        """Start the fake clock at `start_ms`."""
        self._now_ms = start_ms
        self.calls = 0

    def __call__(self) -> int:
        """Return the current fake wall time and count the read."""
        self.calls += 1
        return self._now_ms

    def advance(self, ms: int) -> None:
        """Move the fake clock forward by `ms`."""
        self._now_ms += ms


class RecordingSink:
    """An in-memory `EventSink` — same minimal shape as `tests/unit/events/test_writer.py`."""

    def __init__(self) -> None:
        """Build the fixture sink."""
        self.batches: list[tuple[ChainedEvent, ...]] = []
        self.sealed: list[tuple[str, str]] = []

    def append(self, batch: tuple[ChainedEvent, ...] | list[ChainedEvent]) -> None:
        """Record one appended batch."""
        self.batches.append(tuple(batch))

    def seal(self, run_id: str, final_hash: str) -> None:
        """Record a seal."""
        self.sealed.append((run_id, final_hash))

    @property
    def flush_count(self) -> int:
        """Return how many times `append` was called — one call per flush."""
        return len(self.batches)


# ---------------------------------------------------------------------------------------
# No wall_time_fn: pre-D-16 behaviour, unchanged
# ---------------------------------------------------------------------------------------


def test_with_no_wall_time_fn_only_the_size_trigger_flushes() -> None:
    sink = RecordingSink()
    writer = EventWriter(factories.RUN_ID, sink, batch_size=3)  # wall_time_fn defaults to None

    log = factories.make_log(length=6)
    for event in log[:2]:  # fewer than batch_size
        writer.write(event)

    assert sink.flush_count == 0  # no size trigger yet, and no clock to consult


def test_flush_interval_elapsed_is_always_false_with_no_wall_time_fn() -> None:
    sink = RecordingSink()
    writer = EventWriter(factories.RUN_ID, sink, batch_size=1000, flush_interval_ms=1)
    assert writer._flush_interval_elapsed() is False


# ---------------------------------------------------------------------------------------
# With wall_time_fn: the D-16 trigger fires on elapsed wall time, not buffer size
# ---------------------------------------------------------------------------------------


def test_the_wall_clock_trigger_flushes_a_buffer_below_batch_size_once_the_interval_elapses() -> (
    None
):
    clock = FakeWallClock(start_ms=0)
    sink = RecordingSink()
    writer = EventWriter(
        factories.RUN_ID, sink, batch_size=1000, flush_interval_ms=50, wall_time_fn=clock
    )

    log = factories.make_log(length=6)
    writer.write(log[0])  # buffer has 1 of 1000 — no size flush
    assert sink.flush_count == 0

    clock.advance(50)  # exactly flush_interval_ms
    writer.write(log[1])
    assert sink.flush_count == 1  # the wall-clock trigger fired, not the size trigger


def test_the_wall_clock_trigger_does_not_fire_before_the_interval_elapses() -> None:
    clock = FakeWallClock(start_ms=0)
    sink = RecordingSink()
    writer = EventWriter(
        factories.RUN_ID, sink, batch_size=1000, flush_interval_ms=50, wall_time_fn=clock
    )

    log = factories.make_log(length=6)
    writer.write(log[0])
    clock.advance(49)  # one short of the interval
    writer.write(log[1])
    assert sink.flush_count == 0


def test_the_flush_clock_resets_after_a_successful_flush() -> None:
    clock = FakeWallClock(start_ms=0)
    sink = RecordingSink()
    writer = EventWriter(
        factories.RUN_ID, sink, batch_size=1000, flush_interval_ms=50, wall_time_fn=clock
    )

    log = factories.make_log(length=6)
    writer.write(log[0])
    clock.advance(50)
    writer.write(log[1])  # flush #1, clock rebases to 50
    assert sink.flush_count == 1

    clock.advance(49)  # short of another full interval since the rebase
    writer.write(log[2])
    assert sink.flush_count == 1  # still just the one flush

    clock.advance(1)  # now exactly 50ms since the rebase
    writer.write(log[3])
    assert sink.flush_count == 2


def test_a_failed_flush_does_not_reset_the_flush_clock() -> None:
    """A flush failure must not make the *next* attempt wait another full interval."""
    msg = "sink unavailable"

    class RaisingSink(RecordingSink):
        def append(self, batch: tuple[ChainedEvent, ...] | list[ChainedEvent]) -> None:
            raise RuntimeError(msg)

    clock = FakeWallClock(start_ms=0)
    sink = RaisingSink()
    writer = EventWriter(
        factories.RUN_ID, sink, batch_size=1000, flush_interval_ms=50, wall_time_fn=clock
    )
    log = factories.make_log(length=6)
    writer.write(log[0])
    clock.advance(50)

    try:
        writer.write(log[1])
    except RuntimeError:
        pass

    # The buffered events were not lost, and the clock was not rebased by the failed flush —
    # asserted indirectly via _last_flush_wall_ms, since that is exactly what the module
    # docstring's "flush() ... the flush-timer clock is not reset" guarantee is about.
    assert writer._last_flush_wall_ms == 0


# ---------------------------------------------------------------------------------------
# Flush timing is invisible to I1: the canonical hash never depends on batch boundaries
# ---------------------------------------------------------------------------------------


def test_five_different_batch_and_interval_configurations_produce_one_canonical_hash() -> None:
    """The exact claim in `writer.py`'s module docstring, verified directly."""
    log = factories.make_log(length=12)
    configs: list[tuple[int, int | None]] = [
        (1, None),
        (3, None),
        (128, None),
        (1, 1),
        (5, 10_000),
    ]
    hashes: set[str] = set()
    for batch_size, flush_interval_ms in configs:
        sink = RecordingSink()
        clock = FakeWallClock() if flush_interval_ms is not None else None
        writer = EventWriter(
            factories.RUN_ID,
            sink,
            batch_size=batch_size,
            **(
                {"flush_interval_ms": flush_interval_ms, "wall_time_fn": clock}
                if flush_interval_ms is not None
                else {}
            ),
        )
        for event in log:
            if clock is not None:
                clock.advance(1)
            writer.write(event)
        written = [chained.event for batch in sink.batches for chained in batch]
        hashes.add(canonical_log_hash(written))

    assert len(hashes) == 1, f"flush timing leaked into the canonical hash: {hashes}"
