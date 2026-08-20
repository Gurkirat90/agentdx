"""`GET /runs/{id}/findings` · `/scorecard` (PRD §26.1)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agentdx.config import AgentDXConfig
from agentdx.store.sqlite import FindingRecord, ScorecardRecord, Store


def _finding(run_id: str, finding_id: str, *, severity: str = "critical") -> FindingRecord:
    return FindingRecord(
        finding_id=finding_id,
        run_id=run_id,
        type="lost_update",
        severity=severity,
        title="draft.module_a overwritten without observing reviewer's write",
        description="planner and reviewer both wrote draft.module_a; reviewer's write is lost.",
        evidence={"event_seqs": [4, 18], "span_ids": ["span0000aaaa", "span0002aaaa"]},
        analysis_version="1",
    )


def test_get_findings_empty_by_default(client: TestClient, sealed_run: tuple[str, tuple]) -> None:
    """A run with no findings persisted returns an empty list, not an error."""
    run_id, _events = sealed_run
    response = client.get(f"/api/runs/{run_id}/findings")
    assert response.status_code == 200
    assert response.json() == {"findings": []}


def test_get_findings_404_for_unknown_run(client: TestClient) -> None:
    """An unknown run is `E-RUN-404`."""
    response = client.get("/api/runs/r_missing/findings")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E-RUN-404"


def test_get_findings_projects_real_rows_and_filters(
    client: TestClient, sealed_run: tuple[str, tuple], db_path: Path, api_config: AgentDXConfig
) -> None:
    """A finding persisted via `store.upsert_finding` (e.g. by a bundle import) round-trips."""
    run_id, _events = sealed_run
    store = Store.open(db_path, config=api_config.store)
    try:
        store.upsert_finding(_finding(run_id, "fnd_001", severity="critical"))
        store.upsert_finding(_finding(run_id, "fnd_002", severity="low"))
    finally:
        store.close()

    all_findings = client.get(f"/api/runs/{run_id}/findings")
    assert all_findings.status_code == 200
    ids = {f["id"] for f in all_findings.json()["findings"]}
    assert ids == {"fnd_001", "fnd_002"}

    critical_only = client.get(f"/api/runs/{run_id}/findings", params={"severity": "critical"})
    assert [f["id"] for f in critical_only.json()["findings"]] == ["fnd_001"]

    one = next(f for f in all_findings.json()["findings"] if f["id"] == "fnd_001")
    assert one["evidence"]["event_seqs"] == [4, 18]
    assert one["suppressed_by"] is None


def test_get_scorecard_409_when_not_computed(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """No P17 orchestrator has run in this build — `409 E-SCORE-001`, not an empty `200`."""
    run_id, _events = sealed_run
    response = client.get(f"/api/runs/{run_id}/scorecard")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "E-SCORE-001"


def test_get_scorecard_404_for_unknown_run(client: TestClient) -> None:
    """An unknown run is `E-RUN-404`, checked before the "not computed" refusal."""
    response = client.get("/api/runs/r_missing/scorecard")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E-RUN-404"


def test_get_scorecard_passes_through_a_real_persisted_payload(
    client: TestClient, sealed_run: tuple[str, tuple], db_path: Path, api_config: AgentDXConfig
) -> None:
    """A scorecard someone did persist (e.g. a future P17 orchestrator) is served unreshaped."""
    run_id, _events = sealed_run
    store = Store.open(db_path, config=api_config.store)
    try:
        # No floats anywhere (ADR-007 / `E-EVENT-013`): a scorecard payload is canonical-JSON
        # encoded the same as an event payload, and the ban is codebase-wide, not event-only.
        store.upsert_scorecard(
            ScorecardRecord(
                run_id=run_id,
                payload={
                    "speedup": {"factor_permille": 1000},
                    "buckets": [],
                    "tokens": {"total": 300},
                    "comparability": {"grade": "A"},
                    "resilience": {},
                },
                analysis_version="1",
                computed_at="2026-08-20T00:00:00Z",
            )
        )
    finally:
        store.close()

    response = client.get(f"/api/runs/{run_id}/scorecard")
    assert response.status_code == 200
    body = response.json()
    assert body["comparability"]["grade"] == "A"
    assert body["tokens"]["total"] == 300

    # `GET /runs/{id}`'s own `comparability` field reads the same scorecard row.
    detail = client.get(f"/api/runs/{run_id}")
    assert detail.json()["comparability"] == "A"
