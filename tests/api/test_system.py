"""`GET /api/health` · `/version` · `/metrics` (PRD §26.1, §35)."""

from __future__ import annotations

from importlib.metadata import version as installed_version

from fastapi.testclient import TestClient

from agentdx.events.schema import SCHEMA_VERSION


def test_health_returns_ok(client: TestClient) -> None:
    """`GET /api/health` is a bare liveness probe."""
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_matches_installed_package_and_schema(client: TestClient) -> None:
    """`GET /api/version` reports the real installed version, not a hardcoded literal."""
    response = client.get("/api/version")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == installed_version("agentdx")
    assert body["schema_version"] == SCHEMA_VERSION


def test_metrics_is_prometheus_text_with_every_prd_sec35_metric(client: TestClient) -> None:
    """`GET /api/metrics` emits HELP/TYPE for the full PRD §35 set; computed ones carry a sample."""
    response = client.get("/api/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    for name in (
        "agentdx_scheduler_step_duration_us",
        "agentdx_memory_rss_mb",
        "agentdx_nondeterminism_warnings_total",
    ):
        assert f"# HELP {name}" in body
        assert f"# TYPE {name}" in body
    # Genuinely computed for real, from this process — not fabricated.
    assert "agentdx_memory_rss_mb " in body
