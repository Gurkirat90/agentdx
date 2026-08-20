"""`GET /runs/{id}/graph` · `/waterfall` · `/state` · `/exploration` (PRD §26.1).

Graph and waterfall are genuinely computed on request, not read from a precomputed table
(PRD §26.3's own performance table does not mark either "Precomputed", unlike `run` and
`findings`) — this module calls `analysis.timing`/`analysis.aggregates`, which already do
the real work, and serialises. `state` reads `store.snapshots.state_at`, itself already a
thin call. `exploration` is the one endpoint in this file that is honestly not backed by
anything yet — see its own docstring.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from agentdx.analysis.aggregates import compute_agent_aggregates, compute_edge_aggregates
from agentdx.analysis.timing import TimingNode, build_timing_dag, critical_path
from agentdx.api.deps import get_store
from agentdx.api.errors import NotYetAvailableError, RunNotFoundError
from agentdx.api.models import (
    ExplorationResponse,
    GraphEdge,
    GraphNode,
    GraphResponse,
    StateKey,
    StateResponse,
    WaterfallLane,
    WaterfallResponse,
    WaterfallSpan,
)
from agentdx.events.schema import Event, EventType
from agentdx.store.snapshots import state_at
from agentdx.store.sqlite import Store

router = APIRouter(prefix="/runs/{run_id}", tags=["analysis"])


def _require_run(store: Store, run_id: str) -> None:
    if store.get_run(run_id) is None:
        raise RunNotFoundError(run_id)


def _span_agent_role(nodes: Mapping[str, TimingNode]) -> dict[str, str | None]:
    """Return one representative `role` per `agent_id`, from that agent's own nodes.

    An agent's role is declared once per `agent_step` span (PRD §6.1) and is not expected to
    change mid-run; the first node found for the agent, in `nodes`' iteration order (already
    deterministic — `TimingDAG.nodes` is built in a fixed order, NFR-14), is representative.
    """
    roles: dict[str, str | None] = {}
    for node in nodes.values():
        if node.agent_id is not None and node.agent_id not in roles:
            roles[node.agent_id] = node.role
    return roles


def _span_end_status(events: list[Event]) -> dict[str, str]:
    """Return `{span_id: status}` from every `span_end` event's payload (PRD §9.5)."""
    out: dict[str, str] = {}
    for event in events:
        if event.type is EventType.SPAN_END and event.span_id is not None:
            status = event.payload.get("status")
            if isinstance(status, str):
                out[event.span_id] = status
    return out


def _agent_status(events: list[Event]) -> dict[str, str]:
    """Return `{agent_id: "ok"|"error"}` — "error" if any of that agent's spans ended non-ok."""
    per_span = _span_end_status(events)
    by_span_agent = {
        e.span_id: e.agent_id
        for e in events
        if e.type is EventType.SPAN_START and e.span_id is not None
    }
    out: dict[str, str] = {}
    for span_id, status in per_span.items():
        agent_id = by_span_agent.get(span_id)
        if agent_id is None:
            continue
        if status != "ok":
            out[agent_id] = "error"
        else:
            out.setdefault(agent_id, "ok")
    return out


def _span_names(events: list[Event]) -> dict[str, str | None]:
    """Return `{span_id: name}` from every `span_start` event's payload."""
    out: dict[str, str | None] = {}
    for event in events:
        if event.type is EventType.SPAN_START and event.span_id is not None:
            name = event.payload.get("name")
            out[event.span_id] = name if isinstance(name, str) else None
    return out


@router.get("/graph", response_model=GraphResponse, summary="Node/edge graph (PRD §26.1)")
def get_graph(
    run_id: str,
    store: Annotated[Store, Depends(get_store)],
    at_virtual_ts: Annotated[
        int | None,
        Query(description="Restrict to nodes that had started by this virtual ms (PRD §26.1)."),
    ] = None,
) -> GraphResponse:
    """Return nodes, edges and the critical path, computed live from the sealed event log.

    Raises:
        RunNotFoundError: `E-RUN-404`.
        TimingAnalysisError / OverheadAnalysisError: `409` — the log is not yet in a shape
            this analysis can run over (PRD §36 `E-ANLZ-*`; most commonly, the run has not
            been sealed and still has an open span).

    `at_virtual_ts`, when given, restricts the *reported* nodes/edges to ones whose own
    activity had started by that instant; the critical path and `cp_share`/`cp_ms` figures
    are still computed over the *full* sealed run and only filtered to the surviving node
    ids — this is a simplification of PRD §26.1's "graph state as of that instant", declared
    in `docs/api.md`, not a full historical re-derivation of what the critical path *would
    have been* had the run stopped at that instant.
    """
    _require_run(store, run_id)
    events = list(store.read_events(run_id))
    dag = build_timing_dag(events)
    cp = critical_path(dag)
    edge_aggs = compute_edge_aggregates(dag, cp, events)
    agent_aggs = compute_agent_aggregates(dag, cp, events)
    roles = _span_agent_role(dag.nodes)
    statuses = _agent_status(events)

    included_agents = {
        agg.agent_id
        for agg in agent_aggs
        if at_virtual_ts is None
        or any(
            n.agent_id == agg.agent_id and n.start_virtual_ts_ms <= at_virtual_ts
            for n in dag.nodes.values()
        )
    }

    nodes = tuple(
        GraphNode(
            id=agg.agent_id,
            role=roles.get(agg.agent_id),
            busy_ms=agg.busy_ms,
            idle_ms=agg.idle_ms,
            cp_ms=agg.cp_ms,
            tokens=agg.tokens,
            status=statuses.get(agg.agent_id, "ok"),
        )
        for agg in agent_aggs
        if agg.agent_id in included_agents
    )
    edges = tuple(
        GraphEdge(
            from_=e.src_agent_id,
            to=e.dst_agent_id,
            messages=e.message_count,
            total_handoff_ms=e.total_handoff_ms,
            cp_handoff_ms=e.cp_handoff_ms,
            cp_share=e.cp_share,
            on_critical_path=e.cp_handoff_ms > 0,
        )
        for e in edge_aggs
        if e.src_agent_id in included_agents and e.dst_agent_id in included_agents
    )
    critical_path_agents = tuple(
        dict.fromkeys(
            n.agent_id
            for node_id in cp.path
            if (n := dag.nodes.get(node_id)) is not None and n.agent_id is not None
        )
    )
    return GraphResponse(nodes=nodes, edges=edges, critical_path=critical_path_agents)


@router.get("/waterfall", response_model=WaterfallResponse, summary="Span waterfall (PRD §26.1)")
def get_waterfall(
    run_id: str,
    store: Annotated[Store, Depends(get_store)],
    from_ms: Annotated[int | None, Query(description="Window start, virtual ms.")] = None,
    to_ms: Annotated[int | None, Query(description="Window end, virtual ms.")] = None,
) -> WaterfallResponse:
    """Return per-agent lanes of spans with virtual start/end and critical-path flags.

    Raises:
        RunNotFoundError: `E-RUN-404`.
        TimingAnalysisError: `409` — see `get_graph`.

    `bucket` (PRD §16.2.2's six-way overhead classification) is reported as `null` on every
    span: `analysis.overhead` computes and aggregates bucket *totals*
    (`OverheadDecomposition.bucket_ms`) but exposes no public per-node classification to
    project back onto an individual span — only its private `_classify_node` does, and this
    layer does not reach into another module's private surface. This is a real, declared
    gap (`docs/api.md` "What GET .../waterfall does not yet do"), not a silent omission.
    """
    _require_run(store, run_id)
    events = list(store.read_events(run_id))
    dag = build_timing_dag(events)
    cp = critical_path(dag)
    on_cp = frozenset(cp.path)
    names = _span_names(events)
    statuses = _span_end_status(events)

    lanes: dict[str, list[WaterfallSpan]] = {}
    for node_id, node in dag.nodes.items():
        if node.span_id is None or node.agent_id is None:
            continue  # START/END sentinels and decomposed agent_step_segment nodes
        if from_ms is not None and node.end_virtual_ts_ms < from_ms:
            continue
        if to_ms is not None and node.start_virtual_ts_ms > to_ms:
            continue
        lanes.setdefault(node.agent_id, []).append(
            WaterfallSpan(
                span_id=node.span_id,
                kind=node.kind,
                name=names.get(node.span_id),
                start_ms=node.start_virtual_ts_ms,
                end_ms=node.end_virtual_ts_ms,
                bucket=None,
                on_critical_path=node_id in on_cp,
                status=statuses.get(node.span_id),
                fault_id=None,
                seq_start=min(node.evidence_seq),
                seq_end=max(node.evidence_seq),
            )
        )
    ordered_lanes = tuple(
        WaterfallLane(agent=agent, spans=tuple(sorted(spans, key=lambda s: s.start_ms)))
        for agent, spans in sorted(lanes.items())
    )
    return WaterfallResponse(
        virtual_makespan_ms=dag.virtual_makespan_ms,
        baseline_makespan_ms=None,
        lanes=ordered_lanes,
    )


@router.get(
    "/state", response_model=StateResponse, summary="Reconstructed state (PRD §26.1, §20.4)"
)
def get_state_at(
    run_id: str,
    store: Annotated[Store, Depends(get_store)],
    at_virtual_ts: Annotated[int, Query(description="Reconstruct state as of this virtual ms.")],
) -> StateResponse:
    """Return every state key's last-known value hash as of `at_virtual_ts` (PRD §20.4).

    Raises:
        RunNotFoundError: `E-RUN-404`.
    """
    _require_run(store, run_id)
    values = state_at(store, run_id, at_virtual_ts)
    keys = tuple(
        StateKey(
            key=key,
            value_hash=ref.value_hash,
            value=ref.body,
            writer=ref.writer,
            written_at_seq=ref.seq,
            size_bytes=len(ref.body) if ref.body is not None else None,
        )
        for key, ref in sorted(values.items())
    )
    last_seq = max((k.written_at_seq for k in keys), default=None)
    return StateResponse(at_virtual_ts=at_virtual_ts, at_seq=last_seq, keys=keys)


@router.get(
    "/exploration",
    response_model=ExplorationResponse,
    summary="Bounded schedule exploration results (PRD §26.1, §15.6)",
)
def get_exploration(
    run_id: str, store: Annotated[Store, Depends(get_store)]
) -> ExplorationResponse:
    """Return a run's exploration `Report` (PRD §15), including the required `coverage_statement`.

    Raises:
        RunNotFoundError: `E-RUN-404`.
        NotYetAvailableError: `409` — always, in this build. `explore.report.Report` is real
            (P13) but `api/` may not import `agentdx.explore` (`.importlinter`
            `explore-below-transport-layer`: `explore/` must not import `api/`, and the
            converse contract, `api-never-imports-runtime`, was written before `explore/`
            existed and does not list it as permitted either — confirmed against the actual
            `.importlinter`, not assumed from the CONTEXT.md §4 table's prose). No prompt
            persists an exploration `Report` into the store (no table exists for one — see
            `docs/api.md`), so there is nothing this route can read regardless. Surfaced
            plainly rather than fabricated (PRD §36 rule 1).
    """
    _require_run(store, run_id)
    raise NotYetAvailableError(
        "E-EXPL-001",
        f"Exploration results for run {run_id} are not available: no analysis pipeline in "
        f"this build persists a bounded-exploration report, and api/ may not import "
        f"agentdx.explore directly (CONTEXT.md §4 layer contract). Bounded search: absence "
        f"of findings is not proof of absence.",
        detail={"run_id": run_id},
    )
