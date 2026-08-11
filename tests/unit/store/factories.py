"""Realistic multi-agent event logs for the store suites, built from the schema marks.

`tests/unit/events/factories.py` builds single events and a six-event log; the store needs
logs with real span structure, several agents, many `state_write`s across many keys, and
enough length to cross a snapshot interval. Everything here derives from
`PAYLOAD_SCHEMAS`, so a new required field appears in these logs automatically rather than
being forgotten.

Every log produced here passes `validators.validate_log`. That is asserted directly in
`test_factories_produce_valid_logs`, because a factory that quietly drifted out of contract
would make every store test test the wrong thing.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentdx.events.schema import SCHEMA_VERSION, Event, EventType, PayloadValue
from tests.unit.events.factories import sample_payload

RUN_ID = "r_f2a91"
AGENTS = ("planner", "coder", "reviewer")
STATE_KEYS = ("draft.module_a", "draft.module_b", "plan", "review.notes")


class LogBuilder:
    """Builds a valid, linearly-ordered event log one event at a time.

    The log is a single causal chain: event `n` has `causal_parents == [n - 1]` and carries
    a cumulative vector clock that only ever grows. That is a real shape a cooperative
    single-threaded scheduler produces (PRD §10.2) and it keeps every vclock rule in
    `check_semantic` and `check_cross_event` satisfied without the factory having to model
    concurrency it is not testing.

    Guarantees: `events()` passes `validators.validate_log`; seq is gapless from 0;
    `virtual_ts_ms` is non-decreasing; span-scoped events carry `span_id` and run-scoped
    ones do not.
    """

    def __init__(self, run_id: str = RUN_ID) -> None:
        """Start an empty log for `run_id`."""
        self._run_id = run_id
        self._events: list[Event] = []
        self._clock: dict[str, int] = {}
        self._virtual_ts = 0

    def add(
        self,
        event_type: EventType,
        *,
        agent_id: str | None = None,
        span_id: str | None = None,
        payload: dict[str, PayloadValue] | None = None,
        advance_ms: int = 10,
    ) -> Event:
        """Append one event of `event_type` and return it.

        Guarantees: assigns the next seq, advances the virtual clock by `advance_ms`,
        increments this event's clock slot and snapshots the cumulative vector clock, so the
        parent-before-child vclock rule holds by construction.
        """
        seq = len(self._events)
        self._virtual_ts += advance_ms
        slot = agent_id
        if slot is not None:
            self._clock[slot] = self._clock.get(slot, 0) + 1
        body = dict(sample_payload(event_type))
        if payload:
            body.update(payload)
        event = Event(
            schema_version=SCHEMA_VERSION,
            run_id=self._run_id,
            seq=seq,
            sched_step=seq,
            virtual_ts_ms=self._virtual_ts,
            wall_ts_ms=self._virtual_ts + seq,
            vclock=dict(self._clock),
            type=event_type,
            causal_parents=[seq - 1] if seq else [],
            payload=body,
            agent_id=agent_id,
            clock_slot=slot,
            span_id=span_id,
        )
        self._events.append(event)
        return event

    def events(self) -> tuple[Event, ...]:
        """Return the log built so far, in seq order."""
        return tuple(self._events)


def build_log(
    *,
    spans: int = 4,
    writes_per_span: int = 2,
    run_id: str = RUN_ID,
    sealed: bool = True,
) -> tuple[Event, ...]:
    """Return a valid multi-agent log with real span structure.

    Args:
        spans: Number of `span_start`/`span_end` pairs, distributed round-robin over
            `AGENTS`.
        writes_per_span: `state_write` events inside each span, cycling `STATE_KEYS`, so
            the same key is written by different agents at different times — the shape
            §20.4 reconstruction and the later race detector both care about.
        run_id: The run every event belongs to.
        sealed: Append a `run_end`. False produces the prefix of a run still in progress,
            which is what a crashed run looks like on disk (NFR-13).

    Returns:
        The events, in seq order. Passes `validators.validate_log` either way.
    """
    builder = LogBuilder(run_id)
    builder.add(EventType.RUN_START, payload={"started_at_utc": "2026-08-11T09:00:00Z"})
    write_index = 0
    for index in range(spans):
        agent = AGENTS[index % len(AGENTS)]
        span_id = f"span{index:04d}aaaa"
        builder.add(
            EventType.SPAN_START,
            agent_id=agent,
            span_id=span_id,
            payload={"kind": "agent_step", "name": f"{agent}-step-{index}"},
        )
        builder.add(
            EventType.LLM_CALL,
            agent_id=agent,
            span_id=span_id,
            payload={
                "model": "llama-3.1-8b-instant",
                "cache_key": f"ck_{index:04d}",
                "prompt_hash": f"blake2b:prompt{index:04d}",
                "response_hash": f"blake2b:resp{index:04d}",
                "cache_status": "hit",
            },
        )
        for _ in range(writes_per_span):
            key = STATE_KEYS[write_index % len(STATE_KEYS)]
            builder.add(
                EventType.STATE_READ,
                agent_id=agent,
                span_id=span_id,
                payload={"key": key, "value_hash": f"blake2b:v{write_index:04d}", "missing": False},
            )
            builder.add(
                EventType.STATE_WRITE,
                agent_id=agent,
                span_id=span_id,
                payload={
                    "key": key,
                    "value_hash": f"blake2b:w{write_index:04d}",
                    "prev_value_hash": None,
                    "reducer": None,
                    "txn_id": None,
                    "lock_id": None,
                },
            )
            write_index += 1
        builder.add(
            EventType.SPAN_END,
            agent_id=agent,
            span_id=span_id,
            payload={
                "status": "ok",
                "duration_virtual_ms": 40,
                "duration_wall_ms": 41,
                "error_type": None,
                "error_message": None,
            },
        )
    if sealed:
        events = builder.events()
        builder.add(
            EventType.RUN_END,
            payload={
                "status": "complete",
                "virtual_makespan_ms": events[-1].virtual_ts_ms + 10,
                "wall_makespan_ms": events[-1].wall_ts_ms + 10,
                "event_count": len(events) + 1,
            },
        )
    return builder.events()


def build_log_of_length(target: int, run_id: str = RUN_ID) -> tuple[Event, ...]:
    """Return a sealed log of at least `target` events, for threshold and snapshot tests.

    Guarantees: length is at least `target`; the exact length is whatever a whole number of
    spans produces, because truncating mid-span would give an invalid log rather than a
    smaller one.
    """
    per_span = 2 + 2 * 2  # span_start, llm_call, (state_read + state_write) * 2, span_end
    spans = max(1, (target - 2) // (per_span + 1) + 1)
    return build_log(spans=spans, run_id=run_id)


def run_record_for(events: Sequence[Event], status: str = "running") -> dict[str, object]:
    """Return the `RunRecord` keyword arguments matching a log's `run_start`.

    Keeping this beside the log builder means a test never invents run metadata that
    contradicts the log it is storing — the two would then disagree in ways that look like
    store bugs.
    """
    first = events[0]
    return {
        "run_id": first.run_id,
        "scenario_hash": "blake2b:scenario",
        "graph_hash": "blake2b:graph",
        "mode": "replay",
        "seed": 42,
        "status": status,
        "created_at": "2026-08-11T09:00:00Z",
        "agentdx_version": "0.1.0",
    }
