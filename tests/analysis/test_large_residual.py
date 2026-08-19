"""PRD §16.2.4: when the residual is large, say so — never absorb it into a bucket.

Reuses the hand-computed 12-event log's event stream unchanged (same `run_start`/`run_end`
`virtual_ts_ms` values, same two tool calls, same message) but declares `run_end.payload`'s
`virtual_makespan_ms` far larger than `run_end.virtual_ts_ms - run_start.virtual_ts_ms` — the
one field `timing.build_timing_dag` trusts as the makespan (PRD §16.1.2's `virtual_makespan`)
rather than deriving it from event timestamps. This is a deliberate, honest reproduction of
exactly the scenario PRD §16.2.4 names: "an un-shimmed provider, an unwrapped tool, an
uninstrumented subgraph" — the runtime's own outer clock says the run took 100ms, but every
event's own `virtual_ts_ms` only accounts for 11ms of it. `critical_path` does not raise
(a *short* critical path relative to makespan is explicitly the normal case, per its own
docstring — only a critical path *longer* than the makespan, `E-ANLZ-003`, is a DAG bug); the
overhead decomposer must not raise either, and must not quietly stuff the missing 89ms into
`blocking_wait` or any other bucket to make the arithmetic look tidy.
"""

from __future__ import annotations

import pytest

from agentdx.analysis.overhead import decompose_critical_path
from agentdx.analysis.timing import build_timing_dag, critical_path
from agentdx.events.schema import Event
from tests.analysis._events import (
    message_recv,
    message_send,
    run_end,
    run_start,
    span_end,
    span_start,
    state_read,
    state_write,
    tool_call,
)

_DECLARED_MAKESPAN_MS = 100  # events only account for 11ms — an 89ms instrumentation gap


def _log_with_uninstrumented_gap() -> list[Event]:
    return [
        run_start(seq=0, virtual_ts_ms=0),
        span_start(
            seq=1,
            virtual_ts_ms=0,
            vclock={"alpha": 1},
            causal_parents=[0],
            agent_id="alpha",
            span_id="T1",
            kind="tool_call",
            name="fetch",
        ),
        state_write(
            seq=2,
            virtual_ts_ms=1,
            vclock={"alpha": 2},
            causal_parents=[1],
            agent_id="alpha",
            span_id="T1",
            key="scratch",
        ),
        tool_call(
            seq=3,
            virtual_ts_ms=2,
            vclock={"alpha": 3},
            causal_parents=[2],
            agent_id="alpha",
            span_id="T1",
            tool="fetch",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=5,
        ),
        span_end(
            seq=4,
            virtual_ts_ms=5,
            vclock={"alpha": 4},
            causal_parents=[3],
            agent_id="alpha",
            span_id="T1",
            duration_virtual_ms=5,
        ),
        message_send(
            seq=5,
            virtual_ts_ms=5,
            vclock={"alpha": 5},
            causal_parents=[4],
            agent_id="alpha",
            span_id="T1",
            message_id="m1",
            to="beta",
        ),
        span_start(
            seq=6,
            virtual_ts_ms=8,
            vclock={"beta": 1},
            causal_parents=[0],
            agent_id="beta",
            span_id="T2",
            kind="tool_call",
            name="respond",
        ),
        message_recv(
            seq=7,
            virtual_ts_ms=8,
            vclock={"alpha": 5, "beta": 2},
            causal_parents=[5, 6],
            agent_id="beta",
            span_id="T2",
            message_id="m1",
            from_="alpha",
            delivered_virtual_ts_ms=8,
        ),
        state_read(
            seq=8,
            virtual_ts_ms=9,
            vclock={"alpha": 5, "beta": 3},
            causal_parents=[7],
            agent_id="beta",
            span_id="T2",
            key="other",
            missing=True,
        ),
        tool_call(
            seq=9,
            virtual_ts_ms=9,
            vclock={"alpha": 5, "beta": 4},
            causal_parents=[8],
            agent_id="beta",
            span_id="T2",
            tool="respond",
            args_hash="blake2b:" + "b" * 64,
            duration_virtual_ms=3,
        ),
        span_end(
            seq=10,
            virtual_ts_ms=11,
            vclock={"alpha": 5, "beta": 5},
            causal_parents=[9],
            agent_id="beta",
            span_id="T2",
            duration_virtual_ms=3,
        ),
        # run_end's own virtual_ts_ms stays 11 (same as the hand-computed log) — only the
        # *declared* virtual_makespan_ms payload field diverges, reproducing an outer clock
        # that ran on past the point event instrumentation stopped recording.
        run_end(
            seq=11,
            virtual_ts_ms=11,
            vclock={"alpha": 5, "beta": 5, "_run": 2},
            causal_parents=[10],
            virtual_makespan_ms=_DECLARED_MAKESPAN_MS,
            event_count=12,
            total_tool_calls=2,
        ),
    ]


def test_uninstrumented_gap_produces_large_honest_residual() -> None:
    events = _log_with_uninstrumented_gap()
    dag = build_timing_dag(events)
    assert dag.virtual_makespan_ms == _DECLARED_MAKESPAN_MS

    cp = critical_path(dag)
    assert cp.length_ms == 11  # unchanged from the hand-computed log — events don't lie

    dec = decompose_critical_path(dag, cp, events)

    # The gap is real and large: 89ms / 100ms = 89%.
    assert dec.residual_ms == _DECLARED_MAKESPAN_MS - 11
    assert dec.residual_fraction == pytest.approx(0.89)
    assert dec.residual_flagged is True

    # The accounting identity still holds exactly — the gap is reported, not hidden.
    assert sum(dec.bucket_ms.values()) + dec.residual_ms == dag.virtual_makespan_ms

    # Design Constraint 3: never absorbed into a bucket. The two node/edge buckets keep
    # exactly their hand-computed values from the un-inflated version of this same log.
    assert dec.bucket_ms["productive_work"] == 8
    assert dec.bucket_ms["handoff"] == 3
    assert dec.bucket_ms["blocking_wait"] == 0
