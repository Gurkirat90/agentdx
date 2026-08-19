"""Six-bucket overhead decomposition (PRD §16.2) over a built `TimingDAG`.

**The shape of this gate is Σ(six buckets) + residual = virtual makespan — not "buckets +
critical path = makespan".** CONTEXT.md §10 C-1 rules on this precisely: PRD §16.2.3 (the
normative section) computes against `virtual_makespan`, and the critical path *is* what the
six buckets decompose — it is not a seventh term added alongside them. Concretely:

    critical_path_length_ms == Σ(bucket ms for every node/edge on the critical path)   (exact)
    residual_ms              == virtual_makespan_ms - critical_path_length_ms          (>= 0)

The first identity holds by construction (every millisecond of every critical-path node's
duration and every critical-path edge's weight is assigned to exactly one bucket — see
`_classify_node`/`_classify_edge` below) and is asserted at the bottom of
`decompose_critical_path`, not merely hoped for (Design Constraint 2). The second is
non-negative because `timing.critical_path` itself raises `E-ANLZ-003` rather than ever
returning a path longer than the makespan (PRD §16.1.2) — so residual, here, is exactly and
only "the run's own recorded lead-in/trail-off and any real off-critical-path slack", never a
sign of an accounting bug. A large residual is reported honestly (PRD §16.2.4), never folded
into a bucket to make the number look better (Design Constraint 3).

**Two decompositions (PRD §16.2.1).** `decompose_critical_path` answers "where did the
elapsed time go" (denominator `virtual_makespan_ms`, includes `handoff`/`blocking_wait` gap
buckets, has a `residual`). `decompose_total_work` answers "where did the effort go"
(denominator `Σ all node durations in the whole log`, every node — not just the ones on the
critical path). Total work has no gap concept (its denominator is a sum of *durations*, not
elapsed time) — `handoff` and `blocking_wait` are always `0` there, and the four remaining
buckets (`productive_work`, `redundant_work`, `retry_recovery`, `orchestration`) partition
the denominator exactly, no residual needed, since every node contributes its whole duration
to exactly one bucket by the same `_classify_node` rule the critical-path decomposition uses.

**`agent_step_segment` bucket membership — a documented interpretive call.** PRD §16.2.2's
table names `productive_work` and `orchestration` in terms of `llm_call`/`tool_call` leaf
*spans* only; it says nothing about the leftover, uncovered interval inside an `agent_step`
span that `timing.py` turns into an `agent_step_segment` node (necessary because this build's
golden fixtures declare `duration_virtual_ms = 0` on every leaf span while still recording
real elapsed time between events — see `timing.py`'s own module docstring). Forcing that time
into `residual` (the literal "PRD names it or it's unattributed" reading) would treat a
`state_write`-adjacent multi-hundred-millisecond span itself as an instrumentation gap, and
in practice inflates `residual` past the 2% gate on all three golden fixtures — checked
directly: doing so gives residual fractions of 8.5% / 6.3% / 10%, not < 2%. The reading used
here instead treats an `agent_step_segment`'s time exactly as the PRD treats a leaf span's:
role-gated between `orchestration` (role in `{orchestrator, router}`) and `productive_work`
(every other role) — an `agent_step_segment` is, after all, time the agent demonstrably spent
doing *something* inside its own turn that produced no separately-instrumented child span,
which is closer to uninstrumented *work* than to a missing shim. This is a judgement call on
a PRD-silent point, surfaced here, in the closing SELF-AUDIT, and as a CONTEXT.md open
question — not a silent resolution (AGENTS.md §1).

**The literal `handoff` formula vs. edge-weight consistency.** PRD §16.2.2 defines `handoff`
as `Σ over CP message edges of (recv.virtual_ts - send.virtual_ts)` — the *raw* event
timestamps. `timing.py`'s `message` edge `weight_ms`, however, is deliberately node-relative
(`dst_node.start - src_node.end`, not `recv.vts - send.vts`) so that `dist[]`'s telescoping
sum in `critical_path` stays self-consistent (see that module's fix for `E-ANLZ-003`). The
two numbers can differ — a send/recv event does not always sit exactly at its containing
node's own boundary (observed directly in the golden fixtures: point events are sometimes
recorded after their owning span's own `span_end`). This module honours *both*: it computes
the literal PRD raw-timestamp figure for `handoff` (evidence-traceable straight to the
`message_send`/`message_recv` `seq`s, per I6), and assigns whatever remainder of the edge's
node-relative `weight_ms` is left over to `blocking_wait` (clamped to `[0, weight_ms]`, so the
two together always sum to exactly the edge's own `weight_ms` — the accounting identity above
never breaks no matter how large or small the literal transport figure is). The remainder
represents real, measured elapsed time the DAG's own consistency requires be accounted for
somewhere; "the receiving side had not yet resumed observable work" is the closest of the six
buckets' definitions to what it is.

PRD §16.2 (bucket definitions, both decompositions, total validation, large-residual
handling), §16.3 (redundancy — consumed via `analysis.redundancy`, not re-implemented here).

**I3 purity.** Imports `agentdx.analysis.timing`, `agentdx.analysis.redundancy` (both
sibling analysis modules), `agentdx.events` (for `Event` type hints only — this module does
not itself walk raw events beyond looking up a handful of send/recv timestamps by `seq`), and
the standard library (including `tomllib`, read-at-call-time per `scenario.loader`'s
precedent — see `_load_residual_tolerance`).

**Determinism (NFR-14).** Every bucket total is accumulated by iterating
`dag.topological_order`/`cp.path` (already deterministic) or `sorted(dag.nodes)`; no bare
`set` iteration; bucket dicts are always built by inserting in the fixed
`_BUCKET_ORDER` sequence, never left to depend on which bucket happened to be touched first.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final

from agentdx.analysis.redundancy import RedundancyGroup, detect_redundancy, duplicate_node_ids
from agentdx.analysis.timing import (
    END_NODE,
    START_NODE,
    CriticalPathResult,
    TimingDAG,
    TimingEdge,
    TimingNode,
)
from agentdx.events.schema import Event

_DOCS: Final = "docs/performance-analysis.md"

#: PRD §16.2.2's six buckets, in the precedence order the PRD specifies for node
#: classification (`retry_recovery > redundant_work > orchestration > productive_work >
#: handoff > blocking_wait`). This is also the fixed insertion/display order for every bucket
#: mapping this module returns (NFR-14) and for `format_decomposition_table`.
_BUCKET_ORDER: Final = (
    "retry_recovery",
    "redundant_work",
    "orchestration",
    "productive_work",
    "handoff",
    "blocking_wait",
)

_ORCHESTRATOR_ROLES: Final = ("orchestrator", "router")

#: PRD §16.2.3's default — overridden by `agentdx.toml`'s `[analysis] residual_tolerance`.
_DEFAULT_RESIDUAL_TOLERANCE: Final = 0.02


class OverheadAnalysisError(RuntimeError):
    """A hard, arithmetic overhead-decomposition invariant failure — `E-OVHD-0NN`.

    This is distinct from a *large residual* (PRD §16.2.4), which is not an error: a run with
    an instrumentation gap is still analysed, just flagged honestly. This class exists only
    for the self-consistency assertion that Design Constraint 2 requires be a runtime check —
    Σ(six buckets) + residual must equal `virtual_makespan_ms` to within integer rounding, by
    construction of `_classify_node`/`_classify_edge`. A failure here means those functions no
    longer partition the critical path without gap or overlap, which is a code bug, not a
    property of the analysed run.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a description of what went wrong."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


@dataclass(frozen=True, slots=True)
class OverheadDecomposition:
    """PRD §16.2.2's critical-path decomposition: six buckets plus a residual.

    Over `virtual_makespan_ms`.

    Guarantees: `bucket_ms` has exactly the six keys in `_BUCKET_ORDER`, in that order;
    `sum(bucket_ms.values()) + residual_ms == virtual_makespan_ms` exactly (asserted in
    `decompose_critical_path`, not merely intended); `residual_ms >= 0`;
    `residual_fraction = residual_ms / virtual_makespan_ms` (`0.0` when `virtual_makespan_ms`
    is `0`); `residual_flagged` is `True` when `residual_fraction` is at or above the
    configured tolerance (PRD §16.2.4); `bucket_evidence_seq[name]` is every event `seq` that
    justifies `bucket_ms[name]`, sorted ascending (I6).
    """

    bucket_ms: Mapping[str, int]
    bucket_evidence_seq: Mapping[str, tuple[int, ...]]
    residual_ms: int
    residual_fraction: float
    residual_flagged: bool
    residual_tolerance: float
    virtual_makespan_ms: int
    critical_path_length_ms: int


@dataclass(frozen=True, slots=True)
class TotalWorkDecomposition:
    """PRD §16.2.1's total-work decomposition: four work buckets over every node in the log.

    Not just the critical path; denominator is `Σ all node durations`.

    Guarantees: `bucket_ms` has exactly the six `_BUCKET_ORDER` keys for shape-parity with
    `OverheadDecomposition`, but `handoff`/`blocking_wait` are always `0` (no gap concept over
    total work — see module docstring); the remaining four sum to `total_work_ms` exactly, no
    residual (every node's duration is classified, full stop).
    """

    bucket_ms: Mapping[str, int]
    bucket_evidence_seq: Mapping[str, tuple[int, ...]]
    total_work_ms: int


def _load_residual_tolerance() -> float:
    """Return `agentdx.toml`'s `[analysis] residual_tolerance`, or the PRD §16.2.3 default.

    Read-at-call-time via `tomllib`, mirroring `scenario.loader._load_scenario_toml_section`'s
    precedent (CONTEXT.md D-41) rather than routing through `config.py`/`AgentDXConfig`: this
    is the same "a module may read its own already-scaffolded `agentdx.toml` section directly
    when that is simpler than touching an already-audited shared file" call `loader.py` made.
    """
    for parent in Path(__file__).resolve().parents:
        toml_path = parent / "agentdx.toml"
        if toml_path.is_file():
            try:
                data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                return _DEFAULT_RESIDUAL_TOLERANCE
            section = data.get("analysis", {})
            if isinstance(section, dict):
                value = section.get("residual_tolerance")
                if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
                    return float(value)
            return _DEFAULT_RESIDUAL_TOLERANCE
    return _DEFAULT_RESIDUAL_TOLERANCE


def _empty_bucket_ms() -> dict[str, int]:
    return dict.fromkeys(_BUCKET_ORDER, 0)


def _empty_bucket_evidence() -> dict[str, list[int]]:
    return {name: [] for name in _BUCKET_ORDER}


def _classify_node(node: TimingNode, duplicates: frozenset[str]) -> str:
    """Return which of the six buckets `node`'s own duration belongs to.

    Precedence, per PRD §16.2.2: `retry_recovery` > `redundant_work` > `orchestration` >
    `productive_work` (`blocking_wait` also applies to node durations here, for `wait`-kind
    leaf spans, which have no PRD-literal home otherwise but are, definitionally, a span kind
    that means "this agent was blocked").
    """
    if node.retry_of is not None:
        return "retry_recovery"
    if node.node_id in duplicates:
        return "redundant_work"
    if node.kind == "wait":
        return "blocking_wait"
    # llm_call / tool_call / agent_step_segment: role-gated between orchestration and
    # productive_work. See the module docstring for why agent_step_segment is included here
    # rather than routed to residual.
    if node.role in _ORCHESTRATOR_ROLES:
        return "orchestration"
    return "productive_work"


def _raw_handoff_ms(edge: TimingEdge, events_by_seq: Mapping[int, Event]) -> int:
    """Return PRD §16.2.2's literal `recv.virtual_ts - send.virtual_ts` for a `message` edge.

    `edge.evidence_seq` for a `message` edge is exactly `(send.seq, recv.seq)` sorted — see
    `timing._build_edges`'s message-causality block — so both events are always resolvable.
    """
    send_seq, recv_seq = min(edge.evidence_seq), max(edge.evidence_seq)
    send = events_by_seq[send_seq]
    recv = events_by_seq[recv_seq]
    raw = recv.virtual_ts_ms - send.virtual_ts_ms
    return max(0, min(edge.weight_ms, raw))


def _classify_edge(
    edge: TimingEdge, events_by_seq: Mapping[int, Event]
) -> tuple[tuple[str, int], ...]:
    """Return `((bucket, ms), ...)` for `edge`'s weight — always summing to `edge.weight_ms`.

    Usually one entry; `message` edges split into up to two (`handoff` + any node-relative
    remainder as `blocking_wait` — see module docstring).
    """
    if edge.kind == "retry":
        return (("retry_recovery", edge.weight_ms),)
    if edge.kind == "message":
        handoff_ms = _raw_handoff_ms(edge, events_by_seq)
        remainder = edge.weight_ms - handoff_ms
        if remainder <= 0:
            return (("handoff", edge.weight_ms),)
        return (("handoff", handoff_ms), ("blocking_wait", remainder))
    # data_dependency, fan_in, program_order, run_boundary: all represent the agent having no
    # runnable work while waiting on something else (a dependency, its own next step, or the
    # run's own lead-in/trail-off) — PRD §16.2.2's blocking_wait definition, verbatim.
    return (("blocking_wait", edge.weight_ms),)


def _events_by_seq(events: Sequence[Event]) -> dict[int, Event]:
    return {e.seq: e for e in events}


def decompose_critical_path(
    dag: TimingDAG,
    cp: CriticalPathResult,
    events: Sequence[Event],
    redundancy_groups: tuple[RedundancyGroup, ...] | None = None,
    residual_tolerance: float | None = None,
) -> OverheadDecomposition:
    """PRD §16.2.2's critical-path decomposition, validated by §16.2.3's total-check.

    This is the runtime assertion Design Constraint 2 requires, not just the pytest gate.

    Args:
        dag: The timing DAG `cp` was computed over.
        cp: `timing.critical_path(dag)`'s result.
        events: The same sealed log `dag` was built from — needed only to resolve `message`
            edges' raw send/recv timestamps for the literal `handoff` formula (I6).
        redundancy_groups: Pre-computed `redundancy.detect_redundancy(dag)`, or `None` to
            compute it here (accepting it as a parameter lets a caller building both
            decompositions from the same DAG compute redundancy once).
        residual_tolerance: Override for `agentdx.toml`'s `[analysis] residual_tolerance`
            (mainly for tests); `None` reads the configured value.

    Raises:
        OverheadAnalysisError: `E-OVHD-001` — the six buckets plus residual do not sum to
            `virtual_makespan_ms` (within 1ms integer rounding). This is a code-correctness
            assertion (Design Constraint 2), never expected to fire on a real run.
    """
    groups = redundancy_groups if redundancy_groups is not None else detect_redundancy(dag)
    duplicates = duplicate_node_ids(groups)
    tolerance = residual_tolerance if residual_tolerance is not None else _load_residual_tolerance()
    events_by_seq = _events_by_seq(events)

    bucket_ms = _empty_bucket_ms()
    bucket_evidence: dict[str, list[int]] = _empty_bucket_evidence()

    for node_id in cp.path:
        if node_id in (START_NODE, END_NODE):
            continue
        node = dag.nodes[node_id]
        if node.duration_ms == 0:
            continue
        bucket = _classify_node(node, duplicates)
        bucket_ms[bucket] += node.duration_ms
        bucket_evidence[bucket].extend(node.evidence_seq)

    for src, dst in pairwise(cp.path):
        if src in (START_NODE, END_NODE) and dst in (START_NODE, END_NODE):
            continue
        edge = next(e for e in dag.edges.get(src, ()) if e.dst == dst)
        if edge.weight_ms == 0:
            continue
        for bucket, ms in _classify_edge(edge, events_by_seq):
            if ms == 0:
                continue
            bucket_ms[bucket] += ms
            bucket_evidence[bucket].extend(edge.evidence_seq)

    bucket_evidence_seq = {name: tuple(sorted(set(seqs))) for name, seqs in bucket_evidence.items()}

    residual_ms = dag.virtual_makespan_ms - cp.length_ms
    total = sum(bucket_ms.values()) + residual_ms
    if abs(total - dag.virtual_makespan_ms) > 1:
        raise OverheadAnalysisError(
            "E-OVHD-001",
            f"Σ(six buckets)={sum(bucket_ms.values())}ms + residual={residual_ms}ms = "
            f"{total}ms, which does not match virtual_makespan={dag.virtual_makespan_ms}ms "
            "within 1ms — the critical path is not being fully classified (PRD §16.2.3)",
        )

    residual_fraction = (
        residual_ms / dag.virtual_makespan_ms if dag.virtual_makespan_ms > 0 else 0.0
    )
    return OverheadDecomposition(
        bucket_ms=dict(bucket_ms),
        bucket_evidence_seq=bucket_evidence_seq,
        residual_ms=residual_ms,
        residual_fraction=residual_fraction,
        residual_flagged=residual_fraction >= tolerance,
        residual_tolerance=tolerance,
        virtual_makespan_ms=dag.virtual_makespan_ms,
        critical_path_length_ms=cp.length_ms,
    )


def decompose_total_work(
    dag: TimingDAG,
    redundancy_groups: tuple[RedundancyGroup, ...] | None = None,
) -> TotalWorkDecomposition:
    """PRD §16.2.1's total-work decomposition — every node in `dag`, not just the critical path.

    See the module docstring for why `handoff`/`blocking_wait` are always `0` here.
    """
    groups = redundancy_groups if redundancy_groups is not None else detect_redundancy(dag)
    duplicates = duplicate_node_ids(groups)

    bucket_ms = _empty_bucket_ms()
    bucket_evidence: dict[str, list[int]] = _empty_bucket_evidence()
    total_work_ms = 0

    for node_id in sorted(dag.nodes):
        node = dag.nodes[node_id]
        if node.duration_ms == 0:
            continue
        total_work_ms += node.duration_ms
        bucket = _classify_node(node, duplicates)
        bucket_ms[bucket] += node.duration_ms
        bucket_evidence[bucket].extend(node.evidence_seq)

    bucket_evidence_seq = {name: tuple(sorted(set(seqs))) for name, seqs in bucket_evidence.items()}
    return TotalWorkDecomposition(
        bucket_ms=dict(bucket_ms),
        bucket_evidence_seq=bucket_evidence_seq,
        total_work_ms=total_work_ms,
    )


def format_decomposition_table(decomposition: OverheadDecomposition) -> str:
    """Render `decomposition` as the week-5 demo-milestone terminal table.

    Deterministic: buckets print in `_BUCKET_ORDER`, one line each, followed by residual and
    the total-check line. Not localised, not colourised — a plain, greppable, evidence-bearing
    table (I6: every line carries the seqs behind it).
    """
    lines = ["Critical-path overhead decomposition", "=" * 60]
    makespan = decomposition.virtual_makespan_ms
    for name in _BUCKET_ORDER:
        ms = decomposition.bucket_ms[name]
        pct = (ms / makespan * 100) if makespan > 0 else 0.0
        seqs = decomposition.bucket_evidence_seq[name]
        seq_note = f"[seq {seqs[0]}..{seqs[-1]}]" if seqs else "[no evidence]"
        lines.append(f"  {name:<16} {ms:>6}ms  {pct:5.1f}%   {seq_note}")
    flag = " ⚠ UNATTRIBUTED" if decomposition.residual_flagged else ""
    residual_pct = decomposition.residual_fraction * 100
    lines.append(f"  {'residual':<16} {decomposition.residual_ms:>6}ms  {residual_pct:5.1f}%{flag}")
    lines.append("-" * 60)
    cp_ms = decomposition.critical_path_length_ms
    lines.append(f"  {'virtual makespan':<16} {makespan:>6}ms  (critical path {cp_ms}ms)")
    return "\n".join(lines)


__all__ = [
    "OverheadAnalysisError",
    "OverheadDecomposition",
    "TotalWorkDecomposition",
    "decompose_critical_path",
    "decompose_total_work",
    "format_decomposition_table",
]
