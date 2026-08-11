#!/usr/bin/env python3
"""NFR-1 / FR-1: instrumentation overhead. Writes `bench/results/sdk-overhead.json`.

**What is measured, and why the number is meaningless without the sentence after it.**
"Overhead" is a *ratio*, and a ratio has a denominator. The denominator here is the wall
clock of the same graph, uninstrumented, doing the same simulated work. So the figure depends
entirely on how much work each node does — instrument a graph whose nodes do nothing and the
overhead is enormous and irrelevant; instrument one whose nodes each wait 800 ms on a model
and it is invisible. Quoting only the flattering end of that range is exactly what Rule E1
exists to prevent, so both ends are measured and both are published:

* `no_work` — nodes return immediately. This is the **pure instrumentation cost**, reported
  as a ratio (large, and honestly so) and as microseconds per event, which is the figure that
  actually transfers to another workload.
* `llm_50ms` — each node waits 50 ms, standing in for a model call. **This is the gated
  figure.** 50 ms is 16× *faster* than Q-43.2.3's default LLM latency of 800 ms, so the ratio
  it produces is a conservative upper bound on the ratio at realistic latency: the same
  absolute cost divided by a smaller denominator. If the budget is met here it is met there.
  Nothing is projected — the 800 ms figure is not quoted, because it was not measured.

The instrumented path is the **whole** production path: recording, `EventWriter` validation,
canonical bytes, the blake2b chain, and batched inserts into a real SQLite database with WAL
and both append-only triggers. A benchmark that stopped at the recorder would be measuring a
component nobody ships.

Every configuration is run several times and the **slowest** run is reported. A benchmark
that publishes its best sample is describing a machine that does not exist.

Rule E1 (AGENTS.md §6): the JSON this writes is the file every published overhead number must
cite with a `[bench:sdk-overhead.json]` marker.

Usage: `python bench/harness/sdk_overhead.py [--iterations N] [--repeats N]`
Exit codes: 0 the budget was met · 2 it was not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated, TypedDict

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

import operator  # noqa: E402

from langgraph.graph import END, START, StateGraph  # noqa: E402
from tests.unit.sdk.fakes import StampingRecorder, make_context  # noqa: E402

import agentdx  # noqa: E402
from agentdx.events.schema import SCHEMA_VERSION  # noqa: E402
from agentdx.events.writer import EventWriter  # noqa: E402
from agentdx.store.snapshots import SnapshottingStore  # noqa: E402
from agentdx.store.sqlite import RunRecord  # noqa: E402

OVERHEAD_BUDGET_PERMILLE = 100
"""NFR-1's budget: < 10 % wall clock in passthrough. Expressed in per-mille because ADR-007
forbids floats anywhere a value might reach the event log, and one threshold spelled two ways
is one threshold too many."""

LLM_LATENCY_MS = 50
"""Simulated per-node model latency for the gated configuration. Deliberately 16× lower than
Q-43.2.3's 800 ms default, which makes the measured ratio a conservative upper bound."""

DEFAULT_ITERATIONS = 40
DEFAULT_REPEATS = 3
NODE_COUNT = 4


class BenchState(TypedDict, total=False):
    """The state of the benchmark graph: one plain channel, one reduced channel."""

    task: str
    plan: str
    drafts: Annotated[list[str], operator.add]
    review: str


def build_graph(latency_ms: int) -> object:
    """Return a four-node graph whose nodes each wait `latency_ms`, standing in for a call."""
    delay = latency_ms / 1000

    async def planner(state: BenchState) -> dict[str, object]:
        if delay:
            await asyncio.sleep(delay)
        return {"plan": f"plan:{state.get('task', '')}"}

    async def coder(state: BenchState) -> dict[str, object]:
        if delay:
            await asyncio.sleep(delay)
        return {"drafts": [f"draft:{state.get('plan', '')}"]}

    async def tester(state: BenchState) -> dict[str, object]:
        if delay:
            await asyncio.sleep(delay)
        return {"drafts": [f"tests:{len(state.get('drafts', []))}"]}

    async def reviewer(state: BenchState) -> dict[str, object]:
        if delay:
            await asyncio.sleep(delay)
        return {"review": f"ok:{len(state.get('drafts', []))}"}

    graph = StateGraph(BenchState)
    graph.add_node("planner", planner)
    graph.add_node("coder", coder)
    graph.add_node("tester", tester)
    graph.add_node("reviewer", reviewer)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "coder")
    graph.add_edge("coder", "tester")
    graph.add_edge("tester", "reviewer")
    graph.add_edge("reviewer", END)
    return graph.compile()


async def _time_uninstrumented(latency_ms: int, iterations: int) -> float:
    """Return seconds to run the graph `iterations` times with no instrumentation at all."""
    graph = build_graph(latency_ms)
    started = time.perf_counter()  # determinism-exempt: benchmark harness, outside src/
    for index in range(iterations):
        await graph.ainvoke({"task": f"task-{index}"})
    return time.perf_counter() - started  # determinism-exempt: benchmark harness


async def _time_instrumented(
    latency_ms: int, iterations: int, directory: Path
) -> tuple[float, int]:
    """Return `(seconds, events)` for the full path: record, validate, chain, store."""
    run_id = "r_bench"
    store = SnapshottingStore.open(directory / "bench.db")
    try:
        store.create_run(
            RunRecord(
                run_id=run_id,
                scenario_hash="blake2b:" + "0" * 64,
                graph_hash="blake2b:" + "0" * 64,
                mode="baseline",
                seed=42,
                status="running",
                created_at="2026-08-11T00:00:00Z",
                agentdx_version=agentdx.__version__,
                schema_version=SCHEMA_VERSION,
            )
        )
        writer = EventWriter(run_id, store)
        recorder = StampingRecorder(run_id, writer=writer)
        context, _ = make_context(run_id=run_id, recorder=recorder)
        graph = agentdx.instrument(build_graph(latency_ms), name="bench", context=context)

        started = time.perf_counter()  # determinism-exempt: benchmark harness, outside src/
        for index in range(iterations):
            await graph.ainvoke({"task": f"task-{index}"})
        writer.flush()
        elapsed = time.perf_counter() - started  # determinism-exempt: benchmark harness
        return elapsed, len(recorder.events)
    finally:
        store.close()


def measure(name: str, latency_ms: int, iterations: int, repeats: int) -> dict[str, object]:
    """Run one configuration `repeats` times and report the worst overhead ratio.

    Guarantees: the worst instrumented sample is compared against the *best* uninstrumented
    one, so the published overhead is the least flattering pairing the data supports.
    """
    baselines: list[float] = []
    instrumented: list[float] = []
    events = 0
    for _ in range(repeats):
        baselines.append(asyncio.run(_time_uninstrumented(latency_ms, iterations)))
        with tempfile.TemporaryDirectory() as directory:
            elapsed, events = asyncio.run(
                _time_instrumented(latency_ms, iterations, Path(directory))
            )
            instrumented.append(elapsed)

    baseline = min(baselines)
    worst = max(instrumented)
    overhead_permille = round((worst - baseline) / baseline * 1000)
    added_us_per_event = round((worst - baseline) * 1_000_000 / max(events, 1))
    return {
        "name": name,
        "simulated_node_latency_ms": latency_ms,
        "iterations": iterations,
        "repeats": repeats,
        "events_per_iteration": events // iterations,
        "baseline_seconds_best": round(baseline, 4),
        "instrumented_seconds_worst": round(worst, 4),
        "overhead_permille": overhead_permille,
        "added_microseconds_per_event": added_us_per_event,
    }


def main() -> int:
    """Measure both configurations, write the result file, and gate on NFR-1."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument(
        "--out", type=Path, default=REPO_ROOT / "bench" / "results" / "sdk-overhead.json"
    )
    arguments = parser.parse_args()

    measurements = [
        measure("llm_50ms", LLM_LATENCY_MS, arguments.iterations, arguments.repeats),
        measure("no_work", 0, arguments.iterations, arguments.repeats),
    ]
    headline = measurements[0]
    overhead = int(str(headline["overhead_permille"]))

    result = {
        "benchmark": "sdk-overhead",
        "requirement": "NFR-1",
        "requirement_text": "< 10% wall-clock overhead in passthrough mode",
        "threshold_permille": OVERHEAD_BUDGET_PERMILLE,
        "headline_measurement": "llm_50ms",
        "headline_overhead_permille": overhead,
        "met": overhead < OVERHEAD_BUDGET_PERMILLE,
        "conformant_to_prd_34_1": False,
        "not_measured": [
            "No provider or cache call is in the measured path. Node 'work' is simulated "
            "latency, so neither the passthrough path PRD §34.1 specifies nor the replay "
            "path a real run uses is exercised. The instrumentation side IS the whole "
            "production path; the denominator is a simulation.",
            "Two configurations, not PRD §34.1's three, and neither is scenario-driven.",
            "Worst-instrumented against best-uninstrumented, not median and p90. That is "
            "deliberately the least flattering pairing the data supports, but a single "
            "worst sample is not the statistic §34.1 names.",
            "Measured on the interpreter named in `environment`, which is NOT the project's "
            "pinned CPython 3.12 when it reads 3.10 (CONTEXT.md D-08, D-32).",
        ],
        "gate_status": (
            "FR-1 is met in this measured configuration. NFR-1 is NOT met in full: see "
            "`not_measured` and CONTEXT.md D-31. Do not quote this as 'the NFR-1 number'."
        ),
        "measurements": measurements,
        "method": (
            "The uninstrumented graph is the denominator. The instrumented path is the whole "
            "production path — agent spans, state reads and writes, edge messages, "
            "EventWriter validation, canonical bytes, the blake2b chain, and batched inserts "
            "into a real SQLite database with WAL and both append-only triggers. Each "
            "configuration runs `repeats` times; the WORST instrumented sample is compared "
            "against the BEST uninstrumented one, so the published ratio is the least "
            "flattering pairing the data supports."
        ),
        "interpretation": (
            f"The gated figure simulates {LLM_LATENCY_MS} ms of work per node, which is 16x "
            f"faster than Q-43.2.3's 800 ms default for an LLM call. A smaller denominator "
            f"gives a LARGER ratio, so this is a conservative upper bound on the overhead a "
            f"real graph pays — it is not projected onto 800 ms, because a projected number "
            f"is not a measurement (see CONTEXT.md D-17 for what projecting one cost us). "
            f"`no_work` is the pure instrumentation cost with no work to amortise it "
            f"against; its ratio is large by construction and the microseconds-per-event "
            f"figure beside it is the one that transfers to another workload."
        ),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "langgraph": _distribution_version("langgraph"),
            "nodes_per_graph": NODE_COUNT,
            "journal_mode": "WAL",
            "synchronous": "NORMAL",
        },
    }

    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for measurement in measurements:
        sys.stdout.write(
            f"{measurement['name']:>10}: overhead "
            f"{int(str(measurement['overhead_permille'])) / 10:>7.1f} %  "
            f"(+{measurement['added_microseconds_per_event']} us/event, "
            f"{measurement['events_per_iteration']} events/run)\n"
        )
    sys.stdout.write(
        f"\nNFR-1 budget < {OVERHEAD_BUDGET_PERMILLE / 10:.0f} % wall clock\n"
        f"  headline (llm_50ms)  {overhead / 10:.1f} %  "
        f"{'MET' if result['met'] else 'NOT MET'}\n"
        f"written to {arguments.out}\n"
    )
    return 0 if result["met"] else 2


def _distribution_version(name: str) -> str:
    """Return an installed distribution's version, or `unknown`."""
    import importlib.metadata

    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
