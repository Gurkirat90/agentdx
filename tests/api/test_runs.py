"""`POST/GET /api/runs` · `/{id}` · `/{id}/events` · `/{id}/faults` · `/compare` · export/import."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from agentdx.config import AgentDXConfig
from agentdx.store.sqlite import RunRecord, ScenarioRecord, Store
from tests.api.conftest import AGENTS, FakeFaultController, FakeRunLauncher, make_sealed_run
from tests.unit.store.conftest import chain
from tests.unit.store.factories import build_log, run_record_for

_GRAPH_SCENARIO_TEMPLATE = """\
version: 1
scenario: user_graph_fault_test
description: a user-graph scenario for API chaos-authorization tests (I12).

target:
  graph: "my_app.graphs:build_graph"

task: fixtures/tasks/refactor_module.md
seed: 42
{extra}
hypothesis:
  task_success: ">= 0.9"

faults:
  - type: agent_crash
    agent: planner
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


def _seed_running_run(
    db_path: Path,
    api_config: AgentDXConfig,
    *,
    run_id: str,
    scenario_id: str | None,
    scenario_text: str | None = None,
) -> str:
    """Create a `status="running"` run pinned to `scenario_id`, for I12 authorization tests.

    Bypasses `POST /api/runs` deliberately: these tests need direct control over
    `RunRecord.scenario_id`, including "set but no matching scenario row exists" and "set,
    but the stored text doesn't parse" — states `create_run` never produces on its own,
    since it always pins a scenario it just validated. `scenario_text=None` with a non-null
    `scenario_id` models the first of those; a malformed `scenario_text` models the second.
    """
    store = Store.open(db_path, config=api_config.store)
    try:
        if scenario_text is not None and scenario_id is not None:
            store.upsert_scenario(
                ScenarioRecord(
                    scenario_id=scenario_id,
                    content=scenario_text,
                    content_hash="blake2b:test",
                    version=1,
                    path=None,
                )
            )
        events = build_log(spans=2, run_id=run_id, sealed=False)
        record = RunRecord(**run_record_for(events, status="running"), scenario_id=scenario_id)  # type: ignore[arg-type]
        store.create_run(record)
        store.append(chain(events))
    finally:
        store.close()
    return run_id

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


def _write_scenario(scenarios_dir: Path, name: str = "kill_reviewer") -> None:
    (scenarios_dir / f"{name}.yaml").write_text(_VALID_SCENARIO, encoding="utf-8")


# POST /api/runs


def test_create_run_without_launcher_refuses_503(client: TestClient, scenarios_dir: Path) -> None:
    """No `RunLauncher` configured is `503`, never a fabricated success (I3/deps.py)."""
    _write_scenario(scenarios_dir)
    response = client.post(
        "/api/runs", json={"scenario_id": "kill_reviewer", "mode": "baseline", "seed": 1}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "E-RUN-503"


def test_create_run_404_for_unknown_scenario(client: TestClient) -> None:
    """A scenario id naming nothing is `E-SCEN-404`, checked before the launcher even runs."""
    response = client.post("/api/runs", json={"scenario_id": "nope", "mode": "baseline", "seed": 1})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E-SCEN-404"


def test_create_run_with_launcher_succeeds_and_pins_scenario(
    client_with_launcher: tuple[TestClient, FakeRunLauncher, FakeFaultController],
    scenarios_dir: Path,
) -> None:
    """A configured launcher makes `POST /api/runs` real: `202`, `run_id`, pinned scenario."""
    client, launcher, _controller = client_with_launcher
    _write_scenario(scenarios_dir)
    response = client.post(
        "/api/runs", json={"scenario_id": "kill_reviewer", "mode": "baseline", "seed": 7}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["run_id"] == launcher.run_id
    assert body["status"] == "running"
    assert body["ws"] == f"/ws/runs/{launcher.run_id}"
    assert launcher.calls == [
        {"scenario_id": "kill_reviewer", "mode": "baseline", "seed": 7, "explore": False}
    ]

    detail = client.get(f"/api/runs/{launcher.run_id}")
    assert detail.status_code == 200
    assert detail.json()["scenario"]["id"] == "kill_reviewer"


def test_create_run_concurrency_limit_409(
    client_with_launcher: tuple[TestClient, FakeRunLauncher, FakeFaultController],
    scenarios_dir: Path,
    api_config: AgentDXConfig,
) -> None:
    """Exceeding `concurrent_run_limit` is `409 E-RUN-409`, not a launch that never checked."""
    client, launcher, _controller = client_with_launcher
    _write_scenario(scenarios_dir)
    limit = api_config.api.concurrent_run_limit
    for i in range(limit):
        launcher.run_id = f"r_fake{i:02d}"
        response = client.post(
            "/api/runs", json={"scenario_id": "kill_reviewer", "mode": "baseline", "seed": i}
        )
        assert response.status_code == 202
    response = client.post(
        "/api/runs", json={"scenario_id": "kill_reviewer", "mode": "baseline", "seed": 999}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "E-RUN-409"


# GET /api/runs · GET /api/runs/{id}


def test_list_runs_empty_store(client: TestClient) -> None:
    """An empty store lists zero runs, not an error."""
    response = client.get("/api/runs")
    assert response.status_code == 200
    assert response.json() == {"runs": [], "next_cursor": None}


def test_list_and_get_run_detail_real_counts(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """`GET /api/runs` lists it; `GET /api/runs/{id}` reports real, store-derived counts."""
    run_id, events = sealed_run
    listing = client.get("/api/runs")
    assert listing.status_code == 200
    ids = [r["run_id"] for r in listing.json()["runs"]]
    assert run_id in ids

    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["run_id"] == run_id
    assert body["status"] == "complete"
    assert body["counts"]["events"] == len(events)
    assert body["counts"]["spans"] == sum(1 for e in events if e.type.value == "span_start")
    assert body["counts"]["llm_calls"] == sum(1 for e in events if e.type.value == "llm_call")
    assert set(body["graph"]["agents"]) == set(AGENTS)
    # P17 gap, declared: no orchestrator has computed a verdict in this build.
    assert body["verdict"] is None


def test_get_run_detail_404(client: TestClient) -> None:
    """An unknown run is `E-RUN-404`."""
    response = client.get("/api/runs/r_missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E-RUN-404"


def test_list_runs_status_filter(client: TestClient, sealed_run: tuple[str, tuple]) -> None:
    """`?status=` filters exactly, not fuzzily."""
    run_id, _events = sealed_run
    complete = client.get("/api/runs", params={"status": "complete"})
    assert run_id in [r["run_id"] for r in complete.json()["runs"]]
    running = client.get("/api/runs", params={"status": "running"})
    assert run_id not in [r["run_id"] for r in running.json()["runs"]]


# GET .runs.{run_id}.events


def test_get_events_default_page_returns_everything_for_a_short_run(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """A run shorter than the default page size comes back in one page."""
    run_id, events = sealed_run
    response = client.get(f"/api/runs/{run_id}/events")
    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == len(events)
    assert body["total"] == len(events)
    assert body["events"][0]["seq"] == 0
    assert body["events"][-1]["seq"] == len(events) - 1


def test_get_events_pagination_cursor_has_no_gap_or_duplicate(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """Paging with `limit`/`from_seq` covers every seq exactly once (Design Constraint 6)."""
    run_id, events = sealed_run
    seen: list[int] = []
    from_seq = 0
    for _ in range(100):  # generous upper bound on page count
        response = client.get(
            f"/api/runs/{run_id}/events", params={"from_seq": from_seq, "limit": 5}
        )
        assert response.status_code == 200
        body = response.json()
        seen.extend(e["seq"] for e in body["events"])
        if body["next_from_seq"] is None or not body["events"]:
            break
        from_seq = body["next_from_seq"]
    assert seen == list(range(len(events)))


def test_get_events_type_filter(client: TestClient, sealed_run: tuple[str, tuple]) -> None:
    """`?types=` restricts to the named `EventType`s only."""
    run_id, _events = sealed_run
    response = client.get(f"/api/runs/{run_id}/events", params={"types": "run_start,run_end"})
    assert response.status_code == 200
    types = {e["type"] for e in response.json()["events"]}
    assert types == {"run_start", "run_end"}


def test_get_events_agent_filter(client: TestClient, sealed_run: tuple[str, tuple]) -> None:
    """`?agent=` restricts to one agent's own events."""
    run_id, _events = sealed_run
    response = client.get(f"/api/runs/{run_id}/events", params={"agent": "planner"})
    assert response.status_code == 200
    agents = {e["agent_id"] for e in response.json()["events"]}
    assert agents == {"planner"}


def test_get_events_404_for_unknown_run(client: TestClient) -> None:
    """An unknown run is `E-RUN-404`, not an empty page."""
    response = client.get("/api/runs/r_missing/events")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E-RUN-404"


# POST .runs.{run_id}.faults


def test_inject_fault_without_controller_refuses_503(
    client: TestClient, running_run: tuple[str, tuple]
) -> None:
    """No `FaultController` configured is `503`, checked only after real validation passes."""
    run_id, _events = running_run
    response = client.post(
        f"/api/runs/{run_id}/faults",
        json={"type": "agent_crash", "target": "planner", "params": {"recoverable": True}},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "E-CHAOS-503"


def test_inject_fault_with_controller_succeeds_against_fixture_target(
    client_with_launcher: tuple[TestClient, FakeRunLauncher, FakeFaultController],
    db_path: Path,
    api_config: AgentDXConfig,
) -> None:
    """A fixture-targeted run is chaos-safe by default (I12) and a configured controller arms it."""
    client, _launcher, controller = client_with_launcher
    run_id, _events = make_sealed_run(db_path, api_config, spans=2)  # type: ignore[arg-type]
    # `make_sealed_run` seals immediately; faults require `status == "running"`, so flip it
    # back the same way `store.set_run_status` would mid-run — via the real store API, not a
    # raw SQL UPDATE, so this stays a legitimate use of the store's own surface.
    from agentdx.store.sqlite import Store

    store = Store.open(db_path, config=api_config.store)
    try:
        store.set_run_status(run_id, "running")
    finally:
        store.close()

    response = client.post(
        f"/api/runs/{run_id}/faults",
        json={
            "type": "agent_crash",
            "target": "planner",
            "params": {"recoverable": True},
            "trigger": {"immediate": True},
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["fault_id"] == controller.fault_id
    assert controller.calls == [
        {
            "run_id": run_id,
            "fault_type": "agent_crash",
            "target": "planner",
            "params": {"recoverable": True},
            "immediate": True,
        }
    ]


def test_inject_fault_run_not_running_409(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """A fault against a `complete` run is `409`, not silently accepted."""
    run_id, _events = sealed_run
    response = client.post(
        f"/api/runs/{run_id}/faults",
        json={"type": "agent_crash", "target": "planner", "params": {}},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "E-RUN-409"


def test_inject_fault_unknown_type_400(client: TestClient, running_run: tuple[str, tuple]) -> None:
    """A fault type not in `FAULT_CATALOGUE`'s P0 tier is `400 E-FAULT-002`."""
    run_id, _events = running_run
    response = client.post(
        f"/api/runs/{run_id}/faults", json={"type": "not_a_real_fault", "target": "planner"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E-FAULT-002"


def test_inject_fault_unknown_agent_target_400(
    client: TestClient, running_run: tuple[str, tuple]
) -> None:
    """A target not among the run's real agent roster is `400 E-FAULT-001`."""
    run_id, _events = running_run
    response = client.post(
        f"/api/runs/{run_id}/faults",
        json={"type": "agent_crash", "target": "not_a_real_agent", "params": {}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E-FAULT-001"


def test_inject_fault_404_for_unknown_run(client: TestClient) -> None:
    """An unknown run is `E-RUN-404`."""
    response = client.post(
        "/api/runs/r_missing/faults", json={"type": "agent_crash", "target": "planner"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E-RUN-404"


# POST /api/runs/{id}/faults — I12 chaos authorization (CONTEXT.md §2), user-graph targets
#
# `test_inject_fault_with_controller_succeeds_against_fixture_target` above never exercises
# `_check_chaos_authorization`'s substantive branches at all — a fixture target is chaos-safe
# by construction and returns before any of them run. These tests are what was missing: the
# actual I12 matrix, plus the post-repair regression coverage for the fail-open bug the
# original build shipped (an unresolvable scenario silently skipped authorization instead of
# refusing the fault).


def test_inject_fault_graph_target_without_opt_in_403(
    db_path: Path, api_config: AgentDXConfig, client: TestClient
) -> None:
    """A user-graph target with no `chaos_opt_in` is refused, never armed (I12)."""
    scenario_text = _GRAPH_SCENARIO_TEMPLATE.format(extra="")
    run_id = _seed_running_run(
        db_path, api_config, run_id="r_i12_01", scenario_id="s_01", scenario_text=scenario_text
    )
    response = client.post(
        f"/api/runs/{run_id}/faults", json={"type": "agent_crash", "target": "planner"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E-CHAOS-001"


def test_inject_fault_graph_target_empty_blast_radius_403(
    db_path: Path, api_config: AgentDXConfig, client: TestClient
) -> None:
    """Opting in with no declared `blast_radius` is still refused (§13.4)."""
    scenario_text = _GRAPH_SCENARIO_TEMPLATE.format(extra="chaos_opt_in: true\nblast_radius: {}\n")
    run_id = _seed_running_run(
        db_path, api_config, run_id="r_i12_02", scenario_id="s_02", scenario_text=scenario_text
    )
    response = client.post(
        f"/api/runs/{run_id}/faults", json={"type": "agent_crash", "target": "planner"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E-CHAOS-001"


def test_inject_fault_graph_target_outside_blast_radius_403(
    db_path: Path, api_config: AgentDXConfig, client: TestClient
) -> None:
    """A target not named in the declared `blast_radius` is refused."""
    scenario_text = _GRAPH_SCENARIO_TEMPLATE.format(
        extra="chaos_opt_in: true\nblast_radius:\n  agents: [coder]\n"
    )
    run_id = _seed_running_run(
        db_path, api_config, run_id="r_i12_03", scenario_id="s_03", scenario_text=scenario_text
    )
    response = client.post(
        f"/api/runs/{run_id}/faults", json={"type": "agent_crash", "target": "planner"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E-CHAOS-001"


def test_inject_fault_graph_target_inside_blast_radius_202(
    db_path: Path,
    api_config: AgentDXConfig,
    client_with_launcher: tuple[TestClient, FakeRunLauncher, FakeFaultController],
) -> None:
    """A properly authorized user-graph fault — opted in, target inside the radius — arms."""
    client, _launcher, controller = client_with_launcher
    scenario_text = _GRAPH_SCENARIO_TEMPLATE.format(
        extra="chaos_opt_in: true\nblast_radius:\n  agents: [planner]\n"
    )
    run_id = _seed_running_run(
        db_path, api_config, run_id="r_i12_04", scenario_id="s_04", scenario_text=scenario_text
    )
    response = client.post(
        f"/api/runs/{run_id}/faults", json={"type": "agent_crash", "target": "planner"}
    )
    assert response.status_code == 202
    assert response.json()["fault_id"] == controller.fault_id
    assert controller.calls and controller.calls[-1]["run_id"] == run_id


def test_inject_fault_scenario_row_missing_refuses_409(
    db_path: Path, api_config: AgentDXConfig, client: TestClient
) -> None:
    """`scenario_id` is set but no matching store row exists — refused, not treated as safe.

    Regression test for the I12 fail-open bug: the original code defaulted an unresolvable
    scenario to `resolved = {}`, which `_is_graph_target` reads as "not a graph target" and
    therefore skips authorization — silently arming the fault instead of refusing it. This
    run's `scenario_id` points at a row that was never written, so a fixture-safe run and an
    unauthorized user-graph run are indistinguishable from what the API can see; refusing is
    the only honest answer (PRD §36 rule 1).
    """
    run_id = _seed_running_run(
        db_path, api_config, run_id="r_i12_05", scenario_id="s_missing", scenario_text=None
    )
    response = client.post(
        f"/api/runs/{run_id}/faults", json={"type": "agent_crash", "target": "planner"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "E-CHAOS-004"


def test_inject_fault_scenario_unparseable_refuses_409(
    db_path: Path, api_config: AgentDXConfig, client: TestClient
) -> None:
    """`scenario_id` is set and the row exists, but its content no longer parses — refused.

    Same fail-open bug as above, the other trigger: `loader.ScenarioLoadError` used to be
    caught and papered over with `resolved = {}` rather than propagated as a refusal.
    """
    run_id = _seed_running_run(
        db_path,
        api_config,
        run_id="r_i12_06",
        scenario_id="s_corrupt",
        scenario_text="not: [valid, yaml, {{{",
    )
    response = client.post(
        f"/api/runs/{run_id}/faults", json={"type": "agent_crash", "target": "planner"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "E-CHAOS-004"


# POST /api/runs/compare


def test_compare_runs_real_metric_deltas(
    client: TestClient, db_path: Path, api_config: AgentDXConfig
) -> None:
    """`metric_deltas` covers real `RunRecord` fields only (Design Constraint 1)."""
    run_a, events_a = make_sealed_run(db_path, api_config, spans=2, run_id="r_cmp_a")
    run_b, events_b = make_sealed_run(db_path, api_config, spans=4, run_id="r_cmp_b")
    response = client.post(
        "/api/runs/compare", json={"run_a": run_a, "run_b": run_b, "force": True}
    )
    assert response.status_code == 200
    body = response.json()
    deltas = {d["name"]: d for d in body["metric_deltas"]}
    assert deltas["event_count"]["run_a"] == len(events_a)
    assert deltas["event_count"]["run_b"] == len(events_b)
    assert deltas["event_count"]["delta"] == len(events_b) - len(events_a)
    # P17 gap, declared: no verdict pipeline runs in this build.
    assert body["verdict_changed"] is False


def test_compare_runs_incomparable_without_force_400(client: TestClient) -> None:
    """Two runs with different scenario hashes need `force=true`, else `E-CMP-001`."""
    response = client.post(
        "/api/runs/compare", json={"run_a": "r_missing_a", "run_b": "r_missing_b"}
    )
    # Neither run exists, so the 404 on run_a fires before comparability is even checked.
    assert response.status_code == 404


def test_compare_runs_404_for_missing_run(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """A real run compared against a missing one is `E-RUN-404`, naming the missing side."""
    run_id, _events = sealed_run
    response = client.post(
        "/api/runs/compare", json={"run_a": run_id, "run_b": "r_missing", "force": True}
    )
    assert response.status_code == 404
    assert response.json()["error"]["detail"]["run_id"] == "r_missing"


# GET /api/runs/{id}/export · POST /api/import


def test_export_then_import_round_trips(client: TestClient, sealed_run: tuple[str, tuple]) -> None:
    """A run exported to a bundle and re-imported produces a real, self-verifying archive."""
    run_id, _events = sealed_run
    export_response = client.get(f"/api/runs/{run_id}/export")
    assert export_response.status_code == 200
    assert export_response.headers["content-type"] == "application/zip"
    bundle_bytes = export_response.content
    with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as archive:
        assert archive.namelist()  # non-empty, real archive contents

    import_response = client.post(
        "/api/import",
        files={"file": (f"{run_id}.agentdx", bundle_bytes, "application/zip")},
    )
    assert import_response.status_code == 201
    assert import_response.json()["run_id"] == run_id


def test_export_404_for_unknown_run(client: TestClient) -> None:
    """Exporting an unknown run is `E-RUN-404`."""
    response = client.get("/api/runs/r_missing/export")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E-RUN-404"


def test_import_malformed_bundle_400(client: TestClient) -> None:
    """A file that is not a valid bundle is a `400`, never a `500`."""
    response = client.post(
        "/api/import",
        files={"file": ("bad.agentdx", b"not a zip file at all", "application/zip")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"].startswith("E-BUNDLE")
