"""PRD §16.4's per-edge and per-agent aggregates — hand-computed, plus a golden-fixture smoke test.

**Why a hand-authored log, not just the golden fixtures.** None of the three golden fixtures
contain an `llm_call` event (all three are `tool_call`-only logs — confirmed by grepping
`tests/golden/*.jsonl` for `"type": "llm_call"`, which returns nothing), so `agent.tokens`
has no real-fixture coverage available. This log also deliberately avoids any node having more
than one incoming edge: `timing.critical_path`'s `dist[]` recurrence has node-relative edge
weights (`timing.py`'s fix for `E-ANLZ-003`), and by induction `dist[n] == n.end_virtual_ts_ms`
for *any* fully-processed node regardless of which predecessor supplied it — meaning any node
with two or more real predecessors is a guaranteed tie for that node's own `dist`, and the two
sinks in a naive two-agent log (one per agent) are *always* tied for `dist[END]`, since both
independently telescope to `run_end.virtual_ts_ms`. Neither situation is wrong, but a tied
reconstruction depends on the tie-break rule and would make this test's expected `cp.path`
non-obvious on paper. This log instead chains two agents through a single back-and-forth
(`alpha -> beta -> alpha`, the second `alpha` span on its own `clock_slot` so it is not also
paired to the first via `program_order`), giving every node exactly one predecessor and one
unambiguous critical path — see the arithmetic below.

**The log, seq-by-seq, virtual_ts_ms in brackets:**

    0  run_start                                            [0]
    1  span_start  tool_call  T1  (alpha)                    [0]
    2  tool_call   T1  (detail, duration_virtual_ms=4)        [1]
    3  span_end    T1  duration_virtual_ms=4                  [4]
    4  message_send  T1 -> beta  msg=m1                       [4]
    5  span_start  tool_call  T3  (beta)                      [5]
    6  message_recv  T3  from alpha  msg=m1                   [5]
    7  tool_call   T3  (detail, duration_virtual_ms=3)         [6]
    8  span_end    T3  duration_virtual_ms=3                  [8]
    9  message_send  T3 -> alpha  msg=m2                       [8]
    10 span_start  llm_call  T2  (alpha, clock_slot=alpha2)    [9]
    11 message_recv  T2  from beta  msg=m2                     [9]
    12 llm_call    T2  (detail, prompt=100, completion=50)     [10]
    13 span_end    T2  duration_virtual_ms=4                   [13]
    14 run_end     virtual_makespan_ms=13                      [13]

**Hand-computed timing DAG (edges only relevant to this test):**

    START -> T1     run_boundary, weight = T1.start(0) - run_start.vts(0)      = 0
    T1    -> T3     message,      weight = T3.start(5) - T1.end(4)             = 1
    T3    -> T2     message,      weight = T2.start(9) - T3.end(8)             = 1
    T2    -> END    run_boundary, weight = run_end.vts(13) - T2.end(13)        = 0

T1 has no other predecessor than START; T3 has no other predecessor than T1 (message); T2 has
no other predecessor than T3 (message) — its own agent's *first* span, T1, is never linked to
it by `program_order`, because T2 is given a distinct `clock_slot` ("alpha2") specifically so
`timing._build_edges`'s per-slot program-order grouping does not pair the two. T1 and T3 are
each other's only successor too, so neither is a competing sink — T2 is the DAG's one sink.

**Hand-computed critical path:** `START -> T1 -> T3 -> T2 -> END`, unambiguous (no ties).

    dist[T1] = 0 (rb) + 4 (T1 duration)                      = 4
    dist[T3] = dist[T1] + 1 (message) + 3 (T3 duration)      = 8
    dist[T2] = dist[T3] + 1 (message) + 4 (T2 duration)      = 13
    dist[END] = dist[T2] + 0 (rb)                            = 13

`critical_path_length_ms = 13 == virtual_makespan_ms`, residual `0`. `cp_node_ids = {T1, T3, T2}`
— every node in this log is on the critical path.

**Hand-computed `EdgeAggregate`s.** Both message edges are literal-handoff exactly `1ms`
(`recv.vts - send.vts`: `5 - 4` and `9 - 8`), and both are the only message edge between their
respective agent pairs, and both are the actual critical-path edge for their hop:

    (alpha -> beta): message_count=1, total_handoff_ms=1, cp_handoff_ms=1, cp_share=1/13
    (beta -> alpha): message_count=1, total_handoff_ms=1, cp_handoff_ms=1, cp_share=1/13

**Hand-computed `AgentAggregate`s.**

    alpha: nodes = {T1 (0-4), T2 (9-13)}. busy_ms = 4+4 = 8.
           window = [min(0), max(13)] = [0, 13] -> idle_ms = (13-0) - 8 = 5.
           cp_ms = 8 (both T1, T2 on the critical path). tokens = 100+50 = 150 (T2's llm_call).
    beta:  nodes = {T3 (5-8)}. busy_ms = 3. window = [5, 8] -> idle_ms = (8-5) - 3 = 0.
           cp_ms = 3 (T3 on the critical path). tokens = 0 (beta never calls an LLM here).
"""

from __future__ import annotations

from pathlib import Path

from agentdx.analysis.aggregates import compute_agent_aggregates, compute_edge_aggregates
from agentdx.analysis.timing import build_timing_dag, critical_path
from agentdx.events.canonical import decode_event
from agentdx.events.schema import Event
from tests.analysis._events import (
    llm_call,
    message_recv,
    message_send,
    run_end,
    run_start,
    span_end,
    span_start,
    tool_call,
)

_GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"


def _load(name: str) -> list[Event]:
    path = _GOLDEN_DIR / f"{name}.jsonl"
    with path.open(encoding="utf-8") as f:
        return [decode_event(line) for line in f]


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
        tool_call(
            seq=2,
            virtual_ts_ms=1,
            vclock={"alpha": 2},
            causal_parents=[1],
            agent_id="alpha",
            span_id="T1",
            tool="fetch",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=4,
        ),
        span_end(
            seq=3,
            virtual_ts_ms=4,
            vclock={"alpha": 3},
            causal_parents=[2],
            agent_id="alpha",
            span_id="T1",
            duration_virtual_ms=4,
        ),
        message_send(
            seq=4,
            virtual_ts_ms=4,
            vclock={"alpha": 4},
            causal_parents=[3],
            agent_id="alpha",
            span_id="T1",
            message_id="m1",
            to="beta",
        ),
        span_start(
            seq=5,
            virtual_ts_ms=5,
            vclock={"beta": 1},
            causal_parents=[0],
            agent_id="beta",
            span_id="T3",
            kind="tool_call",
            name="handle",
        ),
        message_recv(
            seq=6,
            virtual_ts_ms=5,
            vclock={"alpha": 4, "beta": 2},
            causal_parents=[4],
            agent_id="beta",
            span_id="T3",
            message_id="m1",
            from_="alpha",
            delivered_virtual_ts_ms=5,
        ),
        tool_call(
            seq=7,
            virtual_ts_ms=6,
            vclock={"alpha": 4, "beta": 3},
            causal_parents=[6],
            agent_id="beta",
            span_id="T3",
            tool="handle",
            args_hash="blake2b:" + "b" * 64,
            duration_virtual_ms=3,
        ),
        span_end(
            seq=8,
            virtual_ts_ms=8,
            vclock={"alpha": 4, "beta": 4},
            causal_parents=[7],
            agent_id="beta",
            span_id="T3",
            duration_virtual_ms=3,
        ),
        message_send(
            seq=9,
            virtual_ts_ms=8,
            vclock={"alpha": 4, "beta": 5},
            causal_parents=[8],
            agent_id="beta",
            span_id="T3",
            message_id="m2",
            to="alpha",
        ),
        span_start(
            seq=10,
            virtual_ts_ms=9,
            vclock={"alpha": 5, "beta": 5},
            causal_parents=[0],
            agent_id="alpha",
            span_id="T2",
            kind="llm_call",
            name="respond",
        ),
        # T2 is deliberately given its own clock_slot, distinct from T1's ("alpha") — see the
        # module docstring for why: it keeps T2's only predecessor the message edge from T3,
        # rather than also being paired to T1 by program_order and creating a dist[] tie.
        message_recv(
            seq=11,
            virtual_ts_ms=9,
            vclock={"alpha": 6, "beta": 5},
            causal_parents=[9],
            agent_id="alpha",
            span_id="T2",
            message_id="m2",
            from_="beta",
            delivered_virtual_ts_ms=9,
        ),
        llm_call(
            seq=12,
            virtual_ts_ms=10,
            vclock={"alpha": 7, "beta": 5},
            causal_parents=[11],
            agent_id="alpha",
            span_id="T2",
            prompt_tokens=100,
            completion_tokens=50,
        ),
        span_end(
            seq=13,
            virtual_ts_ms=13,
            vclock={"alpha": 8, "beta": 5},
            causal_parents=[12],
            agent_id="alpha",
            span_id="T2",
            duration_virtual_ms=4,
        ),
        run_end(
            seq=14,
            virtual_ts_ms=13,
            vclock={"alpha": 8, "beta": 5, "_run": 2},
            causal_parents=[13],
            virtual_makespan_ms=13,
            event_count=15,
            total_tool_calls=2,
        ),
    ]


def _log_with_distinct_clock_slot() -> list[Event]:
    """Patch T2's `clock_slot` to `"alpha2"` — `_events.span_start` has no parameter for it.

    `_events.py`'s builders always set `clock_slot=agent_id`, matching every other test in this
    package; this is the one log in `tests/analysis/` that needs a node's `clock_slot` to
    diverge from its `agent_id`, so the substitution is done here rather than growing every
    builder a rarely-used parameter.
    """
    from dataclasses import replace

    events = _log()
    return [replace(e, clock_slot="alpha2") if e.span_id == "T2" else e for e in events]


def test_hand_computed_edge_aggregates() -> None:
    events = _log_with_distinct_clock_slot()
    dag = build_timing_dag(events)
    cp = critical_path(dag)
    assert cp.length_ms == 13
    assert cp.length_ms == dag.virtual_makespan_ms

    edges = {(e.src_agent_id, e.dst_agent_id): e for e in compute_edge_aggregates(dag, cp, events)}
    assert set(edges) == {("alpha", "beta"), ("beta", "alpha")}

    forward = edges[("alpha", "beta")]
    assert forward.message_count == 1
    assert forward.total_handoff_ms == 1
    assert forward.cp_handoff_ms == 1
    assert forward.cp_share == 1 / 13

    backward = edges[("beta", "alpha")]
    assert backward.message_count == 1
    assert backward.total_handoff_ms == 1
    assert backward.cp_handoff_ms == 1
    assert backward.cp_share == 1 / 13


def test_hand_computed_agent_aggregates() -> None:
    events = _log_with_distinct_clock_slot()
    dag = build_timing_dag(events)
    cp = critical_path(dag)

    agents = {a.agent_id: a for a in compute_agent_aggregates(dag, cp, events)}
    assert set(agents) == {"alpha", "beta"}

    alpha = agents["alpha"]
    assert alpha.busy_ms == 8
    assert alpha.idle_ms == 5
    assert alpha.cp_ms == 8
    assert alpha.tokens == 150

    beta = agents["beta"]
    assert beta.busy_ms == 3
    assert beta.idle_ms == 0
    assert beta.cp_ms == 3
    assert beta.tokens == 0


def test_every_aggregate_traces_to_evidence_seq() -> None:
    """I6: every edge and agent aggregate carries the seqs that justify it."""
    events = _log_with_distinct_clock_slot()
    dag = build_timing_dag(events)
    cp = critical_path(dag)

    for edge in compute_edge_aggregates(dag, cp, events):
        assert edge.evidence_seq
        assert all(isinstance(s, int) for s in edge.evidence_seq)
    for agent in compute_agent_aggregates(dag, cp, events):
        assert agent.evidence_seq
        assert all(isinstance(s, int) for s in agent.evidence_seq)


def test_support_triage_edge_message_counts_match_the_fixture() -> None:
    """Smoke test against real data: 4 known agent pairs, one message each (fixture inspection)."""
    events = _load("support_triage")
    dag = build_timing_dag(events)
    cp = critical_path(dag)

    edges = {(e.src_agent_id, e.dst_agent_id): e for e in compute_edge_aggregates(dag, cp, events)}
    assert set(edges) == {
        ("classifier", "retriever_a"),
        ("classifier", "retriever_b"),
        ("retriever_a", "responder"),
        ("retriever_b", "responder"),
    }
    for edge in edges.values():
        assert edge.message_count == 1
        assert edge.total_handoff_ms >= 0
        assert 0.0 <= edge.cp_share <= 1.0


def test_golden_fixtures_have_no_llm_calls_so_tokens_are_zero() -> None:
    """None of the three golden fixtures contain an `llm_call` event — confirmed, not assumed."""
    for name in ("code_pipeline", "research_fanout", "support_triage"):
        events = _load(name)
        dag = build_timing_dag(events)
        cp = critical_path(dag)
        for agent in compute_agent_aggregates(dag, cp, events):
            assert agent.tokens == 0


def test_agent_cp_ms_never_exceeds_busy_ms() -> None:
    """A structural sanity check across all three real fixtures — not hand-computed per value."""
    for name in ("code_pipeline", "research_fanout", "support_triage"):
        events = _load(name)
        dag = build_timing_dag(events)
        cp = critical_path(dag)
        for agent in compute_agent_aggregates(dag, cp, events):
            assert agent.cp_ms <= agent.busy_ms
            assert agent.idle_ms >= 0
