"""`GET /api/scenarios` · `/{id}` · `POST /validate` (PRD §26.1)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

_VALID_SCENARIO = """\
version: 1
scenario: kill_reviewer
description: reviewer is crashed mid-flight for the API test suite.

target:
  fixture: code_pipeline

task: fixtures/tasks/refactor_module.md
seed: 42

hypothesis:
  task_success: ">= 0.9"

faults:
  - type: agent_crash
    agent: reviewer
    at_virtual_ts: 3000
    recoverable: false

guards:
  max_virtual_duration_ms: 120000
  max_tokens: 200000
  max_retries: 20

success_check:
  type: python
  ref: "fixtures.code_pipeline.checks:task_success"

assertions:
  - no_silent_failures
"""

_INVALID_YAML = "version: 1\nscenario: [unterminated"


def _write_scenario(scenarios_dir: Path, name: str, text: str = _VALID_SCENARIO) -> None:
    (scenarios_dir / f"{name}.yaml").write_text(text, encoding="utf-8")


def test_list_scenarios_discovers_files_in_scenarios_dir(
    client: TestClient, scenarios_dir: Path
) -> None:
    """`GET /api/scenarios` finds a scenario file on disk without any run ever using it."""
    _write_scenario(scenarios_dir, "kill_reviewer")
    response = client.get("/api/scenarios")
    assert response.status_code == 200
    ids = [s["scenario_id"] for s in response.json()["scenarios"]]
    assert ids == ["kill_reviewer"]
    assert response.json()["scenarios"][0]["source"] == "file"


def test_get_scenario_returns_resolved_and_raw_content(
    client: TestClient, scenarios_dir: Path
) -> None:
    """`GET /api/scenarios/{id}` returns both the raw text and the defaults-resolved form."""
    _write_scenario(scenarios_dir, "kill_reviewer")
    response = client.get("/api/scenarios/kill_reviewer")
    assert response.status_code == 200
    body = response.json()
    assert body["scenario_id"] == "kill_reviewer"
    assert body["source"] == "file"
    assert "reviewer is crashed" in body["content"]
    assert body["resolved"]["seed"] == 42


def test_get_scenario_404_for_unknown_id(client: TestClient) -> None:
    """A scenario id in neither the store nor `scenarios_dir` is `E-SCEN-404`."""
    response = client.get("/api/scenarios/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "E-SCEN-404"
    assert body["error"]["docs"] == "docs/api.md#e-scen-404"


def test_validate_by_text_valid_scenario_is_200_with_valid_true(client: TestClient) -> None:
    """A well-formed scenario validates as `200 valid: true`, never a `4xx` (dry-run-safe)."""
    response = client.post("/api/scenarios/validate", json={"text": _VALID_SCENARIO})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["errors"] == []
    assert body["resolved"] is not None


def test_validate_by_text_malformed_yaml_is_200_with_valid_false(client: TestClient) -> None:
    """Malformed YAML is `200 valid: false` with a populated error, not a `4xx`."""
    response = client.post("/api/scenarios/validate", json={"text": _INVALID_YAML})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert len(body["errors"]) >= 1
    assert body["errors"][0]["code"]
    assert body["resolved"] is None


def test_validate_requires_exactly_one_of_text_or_scenario_id(client: TestClient) -> None:
    """Neither `text` nor `scenario_id` given is a `422`, not a silent default."""
    response = client.post("/api/scenarios/validate", json={})
    assert response.status_code == 422
    response = client.post(
        "/api/scenarios/validate", json={"text": _VALID_SCENARIO, "scenario_id": "x"}
    )
    assert response.status_code == 422


def test_validate_by_scenario_id_reads_from_scenarios_dir(
    client: TestClient, scenarios_dir: Path
) -> None:
    """`scenario_id` resolves through the same file-discovery path `GET /{id}` uses."""
    _write_scenario(scenarios_dir, "kill_reviewer")
    response = client.post("/api/scenarios/validate", json={"scenario_id": "kill_reviewer"})
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_validate_by_scenario_id_404_for_unknown_id(client: TestClient) -> None:
    """`scenario_id` naming nothing is `E-SCEN-404`, exactly like `GET /{id}`."""
    response = client.post("/api/scenarios/validate", json={"scenario_id": "nope"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E-SCEN-404"
