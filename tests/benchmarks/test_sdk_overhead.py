"""NFR-1 / FR-1: instrumentation overhead stays under 10 % wall clock (design constraint 6).

Two things, kept apart for the same reason P03 kept them apart in
`test_store_write_throughput.py`.

**The gate.** `test_instrumentation_overhead_meets_nfr_1` measures a smaller run than the
harness so it fits in a test run, and fails the build if the budget is exceeded.

**The honest record.** The remaining tests assert that the committed result file publishes
*both* configurations — the realistic one and the zero-work one whose ratio is enormous — and
that it records the machine it was measured on. A gate that quietly published only the
flattering configuration would be exactly the selective measurement Rule E1 exists to prevent.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "bench" / "harness"))

from sdk_overhead import (  # noqa: E402
    LLM_LATENCY_MS,
    OVERHEAD_BUDGET_PERMILLE,
    _time_instrumented,
    _time_uninstrumented,
)

RESULTS = REPO_ROOT / "bench" / "results" / "sdk-overhead.json"
"""NFR-1's budget is imported from the harness rather than restated, so the gate and the
published figure can never be measured against different numbers."""

GATE_ITERATIONS = 10
GATE_REPEATS = 3

pytestmark = pytest.mark.benchmark


def _load_results() -> dict[str, object]:
    """Return the committed benchmark result, or skip if it has not been produced."""
    if not RESULTS.is_file():
        pytest.skip(f"{RESULTS.name} has not been produced; run bench/harness/sdk_overhead.py")
    parsed: object = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_instrumentation_overhead_meets_nfr_1() -> None:
    """The instrumented graph costs less than 10 % more wall clock than the bare one.

    The comparison is worst-instrumented against best-uninstrumented, which is the least
    flattering pairing available. Do not weaken this assertion (AGENTS.md §5) — if it fails,
    the capture path got more expensive.
    """
    baselines: list[float] = []
    instrumented: list[float] = []
    for _ in range(GATE_REPEATS):
        baselines.append(asyncio.run(_time_uninstrumented(LLM_LATENCY_MS, GATE_ITERATIONS)))
        with tempfile.TemporaryDirectory() as directory:
            elapsed, _ = asyncio.run(
                _time_instrumented(LLM_LATENCY_MS, GATE_ITERATIONS, Path(directory))
            )
            instrumented.append(elapsed)

    baseline = min(baselines)
    worst = max(instrumented)
    overhead_permille = (worst - baseline) / baseline * 1000
    assert overhead_permille < OVERHEAD_BUDGET_PERMILLE, (
        f"NFR-1 budgets < {OVERHEAD_BUDGET_PERMILLE / 10:.0f} % wall-clock overhead; measured "
        f"{overhead_permille / 10:.1f} % over {GATE_ITERATIONS} iterations "
        f"({baseline:.3f}s bare vs {worst:.3f}s instrumented)."
    )


def test_the_published_result_gates_the_same_number() -> None:
    """Rule E1: the committed result names NFR-1 and the same budget."""
    result = _load_results()
    assert result["requirement"] == "NFR-1"
    assert result["threshold_permille"] == OVERHEAD_BUDGET_PERMILLE
    assert result["headline_measurement"] == "llm_50ms"
    assert result["met"] is True
    assert int(str(result["headline_overhead_permille"])) < OVERHEAD_BUDGET_PERMILLE


def test_the_zero_work_configuration_is_published_too() -> None:
    """The unflattering configuration is in the file, with its per-event cost.

    `no_work` has no work to amortise instrumentation against, so its ratio is large by
    construction. Publishing only `llm_50ms` would be describing a workload rather than a
    system; publishing both, with the microseconds-per-event figure that actually transfers,
    is what makes the headline interpretable.
    """
    result = _load_results()
    measurements = result["measurements"]
    assert isinstance(measurements, list)
    names = {str(measurement["name"]) for measurement in measurements}
    assert names == {"llm_50ms", "no_work"}
    for measurement in measurements:
        assert isinstance(measurement, dict)
        assert measurement["repeats"] >= 3
        assert isinstance(measurement["added_microseconds_per_event"], int)
        assert measurement["events_per_iteration"] > 0


def test_the_result_states_what_its_denominator_was() -> None:
    """An overhead ratio without its denominator is not a measurement."""
    result = _load_results()
    interpretation = str(result["interpretation"])
    assert "800 ms" in interpretation, "the file must say how far the gated figure is from real"
    assert "conservative upper bound" in interpretation
    assert "not projected" in interpretation


def test_the_result_records_its_environment() -> None:
    """A wall-clock number without its machine is not reproducible (PRD §34.8)."""
    result = _load_results()
    environment = result["environment"]
    assert isinstance(environment, dict)
    for key in ("python", "machine", "system", "langgraph", "nodes_per_graph"):
        assert key in environment, f"the published result does not record {key}"


def test_the_gate_and_the_harness_share_one_timing_path() -> None:
    """The gate calls the harness's own functions, not a copy of them.

    A gate with its own timing code is a gate that can pass while the published figure fails.
    """
    assert callable(_time_uninstrumented)
    assert callable(_time_instrumented)
    assert time is not None
