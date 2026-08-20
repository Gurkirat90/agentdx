"""Run lifecycle endpoints: create, list, detail, events, faults, compare, export/import.

The one route module that genuinely launches and mutates things, not just reads them — and
the one most shaped by two gaps this build carries into P14 (CONTEXT.md §7):

1. **No `RunHost`.** `sdk.generic.RunHost` — "the half of `agentdx.run` that P06 owns" — does
   not exist. `POST /api/runs` and `POST .../faults` are implemented for real (scenario
   resolution, validation, hashing, concurrency accounting, blast-radius/opt-in checks) and
   refuse cleanly (`RunLaunchUnavailableError`/`FaultControlUnavailableError`, `503`) at the
   one point that would need to signal a process this codebase cannot start yet — see
   `deps.RunLauncher`/`deps.FaultController`.
2. **No orchestrator (P17).** Nothing in this build runs `timing -> ... -> verdict` and
   calls `store.upsert_scorecard`, so `GET /runs/{id}`'s `verdict` is always `None` — see
   `models.RunDetail`'s own docstring. `counts`/`timing`/`determinism`, by contrast, *are*
   real: computed from the stored log via a handful of indexed queries (`Store.
   count_events_of_type`/`sum_llm_tokens`/`list_agent_ids`, added in this prompt — see their
   docstrings), not fabricated placeholders and not the full timing DAG (`get_graph`/
   `get_waterfall` already pay that cost where the PRD's own example payload needs it; this
   route does not need the DAG for anything it reports).
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import version as _installed_version
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from agentdx.api.deps import ApiState, get_state, get_store, get_store_async_http
from agentdx.api.errors import (
    ChaosAuthorizationError,
    CompareIncomparableError,
    FaultControlUnavailableError,
    FaultTargetNotFoundError,
    RunLaunchUnavailableError,
    RunNotFoundError,
    RunNotRunningError,
    ScenarioNotFoundError,
    TooManyConcurrentRunsError,
    UnknownFaultTypeError,
)
from agentdx.api.models import (
    CompareRequest,
    CompareResponse,
    CountsSummary,
    DeterminismSummary,
    EventOut,
    EventsPageResponse,
    FaultInjectRequest,
    FaultInjectResponse,
    GraphRef,
    ImportResponse,
    MetricDelta,
    RunCreateRequest,
    RunCreateResponse,
    RunDetail,
    RunListResponse,
    RunSummary,
    ScenarioRef,
    TimingSummary,
)
from agentdx.api.routes.findings import _project as _project_finding
from agentdx.events.schema import Event, EventType
from agentdx.scenario import loader, validate
from agentdx.scenario.schema import FAULT_CATALOGUE, FaultTier, ParamConstraint, TargetKind
from agentdx.scenario.validate import ScenarioValidationError
from agentdx.store.bundle import export_bundle, import_bundle
from agentdx.store.sqlite import RunRecord, ScenarioRecord, Store

router = APIRouter(tags=["runs"])

# `datetime.now` is banned project-wide (`scripts/check_determinism_hygiene.py`) — every
# other layer takes wall time from `agentdx.wall_time()`, but that accessor lives in
# `runtime/clock.py`, which `api/` may not import even transitively (`lint-imports` confirmed
# this the hard way for `routes/system.py`'s `__version__`, see that module). `api/` is
# allowlisted for clause 4 ("long-lived server, never inside a run") precisely for reads like
# this one — a run's `created_at` is server-clock provenance, not part of any run's own
# recorded, replay-significant timeline.
_ISO_FMT: Final = "%Y-%m-%dT%H:%M:%SZ"


def _now_iso() -> str:
    return datetime.now(UTC).strftime(_ISO_FMT)  # determinism-exempt: §4.1(4) api/ server clock


def _agentdx_version() -> str:
    return _installed_version("agentdx")


# ---------------------------------------------------------------------------------------
# Scenario resolution — shared by POST /api/runs and POST .../faults
# ---------------------------------------------------------------------------------------


def _read_scenario_text(scenario_id: str, state: ApiState, store: Store) -> tuple[str, str | None]:
    """Return `(text, path)` for a scenario id, store first, then `scenarios_dir`.

    Raises:
        ScenarioNotFoundError: neither source has this id (handled globally, `404`).
    """
    record = store.get_scenario(scenario_id)
    if record is not None:
        return record.content, record.path
    directory = state.config.api.scenarios_dir
    for path in (directory / f"{scenario_id}.yaml", directory / f"{scenario_id}.yml"):
        if path.is_file():
            return path.read_text(encoding="utf-8"), str(path)
    raise ScenarioNotFoundError(scenario_id)


def _resolve_and_validate(
    scenario_id: str, state: ApiState, store: Store
) -> tuple[dict[str, object], str, str, str | None]:
    """Return `(resolved_data, scenario_hash, raw_text, path)`, parsed, extended, validated.

    `raw_text` is the exact source the resolution started from — what gets pinned into
    `store.scenarios` by the caller, not a re-serialisation of `resolved_data` (dumping the
    resolved form back to YAML would silently rewrite comments/formatting/`extends` structure
    the author wrote; the source text is the only faithful record of what they authored).

    Raises:
        ScenarioNotFoundError: `404`.
        ScenarioLoadError: `400` — malformed YAML or a broken `extends` chain.
        ScenarioValidationError: `400` — the resolved document fails PRD §21.3's rules. Unlike
            `POST /api/scenarios/validate`, this call site is not dry-run-safe: a run that
            cannot be validated must not be launched, so this is allowed to raise up to the
            global handler rather than being caught and reported as `valid: false`.
    """
    text, path = _read_scenario_text(scenario_id, state, store)
    parsed = loader.parse_scenario_text(text, source_name=path or scenario_id)
    parsed = loader.resolve_extends(parsed)
    errors = validate.validate(parsed)
    if errors:
        raise ScenarioValidationError(errors)
    resolved = loader.resolve_defaults(parsed.data or {})
    scenario_hash = loader.compute_scenario_hash(resolved)
    return resolved, scenario_hash, text, path


def _is_graph_target(resolved: dict[str, object]) -> bool:
    """Return True iff the scenario targets a user graph, not a fixture (I12)."""
    target = resolved.get("target")
    return isinstance(target, dict) and "graph" in target


# ---------------------------------------------------------------------------------------
# §26.1 POST /api/runs · GET /api/runs
# ---------------------------------------------------------------------------------------


@router.post(
    "/runs",
    response_model=RunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a run (PRD §26.1)",
)
def create_run(
    body: RunCreateRequest,
    state: Annotated[ApiState, Depends(get_state)],
    store: Annotated[Store, Depends(get_store)],
) -> RunCreateResponse:
    """Validate, check the concurrency gate, and hand off to the configured `RunLauncher`.

    Raises:
        ScenarioNotFoundError / ScenarioLoadError / ScenarioValidationError: `400`-family —
            the scenario does not exist or does not validate.
        TooManyConcurrentRunsError: `409 E-RUN-409`.
        RunLaunchUnavailableError: `503` — no `RunLauncher` is configured on this server
            (CONTEXT.md §7's `RunHost` gap; see this module's docstring).
    """
    resolved, scenario_hash, text, path = _resolve_and_validate(body.scenario_id, state, store)

    running = sum(1 for r in store.list_runs() if r.status == "running")
    limit = state.config.api.concurrent_run_limit
    if running >= limit:
        raise TooManyConcurrentRunsError(limit, running)

    if state.run_launcher is None:
        raise RunLaunchUnavailableError()
    result = state.run_launcher.launch(
        scenario_id=body.scenario_id, mode=body.mode, seed=body.seed, explore=body.explore.enabled
    )

    # Pin the exact scenario a real run used (PRD §27.2: "editing a scenario cannot
    # retroactively change what a past run was") — upserted under its own id, not the run id;
    # a scenario is reusable across many runs by design (PRD §6.1). `content` is the raw
    # source text, not a re-serialisation of the resolved+defaulted form (see
    # `_resolve_and_validate`'s docstring).
    version = resolved.get("version")
    store.upsert_scenario(
        ScenarioRecord(
            scenario_id=body.scenario_id,
            content=text,
            content_hash=scenario_hash,
            version=version if isinstance(version, int) else 1,
            path=path,
        )
    )

    record = RunRecord(
        run_id=result.run_id,
        scenario_hash=scenario_hash,
        graph_hash=result.graph_hash,
        mode=body.mode,
        seed=body.seed,
        status="running",
        created_at=_now_iso(),
        agentdx_version=_agentdx_version(),
        scenario_id=body.scenario_id,
    )
    store.create_run(record)
    state.concurrency.note_launched(result.run_id)
    return RunCreateResponse(run_id=result.run_id, status="running", ws=f"/ws/runs/{result.run_id}")


def _run_target_kind(store: Store, record: RunRecord) -> str | None:
    """Return `"fixture"`/`"graph"`, best-effort, for the `?fixture=` filter on `GET /runs`.

    `None` when the run's scenario can no longer be resolved (a scenario is mutable and may
    since have been deleted from both the store and `scenarios_dir`) — such a run is excluded
    from a `fixture`-filtered listing rather than guessed at, per PRD §36 rule 1.
    """
    if record.scenario_id is None:
        return None
    scenario = store.get_scenario(record.scenario_id)
    if scenario is None:
        return None
    try:
        parsed = loader.parse_scenario_text(scenario.content, source_name=record.scenario_id)
        resolved = loader.resolve_defaults(parsed.data or {})
    except loader.ScenarioLoadError:
        return None
    return "graph" if _is_graph_target(resolved) else "fixture"


@router.get("/runs", response_model=RunListResponse, summary="List runs (PRD §26.1)")
def list_runs(
    store: Annotated[Store, Depends(get_store)],
    state: Annotated[ApiState, Depends(get_state)],
    status_: Annotated[
        str | None, Query(alias="status", description="Filter: exact status match.")
    ] = None,
    fixture: Annotated[
        bool | None,
        Query(description="Filter: True for fixture-targeted runs, False for user-graph runs."),
    ] = None,
    limit: Annotated[int | None, Query(ge=1, description="Page size.")] = None,
    cursor: Annotated[str | None, Query(description="Opaque `created_at|run_id` cursor.")] = None,
) -> RunListResponse:
    """List runs, newest first, cursor-paginated by `(created_at desc, run_id)` (PRD §26.1).

    `Store.list_runs()` returns every run (ordered by `run_id`, not `created_at`) — there is
    no indexed `created_at`-ordered read to page through (extending `store/` with one is
    outside this prompt's `DELIVERABLES`; see `docs/api.md`). This route re-sorts in Python,
    which is O(run count) rather than O(page size) — acceptable for a `runs` table (hundreds
    to low thousands of rows in realistic local use), unlike the `events` table this same
    trade-off would be disqualifying for (Design Constraint 6).
    """
    effective_limit = limit if limit is not None else state.config.api.runs_page_limit_default
    records = [r for r in store.list_runs() if status_ is None or r.status == status_]
    if fixture is not None:
        records = [r for r in records if (_run_target_kind(store, r) == "fixture") == fixture]
    records.sort(key=lambda r: (r.created_at, r.run_id), reverse=True)

    start = 0
    if cursor is not None:
        for index, r in enumerate(records):
            if f"{r.created_at}|{r.run_id}" == cursor:
                start = index + 1
                break
    page = records[start : start + effective_limit]
    next_cursor = (
        f"{page[-1].created_at}|{page[-1].run_id}" if len(records) > start + len(page) else None
    )
    summaries = tuple(
        RunSummary(
            run_id=r.run_id,
            scenario_id=r.scenario_id,
            status=r.status,
            mode=r.mode,
            seed=r.seed,
            created_at=r.created_at,
            sealed_at=r.sealed_at,
            virtual_makespan_ms=r.virtual_makespan_ms,
            wall_makespan_ms=r.wall_makespan_ms,
            event_count=r.event_count,
            baseline_of=r.baseline_of,
            replay_of=r.replay_of,
        )
        for r in page
    )
    return RunListResponse(runs=summaries, next_cursor=next_cursor)


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}
# ---------------------------------------------------------------------------------------


def _comparability_grade(store: Store, run_id: str) -> str | None:
    """Return the run's comparability grade if a scorecard has been computed, else None."""
    record = store.get_scorecard(run_id)
    if record is None:
        return None
    comparability = record.payload.get("comparability")
    if isinstance(comparability, dict):
        grade = comparability.get("grade")
        if isinstance(grade, str):
            return grade
    return None


@router.get(
    "/runs/{run_id}", response_model=RunDetail, summary="Run metadata + verdict (PRD §26.1)"
)
def get_run_detail(run_id: str, store: Annotated[Store, Depends(get_store)]) -> RunDetail:
    """Return a run's metadata, real counts and (when computed) its verdict.

    Raises:
        RunNotFoundError: `E-RUN-404`.

    `timing.critical_path_ms` is reported as `null`: computing it means building the full
    timing DAG (`analysis.timing.build_timing_dag` + `critical_path`), the same cost
    `GET .../graph` already pays where the PRD's own example payload needs the number — this
    endpoint does not additionally pay that cost on every call just to fill one field callers
    needing it can get from `.../graph` directly. `determinism.unwrapped_tools` is `0` on
    every run: no event or field anywhere in this codebase currently records it (grepped, not
    assumed) — a real, declared gap, not a fabricated zero passed off as a real count.
    """
    record = store.get_run(run_id)
    if record is None:
        raise RunNotFoundError(run_id)

    counts = CountsSummary(
        events=store.event_count(run_id),
        spans=store.count_events_of_type(run_id, EventType.SPAN_START),
        messages=store.count_events_of_type(run_id, EventType.MESSAGE_SEND),
        llm_calls=store.count_events_of_type(run_id, EventType.LLM_CALL),
        tokens=store.sum_llm_tokens(run_id),
    )
    determinism = DeterminismSummary(
        canonical_log_hash=record.canonical_log_hash,
        unwrapped_tools=0,
        nondeterminism_warnings=store.count_events_of_type(
            run_id, EventType.NONDETERMINISM_WARNING
        ),
    )
    timing = TimingSummary(
        virtual_makespan_ms=record.virtual_makespan_ms,
        wall_makespan_ms=record.wall_makespan_ms,
        critical_path_ms=None,
    )
    return RunDetail(
        run_id=record.run_id,
        status=record.status,
        mode=record.mode,
        seed=record.seed,
        scenario=ScenarioRef(id=record.scenario_id, hash=record.scenario_hash),
        graph=GraphRef(hash=record.graph_hash, agents=store.list_agent_ids(run_id)),
        timing=timing,
        counts=counts,
        determinism=determinism,
        verdict=None,
        baseline_run_id=record.baseline_of,
        comparability=_comparability_grade(store, run_id),
    )


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}/events
# ---------------------------------------------------------------------------------------


def _project_event(event: Event) -> EventOut:
    return EventOut(
        schema_version=event.schema_version,
        run_id=event.run_id,
        seq=event.seq,
        sched_step=event.sched_step,
        virtual_ts_ms=event.virtual_ts_ms,
        wall_ts_ms=event.wall_ts_ms,
        vclock=dict(event.vclock),
        type=event.type.value,
        causal_parents=tuple(event.causal_parents),
        payload=dict(event.payload),
        agent_id=event.agent_id,
        clock_slot=event.clock_slot,
        span_id=event.span_id,
        fault_id=event.fault_id,
    )


@router.get(
    "/runs/{run_id}/events",
    response_model=EventsPageResponse,
    summary="Paginated event log (PRD §26.1)",
)
def get_events(
    run_id: str,
    store: Annotated[Store, Depends(get_store)],
    state: Annotated[ApiState, Depends(get_state)],
    from_seq: Annotated[int, Query(ge=0, description="First seq to include, inclusive.")] = 0,
    limit: Annotated[
        int | None, Query(ge=1, description="Page size, capped at the server max.")
    ] = None,
    types: Annotated[str | None, Query(description="Comma-separated `EventType` values.")] = None,
    agent: Annotated[str | None, Query(description="Filter: exact `agent_id` match.")] = None,
) -> EventsPageResponse:
    """Return one page of a run's event log, streamed rather than materialised.

    Design Constraint 6: never load a 200 000-event run into memory for one page.

    Raises:
        RunNotFoundError: `E-RUN-404`.

    `next_from_seq` resumes correctly during a live run: it is the seq immediately after the
    last event this call *read* (matched or not), so a client that pages to the current end
    and asks again later picks up exactly where new events start, with no gap and no
    duplicate — the same cursor discipline PRD §26.2 requires of the WebSocket reconnect path.
    """
    if store.get_run(run_id) is None:
        raise RunNotFoundError(run_id)

    max_limit = state.config.api.events_page_limit_max
    default_limit = state.config.api.events_page_limit_default
    effective_limit = min(limit, max_limit) if limit is not None else default_limit

    wanted_types = frozenset(t.strip() for t in types.split(",") if t.strip()) if types else None

    page: list[EventOut] = []
    last_seen_seq = from_seq - 1
    for event in store.read_events(run_id, from_seq=from_seq):
        last_seen_seq = event.seq
        if wanted_types is not None and event.type.value not in wanted_types:
            continue
        if agent is not None and event.agent_id != agent:
            continue
        page.append(_project_event(event))
        if len(page) >= effective_limit:
            break

    next_from_seq: int | None = last_seen_seq + 1
    return EventsPageResponse(
        events=tuple(page), next_from_seq=next_from_seq, total=store.event_count(run_id)
    )


# ---------------------------------------------------------------------------------------
# §26.1 POST /api/runs/{id}/faults
# ---------------------------------------------------------------------------------------


def _check_param(name: str, value: object, constraint: ParamConstraint) -> str | None:
    """Return a validation error string for `value` against `constraint`, or None if valid.

    Mirrors `scenario/validate.py`'s per-value shape check (`py_type`/`minimum`/`maximum`/
    `choices`) against `FAULT_CATALOGUE`'s own public `ParamConstraint` — written fresh here
    rather than calling `validate.py`'s private `_check_param_constraint` (this module does
    not reach into another module's private surface; see `analysis.py`'s `bucket` field for
    the same rule applied elsewhere in this prompt). `bool` is checked ahead of `int`:
    `isinstance(True, int)` is `True` in Python, so an `int` constraint must reject `bool`
    explicitly, exactly as `validate.py`'s own docstring for `ParamConstraint` notes.
    """
    if constraint.py_type is bool:
        return None if isinstance(value, bool) else f"{name!r} must be a bool"
    if constraint.py_type is int:
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{name!r} must be an int"
        if constraint.minimum is not None and value < constraint.minimum:
            return f"{name!r} must be >= {constraint.minimum}"
        if constraint.maximum is not None and value > constraint.maximum:
            return f"{name!r} must be <= {constraint.maximum}"
        return None
    if constraint.py_type is str:
        if not isinstance(value, str):
            return f"{name!r} must be a str"
        if constraint.choices is not None and value not in constraint.choices:
            return f"{name!r} must be one of {sorted(constraint.choices)}"
        return None
    return None  # no py_type this checker knows how to validate — declared, not silent


def _check_chaos_authorization(resolved: dict[str, object], target: str) -> None:
    """Enforce I12/§13.3 for a user-graph scenario; fixture targets are chaos-safe by default.

    Raises:
        ChaosAuthorizationError: `403 E-CHAOS-001`.
    """
    if not _is_graph_target(resolved):
        return
    if resolved.get("chaos_opt_in") is not True:
        raise ChaosAuthorizationError.opt_in_required()
    blast_radius = resolved.get("blast_radius")
    declared: list[str] = []
    if isinstance(blast_radius, dict):
        for value in blast_radius.values():
            if isinstance(value, list):
                declared.extend(str(v) for v in value)
    if not declared:
        raise ChaosAuthorizationError.blast_radius_empty(blast_radius)
    if target not in declared:
        raise ChaosAuthorizationError.target_outside_blast_radius(target, blast_radius)


@router.post(
    "/runs/{run_id}/faults",
    response_model=FaultInjectResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Inject a fault into a live run (PRD §26.1)",
)
def inject_fault(
    run_id: str,
    body: FaultInjectRequest,
    state: Annotated[ApiState, Depends(get_state)],
    store: Annotated[Store, Depends(get_store)],
) -> FaultInjectResponse:
    """Validate and arm a fault against a running run, via the configured `FaultController`.

    Raises:
        RunNotFoundError: `E-RUN-404`.
        RunNotRunningError: `409` — the run is not `running`.
        UnknownFaultTypeError: `400` — not in `FAULT_CATALOGUE`, or a declared-but-unshipped
            P1 fault (CONTEXT.md §3: only the P0 tier executes in this build).
        FaultTargetNotFoundError: `400 E-FAULT-001` — only checked when the fault's
            `target_kinds` is unambiguously `(agent,)`, against the run's real agent roster
            (`store.list_agent_ids`); edge/tool/state_key/provider targets are not verified
            (declared gap — this build has no cheap way to enumerate them without a graph
            resolution `api/` cannot perform, see this module's docstring).
        ChaosAuthorizationError: `403 E-CHAOS-001` — I12/§13.3.
        FaultControlUnavailableError: `503` — no `FaultController` configured.
    """
    record = store.get_run(run_id)
    if record is None:
        raise RunNotFoundError(run_id)
    if record.status != "running":
        raise RunNotRunningError(run_id, record.status)

    spec = FAULT_CATALOGUE.get(body.type)
    if spec is None or spec.tier is not FaultTier.P0:
        raise UnknownFaultTypeError(body.type)

    for key, value in body.params.items():
        if key not in spec.params:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"fault {body.type!r} does not accept a {key!r} parameter",
            )
        constraint = spec.param_constraints.get(key)
        if constraint is not None:
            problem = _check_param(key, value, constraint)
            if problem is not None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=problem)

    if spec.target_kinds == (TargetKind.AGENT,) and body.target not in store.list_agent_ids(run_id):
        raise FaultTargetNotFoundError(body.target, run_id)

    if record.scenario_id is not None:
        scenario = store.get_scenario(record.scenario_id)
        if scenario is not None:
            try:
                parsed = loader.parse_scenario_text(
                    scenario.content, source_name=record.scenario_id
                )
                resolved = loader.resolve_defaults(parsed.data or {})
            except loader.ScenarioLoadError:
                resolved = {}
            _check_chaos_authorization(resolved, body.target)

    if state.fault_controller is None:
        raise FaultControlUnavailableError()
    result = state.fault_controller.arm(
        run_id=run_id,
        fault_type=body.type,
        target=body.target,
        params=dict(body.params),
        immediate=body.trigger.immediate,
    )
    return FaultInjectResponse(
        fault_id=result.fault_id, armed_at_virtual_ts=result.armed_at_virtual_ts
    )


# ---------------------------------------------------------------------------------------
# §26.1 POST /api/runs/compare
# ---------------------------------------------------------------------------------------


def _metric_delta(name: str, a: int | None, b: int | None) -> MetricDelta:
    delta = float(b) - float(a) if a is not None and b is not None else None
    return MetricDelta(
        name=name,
        run_a=float(a) if a is not None else None,
        run_b=float(b) if b is not None else None,
        delta=delta,
    )


@router.post(
    "/runs/compare", response_model=CompareResponse, summary="Compare two runs (PRD §26.1)"
)
def compare_runs(
    body: CompareRequest, store: Annotated[Store, Depends(get_store)]
) -> CompareResponse:
    """Diff two runs' real, available metrics and findings.

    Raises:
        RunNotFoundError: `E-RUN-404` — either run.
        CompareIncomparableError: `400 E-CMP-001` — different scenario hashes, no `force`.

    `metric_deltas` covers exactly the numeric fields both runs' own `RunRecord`s carry for
    real (`virtual_makespan_ms`, `wall_makespan_ms`, `event_count`) — not a wider metric set,
    because nothing in this build computes one (no `analysis.compare`/`diff` module exists;
    Design Constraint 1: a route needing a missing computation is a declared gap, not
    something to invent inline). `verdict_changed`/`verdict_a`/`verdict_b` are always
    `False`/`None`/`None` for the same reason `GET /runs/{id}`'s `verdict` is always `None`
    (P17 gap). Findings are matched across runs by `(type, subtype, severity, title)` — the
    two runs' `finding_id`s are independently generated and never equal, so identity here is
    content-based, declared as such rather than silently assumed to be `finding_id` equality.
    """
    record_a = store.get_run(body.run_a)
    if record_a is None:
        raise RunNotFoundError(body.run_a)
    record_b = store.get_run(body.run_b)
    if record_b is None:
        raise RunNotFoundError(body.run_b)
    if record_a.scenario_hash != record_b.scenario_hash and not body.force:
        raise CompareIncomparableError(body.run_a, body.run_b)

    metric_deltas = (
        _metric_delta(
            "virtual_makespan_ms", record_a.virtual_makespan_ms, record_b.virtual_makespan_ms
        ),
        _metric_delta("wall_makespan_ms", record_a.wall_makespan_ms, record_b.wall_makespan_ms),
        _metric_delta("event_count", record_a.event_count, record_b.event_count),
    )

    findings_a = {
        (f.type, f.subtype, f.severity, f.title): f for f in store.list_findings(body.run_a)
    }
    findings_b = {
        (f.type, f.subtype, f.severity, f.title): f for f in store.list_findings(body.run_b)
    }
    added = tuple(_project_finding(f) for k, f in findings_b.items() if k not in findings_a)
    removed = tuple(_project_finding(f) for k, f in findings_a.items() if k not in findings_b)
    unchanged = tuple(_project_finding(f) for k, f in findings_a.items() if k in findings_b)

    return CompareResponse(
        run_a=body.run_a,
        run_b=body.run_b,
        metric_deltas=metric_deltas,
        findings_added=added,
        findings_removed=removed,
        findings_unchanged=unchanged,
        verdict_changed=False,
        verdict_a=None,
        verdict_b=None,
    )


# ---------------------------------------------------------------------------------------
# §26.1 GET /api/runs/{id}/export · POST /api/import
# ---------------------------------------------------------------------------------------


@router.get(
    "/runs/{run_id}/export",
    summary="Download a run as a `.agentdx` bundle (PRD §26.1)",
    response_class=FileResponse,
)
def export_run(run_id: str, store: Annotated[Store, Depends(get_store)]) -> FileResponse:
    """Export a sealed run to a self-contained, self-verifying `.agentdx` bundle.

    Raises:
        RunNotFoundError: `E-RUN-404`.
        BundleError: `400` — `E-BUNDLE-004` the run has no events (handled globally).

    Thin: `store.bundle.export_bundle` already resolves `created_at`/`scenario`/
    `agentdx_version` from the store when not given, so this route supplies nothing beyond
    `run_id` and a destination path.
    """
    if store.get_run(run_id) is None:
        raise RunNotFoundError(run_id)
    tmp = NamedTemporaryFile(suffix=".agentdx", delete=False)
    tmp.close()
    dest = export_bundle(store, run_id, Path(tmp.name))
    return FileResponse(
        dest,
        media_type="application/zip",
        filename=f"{run_id}.agentdx",
        background=_delete_after_send(dest),
    )


def _delete_after_send(path: Path) -> BackgroundTask:
    return BackgroundTask(path.unlink, missing_ok=True)


@router.post(
    "/import",
    response_model=ImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import a `.agentdx` bundle (PRD §26.1)",
)
async def import_run(
    file: UploadFile, store: Annotated[Store, Depends(get_store_async_http)]
) -> ImportResponse:
    """Verify and register an uploaded `.agentdx` bundle; return its local `run_id`.

    Raises:
        BundleError: `400` — `E-BUNDLE-001` verification failed · `E-BUNDLE-002` incompatible
            schema · `E-BUNDLE-005` unsafe archive member · `E-BUNDLE-007` a different log
            already exists under this `run_id` (all handled globally).

    Depends on `get_store_async_http`, not the sync `get_store` every other route in this
    module uses: this is the one `async def` route here (`UploadFile.read()` is itself
    async), and a sync generator dependency opens its `sqlite3.Connection` in FastAPI's
    threadpool while an `async def` route body runs on the event loop thread — a real,
    empirically-confirmed cross-thread `sqlite3.ProgrammingError` (see `deps.py::
    get_store_async_http`'s own docstring), not a hypothetical one.
    """
    tmp = NamedTemporaryFile(suffix=".agentdx", delete=False)
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()
        run_id = import_bundle(store, Path(tmp.name))
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    return ImportResponse(run_id=run_id)
