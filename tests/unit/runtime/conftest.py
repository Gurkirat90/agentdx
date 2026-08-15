"""Shared fixtures for the `runtime/` unit suite.

Design constraint 6 (the P06 mission): the scheduler must be testable in isolation with
fake tasks — no LLM, no graph, no fixture. Everything here exists to make building a
`Scheduler` bound to a fresh `VirtualClock` and an in-memory `EventWriter` a one-line call,
so the tests in this directory stay a few lines each.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from agentdx.config import SchedulerConfig
from agentdx.events.schema import Event
from agentdx.events.writer import ChainedEvent, EventWriter
from agentdx.runtime.clock import VirtualClock
from agentdx.runtime.scheduler import Scheduler

RUN_ID = "r_00006"


@dataclass
class MemorySink:
    """An in-memory `EventSink` (`events.writer.EventSink` Protocol) for tests.

    Guarantees: `append` is genuinely append-only (extends a list, never mutates an
    existing entry); `seal` records that it was called, so a test can assert a run reached
    a real seal rather than merely returning without error.
    """

    batches: list[tuple[ChainedEvent, ...]] = field(default_factory=list)
    sealed_run_id: str | None = None
    sealed_hash: str | None = None

    def append(self, batch: Sequence[ChainedEvent]) -> None:
        """Record one atomically-appended batch."""
        self.batches.append(tuple(batch))

    def seal(self, run_id: str, final_hash: str) -> None:
        """Record that `run_id` was sealed at `final_hash`."""
        self.sealed_run_id = run_id
        self.sealed_hash = final_hash

    def events(self) -> tuple[Event, ...]:
        """Return every written event, in the order it was appended, across all batches."""
        return tuple(ce.event for batch in self.batches for ce in batch)


def build_scheduler(
    *,
    seed: int = 42,
    run_id: str = RUN_ID,
    policy: str = "random",
    strict_determinism: bool = True,
    step_budget: int = 100_000,
    delay_schedule: dict[int, int] | None = None,
) -> tuple[Scheduler, MemorySink, VirtualClock]:
    """Build a `Scheduler` wired to a fresh in-memory sink, ready to `run()`.

    Returns the scheduler, the sink it writes to (for asserting on the resulting log), and
    the `VirtualClock` it shares with its `EventWriter` — useful for assertions on virtual
    time without reaching into the scheduler's private state.
    """
    clock = VirtualClock()
    sink = MemorySink()
    writer = EventWriter(run_id, sink, batch_size=1)  # flush immediately — easy assertions
    config = SchedulerConfig(strict_determinism=strict_determinism, step_budget=step_budget)
    scheduler = Scheduler(
        run_id=run_id,
        seed=seed,
        clock=clock,
        writer=writer,
        config=config,
        policy=policy,
        delay_schedule=delay_schedule,
    )
    return scheduler, sink, clock
