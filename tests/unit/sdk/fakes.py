"""Test doubles for the runtime services the SDK is injected with.

**These live in `tests/` deliberately, and it is not a convenience.** The one thing that
makes P06's stamping boundary enforceable is that nothing under `src/agentdx/sdk/` can
construct a `Stamp` or an `Event` — `tests/unit/sdk/test_sdk_never_stamps.py` asserts it
against the AST. A stamping recorder shipped inside the SDK "just for now" would delete that
guarantee, so the only stamping recorder in the tree is this one, and it is a test double.

`StampingRecorder` implements the PRD §14.2 vector-clock rules well enough that every event
it produces passes P02's validators, including the cross-event layer:

* each event increments its own slot;
* an event with causal parents merges their clocks first, so a parent is never ahead of its
  child (`E-EVENT-041`);
* `seq` is gapless, `virtual_ts_ms` is non-decreasing, and every causal parent is `< seq`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from agentdx.events.schema import DraftEvent, Event, EventType, Stamp
from agentdx.events.writer import ChainedEvent, EventWriter
from agentdx.sdk.generic import (
    LifecycleHooks,
    ManualClock,
    RunContext,
    RunResult,
)

RUNTIME_SLOT = "__runtime__"
"""The clock slot for run-scoped events, which have no agent."""


@dataclass
class CollectingSink:
    """An `EventSink` that keeps every batch in memory."""

    batches: list[tuple[ChainedEvent, ...]] = field(default_factory=list)
    sealed_with: str | None = None

    def append(self, batch: Sequence[ChainedEvent]) -> None:
        """Persist a batch — here, remember it."""
        self.batches.append(tuple(batch))

    def seal(self, run_id: str, final_hash: str) -> None:
        """Mark the run complete."""
        self.sealed_with = final_hash

    @property
    def events(self) -> list[Event]:
        """Return every event flushed so far, in seq order."""
        return [chained.event for batch in self.batches for chained in batch]


class StampingRecorder:
    """Stands in for P06: assigns seq, step, timestamps, vector clock and causal parents."""

    def __init__(
        self,
        run_id: str,
        clock: ManualClock | None = None,
        writer: EventWriter | None = None,
    ) -> None:
        """Bind a recorder to a run, optionally writing through a real `EventWriter`."""
        self.run_id = run_id
        self.clock = clock or ManualClock()
        self.writer = writer
        self.events: list[Event] = []
        self._seq = 0
        self._slots: dict[str, dict[str, int]] = {}
        self._by_seq: dict[int, Event] = {}

    def emit(self, draft: DraftEvent, causes: Sequence[int]) -> int:
        """Stamp one draft and record it. Returns the assigned seq."""
        seq = self._seq
        self._seq += 1
        slot = draft.clock_slot or draft.agent_id or RUNTIME_SLOT

        merged = dict(self._slots.get(slot, {}))
        parents = sorted({cause for cause in causes if 0 <= cause < seq})
        for parent in parents:
            for name, count in self._by_seq[parent].vclock.items():
                merged[name] = max(merged.get(name, 0), count)
        merged[slot] = merged.get(slot, 0) + 1
        self._slots[slot] = merged

        event = Event.from_draft(
            draft,
            Stamp(
                seq=seq,
                sched_step=seq,
                virtual_ts_ms=self.clock.virtual_ms(),
                wall_ts_ms=self.clock.wall_ms(),
                vclock=dict(merged),
                causal_parents=parents,
            ),
            self.run_id,
        )
        self.events.append(event)
        self._by_seq[seq] = event
        if self.writer is not None:
            self.writer.write(event)
        return seq

    def of_type(self, event_type: EventType) -> list[Event]:
        """Return every recorded event of one type, in order."""
        return [event for event in self.events if event.type is event_type]

    def payloads(self, event_type: EventType) -> list[dict[str, object]]:
        """Return the payloads of every recorded event of one type, in order."""
        return [dict(event.payload) for event in self.of_type(event_type)]


def make_context(
    *,
    run_id: str = "r_00abc",
    mode: str = "replay",
    capture_bodies: bool = False,
    recorder: StampingRecorder | None = None,
    clock: ManualClock | None = None,
    hooks: LifecycleHooks | None = None,
    cache: object = None,
    config: object = None,
) -> tuple[RunContext, StampingRecorder]:
    """Build a run context wired to a fresh `StampingRecorder`."""
    from agentdx.config import AgentDXConfig

    resolved_clock = clock or ManualClock()
    resolved_recorder = recorder or StampingRecorder(run_id, resolved_clock)
    resolved_config = config if config is not None else AgentDXConfig()
    context = RunContext.create(
        run_id=run_id,
        recorder=resolved_recorder,  # type: ignore[arg-type]
        config=resolved_config,  # type: ignore[arg-type]
        mode=mode,
        clock=resolved_clock,
        cache=cache,  # type: ignore[arg-type]
        hooks=hooks,
        capture_bodies=capture_bodies,
    )
    return context, resolved_recorder


class FakeHost:
    """A `RunHost` that opens and seals a run without a scheduler (P06's job)."""

    def __init__(self, context: RunContext) -> None:
        """Bind the host to a pre-built context."""
        self.context = context
        self.opened = 0
        self.closed: list[str] = []

    async def open_run(self, *, task: str, scenario: str | None, seed: int | None) -> RunContext:
        """Return the pre-built context. A real host emits `run_start` here."""
        self.opened += 1
        return self.context

    async def close_run(self, context: RunContext, *, status: str, output: object) -> RunResult:
        """Return the result. A real host emits `run_end` and seals the log here."""
        self.closed.append(status)
        return RunResult(run_id=context.run_id, status=status, output=output, gaps=context.gaps)
