"""`GET /runs/{id}/graph` · `/waterfall` · `/state` · `/exploration` (PRD §26.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentdx.config import AgentDXConfig
from agentdx.events.schema import Event
from agentdx.store.sqlite import Store
from tests.analysis._events import llm_call, run_end, run_start, span_end, span_start
from tests.api.conftest import AGENTS
from tests.unit.store.conftest import populate


def _nested_span_log() -> tuple[Event, ...]:
    """Return a sealed log with one real `llm_call`-kind leaf span nested inside `agent_step`.

    `tests/unit/store/factories.py::build_log` (what `sealed_run` uses) puts `llm_call`
    straight on the `agent_step` span, never as its own `span_start`/`span_end` pair — a
    valid, schema-conforming log, but one `analysis.timing.build_timing_dag` decomposes
    entirely into `agent_step_segment` nodes (`span_id=None`), which `GET .../waterfall`
    deliberately excludes (its own docstring: "not a span of its own"). A meaningful test of
    the waterfall projection needs at least one genuine leaf span, so this builds one by hand
    with `tests/analysis/_events.py`'s builders — the same ones `analysis/timing`'s own test
    suite uses for exactly this shape.
    """
    return (
        run_start(seq=0, virtual_ts_ms=0),
        span_start(
            seq=1,
            virtual_ts_ms=10,
            vclock={"planner": 1},
            causal_parents=[0],
            agent_id="planner",
            span_id="span_parent",
            kind="agent_step",
            name="planner-step",
        ),
        span_start(
            seq=2,
            virtual_ts_ms=20,
            vclock={"planner": 2},
            causal_parents=[1],
            agent_id="planner",
            span_id="span_child",
            kind="llm_call",
            name="llm",
            parent_span_id="span_parent",
        ),
        llm_call(
            seq=3,
            virtual_ts_ms=30,
            vclock={"planner": 3},
            causal_parents=[2],
            agent_id="planner",
            span_id="span_child",
            prompt_tokens=10,
            completion_tokens=5,
        ),
        span_end(
            seq=4,
            virtual_ts_ms=40,
            vclock={"planner": 4},
            causal_parents=[3],
            agent_id="planner",
            span_id="span_child",
            duration_virtual_ms=20,
        ),
        span_end(
            seq=5,
            virtual_ts_ms=50,
            vclock={"planner": 5},
            causal_parents=[4],
            agent_id="planner",
            span_id="span_parent",
            duration_virtual_ms=40,
        ),
        run_end(
            seq=6,
            virtual_ts_ms=60,
            vclock={"planner": 5},
            causal_parents=[5],
            virtual_makespan_ms=60,
            event_count=7,
        ),
    )


@pytest.fixture
def nested_span_run(db_path: Path, api_config: AgentDXConfig) -> str:
    """Populate `db_path` with `_nested_span_log`'s run and return its `run_id`."""
    store = Store.open(db_path, config=api_config.store)
    try:
        run_id = populate(store, _nested_span_log())
    finally:
        store.close()
    return run_id


def test_get_graph_real_agents_and_critical_path(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """`GET /runs/{id}/graph` computes real per-agent aggregates from the sealed log."""
    run_id, _events = sealed_run
    response = client.get(f"/api/runs/{run_id}/graph")
    assert response.status_code == 200
    body = response.json()
    node_ids = {n["id"] for n in body["nodes"]}
    assert node_ids == set(AGENTS)
    assert body["critical_path"]
    for node in body["nodes"]:
        assert node["busy_ms"] >= 0
        assert node["status"] == "ok"


def test_get_graph_404_for_unknown_run(client: TestClient) -> None:
    """An unknown run is `E-RUN-404`."""
    response = client.get("/api/runs/r_missing/graph")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E-RUN-404"


def test_get_waterfall_lanes_cover_every_agent_with_a_leaf_span(
    client: TestClient, nested_span_run: str
) -> None:
    """`GET /runs/{id}/waterfall` projects real leaf (`llm_call`) spans, sorted by start."""
    response = client.get(f"/api/runs/{nested_span_run}/waterfall")
    assert response.status_code == 200
    body = response.json()
    assert {lane["agent"] for lane in body["lanes"]} == {"planner"}
    lane = body["lanes"][0]
    assert [s["kind"] for s in lane["spans"]] == ["llm_call"]
    assert lane["spans"][0]["span_id"] == "span_child"
    starts = [s["start_ms"] for s in lane["spans"]]
    assert starts == sorted(starts)
    # Declared gap (routes/analysis.py docstring): overhead bucket classification is not
    # exposed per-span in this build.
    assert lane["spans"][0]["bucket"] is None
    assert body["virtual_makespan_ms"] > 0


def test_get_waterfall_only_decomposed_segments_is_an_empty_lane_set(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """A log with no genuine leaf span (`sealed_run`'s flat shape) waterfalls to zero lanes.

    Not a bug: every node `build_timing_dag` produces for this log is an `agent_step_segment`
    (`span_id=None`), which `GET .../waterfall` deliberately excludes — see
    `test_get_waterfall_lanes_cover_every_agent_with_a_leaf_span` for the populated case.
    """
    run_id, _events = sealed_run
    response = client.get(f"/api/runs/{run_id}/waterfall")
    assert response.status_code == 200
    assert response.json()["lanes"] == []


def test_get_waterfall_window_filter(client: TestClient, nested_span_run: str) -> None:
    """`from_ms`/`to_ms` restrict spans to those overlapping the window."""
    in_window = client.get(
        f"/api/runs/{nested_span_run}/waterfall", params={"from_ms": 15, "to_ms": 45}
    ).json()
    assert sum(len(lane["spans"]) for lane in in_window["lanes"]) == 1
    outside_window = client.get(
        f"/api/runs/{nested_span_run}/waterfall", params={"from_ms": 1000, "to_ms": 2000}
    ).json()
    assert outside_window["lanes"] == []


def test_get_state_at_returns_last_known_values(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """`GET /runs/{id}/state?at_virtual_ts=` reconstructs state as of that instant (PRD §20.4)."""
    run_id, events = sealed_run
    at_ts = events[-1].virtual_ts_ms
    response = client.get(f"/api/runs/{run_id}/state", params={"at_virtual_ts": at_ts})
    assert response.status_code == 200
    body = response.json()
    assert body["at_virtual_ts"] == at_ts
    assert body["keys"]
    for key in body["keys"]:
        assert key["value_hash"]
        assert key["writer"] is not None


def test_get_state_at_requires_the_query_param(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """`at_virtual_ts` is required — omitting it is `422`, not an implicit `0`."""
    run_id, _events = sealed_run
    response = client.get(f"/api/runs/{run_id}/state")
    assert response.status_code == 422


def test_get_exploration_always_409_in_this_build(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """No exploration report exists to serve (P17/`explore` layering gap); declared, not faked."""
    run_id, _events = sealed_run
    response = client.get(f"/api/runs/{run_id}/exploration")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "E-EXPL-001"
    # I10 (CONTEXT.md §2) requires this exact sentence verbatim — a loose substring check
    # ("coverage" or "bounded") has no discriminating power against a rephrase that drops it
    # while keeping either word (OP-2 audit, op2-audit-p14.md finding #3, demonstrated live: a
    # mutated message with the real I10 sentence entirely removed still passed the old assert).
    assert (
        "Bounded search: absence of findings is not proof of absence."
        in body["error"]["message"]
    )


def test_get_exploration_404_for_unknown_run(client: TestClient) -> None:
    """The run-existence check happens before the "not available" refusal."""
    response = client.get("/api/runs/r_missing/exploration")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E-RUN-404"
