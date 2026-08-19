"""Hand-built event/turn factories for `explore/`'s unit suite.

Not a `DELIVERABLES` module — test infrastructure only. Every event here is built directly
against `events.schema.Event`, not through a live `Scheduler`, so each unit test's expected
output can be hand-computed from a log the test itself fully controls (AGENTS.md §5).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agentdx.events.schema import Event, EventType, PayloadValue

RUN_ID = "r_expltest"


def schedule_decision(
    *,
    seq: int,
    sched_step: int,
    chosen_task_id: str,
    ready_task_ids: Sequence[str],
    virtual_ready_ts_ms: int = 0,
    reason: str = "initial",
) -> Event:
    """Build one `schedule_decision` event — `EventScope.RUN`, no `agent_id`/`span_id`."""
    return Event(
        schema_version=1,
        run_id=RUN_ID,
        seq=seq,
        sched_step=sched_step,
        virtual_ts_ms=sched_step * 10,
        wall_ts_ms=0,
        vclock={"run": sched_step + 1},
        type=EventType.SCHEDULE_DECISION,
        causal_parents=[seq - 1] if seq > 0 else [],
        payload={
            "chosen_task_id": chosen_task_id,
            "ready_task_ids": list(ready_task_ids),
            "reason": reason,
            "virtual_ready_ts_ms": virtual_ready_ts_ms,
        },
        agent_id=None,
        clock_slot=None,
        span_id=None,
    )


def state_write(
    *,
    seq: int,
    sched_step: int,
    agent_id: str,
    key: str,
    value_hash: str,
    reducer: str | None = None,
    lock_id: str | None = None,
    causal_parents: Sequence[int] = (),
) -> Event:
    """Build one `state_write` event for `agent_id`, at the given `sched_step`."""
    return Event(
        schema_version=1,
        run_id=RUN_ID,
        seq=seq,
        sched_step=sched_step,
        virtual_ts_ms=sched_step * 10,
        wall_ts_ms=0,
        vclock={agent_id: 1},
        type=EventType.STATE_WRITE,
        causal_parents=list(causal_parents),
        payload={
            "key": key,
            "value_hash": value_hash,
            "prev_value_hash": None,
            "reducer": reducer,
            "txn_id": None,
            "lock_id": lock_id,
        },
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=f"span_{agent_id}",
    )


def state_read(
    *,
    seq: int,
    sched_step: int,
    agent_id: str,
    key: str,
    value_hash: str,
    causal_parents: Sequence[int] = (),
) -> Event:
    """Build one `state_read` event for `agent_id`, at the given `sched_step`."""
    return Event(
        schema_version=1,
        run_id=RUN_ID,
        seq=seq,
        sched_step=sched_step,
        virtual_ts_ms=sched_step * 10,
        wall_ts_ms=0,
        vclock={agent_id: 1},
        type=EventType.STATE_READ,
        causal_parents=list(causal_parents),
        payload={"key": key, "value_hash": value_hash, "missing": False},
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=f"span_{agent_id}",
    )


def tool_call(*, seq: int, sched_step: int, agent_id: str, tool: str, args_hash: str) -> Event:
    """Build one `tool_call` event for `agent_id`, at the given `sched_step`."""
    payload: Mapping[str, PayloadValue] = {
        "tool": tool,
        "args_hash": args_hash,
        "result_hash": "rh:0",
        "status": "ok",
        "duration_virtual_ms": 1,
    }
    return Event(
        schema_version=1,
        run_id=RUN_ID,
        seq=seq,
        sched_step=sched_step,
        virtual_ts_ms=sched_step * 10,
        wall_ts_ms=0,
        vclock={agent_id: 1},
        type=EventType.TOOL_CALL,
        causal_parents=[],
        payload=dict(payload),
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=f"span_{agent_id}",
    )
