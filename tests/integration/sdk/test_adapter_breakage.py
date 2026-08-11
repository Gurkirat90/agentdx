"""Definition of done 2: simulated adapter breakage → `instrumentation_gap` + a loud error.

Design constraint 1, end to end and against a real database: take a real compiled LangGraph
graph, break one binding the way an upstream version bump would break it, and assert that the
run does not start, that the reason is written to the log, and that the log the analysers
would read says so.

`--capture=no -s` prints the recorded gap and the raised error, which is what the P04 prompt
asks to be pasted.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

import agentdx
from agentdx.events.schema import SCHEMA_VERSION, EventType
from agentdx.events.validators import validate_log
from agentdx.events.writer import EventWriter
from agentdx.sdk.generic import InstrumentationGapWarning
from agentdx.store.snapshots import SnapshottingStore
from agentdx.store.sqlite import RunRecord
from tests.unit.sdk.fakes import StampingRecorder, make_context
from tests.unit.sdk.graphs import build_pipeline

RUN_ID = "r_0dead"


class _NextMajorBound:
    """A node callable as a future LangGraph might ship it: `ainvoke` lost its `config`.

    This is the exact drift PRD §8.3's proxy design exists to survive. The adapter cannot
    call it correctly, so the only honest options are to record less or to stop — and
    recording less would produce a log with no node spans that still *looks* like a log.
    """

    def invoke(self, node_input: object, config: object = None) -> object:
        return {}

    async def ainvoke(self, node_input: object) -> object:  # <- the breaking change
        return {}


def _break_one_node(graph: object, node_name: str) -> object:
    """Replace one node's callable with the drifted one, leaving the rest of the graph real."""
    nodes = graph.nodes  # type: ignore[attr-defined]
    nodes[node_name] = nodes[node_name].copy({"bound": _NextMajorBound()})
    return graph


@pytest.mark.asyncio
async def test_a_changed_callback_signature_stops_the_run_and_is_in_the_log(
    tmp_path: Path,
) -> None:
    store = SnapshottingStore.open(tmp_path / "runs.db")
    store.create_run(
        RunRecord(
            run_id=RUN_ID,
            scenario_hash="blake2b:" + "5" * 64,
            graph_hash="blake2b:" + "6" * 64,
            mode="baseline",
            seed=42,
            status="running",
            created_at="2026-08-11T00:00:00Z",
            agentdx_version=agentdx.__version__,
            schema_version=SCHEMA_VERSION,
        )
    )
    writer = EventWriter(RUN_ID, store)
    recorder = StampingRecorder(RUN_ID, writer=writer)
    context, _ = make_context(run_id=RUN_ID, recorder=recorder)

    drifted = _break_one_node(build_pipeline(), "coder")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(agentdx.InstrumentationError) as raised:
            agentdx.instrument(drifted, name="code_pipeline", context=context)
    writer.flush()

    rule = "-" * 74
    report = [f"\n--- raised {rule}", str(raised.value), f"--- warning {rule}"]
    report += [
        str(warning.message)
        for warning in caught
        if issubclass(warning.category, InstrumentationGapWarning)
    ]
    stored = list(store.read_events(RUN_ID))
    report.append(f"--- instrumentation_gap events in the stored log {rule}")
    report += [
        f"seq={event.seq} payload={dict(event.payload)}"
        for event in stored
        if event.type is EventType.INSTRUMENTATION_GAP
    ]
    report.append(rule)
    sys.stdout.write("\n".join(report) + "\n")

    # 1. The run did not start.
    assert "E-INSTR-002" in str(raised.value)
    assert "will not record a partially-captured run" in str(raised.value)
    assert "ADR-003" in str(raised.value)

    # 2. It was loud in the process.
    assert any(issubclass(w.category, InstrumentationGapWarning) for w in caught)

    # 3. It is in the log, and the log is still a valid log.
    gaps = [event for event in stored if event.type is EventType.INSTRUMENTATION_GAP]
    assert len(gaps) == 1
    assert gaps[0].payload["construct"] == "node_callback_signature"
    assert gaps[0].payload["location"] == "code_pipeline.coder"
    assert "no longer accept (input, config)" in str(gaps[0].payload["reason"])
    validate_log(stored)

    # 4. And there are no spans — the adapter degraded to nothing rather than to a little.
    assert not any(event.type is EventType.SPAN_START for event in stored)
    store.close()


@pytest.mark.asyncio
async def test_the_healthy_graph_next_to_it_still_binds() -> None:
    # The control: the breakage above is caused by the drift, not by the test harness.
    context, recorder = make_context()
    graph = agentdx.instrument(build_pipeline(), name="code_pipeline", context=context)
    await graph.ainvoke({"task": "t"})
    assert graph.gaps == ()
    assert any(event.type is EventType.SPAN_START for event in recorder.events)
