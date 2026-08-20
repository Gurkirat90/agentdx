"""Pydantic response and request models for the REST surface (PRD §26.1).

Every shape here is a direct, thin projection of a `store`/`analysis` dataclass — this
module adds no computation, only (de)serialisation. Field names and nesting follow the
PRD §26.1 example payloads verbatim wherever the PRD gives one; a field the PRD's own
example omits is documented with the PRD sub-section that introduces it.

`JsonValue` mirrors `agentdx.events.schema.PayloadValue` in Pydantic's vocabulary — the
event/finding/scorecard payload shapes are recursive JSON and this codebase already has one
canonical definition of "what JSON a payload may hold" (`events/schema.py`); this is not a
second one, only OpenAPI's own way of describing the same shape.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
"""A PEP 695 recursive alias, mirroring `events.schema.PayloadValue` for Pydantic's benefit.

`PayloadValue` itself (`events/schema.py`) is declared as a `TypeAlias`-annotated *string* —
a pattern mypy understands but that never resolves to a real runtime type, which is fine for
the plain dataclasses it annotates but not usable directly as a Pydantic field type (Pydantic
builds a real validator from the annotation, so it needs an evaluable type, not a string).
This is a second declaration of the same *shape*, not a competing definition of what JSON a
payload may hold — `events/schema.py` remains the one source of truth for the event schema
itself, and nothing here re-implements schema validation.
"""


class ApiModel(BaseModel):
    """Base class for every response/request model.

    Guarantees: `populate_by_name` lets a model be built from either its Python field name
    or an explicit `alias` (none currently declared — reserved for the day one is needed);
    extra fields on input are rejected rather than silently dropped, so a client typo in a
    request body surfaces as `422`, not as an ignored field.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------------------
# §26 common error envelope
# ---------------------------------------------------------------------------------------


class ErrorBody(ApiModel):
    """The `error` object of the PRD §26 common error envelope."""

    code: str = Field(description="A stable `E-XXX-NNN` code (PRD §36).")
    message: str = Field(description="A human-readable, user-facing explanation.")
    detail: dict[str, JsonValue] = Field(
        default_factory=dict, description="Structured, code-specific context."
    )
    docs: str = Field(description="A link to the docs section explaining this code.")


class ErrorEnvelope(ApiModel):
    """`{"error": {...}}` — returned for every non-2xx response, no exceptions (PRD §26)."""

    error: ErrorBody


# ---------------------------------------------------------------------------------------
# §26.1 POST /api/runs
# ---------------------------------------------------------------------------------------


class ExploreOptions(ApiModel):
    """PRD §26.1 `POST /api/runs` request body's `explore` object."""

    enabled: bool = False


class RunCreateRequest(ApiModel):
    """`POST /api/runs` request body (PRD §26.1, `[SOURCE]`)."""

    scenario_id: str
    mode: Literal["baseline", "chaos", "replay"]
    seed: Annotated[int, Field(ge=-(2**31), le=2**31 - 1)] = 42
    explore: ExploreOptions = Field(default_factory=ExploreOptions)


class RunCreateResponse(ApiModel):
    """`202` response of `POST /api/runs`."""

    run_id: str
    status: str
    ws: str


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs
# ---------------------------------------------------------------------------------------


class RunSummary(ApiModel):
    """One row of `GET /api/runs`'s `runs` array — `RunRecord`, projected."""

    run_id: str
    scenario_id: str | None
    status: str
    mode: str
    seed: int
    created_at: str
    sealed_at: str | None
    virtual_makespan_ms: int | None
    wall_makespan_ms: int | None
    event_count: int | None
    baseline_of: str | None
    replay_of: str | None


class RunListResponse(ApiModel):
    """`GET /api/runs` response."""

    runs: tuple[RunSummary, ...]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}
# ---------------------------------------------------------------------------------------


class ScenarioRef(ApiModel):
    """Which scenario a run used — id (if known) plus its resolved content hash."""

    id: str | None
    hash: str


class GraphRef(ApiModel):
    """A run's graph identity — content hash plus the observed agent roster."""

    hash: str
    agents: tuple[str, ...]


class TimingSummary(ApiModel):
    """Makespan figures for `GET /runs/{id}` — virtual, wall and critical-path ms."""

    virtual_makespan_ms: int | None
    wall_makespan_ms: int | None
    critical_path_ms: int | None


class CountsSummary(ApiModel):
    """Real per-run counts: events, spans, messages, LLM calls and tokens."""

    events: int
    spans: int
    messages: int
    llm_calls: int
    tokens: int


class DeterminismSummary(ApiModel):
    """A run's determinism health: canonical log hash and warning counts."""

    canonical_log_hash: str | None
    unwrapped_tools: int
    nondeterminism_warnings: int


class Metric(ApiModel):
    """`analysis.verdict.Metric`, projected (PRD §18.4)."""

    name: str
    value: float
    unit: str


class Evidence(ApiModel):
    """`analysis.verdict.Evidence`, projected (PRD §18.4) — I6: `event_seqs` non-empty."""

    event_seqs: tuple[int, ...]
    span_ids: tuple[str, ...]
    computation: str


class VerdictFinding(ApiModel):
    """One finding cited by a `Verdict` (PRD §18.4), projected."""

    finding_id: str
    type: str
    severity: str
    claim: str
    metric: Metric
    evidence: Evidence
    confidence: str


class Recommendation(ApiModel):
    """One `analysis.verdict.Recommendation`, projected (PRD §18.4)."""

    trigger: str
    text: str
    evidence: Evidence


class VerdictOut(ApiModel):
    """`analysis.verdict.Verdict`, projected — `GET /runs/{id}`'s `verdict` object (PRD §18)."""

    verdict_class: str = Field(alias="class")
    secondary_classes: tuple[str, ...]
    coordination_score: int | None
    confidence: str
    headline: str
    findings: tuple[VerdictFinding, ...]
    recommendations: tuple[Recommendation, ...]
    evidence: Evidence


class RunDetail(ApiModel):
    """`GET /api/runs/{id}` response (PRD §26.1, `[SOURCE]`).

    `verdict` is `None` until the run's scorecard has been computed and persisted — no
    prompt in this build yet runs the full `timing -> overhead -> redundancy -> race ->
    baseline -> resilience -> verdict` pipeline and calls `store.upsert_scorecard` (P17,
    not started; see `docs/api.md` "What this endpoint does not yet do"). This is reported
    as `null`, not fabricated and not a 404 — PRD §36 rule 2, "partial results beat no
    results": the run's own metadata is always real, even when its verdict is not yet in.
    """

    run_id: str
    status: str
    mode: str
    seed: int
    scenario: ScenarioRef
    graph: GraphRef
    timing: TimingSummary
    counts: CountsSummary
    determinism: DeterminismSummary
    verdict: VerdictOut | None
    baseline_run_id: str | None
    comparability: str | None


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}/events
# ---------------------------------------------------------------------------------------


class EventOut(ApiModel):
    """One event, exactly as `events.canonical.encode_event` serialises it."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)
    schema_version: int
    run_id: str
    seq: int
    sched_step: int
    virtual_ts_ms: int
    wall_ts_ms: int
    vclock: dict[str, int]
    type: str
    causal_parents: tuple[int, ...]
    payload: dict[str, JsonValue]
    agent_id: str | None = None
    clock_slot: str | None = None
    span_id: str | None = None
    fault_id: str | None = None


class EventsPageResponse(ApiModel):
    """`GET /api/runs/{id}/events` response — one page, cursor and total."""

    events: tuple[EventOut, ...]
    next_from_seq: int | None
    total: int


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}/graph
# ---------------------------------------------------------------------------------------


class GraphNode(ApiModel):
    """One agent node of `GET /api/runs/{id}/graph` (PRD §26.1)."""

    id: str
    role: str | None
    busy_ms: int
    idle_ms: int
    cp_ms: int
    tokens: int
    status: str


class GraphEdge(ApiModel):
    """One agent-pair edge of `GET /api/runs/{id}/graph` (PRD §26.1)."""

    from_: str = Field(alias="from")
    to: str
    messages: int
    total_handoff_ms: int
    cp_handoff_ms: int
    cp_share: float
    on_critical_path: bool


class GraphResponse(ApiModel):
    """`GET /api/runs/{id}/graph` response: nodes, edges and the critical path."""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    critical_path: tuple[str, ...]


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}/waterfall
# ---------------------------------------------------------------------------------------


class WaterfallSpan(ApiModel):
    """One span row of `GET /api/runs/{id}/waterfall` (PRD §26.1)."""

    span_id: str
    kind: str
    name: str | None
    start_ms: int
    end_ms: int
    bucket: str | None
    on_critical_path: bool
    status: str | None
    fault_id: str | None
    seq_start: int
    seq_end: int


class WaterfallLane(ApiModel):
    """One agent's lane of spans in a `GET /api/runs/{id}/waterfall` response."""

    agent: str
    spans: tuple[WaterfallSpan, ...]


class WaterfallResponse(ApiModel):
    """`GET /api/runs/{id}/waterfall` response: makespans plus every lane."""

    virtual_makespan_ms: int
    baseline_makespan_ms: int | None
    lanes: tuple[WaterfallLane, ...]


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}/findings
# ---------------------------------------------------------------------------------------


class FindingOut(ApiModel):
    """`store.FindingRecord`, projected."""

    id: str
    type: str
    subtype: str | None
    severity: str
    title: str
    description: str
    evidence: dict[str, JsonValue]
    recommendation: str | None
    repro_scenario: str | None
    suppressed_by: str | None


class FindingsResponse(ApiModel):
    """`GET /api/runs/{id}/findings` response — one run's findings, filtered."""

    findings: tuple[FindingOut, ...]


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}/scorecard
# ---------------------------------------------------------------------------------------


class ScorecardResponse(ApiModel):
    """PRD §17.4's structure, as persisted (`store.ScorecardRecord.payload`).

    Passed through unreshaped: the route serialises what the analysis pipeline wrote, and
    inventing a stricter schema here would be a second, competing opinion of what a
    scorecard contains.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------------------
# §26.1 POST /api/runs/{id}/faults
# ---------------------------------------------------------------------------------------


class FaultTrigger(ApiModel):
    """The `trigger` object of a `POST /api/runs/{id}/faults` request body."""

    model_config = ConfigDict(extra="allow")
    immediate: bool = False


class FaultInjectRequest(ApiModel):
    """`POST /api/runs/{id}/faults` request body (PRD §26.1, `[SOURCE]`)."""

    type: str
    target: str
    params: dict[str, JsonValue] = Field(default_factory=dict)
    trigger: FaultTrigger = Field(default_factory=FaultTrigger)


class FaultInjectResponse(ApiModel):
    """`202` response of `POST /api/runs/{id}/faults`."""

    fault_id: str
    armed_at_virtual_ts: int


# ---------------------------------------------------------------------------------------
# §26.1 POST /api/runs/compare
# ---------------------------------------------------------------------------------------


class CompareRequest(ApiModel):
    """`POST /api/runs/compare` request body (PRD §26.1, `[SOURCE]`)."""

    run_a: str
    run_b: str
    force: bool = False


class MetricDelta(ApiModel):
    """One metric's value in each run plus the difference between them."""

    name: str
    run_a: float | None
    run_b: float | None
    delta: float | None


class CompareResponse(ApiModel):
    """`POST /api/runs/compare` response: metric deltas, finding diff, verdict change."""

    run_a: str
    run_b: str
    metric_deltas: tuple[MetricDelta, ...]
    findings_added: tuple[FindingOut, ...]
    findings_removed: tuple[FindingOut, ...]
    findings_unchanged: tuple[FindingOut, ...]
    verdict_changed: bool
    verdict_a: str | None
    verdict_b: str | None


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/scenarios · GET /api/scenarios/{id} · POST /api/scenarios/validate
# ---------------------------------------------------------------------------------------


class ScenarioSummary(ApiModel):
    """One row of `GET /api/scenarios`'s listing — store- or file-sourced."""

    scenario_id: str
    source: Literal["store", "file"]
    path: str | None
    version: int | None


class ScenarioListResponse(ApiModel):
    """`GET /api/scenarios` response."""

    scenarios: tuple[ScenarioSummary, ...]


class ScenarioDetail(ApiModel):
    """`GET /api/scenarios/{id}` response: raw content plus its resolved form."""

    scenario_id: str
    source: Literal["store", "file"]
    path: str | None
    content: str
    content_hash: str | None
    resolved: dict[str, JsonValue]


class ScenarioValidateRequest(ApiModel):
    """`POST /api/scenarios/validate` request body.

    Exactly one of `text` (raw YAML, e.g. from the UI's chaos panel) or `scenario_id` (an
    already-known scenario, from the store or `scenarios_dir`) must be given.
    """

    text: str | None = None
    scenario_id: str | None = None
    source_name: str = "<scenario>"


class ScenarioValidationErrorOut(ApiModel):
    """One `scenario.validate.ScenarioError`, projected."""

    code: str
    path: str
    message: str
    suggestion: str
    file: str | None
    line: int | None
    docs: str


class ScenarioValidateResponse(ApiModel):
    """`POST /api/scenarios/validate` response — always `200`, see the route's docstring."""

    valid: bool
    resolved: dict[str, JsonValue] | None
    errors: tuple[ScenarioValidationErrorOut, ...]
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}/exploration
# ---------------------------------------------------------------------------------------


class ExplorationResponse(ApiModel):
    """PRD §26.1 / §15.6. `coverage_statement` is I10's required, verbatim field."""

    k: int
    schedules_executed: int
    unique_schedules: int
    reduced_away: int
    new_findings: tuple[FindingOut, ...]
    capped: bool
    coverage_statement: str


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}/state
# ---------------------------------------------------------------------------------------


class StateKey(ApiModel):
    """One state key's reconstructed value as of a virtual timestamp (PRD §20.4)."""

    key: str
    value_hash: str
    value: JsonValue = None
    writer: str | None
    written_at_seq: int
    size_bytes: int | None = None


class StateResponse(ApiModel):
    """`GET /api/runs/{id}/state` response."""

    at_virtual_ts: int
    at_seq: int | None
    keys: tuple[StateKey, ...]


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}/export · POST /api/import
# ---------------------------------------------------------------------------------------


class ImportResponse(ApiModel):
    """`POST /api/import` response: the newly assigned local `run_id`."""

    run_id: str


# ---------------------------------------------------------------------------------------
# §35 GET /api/health · GET /api/version
# ---------------------------------------------------------------------------------------


class HealthResponse(ApiModel):
    """`GET /api/health` response (PRD §35)."""

    status: Literal["ok"] = "ok"


class VersionResponse(ApiModel):
    """`GET /api/version` response: package version plus event schema version."""

    version: str
    schema_version: int
