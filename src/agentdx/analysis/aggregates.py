r"""Per-edge and per-agent aggregates (PRD §16.4) over a built `TimingDAG`.

PRD §16.4 defines eight metrics "for the graph panel and the verdict's recommendation":
`edge.message_count`, `edge.total_handoff_ms`, `edge.cp_handoff_ms`, `edge.cp_share`,
`agent.busy_ms`, `agent.idle_ms`, `agent.cp_ms`, `agent.tokens`. This module computes all
eight as two small, pure functions over an already-built `TimingDAG` and a `CriticalPathResult`
— it adds no new graph-construction logic and does not change `timing.py`'s or `overhead.py`'s
output.

**"The edge", disambiguated.** §16.4's table is written for the graph panel, which shows one
edge per *agent pair* in the visual agent graph — not one row per `TimingEdge` in the internal
timing DAG (a single agent pair can exchange many messages across a run, each its own
`TimingEdge`). This module aggregates every `message`-kind `TimingEdge` between the same
`(src_agent_id, dst_agent_id)` ordered pair into one `EdgeAggregate`. Edges whose endpoints
cannot be attributed to a real agent (`TimingNode.agent_id is None` — legal per the event
schema) are excluded rather than attributed to a fabricated key (I9).

**`handoff`, reused from §16.2.2, not redefined.** `edge.total_handoff_ms`/`cp_handoff_ms` use
the same literal `recv.virtual_ts - send.virtual_ts` formula `overhead.py`'s `handoff` bucket
uses (PRD §16.2.2) — not `timing.py`'s node-relative `weight_ms` (see that module's docstring
for why the two differ). The formula is duplicated here rather than imported from `overhead.py`
to keep this module's only dependency on `timing.py`, matching `redundancy.py`'s precedent for
why small, pure per-module helpers are re-derived across sibling `analysis/` files rather than
cross-imported through a private name.

**Critical-path edge selection matches `overhead.py`'s own rule exactly.** For each consecutive
hop `(a, b)` in `cp.path`, the edge actually charged to the critical path is
`next(e for e in dag.edges[a] if e.dst == b)` — `overhead.decompose_critical_path`'s own
selection rule (its edges are pre-sorted by `(dst, kind)`, so this is deterministic). A message
edge only counts toward `cp_handoff_ms` if it is *that* edge, not merely any message edge
sharing the same endpoints — reusing a different selection rule here would silently disagree
with what `overhead.py` already reports as the critical path's own handoff bucket.

**`agent.busy_ms`/`idle_ms` — a documented interpretive call (CONTEXT.md C-20).** PRD §16.4
names these "Occupancy" with no formula. This module reads an agent's occupancy window as
`[min(start of its own nodes), max(end of its own nodes)]` — the span during which the agent
was observably in play — and defines `idle_ms` as whatever of that window `busy_ms` (the sum of
the agent's own node durations, over the whole DAG, not just the critical path) does not cover.
An agent active in only one contiguous span has `idle_ms == 0` by construction; a genuine
between-turns gap inside that window is what registers as idle. Time before an agent's first
node or after its last is not counted as anything for that agent — it is not idle (the agent
was not yet, or no longer, part of the run) and not busy.

**`agent.tokens`.** Summed from `llm_call.payload.prompt_tokens` + `completion_tokens`
(PRD §9's event schema) for every `llm_call` event whose `agent_id` matches, across the whole
log — not just the critical path, matching total-work's own denominator convention in
`overhead.py`. None of this build's three golden fixtures contain an `llm_call` event (all
three are `tool_call`-only logs), so this is exercised by a hand-authored test, not a fixture
smoke test — see `tests/analysis/test_aggregates.py`.

**I3 purity.** Imports only `agentdx.analysis.timing` and `agentdx.events`, plus the standard
library.

**Determinism (NFR-14).** Both returned tuples are sorted by their own key tuple
(`(src_agent_id, dst_agent_id)` / `agent_id`); every accumulation iterates `sorted(dag.nodes)`
or `dag.edges` (already deterministic per `timing.py`); no bare `set` iteration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

from agentdx.analysis.timing import END_NODE, START_NODE, CriticalPathResult, TimingDAG, TimingEdge
from agentdx.events.schema import Event, EventType

_MESSAGE_KIND: Final = "message"


@dataclass(frozen=True, slots=True)
class EdgeAggregate:
    """PRD §16.4's per-agent-pair edge aggregate, over every `message` edge between the two.

    Guarantees: `message_count >= 1` (a pair with zero messages is never reported);
    `cp_handoff_ms <= total_handoff_ms`; `cp_share = cp_handoff_ms / virtual_makespan_ms`
    (`0.0` when the makespan is `0`); `evidence_seq` is every contributing `message_send`/
    `message_recv` seq, sorted ascending (I6).
    """

    src_agent_id: str
    dst_agent_id: str
    message_count: int
    total_handoff_ms: int
    cp_handoff_ms: int
    cp_share: float
    evidence_seq: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AgentAggregate:
    """PRD §16.4's per-agent aggregate.

    Guarantees: `busy_ms + idle_ms` is the agent's own occupancy window (see module docstring);
    `cp_ms <= busy_ms`; `tokens >= 0`; `evidence_seq` is every node's `evidence_seq` plus every
    contributing `llm_call` seq, sorted ascending (I6).
    """

    agent_id: str
    busy_ms: int
    idle_ms: int
    cp_ms: int
    tokens: int
    evidence_seq: tuple[int, ...]


def _raw_handoff_ms(edge: TimingEdge, events_by_seq: Mapping[int, Event]) -> int:
    """Return PRD §16.2.2's literal `recv.virtual_ts - send.virtual_ts` for a `message` edge.

    Duplicated from `overhead._raw_handoff_ms` — see the module docstring for why. `edge`'s
    `evidence_seq` for a `message` edge is exactly `(send.seq, recv.seq)` sorted, per
    `timing._build_edges`'s message-causality block, so both events always resolve.
    """
    send_seq, recv_seq = min(edge.evidence_seq), max(edge.evidence_seq)
    send = events_by_seq[send_seq]
    recv = events_by_seq[recv_seq]
    return max(0, recv.virtual_ts_ms - send.virtual_ts_ms)


def _critical_path_edges(
    dag: TimingDAG, cp: CriticalPathResult
) -> dict[tuple[str, str], TimingEdge]:
    """Return `{(src, dst): edge}` for every real hop `cp.path` actually crosses.

    Mirrors `overhead.decompose_critical_path`'s own edge-selection rule exactly (see module
    docstring) — `START_NODE`/`END_NODE` boundary hops are skipped, since a `run_boundary`
    edge is never a `message` edge and therefore never contributes to an `EdgeAggregate`.
    """
    out: dict[tuple[str, str], TimingEdge] = {}
    for src, dst in pairwise(cp.path):
        if src in (START_NODE, END_NODE) and dst in (START_NODE, END_NODE):
            continue
        edge = next((e for e in dag.edges.get(src, ()) if e.dst == dst), None)
        if edge is not None:
            out[(src, dst)] = edge
    return out


def compute_edge_aggregates(
    dag: TimingDAG, cp: CriticalPathResult, events: Sequence[Event]
) -> tuple[EdgeAggregate, ...]:
    """Return PRD §16.4's edge aggregates, one per `(src_agent_id, dst_agent_id)` pair.

    Args:
        dag: The timing DAG to aggregate over.
        cp: `timing.critical_path(dag)`'s result, for `cp_handoff_ms`/`cp_share`.
        events: The same sealed log `dag` was built from — needed to resolve `message` edges'
            raw send/recv timestamps for the literal `handoff` formula (I6).

    Returns:
        Aggregates sorted by `(src_agent_id, dst_agent_id)`. Deterministic (NFR-14).
    """
    events_by_seq = {e.seq: e for e in events}
    cp_edges = _critical_path_edges(dag, cp)

    totals: dict[tuple[str, str], list[int]] = {}  # key -> [message_count, total_ms, cp_ms]
    evidence: dict[tuple[str, str], list[int]] = {}

    for src in sorted(dag.edges):
        for edge in dag.edges[src]:
            if edge.kind != _MESSAGE_KIND:
                continue
            src_node = dag.nodes.get(edge.src)
            dst_node = dag.nodes.get(edge.dst)
            if src_node is None or dst_node is None:
                continue
            src_agent, dst_agent = src_node.agent_id, dst_node.agent_id
            if src_agent is None or dst_agent is None:
                continue  # cannot attribute to a real agent pair — never fabricated (I9)

            key = (src_agent, dst_agent)
            handoff = _raw_handoff_ms(edge, events_by_seq)
            on_cp = cp_edges.get((edge.src, edge.dst)) == edge

            bucket = totals.setdefault(key, [0, 0, 0])
            bucket[0] += 1
            bucket[1] += handoff
            if on_cp:
                bucket[2] += handoff
            evidence.setdefault(key, []).extend(edge.evidence_seq)

    makespan = dag.virtual_makespan_ms
    result = []
    for key in sorted(totals):
        count, total_ms, cp_ms = totals[key]
        share = (cp_ms / makespan) if makespan > 0 else 0.0
        result.append(
            EdgeAggregate(
                src_agent_id=key[0],
                dst_agent_id=key[1],
                message_count=count,
                total_handoff_ms=total_ms,
                cp_handoff_ms=cp_ms,
                cp_share=share,
                evidence_seq=tuple(sorted(set(evidence[key]))),
            )
        )
    return tuple(result)


def compute_agent_aggregates(
    dag: TimingDAG, cp: CriticalPathResult, events: Sequence[Event]
) -> tuple[AgentAggregate, ...]:
    """Return PRD §16.4's per-agent aggregates, one per distinct `agent_id` in `dag.nodes`.

    Args:
        dag: The timing DAG to aggregate over.
        cp: `timing.critical_path(dag)`'s result, for `cp_ms`.
        events: The same sealed log `dag` was built from — needed only to sum `llm_call`
            token fields per agent (I6).

    Returns:
        Aggregates sorted by `agent_id`. Deterministic (NFR-14).
    """
    cp_node_ids = frozenset(cp.path) - frozenset((START_NODE, END_NODE))

    nodes_by_agent: dict[str, list[str]] = {}
    for node_id in sorted(dag.nodes):
        agent_id = dag.nodes[node_id].agent_id
        if agent_id is not None:
            nodes_by_agent.setdefault(agent_id, []).append(node_id)

    tokens_by_agent: dict[str, int] = {}
    token_evidence: dict[str, list[int]] = {}
    for event in events:
        if event.type is not EventType.LLM_CALL or event.agent_id is None:
            continue
        prompt = event.payload.get("prompt_tokens")
        completion = event.payload.get("completion_tokens")
        prompt_n = prompt if isinstance(prompt, int) and not isinstance(prompt, bool) else 0
        completion_n = (
            completion if isinstance(completion, int) and not isinstance(completion, bool) else 0
        )
        tokens_by_agent[event.agent_id] = (
            tokens_by_agent.get(event.agent_id, 0) + prompt_n + completion_n
        )
        token_evidence.setdefault(event.agent_id, []).append(event.seq)

    result = []
    for agent_id in sorted(nodes_by_agent):
        node_ids = nodes_by_agent[agent_id]
        nodes = [dag.nodes[nid] for nid in node_ids]
        busy_ms = sum(n.duration_ms for n in nodes)
        window_start = min(n.start_virtual_ts_ms for n in nodes)
        window_end = max(n.end_virtual_ts_ms for n in nodes)
        idle_ms = max(0, (window_end - window_start) - busy_ms)
        cp_ms = sum(n.duration_ms for n in nodes if n.node_id in cp_node_ids)

        evidence_seq = sorted(
            {seq for n in nodes for seq in n.evidence_seq} | set(token_evidence.get(agent_id, ()))
        )
        result.append(
            AgentAggregate(
                agent_id=agent_id,
                busy_ms=busy_ms,
                idle_ms=idle_ms,
                cp_ms=cp_ms,
                tokens=tokens_by_agent.get(agent_id, 0),
                evidence_seq=tuple(evidence_seq),
            )
        )
    return tuple(result)


__all__ = [
    "AgentAggregate",
    "EdgeAggregate",
    "compute_agent_aggregates",
    "compute_edge_aggregates",
]
