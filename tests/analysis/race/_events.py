"""A `state_write`/`state_read` builder with the fields `agentdx.analysis.race` tests need.

Extends `tests/analysis/_events.py` rather than forking it: `ev()` and `RUN_ID` are imported
and reused unchanged (that module's own docstring explains why it, in turn, is not the
schema-generic `tests/unit/events/factories.py`). The shared `_events.py::state_write`/
`state_read` hardcode `reducer=None` and one fixed `value_hash` — correct defaults for the
timing/aggregates tests they were written for, wrong for this module's tests, whose entire
subject is value divergence (§14.6) and the guard fields (`reducer`, `lock_id`, `txn_id`,
§14.7). Rather than change the shared builder's defaults (a `tests/analysis/_events.py` edit
is outside this prompt's `DELIVERABLES`), this module adds its own richer `state_write`/
`state_read` here, local to `tests/analysis/race/`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from agentdx.events.schema import Event, EventType, PayloadValue
from tests.analysis._events import RUN_ID, ev
from tests.analysis._events import message_recv as _shared_message_recv
from tests.analysis._events import message_send as _shared_message_send

__all__ = ["RUN_ID", "ev", "message_recv", "message_send", "state_read", "state_write"]


def value_hash(tag: str) -> str:
    """Return a deterministic, distinct `blake2b:`-shaped hash for a short `tag`.

    Not a real hash of `tag` — a legible, collision-free stand-in (`blake2b:<tag padded to 64
    hex chars>`), matching `tests/analysis/_events.py`'s own convention of literal
    `"blake2b:" + "<digit>" * 64` sentinels. Two calls with different `tag`s are always
    unequal; two calls with the same `tag` are always equal — exactly the two properties
    `agentdx.analysis.race`'s value-divergence tests (§14.6) need and nothing more.
    """
    hex_tag = tag.encode("utf-8").hex()
    return "blake2b:" + (hex_tag * ((64 // len(hex_tag)) + 1))[:64]


def state_write(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    key: str,
    value: str = "v",
    reducer: str | None = None,
    lock_id: str | None = None,
    txn_id: str | None = None,
    clock_slot: str | None = None,
    fault_id: str | None = None,
) -> Event:
    """Build one `state_write` event with every §14.7 guard field caller-controlled.

    `value` is hashed via `value_hash()` (see there) — two writes with the same `value` are
    non-divergent (G2 fires); two with different `value`s always diverge.
    """
    payload: dict[str, PayloadValue] = {
        "key": key,
        "value_hash": value_hash(value),
        "prev_value_hash": None,
        "reducer": reducer,
        "txn_id": txn_id,
        "lock_id": lock_id,
    }
    event = ev(
        EventType.STATE_WRITE,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=clock_slot if clock_slot is not None else agent_id,
        span_id=span_id,
        payload=payload,
    )
    if fault_id is not None:
        event = _with_fault_id(event, fault_id)
    return event


def state_read(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    key: str,
    value: str = "v",
    missing: bool = False,
    clock_slot: str | None = None,
    fault_id: str | None = None,
) -> Event:
    """Build one `state_read` event, hashed the same way `state_write` is (see there)."""
    payload: dict[str, PayloadValue] = {
        "key": key,
        "missing": missing,
        "value_hash": value_hash(value),
    }
    event = ev(
        EventType.STATE_READ,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=clock_slot if clock_slot is not None else agent_id,
        span_id=span_id,
        payload=payload,
    )
    if fault_id is not None:
        event = _with_fault_id(event, fault_id)
    return event


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
    """Build one `message_send` event (re-exported from the shared builder, unmodified)."""
    return _shared_message_send(
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        span_id=span_id,
        message_id=message_id,
        to=to,
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
    """Build one `message_recv` event (re-exported from the shared builder, unmodified)."""
    return _shared_message_recv(
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        span_id=span_id,
        message_id=message_id,
        from_=from_,
        delivered_virtual_ts_ms=delivered_virtual_ts_ms,
    )


def _with_fault_id(event: Event, fault_id: str) -> Event:
    """Return a copy of `event` with `fault_id` set.

    `Event` is a frozen dataclass with no `fault_id` constructor knob exposed by `ev()`
    (`tests/analysis/_events.py` — none of its callers have needed one before this module);
    `dataclasses.replace` is the standard, non-mutating way to set it without editing that
    shared builder.
    """
    return replace(event, fault_id=fault_id)
