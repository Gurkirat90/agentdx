"""Definition of done 1: one line instruments a LangGraph graph and yields a valid log.

"Valid" means what P02 means by it — `validate_log` over all three layers — and the log is
written the way a real run writes it: `EventWriter` → chain → `SnapshottingStore` → SQLite,
with the append-only triggers installed. Nothing here validates a log that only ever existed
in memory, because a log that never reached the store proves nothing about the store.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import agentdx
from agentdx.events.canonical import verify_chain
from agentdx.events.schema import SCHEMA_VERSION, EventType
from agentdx.events.validators import validate_log
from agentdx.events.writer import EventWriter
from agentdx.store.snapshots import SnapshottingStore
from agentdx.store.sqlite import RunRecord
from tests.unit.sdk.fakes import StampingRecorder, make_context
from tests.unit.sdk.graphs import build_pipeline

RUN_ID = "r_0be71"


def _open_store(tmp_path: Path) -> SnapshottingStore:
    store = SnapshottingStore.open(tmp_path / "runs.db")
    store.create_run(
        RunRecord(
            run_id=RUN_ID,
            scenario_hash="blake2b:" + "1" * 64,
            graph_hash="blake2b:" + "2" * 64,
            mode="baseline",
            seed=42,
            status="running",
            created_at="2026-08-11T00:00:00Z",
            agentdx_version=agentdx.__version__,
            schema_version=SCHEMA_VERSION,
        )
    )
    return store


@pytest.mark.asyncio
async def test_a_fixture_graph_is_instrumented_in_one_line_and_the_log_validates(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    writer = EventWriter(RUN_ID, store)
    recorder = StampingRecorder(RUN_ID, writer=writer)
    context, _ = make_context(run_id=RUN_ID, recorder=recorder)

    # --- the one line -------------------------------------------------------------------
    graph = agentdx.instrument(build_pipeline(), name="code_pipeline", context=context)
    # ------------------------------------------------------------------------------------

    output = await graph.ainvoke({"task": "add pagination to the API"})
    writer.flush()

    assert output["review"] == "reviewed 1 draft(s)"

    # P02's validators, all three layers, over the log the store actually holds.
    stored = list(store.read_events(RUN_ID))
    validate_log(stored)
    assert [event.seq for event in stored] == list(range(len(stored)))

    chain = [(c.prev_hash, c.this_hash) for c in store.read_chained(RUN_ID)]
    assert verify_chain(stored, chain) is None

    types = {event.type for event in stored}
    assert EventType.SPAN_START in types
    assert EventType.SPAN_END in types
    assert EventType.STATE_READ in types
    assert EventType.STATE_WRITE in types
    assert EventType.MESSAGE_SEND in types
    assert EventType.MESSAGE_RECV in types
    assert types & {EventType.INSTRUMENTATION_GAP} == set(), (
        "a clean graph on the pinned LangGraph must bind every construct"
    )
    store.close()


@pytest.mark.asyncio
async def test_the_log_survives_the_append_only_triggers(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    writer = EventWriter(RUN_ID, store)
    recorder = StampingRecorder(RUN_ID, writer=writer)
    context, _ = make_context(run_id=RUN_ID, recorder=recorder)
    graph = agentdx.instrument(build_pipeline(), name="code_pipeline", context=context)

    await graph.ainvoke({"task": "t"})
    writer.flush()
    store.close()

    connection = sqlite3.connect(tmp_path / "runs.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE events SET agent_id = 'x'")
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_agent_ids_can_be_remapped_without_touching_the_graph(tmp_path: Path) -> None:
    # PRD §8.2 item 1: `agent_from=lambda node_name: node_name`. Identity must be stable
    # across runs or PRD §17's baseline comparison breaks, so the mapping is the user's.
    store = _open_store(tmp_path)
    writer = EventWriter(RUN_ID, store)
    recorder = StampingRecorder(RUN_ID, writer=writer)
    context, _ = make_context(run_id=RUN_ID, recorder=recorder)

    graph = agentdx.instrument(
        build_pipeline(),
        name="code_pipeline",
        agent_from=lambda node_name: f"team/{node_name}",
        context=context,
    )
    await graph.ainvoke({"task": "t"})
    writer.flush()

    agents = sorted(
        {event.agent_id for event in store.read_events(RUN_ID) if event.agent_id is not None}
    )
    assert agents == ["team/coder", "team/planner", "team/reviewer"]
    store.close()
