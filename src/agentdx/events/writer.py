"""The append-only event writer: batching, chaining, sealing (PRD §9.6 step 4, §9.7).

The writer is deliberately the least clever module in the package. It does not stamp, does
not order, does not choose, and does not persist. It validates, chains, batches and hands
batches to an injected sink.

**The stamping boundary (design constraint 6, PRD §9.6).** `write` accepts only a fully
stamped `Event`, and `Event` can only be built through `Event.from_draft(draft, stamp, ...)`,
which demands a `Stamp`. The writer holds no sequence counter, no clock and no vector clock,
so it has nothing to stamp *with*. A later prompt that tried to make the writer assign `seq`
would have to add state to this class and change `write`'s signature — a visible edit in a
diff, not an accident. That is the whole point: PRD §9.6 puts stamping under the scheduler
lock in P06, and this module is where that decision is made unenforceable-by-accident.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Protocol, runtime_checkable

from agentdx.events.canonical import CHAIN_GENESIS, chain_hash
from agentdx.events.schema import Event, EventType
from agentdx.events.validators import EventValidationError, ValidationError, validate_event

DEFAULT_BATCH_SIZE: Final = 128
"""Events buffered before a flush. PRD §39.6 wants appends off the hot path in batches;
128 keeps a batch inside one SQLite transaction without holding events long enough to lose
a meaningful window on a crash."""

_BATCH_SIZE_MSG: Final = "batch_size must be >= 1"


@dataclass(frozen=True, slots=True)
class ChainedEvent:
    """An event together with its position in the tamper-evident chain.

    `prev_hash` and `this_hash` are carried *beside* the event, never inside it: they are
    `events` table columns in PRD §38, and an event whose canonical form contained its own
    hash would be self-referential (ruling C-4).
    """

    event: Event
    prev_hash: str
    this_hash: str


@runtime_checkable
class EventSink(Protocol):
    """The persistence target, implemented by `store/sqlite.py` at P03.

    Declaring it here rather than importing the store keeps `events/` the root of the layer
    contract with no imports of its own (CONTEXT.md §4), and lets the writer be tested
    against an in-memory sink with no database at all.
    """

    def append(self, batch: Sequence[ChainedEvent]) -> None:
        """Persist a batch atomically. Must not modify or delete anything (I2)."""
        ...

    def seal(self, run_id: str, final_hash: str) -> None:
        """Mark the run complete. The sink must refuse later appends for this run."""
        ...


class WriterStateError(Exception):
    """Raised when the writer is used outside its contract — after a seal, or out of order.

    Guarantees: carries a `ValidationError` with a stable `E-EVENT-NNN` code, so callers
    branch on the same code space as schema failures.
    """

    def __init__(self, error: ValidationError) -> None:
        """Build the exception from the single error that describes the misuse."""
        self.error = error
        super().__init__(str(error))


class EventWriter:
    """Validates, chains and batches stamped events into an injected sink.

    Guarantees:

    * **Append-only (I2).** The class exposes no update or delete path, and the chain it
      maintains makes an out-of-band edit detectable by `canonical.verify_chain`.
    * **No stamping.** Holds no counter, clock or vector clock; `write` requires an already
      stamped `Event`.
    * **Ordered.** Rejects an event whose `seq` is not exactly one past the last written,
      so a dropped or duplicated event is caught at the writer rather than at seal.
    * **Sealed once.** After a `run_end` event the writer refuses everything (PRD §9.7).

    Not thread-safe by design: PRD §10.2 runs the scheduler on a single OS thread, and a
    lock here would imply concurrency that CONTEXT.md §3 has ruled out.
    """

    def __init__(
        self, run_id: str, sink: EventSink, *, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> None:
        """Create a writer bound to one run and one sink.

        Args:
            run_id: The run every event must belong to.
            sink: Persistence target; implemented at P03.
            batch_size: Events buffered before an automatic flush. Must be >= 1.
        """
        if batch_size < 1:
            raise ValueError(_BATCH_SIZE_MSG)
        self._run_id = run_id
        self._sink = sink
        self._batch_size = batch_size
        self._buffer: list[ChainedEvent] = []
        self._prev_hash = CHAIN_GENESIS
        self._last_seq: int | None = None
        self._previous: Event | None = None
        self._sealed = False

    @property
    def sealed(self) -> bool:
        """Return True once `run_end` has been written and the log closed."""
        return self._sealed

    @property
    def last_hash(self) -> str:
        """Return the chain head — the `this_hash` of the last event written."""
        return self._prev_hash

    def write(self, event: Event) -> None:
        """Validate, chain and buffer one fully stamped event (PRD §9.6 steps 3–4).

        Guarantees: on return the event is either buffered or persisted, and the chain head
        has advanced. On any raise, nothing has been buffered and the chain head is
        unchanged — a rejected event leaves no trace, which is what makes the seq check
        below meaningful on the next call.

        Raises:
            WriterStateError: `E-EVENT-050` the run is sealed · `E-EVENT-051` the event
                belongs to a different run · `E-EVENT-022` seq is not exactly one past the
                last written event.
            EventValidationError: the event failed structural or semantic validation.
        """
        if self._sealed:
            raise WriterStateError(
                ValidationError(
                    "E-EVENT-050",
                    f"run {self._run_id} is sealed; the log is append-only and closed",
                    event.seq,
                )
            )
        if event.run_id != self._run_id:
            raise WriterStateError(
                ValidationError(
                    "E-EVENT-051",
                    f"event belongs to run {event.run_id!r}, writer is bound to {self._run_id!r}",
                    event.seq,
                    "run_id",
                )
            )
        expected = 0 if self._last_seq is None else self._last_seq + 1
        if event.seq != expected:
            raise WriterStateError(
                ValidationError(
                    "E-EVENT-022",
                    f"seq must be gapless: expected {expected}, got {event.seq}. The writer "
                    f"does not assign seq — stamping happens under the scheduler lock",
                    event.seq,
                    "seq",
                )
            )

        validate_event(event, self._previous)

        this_hash = chain_hash(self._prev_hash, event)
        self._buffer.append(ChainedEvent(event, self._prev_hash, this_hash))
        self._prev_hash = this_hash
        self._last_seq = event.seq
        self._previous = event

        if event.type is EventType.RUN_END:
            self.flush()
            self._sink.seal(self._run_id, this_hash)
            self._sealed = True
        elif len(self._buffer) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """Hand the buffered batch to the sink and clear it.

        Guarantees: a no-op on an empty buffer. If the sink raises, the buffer is left
        intact so the caller may retry without losing events.
        """
        if not self._buffer:
            return
        self._sink.append(tuple(self._buffer))
        self._buffer.clear()

    def __enter__(self) -> EventWriter:
        """Enter a context that flushes on exit."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Flush on clean exit; on an exception, leave the buffer for the caller.

        A run that crashed mid-way is still evidence, but flushing during exception
        unwinding could mask the original error behind a sink failure.
        """
        if exc_type is None:
            self.flush()


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "ChainedEvent",
    "EventSink",
    "EventValidationError",
    "EventWriter",
    "WriterStateError",
]
