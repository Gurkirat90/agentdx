"""`GET /api/runs/{id}/scorecard` (PRD §26.1) — precomputed, read straight from the store.

`store.get_scorecard(run_id).payload` already *is* the PRD §17.4 structure (`speedup{}`,
`buckets[]`, `tokens{}`, `comparability{}`, `resilience{}`) — whatever analysis pipeline
computed and called `store.upsert_scorecard` wrote it in that shape. This route passes it
through unreshaped (`ScorecardResponse` is `extra="allow"`, by design — see `models.py`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from agentdx.api.deps import get_store
from agentdx.api.errors import NotYetAvailableError, RunNotFoundError
from agentdx.api.models import ScorecardResponse
from agentdx.store.sqlite import Store

router = APIRouter(prefix="/runs/{run_id}", tags=["scorecard"])


@router.get("/scorecard", response_model=ScorecardResponse, summary="Scorecard (PRD §26.1)")
def get_scorecard(run_id: str, store: Annotated[Store, Depends(get_store)]) -> ScorecardResponse:
    """Return a run's persisted scorecard.

    Raises:
        RunNotFoundError: `E-RUN-404`.
        NotYetAvailableError: `409` `E-SCORE-001` — the run exists but no scorecard has been
            computed and persisted for it yet. No prompt in this build runs the full
            `baseline -> verdict` pipeline and calls `store.upsert_scorecard` (P17, not
            started) — see `docs/api.md`. Reported plainly, not as an empty `200` (PRD §36
            rule 1) and not as `404` (the run itself is real; its scorecard is what's missing).
    """
    if store.get_run(run_id) is None:
        raise RunNotFoundError(run_id)
    record = store.get_scorecard(run_id)
    if record is None:
        raise NotYetAvailableError(
            "E-SCORE-001",
            f"No scorecard has been computed for run {run_id} yet",
            detail={"run_id": run_id},
        )
    return ScorecardResponse.model_validate(dict(record.payload))
