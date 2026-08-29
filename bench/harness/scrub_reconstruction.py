#!/usr/bin/env python3
"""FR-10 / PRD §20.4: state reconstruction at an arbitrary virtual timestamp — scrub latency.

**What is measured, stated precisely (Rule E1, AGENTS.md §6).** `agentdx.store.snapshots.
state_at(store, run_id, virtual_ts_ms)` is the exact function `GET /api/runs/{id}/state`
(`api/routes/analysis.py::get_state_at`) calls, and the exact function PRD §20.4's own
pseudocode names — this benchmarks that function directly, against a `SnapshottingStore`
seeded the way a real run actually is (snapshots written inline during ingestion via
`_on_batch_persisted`, not rebuilt after the fact), never a hand-rolled substitute.

Two thresholds are gated, not one, because two different documents each name a real number and
neither should be quietly dropped:

* **PRD §20.4**: "Target: <100 ms for a 5 000-event run." — run at exactly that log length.
* **This prompt's FR-10** ("scrubbing reconstructs state in <200ms p95"): the harder, more
  general number. Run at a larger log length too (`--large-events`, default 50 000) for
  headroom beyond the one §20.4 explicitly sized for.

`virtual_ts_ms` sample points are drawn uniformly at random (seeded, `random.Random(42)` —
reproducible, not cherry-picked) across the run's own `[min_ts, max_ts]` virtual-time span, so
the reported p95 reflects scrubbing to an arbitrary point, not just the timestamps a span
happens to start or end on.

Rule E1: the JSON this writes is the file every published scrub-latency number must cite with
a `[bench:scrub-reconstruction.json]` marker.

Usage: `python3.12 bench/harness/scrub_reconstruction.py [--events N] [--large-events N]
[--samples N]`
Exit codes: 0 both thresholds were met · 2 either was not.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from tests.unit.store.factories import build_log_of_length, run_record_for  # noqa: E402

from agentdx.config import StoreConfig  # noqa: E402
from agentdx.events.canonical import build_chain  # noqa: E402
from agentdx.events.schema import Event  # noqa: E402
from agentdx.events.writer import ChainedEvent  # noqa: E402
from agentdx.store.snapshots import SnapshottingStore, state_at  # noqa: E402
from agentdx.store.sqlite import RunRecord  # noqa: E402

SECTION_TARGET_MS = 100.0
"""PRD §20.4's own literal number, for the 5 000-event configuration only."""

FR10_TARGET_P95_MS = 200.0
"""This prompt's FR-10: scrubbing reconstructs state in <200ms p95 — the headline gate."""

DEFAULT_SECTION_EVENTS = 5_000
"""PRD §20.4's own literal run size — not a round number picked for convenience."""

DEFAULT_LARGE_EVENTS = 50_000
"""Headroom beyond §20.4's own 5 000-event case, for FR-10's more general p95 claim."""

DEFAULT_SAMPLES = 200
BATCH_SIZE = 128
SAMPLE_SEED = 42


def _seed_store(events: list[Event], directory: Path) -> tuple[SnapshottingStore, str]:
    """Ingest a pre-built log through `SnapshottingStore`.

    The same inline-snapshot path a real recorded run takes (`store/snapshots.py`'s own
    class docstring: "use this class wherever a run is being recorded").
    """
    chained = [
        ChainedEvent(event=event, prev_hash=prev, this_hash=this)
        for event, (prev, this) in zip(events, build_chain(events), strict=True)
    ]
    config = StoreConfig(snapshot_interval_events=500, append_batch_size=BATCH_SIZE)
    store = SnapshottingStore.open(directory / "bench.db", config=config)
    store.create_run(RunRecord(**run_record_for(events, status="sealed")))  # type: ignore[arg-type]
    for start in range(0, len(chained), BATCH_SIZE):
        store.append(chained[start : start + BATCH_SIZE])
    return store, events[0].run_id


def _percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile over an already-sorted list.

    No interpolation, so the reported figure is always one of the samples actually
    measured, never an invented in-between value.
    """
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, round(p / 100 * (len(sorted_values) - 1))))
    return sorted_values[idx]


def measure(name: str, target_events: int, samples: int, seed: int) -> dict[str, object]:
    """Build one log of `target_events`, ingest it, and sample `samples` random scrub positions.

    Returns the latency distribution — construction and ingestion excluded from every
    timed sample, same discipline as `store_write_throughput.py`.
    """
    events = build_log_of_length(target_events)
    rng = random.Random(seed)  # noqa: S311 -- benchmark sample selection, not cryptographic
    min_ts = min(e.virtual_ts_ms for e in events)
    max_ts = max(e.virtual_ts_ms for e in events)
    targets = [rng.randint(min_ts, max_ts) for _ in range(samples)]

    with tempfile.TemporaryDirectory() as directory:
        store, run_id = _seed_store(list(events), Path(directory))
        try:
            latencies_ms: list[float] = []
            for ts in targets:
                started = time.perf_counter()  # determinism-exempt: benchmark harness, outside src/
                state_at(store, run_id, ts)
                latencies_ms.append((time.perf_counter() - started) * 1000)  # determinism-exempt
        finally:
            store.close()

    latencies_ms.sort()
    return {
        "name": name,
        "events": len(events),
        "samples": samples,
        "virtual_ts_range_ms": [min_ts, max_ts],
        "p50_ms": round(_percentile(latencies_ms, 50), 3),
        "p95_ms": round(_percentile(latencies_ms, 95), 3),
        "p99_ms": round(_percentile(latencies_ms, 99), 3),
        "max_ms": round(latencies_ms[-1], 3) if latencies_ms else 0.0,
        "min_ms": round(latencies_ms[0], 3) if latencies_ms else 0.0,
    }


def main() -> int:
    """Measure both configurations, write the result file, and gate on both §20.4 and FR-10."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=DEFAULT_SECTION_EVENTS)
    parser.add_argument("--large-events", type=int, default=DEFAULT_LARGE_EVENTS)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument(
        "--out", type=Path, default=REPO_ROOT / "bench" / "results" / "scrub-reconstruction.json"
    )
    args = parser.parse_args()

    section_run = measure("section_20_4_5000_events", args.events, args.samples, SAMPLE_SEED)
    large_run = measure(
        "fr10_headroom_50000_events", args.large_events, args.samples, SAMPLE_SEED + 1
    )

    section_p95 = float(str(section_run["p95_ms"]))
    large_p95 = float(str(large_run["p95_ms"]))
    section_met = section_p95 < SECTION_TARGET_MS
    fr10_met = large_p95 < FR10_TARGET_P95_MS
    # FR-10 is evaluated at *both* log sizes — a scrub target that only holds at the small,
    # PRD-sized run and not at 10x that size would be a threshold met by accident, not by the
    # snapshot-every-500-events design actually working (PRD §20.4: "Reconstruction is
    # therefore O(500) worst case regardless of run length" — the claim this second run tests).
    section_fr10_met = section_p95 < FR10_TARGET_P95_MS
    overall_met = section_met and fr10_met and section_fr10_met

    result = {
        "benchmark": "scrub-reconstruction",
        "requirement": "FR-10 / PRD §20.4",
        "requirement_text": (
            "FR-10: scrubbing reconstructs state at an arbitrary virtual timestamp in <200ms "
            "p95. PRD §20.4: 'Target: <100 ms for a 5 000-event run.'"
        ),
        "function_under_test": "agentdx.store.snapshots.state_at(store, run_id, virtual_ts_ms)",
        "gate_status": (
            "Pass/fail against threshold is the claim; the exact p50/p95/p99 millisecond "
            "figures are real wall-clock measurements and will vary run to run and machine "
            "to machine (D-68). Any prose citing this file must reference the threshold "
            "result, not quote a specific digit as a permanent fact."
        ),
        "thresholds": {
            "section_20_4_ms": SECTION_TARGET_MS,
            "fr10_p95_ms": FR10_TARGET_P95_MS,
        },
        "met": overall_met,
        "section_20_4": {**section_run, "threshold_ms": SECTION_TARGET_MS, "met": section_met},
        "fr10_headroom": {**large_run, "threshold_ms": FR10_TARGET_P95_MS, "met": fr10_met},
        "method": (
            "Each configuration builds a fresh log via `build_log_of_length`, ingests it "
            "through `SnapshottingStore` (inline snapshots every 500 events, the real "
            "recording path), then samples `--samples` virtual timestamps uniformly at "
            "random (seeded) across the run's own timespan and times `state_at` for each — "
            "log construction and ingestion are excluded from every timed sample. Percentiles "
            "are nearest-rank over the sorted sample, not interpolated."
        ),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "snapshot_interval_events": 500,
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    sys.stdout.write(
        f"{section_run['name']:>28}: p50={section_run['p50_ms']}ms p95={section_run['p95_ms']}ms "
        f"p99={section_run['p99_ms']}ms  (§20.4 <{SECTION_TARGET_MS}ms: "
        f"{'MET' if section_met else 'NOT MET'})\n"
        f"{large_run['name']:>28}: p50={large_run['p50_ms']}ms p95={large_run['p95_ms']}ms "
        f"p99={large_run['p99_ms']}ms  (FR-10 <{FR10_TARGET_P95_MS}ms p95: "
        f"{'MET' if fr10_met else 'NOT MET'})\n"
        f"written to {args.out}\n"
    )
    return 0 if overall_met else 2


if __name__ == "__main__":
    raise SystemExit(main())
