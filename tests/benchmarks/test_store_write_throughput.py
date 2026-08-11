"""NFR-10: the store's write path sustains ≥ 20 000 events/s (design constraint 3).

Two things happen here, deliberately kept apart.

**The gate.** `test_store_append_meets_nfr_10` fails the build if the store's own write path
drops below the threshold. It runs a smaller log than `bench/harness/store_write_throughput.py`
so it fits in a test run; the published figure comes from the harness, and Rule E1 markers
cite `bench/results/store-write-throughput.json`, never this test.

**The honest record.** `test_the_composed_path_shortfall_is_recorded` does not assert a
rate. It asserts that the committed result file *states* the composed writer→store rate and
whether it met the threshold. The composed path is currently below 20 000 events/s and the
cause is in `events/canonical.py`, which P03 may not change (AGENTS.md §2). A test that
quietly gated only the fast path and never mentioned the slow one would be the kind of
selective measurement Rule E1 exists to prevent, so the shortfall is asserted to be
*published* rather than asserted away.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agentdx.config import StoreConfig
from agentdx.events.canonical import build_chain
from agentdx.events.writer import ChainedEvent
from agentdx.store.sqlite import RunRecord, Store
from tests.unit.store.factories import build_log_of_length, run_record_for

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "bench" / "harness"))

from store_write_throughput import TARGET_EVENTS_PER_SECOND  # noqa: E402

RESULTS = REPO_ROOT / "bench" / "results" / "store-write-throughput.json"
"""NFR-10's threshold is imported from the harness rather than restated here, so the gate
and the published figure can never be measured against different numbers."""

GATE_EVENTS = 30_000
BATCH_SIZE = 128

pytestmark = pytest.mark.benchmark


def _load_results() -> dict[str, object]:
    """Return the committed benchmark result, or skip if it has not been produced."""
    if not RESULTS.is_file():
        pytest.skip(f"{RESULTS.name} has not been produced; run bench/harness/")
    parsed: object = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_store_append_meets_nfr_10() -> None:
    """The store's batched write path sustains at least 20 000 events/s.

    Batched, not per-event: `append` writes a whole batch in one transaction. Writing one
    event per transaction on this same hardware is roughly two orders of magnitude slower,
    which is why design constraint 3 names batching explicitly.
    """
    events = build_log_of_length(GATE_EVENTS)
    chained = [
        ChainedEvent(event=event, prev_hash=prev, this_hash=this)
        for event, (prev, this) in zip(events, build_chain(events), strict=True)
    ]
    with tempfile.TemporaryDirectory() as directory:
        store = Store.open(
            Path(directory) / "bench.db",
            config=StoreConfig(append_batch_size=BATCH_SIZE),
        )
        try:
            store.create_run(RunRecord(**run_record_for(events)))  # type: ignore[arg-type]
            started = time.perf_counter()
            for start in range(0, len(chained), BATCH_SIZE):
                store.append(chained[start : start + BATCH_SIZE])
            elapsed = time.perf_counter() - started
        finally:
            store.close()

    rate = len(chained) / elapsed
    assert rate >= TARGET_EVENTS_PER_SECOND, (
        f"NFR-10 requires >= {TARGET_EVENTS_PER_SECOND:,} events/s; measured "
        f"{rate:,.0f} events/s over {len(chained):,} events. Do not weaken this "
        f"assertion (AGENTS.md §5) — the write path is what changed."
    )


def test_the_published_result_exists_and_gates_the_same_number() -> None:
    """Rule E1: the committed result file names NFR-10 and the same threshold.

    A published statistic must trace to a reproducible measurement. This asserts that the
    file every marker resolves to actually describes the requirement it claims to.
    """
    result = _load_results()
    assert result["requirement"] == "NFR-10"
    assert result["threshold_events_per_second"] == TARGET_EVENTS_PER_SECOND
    assert result["headline_measurement"] == "store_append"
    assert result["met"] is True
    assert int(str(result["headline_events_per_second"])) >= TARGET_EVENTS_PER_SECOND


def test_the_composed_path_shortfall_is_recorded() -> None:
    """The writer→store figure is published whether or not it flatters the store.

    Asserts that the result file states the composed rate and its met/not-met status, and
    names a cause. This test passes whether the composed path meets the threshold or not —
    what it refuses to allow is the number going unpublished.
    """
    result = _load_results()
    composed = result["composed_path"]
    assert isinstance(composed, dict)
    assert composed["measurement"] == "writer_to_store"
    assert isinstance(composed["events_per_second"], int)
    assert isinstance(composed["met"], bool)
    assert "canonical" in str(composed["note"]), "the shortfall is recorded without a cause"


def test_the_result_records_its_environment() -> None:
    """A throughput number without its machine is not reproducible (§34.8)."""
    result = _load_results()
    environment = result["environment"]
    assert isinstance(environment, dict)
    for key in ("python", "machine", "system", "batch_size", "journal_mode", "synchronous"):
        assert key in environment, f"the published result does not record {key}"
    assert environment["journal_mode"] == "WAL"


def test_the_result_reports_the_worst_run_not_the_best() -> None:
    """The published figure is the slowest sample of several (§34.8).

    A benchmark that publishes its best run describes a machine nobody has.
    """
    result = _load_results()
    measurements = result["measurements"]
    assert isinstance(measurements, list)
    for measurement in measurements:
        assert isinstance(measurement, dict)
        assert measurement["repeats"] >= 3
        assert measurement["events_per_second_worst"] <= measurement["events_per_second_best"]
    headline = next(m for m in measurements if m["name"] == "store_append")
    assert result["headline_events_per_second"] == headline["events_per_second_worst"]
