"""The **timing DAG** (PRD §16.1) — critical path and parallelism, over a sealed event log.

**This is the timing DAG, not the causality graph (PRD §14.1) — do not confuse them.**
The two graphs share no code and must not: the causality graph's edges are pure
happens-before (program order, message send/recv, lock release/acquire, barrier) and it
is what `analysis.race` (P12, not yet built) walks to find write-write/read-write/write-read
conflicts. Shared-state access deliberately does *not* create a causality edge, because a
detector that treated a racy write→read as ordered could never report the race it exists to
find. The timing DAG below is a different graph over the same log: its nodes are **leaf
spans** (virtual work), its edges add **observed data dependencies** and **retry links** on
top of program order and message causality, and it exists to answer "what determined the
elapsed time", not "what happened concurrently". This module must never import
`analysis.causality` or `analysis.race`, and must never be imported by them for anything
other than the two pure helpers explicitly re-derived below (kept local rather than shared,
matching the precedent in `runtime.faults.taint`'s module docstring for why some small pieces
of causal logic are deliberately duplicated across layers rather than imported).

PRD §16.1.1 (constructing the timing DAG), §16.1.2 (longest weighted path), §16.1.3
(parallelism metrics), §14.1 (the two graphs), §14.2 (vector clocks, duplicated locally as
`_happens_before` — `analysis.causality` does not exist yet; P12 should reuse or re-derive,
never diverge).

**I3 purity.** This module imports only `agentdx.events` (the closed event contract) and the
standard library. No `agentdx.runtime`, no `agentdx.sdk`, no model client — `.importlinter`'s
`analysis-is-pure` contract has no allowlist entry for this file or any other.

**Determinism (NFR-14).** Every collection this module returns is either a `tuple` built in a
stable order or a `dict` whose *values* were themselves built deterministically; nothing here
iterates a bare `set`. Ties (equal timestamps, equal weights) are broken by `(virtual_ts_ms,
node_id)` or `(end_seq, span_id)` as PRD §16.1.2 specifies, never by insertion order.

**Node kinds and what fills the gaps.** PRD §16.1.1 names four node kinds: `llm_call`,
`tool_call`, `wait` (each one whole leaf span) and "agent_step segments not covered by a
child span" (a *decomposition* of a parent `agent_step` span into the leftover intervals its
children do not occupy, "so that no virtual millisecond is counted twice"). A fifth span kind
is legal in the schema — `handoff` — but no code path in this build ever emits one
(`grep -rn 'kind=.handoff' src/agentdx/` returns nothing); §16.1.1 does not name it as a node
kind either, so encountering one is treated as unsupported rather than silently guessed at
(`E-ANLZ-002`). Every leaf span's own *duration* comes from its `duration_virtual_ms` field
(the declared, evidenced number); every `agent_step` segment's duration is *derived* from the
recorded `virtual_ts_ms` of its span's own `span_start`/`span_end` events, not from a declared
duration field (an `agent_step` span has no work duration of its own to declare — it is a
container). This split is what makes segment decomposition correct even when a span's
declared `duration_virtual_ms` does not span the interval its children's *events* actually
occupy (the provisional week-1 fixture goldens are exactly such logs — every declared
duration is 0 — see CONTEXT.md §14 "ADR-001 consequence 2 ... still owed").
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

from agentdx.events.schema import Event, EventType

_DOCS: Final = "docs/performance-analysis.md"

#: The only span kinds this build knows how to turn into timing-DAG nodes. `agent_step` is
#: handled separately (decomposed into segments, never a node in its own right).
_LEAF_KINDS: Final = ("llm_call", "tool_call", "wait")

#: Sentinel node ids for the virtual run-boundary nodes (PRD §16.1.1's "Run boundary" edge
#: kind). Chosen so they sort before/after every real span id, which is opaque lowercase hex
#: (`events.canonical`'s span-id generator) and therefore never collides with these.
START_NODE: Final = "_START"
END_NODE: Final = "_END"

# Program-order / message / data-dependency / retry / fan-in / run-boundary — PRD §16.1.1's
# table, verbatim as the closed set of edge kinds this module produces.
EdgeKind: Final = (
    "program_order",
    "message",
    "data_dependency",
    "retry",
    "fan_in",
    "run_boundary",
)


class TimingAnalysisError(RuntimeError):
    """A timing-DAG construction or validation failure, carrying a stable `E-ANLZ-0NN` code.

    Same shape as `runtime.cache.key.KeyMaterialError` and friends: a code plus a docs anchor,
    never a bare message, so a caller (or a human reading a traceback) can look the failure up.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a description of what went wrong."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


@dataclass(frozen=True, slots=True)
class TimingNode:
    """One node of the timing DAG — a leaf span, or a decomposed `agent_step` segment.

    Guarantees: `duration_ms >= 0`; `evidence_seq` is non-empty and sorted ascending (I6 —
    every node traces to concrete `seq` values); `node_id` is unique within one `TimingDAG`.
    """

    node_id: str
    kind: str
    """One of `_LEAF_KINDS`, or `"agent_step_segment"` for a decomposed leftover interval."""
    span_id: str | None
    """The real span id for a leaf-span node; `None` for a decomposed segment (which is a
    sub-interval of its parent's span, not a span of its own)."""
    agent_id: str | None
    clock_slot: str | None
    role: str | None
    """The owning agent's declared role (`worker`/`orchestrator`/`router`/`tool_proxy`), read
    from the enclosing `agent_step` span's `attributes.role` (PRD §6.1, §16.2.2) — never from
    this node's own span, since only `agent_step` spans carry it. `None` if undeclared, which
    PRD's overhead bucketing treats as `worker` (§16.2.2 only special-cases orchestrator/router)."""
    start_virtual_ts_ms: int
    duration_ms: int
    tool_name: str | None
    """The `tool_call` event's `tool` field, for `kind == "tool_call"` nodes only (PRD §16.3's
    redundancy grouping key). `None` otherwise."""
    args_hash: str | None
    """The `tool_call` event's `args_hash`, for `kind == "tool_call"` nodes only. `None`
    otherwise."""
    retry_of: str | None
    """The span id this node retries, from `span_start.attributes.retry_of` (PRD §10.9),
    or `None` for a first attempt."""
    vclock: Mapping[str, int]
    """The vector clock at the point this node begins — the enclosing span's `span_start`
    event's `vclock` for a leaf span; the parent `agent_step`'s `span_start.vclock` for a
    segment (an approximation adequate for the one thing segments' clocks are used for:
    nothing in this build's redundancy/race scope groups on a segment's concurrency, since
    `analysis.redundancy` only ever inspects `tool_call` leaf nodes)."""
    evidence_seq: tuple[int, ...]
    anchor_seq: int
    """The single `seq` this node is topologically ordered by — always `min(evidence_seq)`.
    A separate field (rather than re-deriving it at every call site) because Kahn's-algorithm
    tie-breaking (`_topological_order`) needs it on every node, every round."""

    @property
    def end_virtual_ts_ms(self) -> int:
        """Return the virtual millisecond this node's own declared/derived work ends at."""
        return self.start_virtual_ts_ms + self.duration_ms


@dataclass(frozen=True, slots=True)
class TimingEdge:
    """One directed, weighted edge of the timing DAG.

    Guarantees: `weight_ms >= 0`; `evidence_seq` is non-empty and sorted ascending (I6).
    """

    kind: str
    """One of `EdgeKind`."""
    src: str
    dst: str
    weight_ms: int
    evidence_seq: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TimingDAG:
    """The complete timing DAG for one sealed run (PRD §16.1.1).

    Guarantees: `nodes` and `edges` are plain `dict`s built in a deterministic order (NFR-14);
    `topological_order` is a valid topological ordering of `nodes.keys()` starting at
    `START_NODE` and ending at `END_NODE`, with ties broken by `(start_virtual_ts_ms,
    node_id)` — the same rule PRD §16.1.2 specifies for the critical path itself, applied one
    level down so the whole traversal is reproducible, not just its answer.
    """

    run_id: str
    nodes: Mapping[str, TimingNode]
    edges: Mapping[str, tuple[TimingEdge, ...]]
    """Outgoing edges, keyed by `src`, each tuple sorted by `(dst, kind)`."""
    topological_order: tuple[str, ...]
    virtual_makespan_ms: int
    """`run_end.payload.virtual_makespan_ms` — PRD §16.1.2's `virtual_makespan`, the
    denominator every downstream ratio in §16 is computed against (I11: virtual, never wall)."""


# ---------------------------------------------------------------------------------------
# PRD §14.2, duplicated locally — see the module docstring for why
# ---------------------------------------------------------------------------------------


def _happens_before(a: Mapping[str, int], b: Mapping[str, int]) -> bool:
    """Return whether vector clock `a` happens-before `b` (PRD §14.2, verbatim)."""
    at_most = all(a.get(s, 0) <= b.get(s, 0) for s in a)
    strictly_less = any(a.get(s, 0) < b.get(s, 0) for s in sorted(set(a) | set(b)))
    return at_most and strictly_less


# ---------------------------------------------------------------------------------------
# Building the DAG
# ---------------------------------------------------------------------------------------


def _str_field(payload: Mapping[str, object], key: str) -> str | None:
    """Return `payload[key]` if it is a `str`, else `None` — a typed, defensive accessor."""
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _int_field(payload: Mapping[str, object], key: str) -> int | None:
    """Return `payload[key]` if it is an `int` (and not a `bool`), else `None`."""
    value = payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _attributes(span_start: Event) -> Mapping[str, object]:
    """Return `span_start.payload["attributes"]`, or `{}` if absent/malformed."""
    value = span_start.payload.get("attributes")
    return value if isinstance(value, Mapping) else {}


@dataclass
class _SpanRecord:
    """Working state for one span while the DAG is being assembled — not part of the public API."""

    span_id: str
    start: Event
    end: Event
    kind: str
    parent_span_id: str | None
    detail: Event | None
    """The `tool_call`/`llm_call` typed event carrying this span's kind-specific fields, for
    `kind in _LEAF_KINDS - {"wait"}`. `None` for `wait` (no dedicated event type exists for it)
    and for `agent_step` (decomposed, never a single node)."""


def _index_spans(events: Sequence[Event]) -> dict[str, _SpanRecord]:
    """Return every span, keyed by `span_id`, with its `span_start`/`span_end`/detail event.

    Raises:
        TimingAnalysisError: `E-ANLZ-001` — a `span_start` has no matching `span_end` (a
            truncated or malformed log), or a `tool_call`/`llm_call`-kind span has no
            corresponding typed event carrying its fields.
    """
    starts: dict[str, Event] = {}
    ends: dict[str, Event] = {}
    details: dict[str, Event] = {}
    for event in events:
        span_id = event.span_id
        if span_id is None:
            continue
        if event.type is EventType.SPAN_START:
            starts[span_id] = event
        elif event.type is EventType.SPAN_END:
            ends[span_id] = event
        elif event.type in (EventType.TOOL_CALL, EventType.LLM_CALL):
            details.setdefault(span_id, event)

    spans: dict[str, _SpanRecord] = {}
    for span_id in sorted(starts):
        start = starts[span_id]
        end = ends.get(span_id)
        if end is None:
            raise TimingAnalysisError(
                "E-ANLZ-001",
                f"span {span_id!r} (span_start seq {start.seq}) has no span_end — "
                "the log is truncated or malformed",
            )
        kind = _str_field(start.payload, "kind") or ""
        detail = details.get(span_id)
        if kind in ("tool_call", "llm_call") and detail is None:
            raise TimingAnalysisError(
                "E-ANLZ-001",
                f"span {span_id!r} (seq {start.seq}) has kind={kind!r} but no {kind} event",
            )
        spans[span_id] = _SpanRecord(
            span_id=span_id,
            start=start,
            end=end,
            kind=kind,
            parent_span_id=_str_field(start.payload, "parent_span_id"),
            detail=detail,
        )
    return spans


def _role_by_agent(spans: Mapping[str, _SpanRecord]) -> dict[str, str]:
    """Return `{agent_id: role}` from every `agent_step` span's `attributes.role` (PRD §6.1).

    The *first* declared role for an agent id wins (an agent's role should not change run to
    run; if a log somehow disagrees with itself, silently preferring one over the other is at
    least deterministic, and is favoured over raising, since role only affects which of two
    already-computed buckets a millisecond lands in — never whether it is counted at all).
    """
    roles: dict[str, str] = {}
    for span_id in sorted(spans):
        record = spans[span_id]
        if record.kind != "agent_step" or record.start.agent_id is None:
            continue
        role = _str_field(_attributes(record.start), "role")
        if role is not None:
            roles.setdefault(record.start.agent_id, role)
    return roles


def _children_by_parent(spans: Mapping[str, _SpanRecord]) -> dict[str | None, list[str]]:
    """Return `{parent_span_id: [child_span_id, ...]}`, each list sorted `(start_ts, span_id)`."""
    out: dict[str | None, list[str]] = {}
    for span_id in sorted(spans):
        out.setdefault(spans[span_id].parent_span_id, []).append(span_id)
    for children in out.values():
        children.sort(key=lambda sid: (spans[sid].start.virtual_ts_ms, sid))
    return out


def _leaf_node(record: _SpanRecord) -> TimingNode:
    """Build the one `TimingNode` for a `llm_call`/`tool_call`/`wait` leaf span."""
    duration = _int_field(record.end.payload, "duration_virtual_ms")
    if duration is None:
        raise TimingAnalysisError(
            "E-ANLZ-001",
            f"span {record.span_id!r} (seq {record.end.seq}) span_end has no duration_virtual_ms",
        )
    tool_name = args_hash = None
    if record.kind == "tool_call" and record.detail is not None:
        tool_name = _str_field(record.detail.payload, "tool")
        args_hash = _str_field(record.detail.payload, "args_hash")
    evidence_seqs = [record.start.seq, record.end.seq]
    if record.detail is not None:
        evidence_seqs.append(record.detail.seq)
    evidence = tuple(sorted(set(evidence_seqs)))
    return TimingNode(
        node_id=record.span_id,
        kind=record.kind,
        span_id=record.span_id,
        agent_id=record.start.agent_id,
        clock_slot=record.start.clock_slot,
        role=None,  # filled in by the caller once the full agent->role map is known
        start_virtual_ts_ms=record.start.virtual_ts_ms,
        duration_ms=duration,
        tool_name=tool_name,
        args_hash=args_hash,
        retry_of=_str_field(_attributes(record.start), "retry_of"),
        vclock=dict(record.start.vclock),
        evidence_seq=evidence,
        anchor_seq=evidence[0],
    )


def _segment_intervals(
    outer_start: int, outer_end: int, children: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Return the sorted, non-overlapping gaps in `[outer_start, outer_end)` `children` miss.

    `children` need not be sorted or non-overlapping; this function sorts and merges
    defensively (single-threaded cooperative execution should never produce overlapping
    children of one span, but a malformed/foreign log should not crash the analyser over it).
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(children):
        clipped = (max(start, outer_start), min(end, outer_end))
        if clipped[1] <= clipped[0]:
            continue
        if merged and clipped[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], clipped[1]))
        else:
            merged.append(clipped)

    segments: list[tuple[int, int]] = []
    cursor = outer_start
    for start, end in merged:
        if start > cursor:
            segments.append((cursor, start))
        cursor = max(cursor, end)
    if outer_end > cursor:
        segments.append((cursor, outer_end))
    return segments


def _point_events_by_span(events: Sequence[Event]) -> dict[str, list[Event]]:
    """Return `{span_id: [point events in that span, seq order]}` for edge-building.

    "Point events" are the span-scoped types that are not `span_start`/`span_end` themselves
    and carry no duration of their own: `message_send`, `message_recv`, `state_read`,
    `state_write`, `lock_acquire`, `lock_release`, `barrier`. `tool_call`/`llm_call` detail
    events are excluded — they are folded into their leaf span's own node, not treated as
    separate attachable points.
    """
    boundary_types = {
        EventType.SPAN_START,
        EventType.SPAN_END,
        EventType.TOOL_CALL,
        EventType.LLM_CALL,
    }
    out: dict[str, list[Event]] = {}
    for event in events:
        if event.span_id is not None and event.type not in boundary_types:
            out.setdefault(event.span_id, []).append(event)
    return out


def build_timing_dag(events: Sequence[Event]) -> TimingDAG:
    """Build the PRD §16.1.1 timing DAG from a sealed, `seq`-ordered event log.

    Guarantees: every `llm_call`/`tool_call`/`wait` leaf span becomes exactly one node; every
    `agent_step` span is decomposed into zero or more `agent_step_segment` nodes covering
    every virtual millisecond of it that no child span occupies, so no millisecond is counted
    twice (PRD §16.1.1). Edges are added for every kind in PRD §16.1.1's table: program order
    (per clock slot), message causality, observed data dependencies (only where also ordered
    by happens-before — PRD §14.1's "the two graphs must stay consistent"), retries, fan-in
    joins (from `causal_parents` with more than one entry), and run-boundary edges from
    `START_NODE`/to `END_NODE` (weighted by the real lead-in/trail-off gap against
    `run_start`/`run_end`'s own `virtual_ts_ms`, not zero — see `_build_edges`).

    Args:
        events: A sealed log's events, `seq`-ascending (any prefix that still contains a
            `run_start` and a `run_end` is accepted; the log need not be the whole run).

    Returns:
        The `TimingDAG`. Deterministic: the same `events` always yields byte-identical
        `nodes`/`edges` contents and the same `topological_order` (NFR-14).

    Raises:
        TimingAnalysisError: `E-ANLZ-001` a malformed/truncated span; `E-ANLZ-002` a span
            kind this build does not know how to turn into a node; `E-ANLZ-005` no
            `run_start`/`run_end` pair in `events`.
    """
    ordered = sorted(events, key=lambda e: e.seq)
    run_start = next((e for e in ordered if e.type is EventType.RUN_START), None)
    run_end = next((e for e in ordered if e.type is EventType.RUN_END), None)
    if run_start is None or run_end is None:
        raise TimingAnalysisError(
            "E-ANLZ-005", "the log has no run_start/run_end pair — cannot compute a makespan"
        )
    makespan = _int_field(run_end.payload, "virtual_makespan_ms")
    if makespan is None:
        raise TimingAnalysisError(
            "E-ANLZ-005", f"run_end (seq {run_end.seq}) has no virtual_makespan_ms"
        )

    spans = _index_spans(ordered)
    for span_id in sorted(spans):
        if spans[span_id].kind not in (*_LEAF_KINDS, "agent_step"):
            raise TimingAnalysisError(
                "E-ANLZ-002",
                f"span {span_id!r} (seq {spans[span_id].start.seq}) has kind="
                f"{spans[span_id].kind!r}, which PRD §16.1.1 does not name as a timing-DAG "
                "node kind",
            )
    roles = _role_by_agent(spans)
    children = _children_by_parent(spans)
    points_by_span = _point_events_by_span(ordered)

    nodes: dict[str, TimingNode] = {}
    segments_by_agent_step: dict[str, list[tuple[int, int, str]]] = {}

    for span_id in sorted(spans):
        record = spans[span_id]
        if record.kind in _LEAF_KINDS:
            node = _leaf_node(record)
            if node.agent_id is not None and node.role is None:
                node = _replace_role(node, roles.get(node.agent_id))
            nodes[node.node_id] = node

    for span_id in sorted(spans):
        record = spans[span_id]
        if record.kind != "agent_step":
            continue
        outer = (record.start.virtual_ts_ms, record.end.virtual_ts_ms)
        child_intervals = [
            (spans[cid].start.virtual_ts_ms, spans[cid].end.virtual_ts_ms)
            for cid in children.get(span_id, [])
        ]
        gaps = _segment_intervals(outer[0], outer[1], child_intervals)
        segs: list[tuple[int, int, str]] = []
        for index, (seg_start, seg_end) in enumerate(gaps):
            seg_id = f"{span_id}#seg{index}"
            span_points = points_by_span.get(span_id, ())
            evidence = _segment_evidence(record, span_points, seg_start, seg_end)
            nodes[seg_id] = TimingNode(
                node_id=seg_id,
                kind="agent_step_segment",
                span_id=None,
                agent_id=record.start.agent_id,
                clock_slot=record.start.clock_slot,
                role=roles.get(record.start.agent_id) if record.start.agent_id else None,
                start_virtual_ts_ms=seg_start,
                duration_ms=seg_end - seg_start,
                tool_name=None,
                args_hash=None,
                retry_of=None,
                vclock=dict(record.start.vclock),
                evidence_seq=evidence,
                anchor_seq=evidence[0],
            )
            segs.append((seg_start, seg_end, seg_id))
        segments_by_agent_step[span_id] = segs

    def resolve_container(span_id: str, virtual_ts_ms: int) -> str:
        """Return the node id whose interval contains an event at `virtual_ts_ms` in `span_id`."""
        record = spans[span_id]
        if record.kind in _LEAF_KINDS:
            return span_id
        segs = segments_by_agent_step.get(span_id, [])
        if not segs:
            # A degenerate agent_step (zero-width, fully covered by children with no gap):
            # attribute to the nearest child, which is the only real activity there was.
            child_ids = children.get(span_id, [])
            if child_ids:
                return resolve_container(child_ids[-1], virtual_ts_ms)
            raise TimingAnalysisError(
                "E-ANLZ-001",
                f"span {span_id!r} has no segments and no children to attach an event to",
            )
        best = segs[0][2]
        for seg_start, _seg_end, seg_id in segs:
            if seg_start <= virtual_ts_ms:
                best = seg_id
            else:
                break
        return best

    edges = _build_edges(
        ordered, spans, nodes, segments_by_agent_step, resolve_container, run_start, run_end
    )
    order = _topological_order(nodes, edges)

    return TimingDAG(
        run_id=run_start.run_id,
        nodes=nodes,
        edges=edges,
        topological_order=order,
        virtual_makespan_ms=makespan,
    )


def _replace_role(node: TimingNode, role: str | None) -> TimingNode:
    """Return a copy of `node` with `role` set.

    `dataclasses.replace` would work too; this avoids importing it for the single field the
    build loop ever changes post-construction.
    """
    if role is None:
        return node
    return TimingNode(
        node_id=node.node_id,
        kind=node.kind,
        span_id=node.span_id,
        agent_id=node.agent_id,
        clock_slot=node.clock_slot,
        role=role,
        start_virtual_ts_ms=node.start_virtual_ts_ms,
        duration_ms=node.duration_ms,
        tool_name=node.tool_name,
        args_hash=node.args_hash,
        retry_of=node.retry_of,
        vclock=node.vclock,
        evidence_seq=node.evidence_seq,
        anchor_seq=node.anchor_seq,
    )


def _segment_evidence(
    record: _SpanRecord, points: Sequence[Event], seg_start: int, seg_end: int
) -> tuple[int, ...]:
    """Return the evidence seqs for one `agent_step` segment.

    Contained point events, or the span's own `span_start`/`span_end` if the segment is
    empty of point events (still real evidence — the segment exists because the span's own
    boundary events say so).
    """
    contained = tuple(sorted(e.seq for e in points if seg_start <= e.virtual_ts_ms <= seg_end))
    if contained:
        return contained
    return tuple(sorted({record.start.seq, record.end.seq}))


def _build_edges(
    events: Sequence[Event],
    spans: Mapping[str, _SpanRecord],
    nodes: Mapping[str, TimingNode],
    segments_by_agent_step: Mapping[str, list[tuple[int, int, str]]],
    resolve_container: Callable[[str, int], str],
    run_start: Event,
    run_end: Event,
) -> dict[str, tuple[TimingEdge, ...]]:
    """Return every outgoing edge, keyed by `src`, each tuple sorted `(dst, kind)`."""
    edges: list[TimingEdge] = []

    # --- program order: per clock slot, the flat sequence of every node in that slot -------
    by_slot: dict[str, list[TimingNode]] = {}
    for node in nodes.values():
        slot = node.clock_slot or node.agent_id
        if slot is not None:
            by_slot.setdefault(slot, []).append(node)
    for slot in sorted(by_slot):
        ordered_nodes = sorted(by_slot[slot], key=lambda n: (n.start_virtual_ts_ms, n.node_id))
        for prev, nxt in pairwise(ordered_nodes):
            weight = max(0, nxt.start_virtual_ts_ms - prev.end_virtual_ts_ms)
            edges.append(
                TimingEdge(
                    kind="program_order",
                    src=prev.node_id,
                    dst=nxt.node_id,
                    weight_ms=weight,
                    evidence_seq=tuple(sorted({prev.evidence_seq[-1], nxt.evidence_seq[0]})),
                )
            )

    # --- message causality -------------------------------------------------------------------
    sends: dict[str, Event] = {}
    recvs: dict[str, Event] = {}
    for event in events:
        message_id = _str_field(event.payload, "message_id")
        if message_id is None:
            continue
        if event.type is EventType.MESSAGE_SEND:
            sends.setdefault(message_id, event)
        elif event.type is EventType.MESSAGE_RECV:
            recvs.setdefault(message_id, event)
    for message_id in sorted(set(sends) & set(recvs)):
        send, recv = sends[message_id], recvs[message_id]
        if send.span_id is None or recv.span_id is None:
            continue
        src = resolve_container(send.span_id, send.virtual_ts_ms)
        dst = resolve_container(recv.span_id, recv.virtual_ts_ms)
        # Weight is the gap between the SRC NODE's occupied end and the DST NODE's start —
        # not the raw send/recv event timestamps. `dist[]` already accounts for src's own
        # duration up to its occupied end (see the recurrence in `critical_path`), so anchoring
        # the edge weight to the raw send event's timestamp (which typically falls near the
        # *start* of its containing node, not the end) would double-count that node's own
        # duration. Node-to-node gaps keep the telescoping `dist[]` sum self-consistent, in
        # the same way `program_order` edges already do below.
        weight = max(0, nodes[dst].start_virtual_ts_ms - nodes[src].end_virtual_ts_ms)
        edges.append(
            TimingEdge(
                kind="message",
                src=src,
                dst=dst,
                weight_ms=weight,
                evidence_seq=tuple(sorted({send.seq, recv.seq})),
            )
        )

    # --- data dependency: for each state_read, its nearest happens-before write in another
    # slot (PRD §16.1.1's "first state_read ... that happens-after it", read per-read so a
    # later write correctly supersedes an earlier one as the governing dependency) ------------
    writes_by_key: dict[str, list[Event]] = {}
    for event in events:
        if event.type is EventType.STATE_WRITE:
            key = _str_field(event.payload, "key")
            if key is not None:
                writes_by_key.setdefault(key, []).append(event)
    for event in events:
        if event.type is not EventType.STATE_READ or event.span_id is None:
            continue
        key = _str_field(event.payload, "key")
        if key is None:
            continue
        candidates = [w for w in writes_by_key.get(key, []) if w.seq < event.seq]
        candidates.sort(key=lambda w: w.seq, reverse=True)
        for write in candidates:
            if write.span_id is None:
                continue
            if write.clock_slot == event.clock_slot:
                continue
            if not _happens_before(write.vclock, event.vclock):
                continue
            src = resolve_container(write.span_id, write.virtual_ts_ms)
            dst = resolve_container(event.span_id, event.virtual_ts_ms)
            # Node-to-node gap, not raw write/read event timestamps — see the identical
            # rationale on the `message` edge weight above.
            weight = max(0, nodes[dst].start_virtual_ts_ms - nodes[src].end_virtual_ts_ms)
            edges.append(
                TimingEdge(
                    kind="data_dependency",
                    src=src,
                    dst=dst,
                    weight_ms=weight,
                    evidence_seq=tuple(sorted({write.seq, event.seq})),
                )
            )
            break

    # --- retry: span_start.attributes.retry_of names the span this one retries --------------
    for span_id in sorted(spans):
        record = spans[span_id]
        if record.kind not in _LEAF_KINDS:
            continue
        retried = _str_field(_attributes(record.start), "retry_of")
        if retried is None or retried not in spans:
            continue
        prior = spans[retried]
        prior_node = nodes.get(retried)
        this_node = nodes.get(span_id)
        if prior_node is None or this_node is None:
            continue
        weight = max(0, this_node.start_virtual_ts_ms - prior_node.end_virtual_ts_ms)
        edges.append(
            TimingEdge(
                kind="retry",
                src=retried,
                dst=span_id,
                weight_ms=weight,
                evidence_seq=tuple(sorted({prior.end.seq, record.start.seq})),
            )
        )

    # --- fan-in join: a span_start with more than one causal_parents entry ------------------
    for span_id in sorted(spans):
        record = spans[span_id]
        parents = sorted(set(record.start.causal_parents))
        if len(parents) < 2:
            continue
        dst = resolve_container(span_id, record.start.virtual_ts_ms)
        by_seq = {e.seq: e for e in events}
        for parent_seq in parents:
            producer = by_seq.get(parent_seq)
            if producer is None or producer.span_id is None:
                continue
            src = resolve_container(producer.span_id, producer.virtual_ts_ms)
            if src == dst:
                continue
            # Node-to-node gap, not raw event timestamps — see the identical rationale on the
            # `message` edge weight above.
            weight = max(0, nodes[dst].start_virtual_ts_ms - nodes[src].end_virtual_ts_ms)
            edges.append(
                TimingEdge(
                    kind="fan_in",
                    src=src,
                    dst=dst,
                    weight_ms=weight,
                    evidence_seq=tuple(sorted({parent_seq, record.start.seq})),
                )
            )

    # --- run boundary: START -> every source node; every sink node -> END -------------------
    # Weight is the real gap between the run's own boundary event and the node's occupied
    # start/end — NOT hard-coded to 0. A run virtually always has some lead-in before its
    # first observed span starts (time from `run_start` to the first `span_start`) and some
    # trail-off after its last span ends (time to `run_end`); zeroing these out would make
    # `critical_path_length_ms` structurally always fall short of `virtual_makespan_ms` by
    # exactly that lead-in + trail-off, inflating `overhead.residual` by a fixed amount on
    # every run regardless of how well-instrumented it is — the opposite of I9 (no unmeasured
    # statistics: the lead-in/trail-off *is* measured, right here, so it must be attributed).
    #
    # This deliberately diverges from PRD §16.1.1's edge table, which gives `run_boundary` a
    # literal weight of 0 — a real, documented PRD contradiction, not a silent guess. See
    # CONTEXT.md D-50 (the deviation) and C-19 (the ruling): the change decides gate G5's
    # pass/fail status on every golden fixture (0% residual here vs. 8.5-10% with the literal
    # PRD weight, measured directly), and was found undeclared by a same-session independent
    # review before being formalised. `overhead.py`'s `_classify_edge` routes this edge kind's
    # weight to `blocking_wait`, not `residual` — see that module's docstring.
    has_incoming = {e.dst for e in edges}
    has_outgoing = {e.src for e in edges}
    for node_id in sorted(nodes):
        if node_id not in has_incoming:
            node = nodes[node_id]
            weight = max(0, node.start_virtual_ts_ms - run_start.virtual_ts_ms)
            edges.append(
                TimingEdge(
                    kind="run_boundary",
                    src=START_NODE,
                    dst=node_id,
                    weight_ms=weight,
                    evidence_seq=tuple(sorted({run_start.seq, node.anchor_seq})),
                )
            )
        if node_id not in has_outgoing:
            node = nodes[node_id]
            weight = max(0, run_end.virtual_ts_ms - node.end_virtual_ts_ms)
            edges.append(
                TimingEdge(
                    kind="run_boundary",
                    src=node_id,
                    dst=END_NODE,
                    weight_ms=weight,
                    evidence_seq=tuple(sorted({node.anchor_seq, run_end.seq})),
                )
            )

    grouped: dict[str, list[TimingEdge]] = {}
    for edge in edges:
        grouped.setdefault(edge.src, []).append(edge)
    return {
        src: tuple(sorted(items, key=lambda e: (e.dst, e.kind)))
        for src, items in sorted(grouped.items())
    }


def _topological_order(
    nodes: Mapping[str, TimingNode], edges: Mapping[str, tuple[TimingEdge, ...]]
) -> tuple[str, ...]:
    """Return a deterministic topological order over `{START_NODE, *nodes, END_NODE}`.

    Kahn's algorithm: at every round, the frontier of zero-remaining-in-degree ids is
    processed in `(start_virtual_ts_ms, node_id)` order (`START_NODE`/`END_NODE` sort first/
    last by construction of their sentinel ids), so the same DAG always yields the same order.

    Raises:
        TimingAnalysisError: `E-ANLZ-003` — the edge set contains a cycle, which cannot happen
            for a well-formed log (every edge is derived from a real happens-after
            relationship) and indicates a defect in the DAG construction, not the input log.
    """
    all_ids = {START_NODE, END_NODE, *nodes}
    start_ts = {START_NODE: -1, END_NODE: 1 << 62}
    for node_id, node in nodes.items():
        start_ts[node_id] = node.start_virtual_ts_ms

    in_degree = dict.fromkeys(all_ids, 0)
    for out_edges in edges.values():
        for edge in out_edges:
            in_degree[edge.dst] = in_degree.get(edge.dst, 0) + 1

    frontier = sorted(
        (node_id for node_id in all_ids if in_degree[node_id] == 0),
        key=lambda n: (start_ts[n], n),
    )
    order: list[str] = []
    remaining = dict(in_degree)
    while frontier:
        current = frontier.pop(0)
        order.append(current)
        for edge in edges.get(current, ()):
            remaining[edge.dst] -= 1
            if remaining[edge.dst] == 0:
                _insert_sorted(frontier, edge.dst, key=lambda n: (start_ts[n], n))

    if len(order) != len(all_ids):
        raise TimingAnalysisError(
            "E-ANLZ-003",
            f"the timing DAG contains a cycle — only {len(order)}/{len(all_ids)} nodes could "
            "be topologically ordered",
        )
    return tuple(order)


def _insert_sorted(items: list[str], value: str, *, key: Callable[[str], tuple[int, str]]) -> None:
    """Insert `value` into the sorted `items` list, keeping it sorted by `key`."""
    lo, hi = 0, len(items)
    value_key = key(value)
    while lo < hi:
        mid = (lo + hi) // 2
        if key(items[mid]) < value_key:
            lo = mid + 1
        else:
            hi = mid
    items.insert(lo, value)


# ---------------------------------------------------------------------------------------
# PRD §16.1.2 — longest weighted path
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CriticalPathResult:
    """The result of PRD §16.1.2's longest-weighted-path computation.

    Guarantees: `path` starts at `START_NODE` and ends at `END_NODE`; `length_ms` is exactly
    the sum of every edge weight and every intermediate node's `duration_ms` along `path`
    (this is asserted, not merely intended — see `critical_path`'s docstring).
    """

    path: tuple[str, ...]
    length_ms: int


def critical_path(dag: TimingDAG) -> CriticalPathResult:
    """Return the PRD §16.1.2 longest weighted path through `dag`, START to END.

    ```
    dist[n] = length of the longest path from START to n, ending with n's own duration
              (a node's duration counts once, when the path arrives at it — never twice,
              since dist[] is only ever read for a node already fully processed)
    ```

    **Determinism / tie-break (PRD §16.1.2):** when two predecessors give a node the same
    `dist`, the one with the larger `(end_seq, span_id)` — i.e. the *later*-evidenced,
    lexicographically-later predecessor — is preferred, matching the PRD's literal
    `tiebreak(n) < tiebreak(prev[m])` comparison read as "keep the existing `prev[m]` unless
    the challenger's tiebreak is strictly smaller", which favours the *first-seen* (by this
    function's deterministic node processing order) predecessor on an exact tie. Because nodes
    are processed in the one deterministic topological order `dag.topological_order`, "first
    seen" is itself deterministic, so the whole path is reproducible across analyses of the
    same log (NFR-14).

    Raises:
        TimingAnalysisError: `E-ANLZ-003` — `length_ms > dag.virtual_makespan_ms`, i.e. the
            DAG claims more elapsed time than the run recorded. PRD §16.1.2: "the DAG is
            wrong." A DAG whose critical path is *shorter* than the makespan is normal (the
            shortfall is unexplained idle time, reported honestly as the residual bucket —
            PRD §16.2.4) and is not an error here.
    """
    if not dag.nodes:
        # No spans at all — no critical path to walk. `_build_edges` never emits a direct
        # START_NODE -> END_NODE edge (run-boundary edges are only emitted per real node), so
        # this case is handled here rather than by teaching edge-building about an empty DAG.
        return CriticalPathResult(path=(START_NODE, END_NODE), length_ms=0)

    all_ids = {START_NODE, END_NODE, *dag.nodes}
    unreached = -1
    dist: dict[str, int] = dict.fromkeys(all_ids, unreached)
    dist[START_NODE] = 0
    prev: dict[str, str | None] = dict.fromkeys(all_ids, None)

    def duration_of(node_id: str) -> int:
        if node_id in (START_NODE, END_NODE):
            return 0
        return dag.nodes[node_id].duration_ms

    def tiebreak(node_id: str) -> tuple[int, str]:
        if node_id in (START_NODE, END_NODE):
            return (-1, node_id)
        node = dag.nodes[node_id]
        return (node.evidence_seq[-1], node.span_id or node_id)

    for node_id in dag.topological_order:
        if dist[node_id] < 0:
            continue  # unreachable from START — cannot happen for a well-formed DAG, but
            # every node here has a run_boundary edge FROM START if it has no other incoming
            # edge, so this only guards a construction bug, not a normal input.
        for edge in dag.edges.get(node_id, ()):
            candidate = dist[node_id] + edge.weight_ms + duration_of(edge.dst)
            existing_prev = prev[edge.dst]
            if candidate > dist[edge.dst]:
                dist[edge.dst] = candidate
                prev[edge.dst] = node_id
            elif (
                candidate == dist[edge.dst]
                and existing_prev is not None
                and tiebreak(node_id) < tiebreak(existing_prev)
            ):
                prev[edge.dst] = node_id

    path: list[str] = []
    cursor: str | None = END_NODE
    while cursor is not None:
        path.append(cursor)
        cursor = prev[cursor]
    path.reverse()

    length_ms = max(0, dist[END_NODE])
    if length_ms > dag.virtual_makespan_ms:
        raise TimingAnalysisError(
            "E-ANLZ-003",
            f"critical_path_length={length_ms}ms exceeds virtual_makespan="
            f"{dag.virtual_makespan_ms}ms — the timing DAG is wrong (PRD §16.1.2)",
        )
    return CriticalPathResult(path=tuple(path), length_ms=length_ms)


# ---------------------------------------------------------------------------------------
# PRD §16.1.3 — parallelism metrics
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParallelismMetrics:
    """PRD §16.1.3's parallelism metrics — the honest measure of concurrency actually used."""

    total_work_ms: int
    """Σ `duration_ms` of every `llm_call`/`tool_call` leaf node in the whole log (not just
    the critical path) — PRD §16.1.3's `total_work`."""
    critical_path_length_ms: int
    average_parallelism: float
    """`total_work_ms / critical_path_length_ms`. `0.0` when the critical path has zero
    length (an all-idle or single-instant log) rather than raising a division error — there
    is no concurrency to measure, and reporting 0 is more honest than refusing to answer."""


def parallelism_metrics(dag: TimingDAG, path_result: CriticalPathResult) -> ParallelismMetrics:
    """Return PRD §16.1.3's parallelism metrics for `dag`."""
    total_work = sum(
        node.duration_ms for node in dag.nodes.values() if node.kind in ("llm_call", "tool_call")
    )
    average = (total_work / path_result.length_ms) if path_result.length_ms > 0 else 0.0
    return ParallelismMetrics(
        total_work_ms=total_work,
        critical_path_length_ms=path_result.length_ms,
        average_parallelism=average,
    )


def overlap(a: TimingNode, b: TimingNode) -> float:
    """Return PRD §16.1.3's `overlap(a, b)`.

    The fraction of the smaller span's busy time that coincides with the other's. `0.0` when
    either node has zero duration (no busy time to overlap) or the intervals do not intersect.
    """
    lo = max(a.start_virtual_ts_ms, b.start_virtual_ts_ms)
    hi = min(a.end_virtual_ts_ms, b.end_virtual_ts_ms)
    intersection = max(0, hi - lo)
    denominator = min(a.duration_ms, b.duration_ms)
    return (intersection / denominator) if denominator > 0 else 0.0


__all__ = [
    "END_NODE",
    "START_NODE",
    "CriticalPathResult",
    "ParallelismMetrics",
    "TimingAnalysisError",
    "TimingDAG",
    "TimingEdge",
    "TimingNode",
    "build_timing_dag",
    "critical_path",
    "overlap",
    "parallelism_metrics",
]
