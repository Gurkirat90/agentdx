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

**The wall-clock flush timer (closes CONTEXT.md D-16).** PRD §27.3 specifies two flush
triggers — a size trigger and a "or 50 ms" wall-clock trigger — and only the first existed
before P06. A size-only trigger means a slow run (say, 10 events/s against a 128-event
batch) shows the live API nothing for over ten seconds and then 128 events at once, which
is what made the Control Tower look broken at P14–P16 rather than the writer. The fix needed
a clock, and AGENTS.md §4.1 clause 3 permits this file to reach one *only* through
`agentdx.wall_time()` — named by the rule and by `scripts/check_determinism_hygiene.py`'s
own allowlist. **`events/` may import nothing (CONTEXT.md §4: "the root contract"), and
`agentdx.wall_time()` lives in `runtime/`**, so this file cannot import it directly without
breaking the one contract every other layer's guarantees rest on — confirmed the hard way:
`import-linter` fails two contracts (`events/` importing `runtime/`, and transitively
`store/` importing `runtime/` through this file) the moment that import is added. The
resolution already has a precedent one paragraph up: `EventSink` is a structural type
declared here and *implemented* outside; `wall_time_fn` is the same pattern applied to a
callable instead of an object — declared as a plain `Callable[[], int]` here, and it is
`agentdx.wall_time` only because whoever constructs the writer for a real run (`runtime/`,
which may import both `events` and its own `clock.py`) passes it in. **Flush timing is
invisible to I1**: it changes only when bytes already written reach the sink, never what
those bytes are or what order they were validated and chained in — five different batch
sizes over one log produce one canonical log hash regardless (verified in
`tests/unit/events/test_writer_flush_timer.py`), so reading the real clock through the
injected callable never touches invariant I1. Passing no `wall_time_fn` disables the timer
entirely (size-only, the pre-D-16 behaviour) rather than defaulting to a real-clock read
`events/` cannot perform on its own — this keeps every existing caller's behaviour
unchanged, and is the *only* option a module barred from importing `runtime/` has for a
"do nothing extra unless asked" default.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
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

DEFAULT_FLUSH_INTERVAL_MS: Final = 50
"""PRD §27.3: "batches (64 events or 50 ms)". The size half shipped at P02 as
`DEFAULT_BATCH_SIZE`; this is the wall-clock half, closing D-16. Wall-clock, not virtual —
this governs when buffered bytes reach the sink, a liveness property of the process, not a
property of the run being replayed."""

_BATCH_SIZE_MSG: Final = "batch_size must be >= 1"
_FLUSH_INTERVAL_MSG: Final = "flush_interval_ms must be >= 1"


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
        self,
        run_id: str,
        sink: EventSink,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval_ms: int = DEFAULT_FLUSH_INTERVAL_MS,
        wall_time_fn: Callable[[], int] | None = None,
    ) -> None:
        """Create a writer bound to one run and one sink.

        Args:
            run_id: The run every event must belong to.
            sink: Persistence target; implemented at P03.
            batch_size: Events buffered before an automatic flush. Must be >= 1.
            flush_interval_ms: Wall-clock milliseconds since the last flush after which the
                next `write` flushes regardless of buffer size (PRD §27.3, D-16). Must be
                >= 1. Meaningless — and never consulted — when `wall_time_fn` is None.
            wall_time_fn: The real-clock accessor for the flush timer, injected because
                `events/` may not import `runtime/` to reach `agentdx.wall_time` itself
                (see the module docstring). `None` (the default) disables the wall-clock
                trigger entirely, leaving only the pre-D-16 size trigger — every caller
                that does not know about D-16 keeps its exact prior behaviour.
        """
        if batch_size < 1:
            raise ValueError(_BATCH_SIZE_MSG)
        if flush_interval_ms < 1:
            raise ValueError(_FLUSH_INTERVAL_MSG)
        self._run_id = run_id
        self._sink = sink
        self._batch_size = batch_size
        self._flush_interval_ms = flush_interval_ms
        self._wall_time_fn = wall_time_fn
        self._buffer: list[ChainedEvent] = []
        self._prev_hash = CHAIN_GENESIS
        self._last_seq: int | None = None
        self._previous: Event | None = None
        self._sealed = False
        self._last_flush_wall_ms = wall_time_fn() if wall_time_fn is not None else 0

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
        elif len(self._buffer) >= self._batch_size or self._flush_interval_elapsed():
            self.flush()

    def _flush_interval_elapsed(self) -> bool:
        """Return True once `flush_interval_ms` has passed since the last flush (D-16).

        Always False when no `wall_time_fn` was injected — there is no clock to consult.
        """
        if self._wall_time_fn is None:
            return False
        return self._wall_time_fn() - self._last_flush_wall_ms >= self._flush_interval_ms

    def flush(self) -> None:
        """Hand the buffered batch to the sink and clear it.

        Guarantees: a no-op on an empty buffer. If the sink raises, the buffer is left
        intact so the caller may retry without losing events, and the flush-timer clock is
        not reset — a failed flush must not make the *next* attempt wait another full
        interval.
        """
        if not self._buffer:
            return
        self._sink.append(tuple(self._buffer))
        self._buffer.clear()
        if self._wall_time_fn is not None:
            self._last_flush_wall_ms = self._wall_time_fn()

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
    "DEFAULT_FLUSH_INTERVAL_MS",
    "ChainedEvent",
    "EventSink",
    "EventValidationError",
    "EventWriter",
    "WriterStateError",
]
