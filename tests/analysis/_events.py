"""A minimal, fully-explicit `Event` builder for `tests/analysis`'s hand-authored logs.

Deliberately not `tests/unit/events/factories.py`: that module exists to generate
schema-conforming *arbitrary* payloads for property tests over every field in the contract,
and its `make_event` defaults (single clock slot, mechanical `vclock`/`causal_parents`) are
the wrong shape for a log whose whole point is a specific, hand-verifiable timing scenario.
Every event built here has a payload written out in full at the call site, so a reader can
check it against the real event-schema tables in `docs/event-schema.md` without cross-
referencing a generator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agentdx.events.schema import Event, EventType, PayloadValue

RUN_ID = "r_test01"


def ev(
    event_type: EventType,
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    payload: Mapping[str, PayloadValue],
    causal_parents: Sequence[int] = (),
    agent_id: str | None = None,
    clock_slot: str | None = None,
    span_id: str | None = None,
) -> Event:
    """Build one fully-specified `Event`.

    `wall_ts_ms`/`sched_step` mirror `seq` — volatile and never read by any `analysis/` code
    (I11), so their exact value is immaterial here.
    """
    return Event(
        schema_version=1,
        run_id=RUN_ID,
        seq=seq,
        sched_step=seq,
        virtual_ts_ms=virtual_ts_ms,
        wall_ts_ms=seq,
        vclock=dict(vclock),
        type=event_type,
        causal_parents=list(causal_parents),
        payload=dict(payload),
        agent_id=agent_id,
        clock_slot=clock_slot,
        span_id=span_id,
    )


def run_start(*, seq: int, virtual_ts_ms: int) -> Event:
    return ev(
        EventType.RUN_START,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock={"_run": 1},
        causal_parents=(),
        payload={
            "agentdx_version": "0.1.0",
            "cache_mode": "replay",
            "calibration_id": None,
            "delay_schedule_hash": "blake2b:" + "0" * 64,
            "env": {},
            "graph_hash": "blake2b:" + "1" * 64,
            "host": "test",
            "mode": "baseline",
            "model": "test-model",
            "pid": 0,
            "provider_host": "offline",
            "provider_sdk_version": "test",
            "scenario_hash": "blake2b:" + "2" * 64,
            "scenario_id": None,
            "sdk_version": "0.1.0",
            "seed": 0,
            "started_at_utc": "1970-01-01T00:00:00Z",
        },
    )


def run_end(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    virtual_makespan_ms: int,
    event_count: int,
    total_tool_calls: int = 0,
    status: str = "complete",
) -> Event:
    return ev(
        EventType.RUN_END,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        payload={
            "event_count": event_count,
            "status": status,
            "total_completion_tokens": 0,
            "total_llm_calls": 0,
            "total_prompt_tokens": 0,
            "total_tool_calls": total_tool_calls,
            "virtual_makespan_ms": virtual_makespan_ms,
            "wall_makespan_ms": 0,
        },
    )


def span_start(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    kind: str,
    name: str,
    parent_span_id: str | None = None,
    attributes: Mapping[str, PayloadValue] | None = None,
) -> Event:
    return ev(
        EventType.SPAN_START,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=span_id,
        payload={
            "attributes": dict(attributes) if attributes is not None else {},
            "kind": kind,
            "name": name,
            "parent_span_id": parent_span_id,
        },
    )


def span_end(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    duration_virtual_ms: int,
) -> Event:
    return ev(
        EventType.SPAN_END,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=span_id,
        payload={
            "duration_virtual_ms": duration_virtual_ms,
            "duration_wall_ms": 0,
            "error_type": None,
            "error_message": None,
            "status": "ok",
        },
    )


def tool_call(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    tool: str,
    args_hash: str,
    duration_virtual_ms: int,
) -> Event:
    return ev(
        EventType.TOOL_CALL,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=span_id,
        payload={
            "tool": tool,
            "args_hash": args_hash,
            "result_hash": "blake2b:" + "3" * 64,
            "status": "ok",
            "duration_virtual_ms": duration_virtual_ms,
        },
    )


def llm_call(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> Event:
    return ev(
        EventType.LLM_CALL,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=span_id,
        payload={
            "model": "test-model",
            "params_hash": "blake2b:" + "7" * 64,
            "prompt_hash": "blake2b:" + "8" * 64,
            "response_hash": "blake2b:" + "9" * 64,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_status": "hit",
            "cache_key": "blake2b:" + "a" * 64,
            "perturbed_from_run": None,
        },
    )


def message_send(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    message_id: str,
    to: str,
) -> Event:
    return ev(
        EventType.MESSAGE_SEND,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=span_id,
        payload={
            "edge": f"{agent_id}->{to}",
            "message_id": message_id,
            "payload_bytes": 1,
            "payload_hash": "blake2b:" + "4" * 64,
            "to": to,
        },
    )


def message_recv(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    message_id: str,
    from_: str,
    delivered_virtual_ts_ms: int,
) -> Event:
    return ev(
        EventType.MESSAGE_RECV,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=span_id,
        payload={
            "delivered_virtual_ts_ms": delivered_virtual_ts_ms,
            "duplicate": False,
            "edge": f"{from_}->{agent_id}",
            "from": from_,
            "message_id": message_id,
            "reordered": False,
        },
    )


def state_write(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    key: str,
) -> Event:
    return ev(
        EventType.STATE_WRITE,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=span_id,
        payload={
            "key": key,
            "lock_id": None,
            "prev_value_hash": None,
            "reducer": None,
            "txn_id": None,
            "value_hash": "blake2b:" + "5" * 64,
        },
    )


def fault_injected(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    fault_id: str,
    fault_type: str = "latency",
    target: str = "agent_a",
    params: Mapping[str, PayloadValue] | None = None,
    trigger: Mapping[str, PayloadValue] | None = None,
) -> Event:
    return ev(
        EventType.FAULT_INJECTED,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        payload={
            "fault_id": fault_id,
            "fault_type": fault_type,
            "target": target,
            "params": dict(params) if params is not None else {},
            "trigger": dict(trigger) if trigger is not None else {},
        },
    )


def fault_effect(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    fault_id: str,
    effect: str = "delay",
    target: str = "agent_a",
    delay_virtual_ms: int | None = None,
    exception_type: str | None = None,
    message_id: str | None = None,
) -> Event:
    return ev(
        EventType.FAULT_EFFECT,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        payload={
            "fault_id": fault_id,
            "effect": effect,
            "target": target,
            "delay_virtual_ms": delay_virtual_ms,
            "exception_type": exception_type,
            "message_id": message_id,
        },
    )


def assertion_result(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    assertion_id: str,
    kind: str = "success_check",
    passed: bool,
    expected: str | None = None,
    actual: str | None = None,
) -> Event:
    return ev(
        EventType.ASSERTION_RESULT,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        payload={
            "assertion_id": assertion_id,
            "kind": kind,
            "passed": passed,
            "expected": expected,
            "actual": actual,
        },
    )


def state_read(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    key: str,
    missing: bool = False,
) -> Event:
    return ev(
        EventType.STATE_READ,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=span_id,
        payload={
            "key": key,
            "missing": missing,
            "value_hash": "blake2b:" + "6" * 64,
        },
    )
