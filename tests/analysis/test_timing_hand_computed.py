"""A 12-event log with the critical path computed by hand (DEFINITION OF DONE).

Two agents, `alpha` and `beta`, each with one `tool_call` leaf span (no `agent_step`
wrapper — a leaf span with no parent is legal, and keeping this log to exactly 12 events
means every number below can be checked on paper without a scheduler to run). `alpha`
finishes its tool call, sends a message, and `beta` receives it and does its own tool call.
A `state_write`/`state_read` pair is included on unrelated keys specifically so it does
*not* create a data-dependency edge — proof that an unmatched key is correctly a no-op,
not an accident of the log being too simple to exercise that path.

**The log, seq-by-seq, virtual_ts_ms in brackets:**

    0  run_start                                    [0]
    1  span_start  tool_call  T1  (alpha)            [0]
    2  state_write T1  key=scratch                   [1]
    3  tool_call   T1  (detail)                       [2]
    4  span_end    T1  duration_virtual_ms=5          [5]
    5  message_send  T1 -> beta  msg=m1               [5]
    6  span_start  tool_call  T2  (beta)              [8]
    7  message_recv  T2  from alpha  msg=m1           [8]
    8  state_read  T2  key=other (no matching write)  [9]
    9  tool_call   T2  (detail)                        [9]
    10 span_end    T2  duration_virtual_ms=3          [11]
    11 run_end     virtual_makespan_ms=11             [11]

**Hand-computed timing DAG:**

- Two leaf nodes: `T1` (kind=tool_call, start=0, duration=5, end=5) and `T2` (kind=tool_call,
  start=8, duration=3, end=11). Neither has a parent span, so neither is decomposed into
  `agent_step_segment`s — the DAG has exactly these two real nodes.
- `state_write`(key=scratch) has no `state_read` of "scratch" anywhere in the log, so it
  produces no `data_dependency` edge. `state_read`(key=other) has no matching write, so it
  produces no edge either (`writes_by_key.get("other", [])` is empty). Zero data-dependency
  edges in this log — checked explicitly below.
- One `message` edge `T1 -> T2`, weight = `T2.start_virtual_ts_ms(8) - T1.end_virtual_ts_ms(5)
  = 3`ms (node-relative, per `timing._build_edges`'s fix for `E-ANLZ-003`).
- Run-boundary edges: `START -> T1` weight = `T1.start(0) - run_start.vts(0) = 0`.
  `T2 -> END` weight = `run_end.vts(11) - T2.end(11) = 0`.

**Hand-computed critical path:** `START -> T1 -> T2 -> END`.

    dist[START] = 0
    dist[T1]    = dist[START] + 0 (run_boundary) + 5 (T1's own duration)      = 5
    dist[T2]    = dist[T1]    + 3 (message T1->T2) + 3 (T2's own duration)    = 11
    dist[END]   = dist[T2]    + 0 (run_boundary)                              = 11

`critical_path_length_ms = 11`, which equals `virtual_makespan_ms = 11` exactly (run_end.vts
- run_start.vts = 11 - 0 = 11, matching the declared `virtual_makespan_ms` payload field) —
this log has zero residual, on paper, before any code runs.

**Hand-computed overhead decomposition:** neither agent has an orchestrator/router role (no
`agent_step` span exists to declare one, so both nodes' `role` is `None`), so both `T1` and
`T2` are `productive_work` (8ms total). The one `message` edge's literal PRD handoff formula
(`recv.vts(8) - send.vts(5) = 3`) exactly equals the edge's node-relative `weight_ms` (also
`3`, computed above) — no remainder, so all 3ms go to `handoff`, none to `blocking_wait`.
Buckets: `productive_work=8, handoff=3`, everything else `0`, `residual=0`.
"""

from __future__ import annotations

from agentdx.analysis.overhead import decompose_critical_path
from agentdx.analysis.timing import END_NODE, START_NODE, build_timing_dag, critical_path
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


def _log() -> list[Event]:
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
        run_end(
            seq=11,
            virtual_ts_ms=11,
            vclock={"alpha": 5, "beta": 5, "_run": 2},
            causal_parents=[10],
            virtual_makespan_ms=11,
            event_count=12,
            total_tool_calls=2,
        ),
    ]


def test_log_is_exactly_twelve_events() -> None:
    assert len(_log()) == 12


def test_no_data_dependency_edge_from_unmatched_keys() -> None:
    dag = build_timing_dag(_log())
    kinds = {e.kind for edges in dag.edges.values() for e in edges}
    assert "data_dependency" not in kinds


def test_hand_computed_critical_path() -> None:
    dag = build_timing_dag(_log())
    assert dag.virtual_makespan_ms == 11

    cp = critical_path(dag)
    assert cp.path == (START_NODE, "T1", "T2", END_NODE)
    assert cp.length_ms == 11
    assert cp.length_ms == dag.virtual_makespan_ms


def test_hand_computed_overhead_decomposition() -> None:
    dag = build_timing_dag(_log())
    cp = critical_path(dag)
    dec = decompose_critical_path(dag, cp, _log())

    assert dec.bucket_ms["productive_work"] == 8
    assert dec.bucket_ms["handoff"] == 3
    assert dec.bucket_ms["blocking_wait"] == 0
    assert dec.bucket_ms["orchestration"] == 0
    assert dec.bucket_ms["redundant_work"] == 0
    assert dec.bucket_ms["retry_recovery"] == 0
    assert dec.residual_ms == 0
    assert sum(dec.bucket_ms.values()) + dec.residual_ms == dag.virtual_makespan_ms
