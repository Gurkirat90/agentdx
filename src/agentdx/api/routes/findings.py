"""`GET /api/runs/{id}/findings` (PRD §26.1) — precomputed, read straight from the store.

PRD §26.3: target p95 <50 ms, "Precomputed". `store.list_findings` is an indexed read; this
route adds nothing but the query-parameter filter and the Pydantic projection.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query

from agentdx.api.deps import get_store
from agentdx.api.errors import RunNotFoundError
from agentdx.api.models import FindingOut, FindingsResponse, JsonValue
from agentdx.store.sqlite import FindingRecord, Store

router = APIRouter(prefix="/runs/{run_id}", tags=["findings"])


def _project(record: FindingRecord) -> FindingOut:
    # `record.evidence: Mapping[str, events.schema.PayloadValue]` and `JsonValue` are two
    # independently declared aliases over the same recursive JSON shape — `PayloadValue` is a
    # string-form `TypeAlias` (see `models.py`'s own docstring for why it can't be reused
    # directly), so mypy sees two distinct types even though every value is JSON-safe by the
    # same YAML/event-schema construction argument `scenarios.py::_as_json_object` makes.
    evidence = cast(dict[str, JsonValue], dict(record.evidence))
    return FindingOut(
        id=record.finding_id,
        type=record.type,
        subtype=record.subtype,
        severity=record.severity,
        title=record.title,
        description=record.description,
        evidence=evidence,
        recommendation=record.recommendation,
        repro_scenario=record.repro_scenario_path,
        suppressed_by=record.suppressed_by,
    )


@router.get("/findings", response_model=FindingsResponse, summary="Findings (PRD §26.1)")
def get_findings(
    run_id: str,
    store: Annotated[Store, Depends(get_store)],
    severity: Annotated[str | None, Query(description="Filter: exact severity match.")] = None,
    type: Annotated[str | None, Query(description="Filter: exact type match.")] = None,
    include_suppressed: Annotated[
        bool, Query(description="Include findings a guard suppressed.")
    ] = False,
) -> FindingsResponse:
    """Return a run's findings, severity/type-filtered, precomputed (PRD §26.1, §26.3).

    Raises:
        RunNotFoundError: `E-RUN-404`.
    """
    if store.get_run(run_id) is None:
        raise RunNotFoundError(run_id)
    records = store.list_findings(run_id)
    findings = tuple(
        _project(r)
        for r in records
        if (severity is None or r.severity == severity)
        and (type is None or r.type == type)
        and (include_suppressed or r.suppressed_by is None)
    )
    return FindingsResponse(findings=findings)
