"""`GET /api/scenarios` · `GET /api/scenarios/{id}` · `POST /api/scenarios/validate` (PRD Sec26.1).

Two sources of scenarios, both real, neither authoritative over the other: a scenario a past
run referenced is in the store (`store.get_scenario`, content-hash pinned — PRD Sec27.2: "editing
a scenario cannot retroactively change what a past run was"); a scenario file sitting in
`agentdx.toml`'s `[api] scenarios_dir` is discoverable on disk before any run has ever used it.
`validate` is deliberately dry-run-safe: a malformed or invalid scenario is a `200` with
`valid: false` and populated `errors`, never a `4xx` — that is the one thing this endpoint
exists to let the UI's chaos panel do without a round trip through a failed run.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, status

from agentdx.api.deps import ApiState, get_state, get_store
from agentdx.api.errors import ScenarioNotFoundError
from agentdx.api.models import (
    JsonValue,
    ScenarioDetail,
    ScenarioListResponse,
    ScenarioSummary,
    ScenarioValidateRequest,
    ScenarioValidateResponse,
    ScenarioValidationErrorOut,
)
from agentdx.scenario import loader, validate
from agentdx.store.sqlite import Store


def _as_json_object(data: dict[str, object]) -> dict[str, JsonValue]:
    """Narrow a resolved scenario document's declared `dict[str, object]` to `JsonValue`.

    `scenario.loader.resolve_defaults` types its return as `dict[str, object]` because
    `scenario/` has no notion of "JSON-safe" as a first-class type — but every value in it
    genuinely is JSON-safe, by construction: it comes from `yaml.safe_load` (never arbitrary
    Python objects) followed only by dict/list merging and defaulting (never a value the
    parser didn't produce). This is a cast, not a runtime check, because there is nothing
    cheaper to check that YAML's own type contract doesn't already guarantee.
    """
    return cast(dict[str, JsonValue], data)


router = APIRouter(prefix="/scenarios", tags=["scenarios"])


def _discover_files(state: ApiState) -> dict[str, ScenarioSummary]:
    """Return every `*.yaml`/`*.yml` under `scenarios_dir`, keyed by its stem (scenario id).

    A file that fails to parse is skipped, not raised — this is a *listing*, and one broken
    file must not take the whole catalogue down (PRD Sec36 rule 1's spirit, applied to a read).
    """
    directory = state.config.api.scenarios_dir
    if not directory.is_dir():
        return {}
    found: dict[str, ScenarioSummary] = {}
    for path in sorted((*directory.glob("*.yaml"), *directory.glob("*.yml"))):
        try:
            parsed = loader.load_scenario_file(path)
        except loader.ScenarioLoadError:
            continue
        if parsed.data is None:
            continue
        version = parsed.data.get("version")
        found[path.stem] = ScenarioSummary(
            scenario_id=path.stem,
            source="file",
            path=str(path),
            version=version if isinstance(version, int) else None,
        )
    return found


@router.get("", response_model=ScenarioListResponse, summary="List scenarios (PRD Sec26.1)")
def list_scenarios(
    state: Annotated[ApiState, Depends(get_state)],
    store: Annotated[Store, Depends(get_store)],
) -> ScenarioListResponse:
    """Return every scenario the store has persisted, unioned with `scenarios_dir`'s files.

    A scenario id present in both sources is reported once, from the store — the store's
    copy is the one a real run's `runs.scenario_hash` actually pins (PRD Sec27.2), so it is the
    more authoritative of the two for an id that exists in both places.
    """
    by_id = _discover_files(state)
    for run_record_scenario_id in _store_scenario_ids(store):
        record = store.get_scenario(run_record_scenario_id)
        if record is not None:
            by_id[record.scenario_id] = ScenarioSummary(
                scenario_id=record.scenario_id,
                source="store",
                path=record.path,
                version=record.version,
            )
    return ScenarioListResponse(scenarios=tuple(by_id[k] for k in sorted(by_id)))


def _store_scenario_ids(store: Store) -> tuple[str, ...]:
    """Return every `scenario_id` the store has a row for, discovered via the runs it seeded.

    `Store` has no `list_scenarios`; scenarios are looked up by id, not enumerated (PRD
    Sec27.2 gives `scenarios` no reason to be listed independently of the runs that reference
    them). Enumerating run records' `scenario_id` is the honest way to discover which
    scenario ids the store actually holds without adding a query `store/` does not have —
    extending `store/` is outside this prompt's `DELIVERABLES`.
    """
    ids = {r.scenario_id for r in store.list_runs() if r.scenario_id is not None}
    return tuple(sorted(ids))


@router.get(
    "/{scenario_id}", response_model=ScenarioDetail, summary="Fetch a scenario (PRD Sec26.1)"
)
def get_scenario(
    scenario_id: str,
    state: Annotated[ApiState, Depends(get_state)],
    store: Annotated[Store, Depends(get_store)],
) -> ScenarioDetail:
    """Return one scenario's raw content plus its resolved (defaults-applied) form.

    Raises:
        ScenarioNotFoundError: `E-SCEN-404` — neither source has this id.
    """
    record = store.get_scenario(scenario_id)
    if record is not None:
        parsed = loader.parse_scenario_text(record.content, source_name=record.path or scenario_id)
        resolved = loader.resolve_defaults(parsed.data or {})
        return ScenarioDetail(
            scenario_id=scenario_id,
            source="store",
            path=record.path,
            content=record.content,
            content_hash=record.content_hash,
            resolved=_as_json_object(resolved),
        )

    directory = state.config.api.scenarios_dir
    for path in (directory / f"{scenario_id}.yaml", directory / f"{scenario_id}.yml"):
        if path.is_file():
            parsed = loader.load_scenario_file(path)
            resolved = loader.resolve_defaults(parsed.data or {})
            return ScenarioDetail(
                scenario_id=scenario_id,
                source="file",
                path=str(path),
                content=parsed.text,
                content_hash=None,
                resolved=_as_json_object(resolved),
            )
    raise ScenarioNotFoundError(scenario_id)


@router.post(
    "/validate",
    response_model=ScenarioValidateResponse,
    summary="Dry-run validate a scenario (PRD Sec26.1)",
)
def validate_scenario(
    body: ScenarioValidateRequest,
    state: Annotated[ApiState, Depends(get_state)],
    store: Annotated[Store, Depends(get_store)],
) -> ScenarioValidateResponse:
    """Validate scenario text or an existing scenario id and return the resolved document.

    Never raises for an invalid scenario (Design Constraint 1's "thin" still holds: this
    calls `scenario.loader`/`scenario.validate` and serialises, no rule lives here) — a
    malformed or invalid scenario is a `200` with `valid: false`, because that is what makes
    this endpoint safe for the UI's chaos panel to call on every keystroke.

    Raises:
        ScenarioNotFoundError: `scenario_id` was given and names nothing in either source.
    """
    text, source_name = _resolve_input(body, state, store)

    try:
        parsed = loader.parse_scenario_text(text, source_name=source_name)
        parsed = loader.resolve_extends(parsed)
    except loader.ScenarioLoadError as exc:
        error_out = ScenarioValidationErrorOut(
            code=exc.code,
            path="<root>",
            message=exc.message,
            suggestion="Fix the YAML syntax and re-validate.",
            file=source_name,
            line=exc.line,
            docs=f"docs/scenario-reference.md#{exc.code.lower()}",
        )
        return ScenarioValidateResponse(valid=False, resolved=None, errors=(error_out,))

    errors = validate.validate(parsed)
    resolved = loader.resolve_defaults(parsed.data) if parsed.data is not None else None
    errors_out = tuple(
        ScenarioValidationErrorOut(
            code=e.code,
            path=e.path,
            message=e.message,
            suggestion=e.suggestion,
            file=e.file,
            line=e.line,
            docs=e.docs_url,
        )
        for e in errors
    )
    return ScenarioValidateResponse(
        valid=len(errors_out) == 0,
        resolved=_as_json_object(resolved) if resolved is not None else None,
        errors=errors_out,
    )


def _resolve_input(body: ScenarioValidateRequest, state: ApiState, store: Store) -> tuple[str, str]:
    """Return `(text, source_name)` for the request, per `ScenarioValidateRequest`'s contract.

    Raises:
        ScenarioNotFoundError: `scenario_id` was given and names nothing in either source.
        HTTPException: `422` — neither `text` nor `scenario_id` was given, or both were.
    """
    if body.text is not None and body.scenario_id is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="exactly one of `text` or `scenario_id` must be given, not both",
        )
    if body.text is not None:
        return body.text, body.source_name
    if body.scenario_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="one of `text` or `scenario_id` must be given",
        )

    record = store.get_scenario(body.scenario_id)
    if record is not None:
        return record.content, record.path or body.scenario_id

    directory = state.config.api.scenarios_dir
    for path in (directory / f"{body.scenario_id}.yaml", directory / f"{body.scenario_id}.yml"):
        if path.is_file():
            return path.read_text(encoding="utf-8"), str(path)
    raise ScenarioNotFoundError(body.scenario_id)
