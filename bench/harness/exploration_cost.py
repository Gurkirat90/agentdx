#!/usr/bin/env python3
"""PRD §34.4: exploration cost and schedule coverage.

**What is measured, stated precisely (Rule E1, AGENTS.md §6).** For each of two
Scheduler-driven synthetic scenarios (`tests.integration.explore._harness`'s
`make_research_fanout_executor`/`make_code_pipeline_executor`), `agentdx.explore.generate.
explore` — the real PRD §15.3 bounded-BFS function, run through no proxy — is executed once
per `delay_bound_k` in `{0, 1, 2, 3}` (the PRD's own requested sweep). Recorded per `(scenario,
k)`: schedules executed, schedules reduced away (pruned by `reduce.interesting_steps` plus
deduplicated by `dedup.SeenSchedules`), wall time, and whether `analysis.race.detect_conflicts`
— the real gate G1/G2 detector, the same one `race_accuracy.py` benchmarks — reports the
scenario's seeded finding in at least one executed schedule at that `k`. The smallest `k` at
which each scenario's own expected outcome first appears is the published number the PRD asks
for ("this is what substantiates the k=2 default with the project's own data").

**Why these two synthetic scenarios, not the three real fixtures.** D-55/D-88 (`CONTEXT.md`
§9) already rule on this, and this harness inherits that ruling rather than relitigating it:
`explore()`'s `ScheduleExecutor` drives a `Scheduler` directly via `delay_schedule`, but
neither `code_pipeline` nor `research_fanout` nor `support_triage` routes LangGraph's own
Pregel dispatch through `Scheduler.yield_point`/`spawn` (D-55's premise) — and even where that
routing is now real (D-89's own P18.3 finding, confirmed live this session), D-88 found live
that wiring `explore()` to the real fixtures today is **actively unsafe**: D-80's
`run_id`-reuse optimization silently collapses distinct schedules at one seed onto the same
cached run, fabricating a report with no warning. This benchmark therefore reuses exactly the
signed-off, already-`VERIFIED`-audited synthetic harness P13 built for this same reason
(`op2-audit-p13.md`'s own live demonstration of that hazard) — not a new decision, an inherited
one. **`support_triage` has no synthetic explore-shaped harness at all** (P13 built exactly
two shapes, `research_fanout` and `code_pipeline`, per its own module docstring) — disclosed
as a real scope boundary, not silently generalized to "all three fixtures."

**Why finding-presence is expected to be schedule-invariant here, not a benchmark defect.**
`_harness.py`'s own module docstring already states this: `analysis.race.detect_conflicts`
decides concurrency from the causality graph (vector clocks), not from which physical
interleaving `explore()` chose, and neither scenario ever declares a `causes=` edge between
one writer and another — so the same finding (or absence of one) is present at every `k`,
including `k=0`. A "smallest k found" of 0 for the defective scenario, and "never found at any
k in {0,1,2,3}" for the safe one, are therefore the *correct*, expected results for this
specific harness, not evidence the sweep did nothing — see each row's own `note` field.

Usage: `python3.12 bench/harness/exploration_cost.py`
Exit codes: always 0 — this benchmark is descriptive (PRD §34.4 states no gate), except a
genuine internal contradiction (the code_pipeline-shaped scenario failing to reproduce its own
seeded defect at any k) which is treated as a hard failure (exit 2), since that would mean the
harness itself is broken, not that the system under test passed or failed a threshold.
"""

from __future__ import annotations

import json
import platform
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from tests.integration.explore._harness import (  # noqa: E402
    ScheduleExecutor,
    make_code_pipeline_executor,
    make_research_fanout_executor,
)

from agentdx.analysis.race import detect_conflicts  # noqa: E402
from agentdx.explore.generate import ExplorationResult, explore  # noqa: E402
from agentdx.explore.generate import ScheduleExecutor as _GenerateScheduleExecutor  # noqa: E402

K_VALUES = (0, 1, 2, 3)
SCHEDULE_CAP_N = 500
TIME_BUDGET_S = 30.0


@dataclass(frozen=True, slots=True)
class KRow:
    """One `(scenario, k)` measurement."""

    k: int
    schedules_executed: int
    schedules_reduced_away: int
    pruned_by_reduction: int
    duplicate_children: int
    unique_signatures: int
    wall_time_s: float
    capped: bool
    budget_exceeded: bool
    finding_present: bool


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """One scenario's full k-sweep, plus the derived "smallest k found" verdict."""

    scenario_id: str
    defect_expected: bool
    rows: tuple[KRow, ...]
    smallest_k_found: int | None
    met: bool
    note: str


def _measure_one(executor: ScheduleExecutor, k: int) -> KRow:
    """Run `explore()` once at `k`; return its published row."""
    start = time.perf_counter()
    # `tests.integration.explore._harness.ScheduleExecutor` and
    # `agentdx.explore.generate.ScheduleExecutor` are structurally identical (both
    # `Callable[[DelaySchedule], tuple[Event, ...]]`/an equivalent Protocol) — the harness
    # module deliberately defines its own alias rather than importing the Protocol, per its
    # own comment ("no import-time dependency on explore/ beyond the DelaySchedule type").
    # mypy does not unify the two independently-named aliases automatically; this cast
    # documents that pre-existing, intentional cross-module structural contract, the same
    # pattern `cli/host.py` already uses at its own `runtime.cache`/`sdk.generic` boundary.
    result: ExplorationResult = explore(
        cast("_GenerateScheduleExecutor", executor),
        delay_bound_k=k,
        schedule_cap_n=SCHEDULE_CAP_N,
        time_budget_s=TIME_BUDGET_S,
    )
    wall_s = time.perf_counter() - start

    found_in_any = any(bool(detect_conflicts(schedule.events)) for schedule in result.results)
    return KRow(
        k=k,
        schedules_executed=len(result.results),
        schedules_reduced_away=result.pruned_by_reduction + result.duplicate_children,
        pruned_by_reduction=result.pruned_by_reduction,
        duplicate_children=result.duplicate_children,
        unique_signatures=result.unique_signatures,
        wall_time_s=round(wall_s, 6),
        capped=result.capped,
        budget_exceeded=result.budget_exceeded,
        finding_present=found_in_any,
    )


def _measure_scenario(
    scenario_id: str, executor: ScheduleExecutor, *, defect_expected: bool
) -> ScenarioResult:
    rows = tuple(_measure_one(executor, k) for k in K_VALUES)
    smallest_k_found: int | None = None
    for row in rows:
        if row.finding_present and smallest_k_found is None:
            smallest_k_found = row.k

    if defect_expected:
        met = smallest_k_found is not None
        note = (
            f"defect expected and found (smallest k={smallest_k_found})"
            if met
            else "defect expected but NOT FOUND at any swept k — this is a real failure"
        )
    else:
        met = smallest_k_found is None
        note = (
            "no defect expected, and none found at any swept k (correctly proving G2 — "
            "the declared reducer suppresses the finding at every k)"
            if met
            else f"no defect expected, but one fired at k={smallest_k_found} — a real failure"
        )

    return ScenarioResult(
        scenario_id=scenario_id,
        defect_expected=defect_expected,
        rows=rows,
        smallest_k_found=smallest_k_found,
        met=met,
        note=note,
    )


def main() -> int:
    """Sweep k in {0,1,2,3} for both synthetic scenarios; publish the coverage table."""
    scenarios = (
        _measure_scenario(
            "code_pipeline_shaped", make_code_pipeline_executor(), defect_expected=True
        ),
        _measure_scenario(
            "research_fanout_shaped", make_research_fanout_executor(), defect_expected=False
        ),
    )
    overall_met = all(s.met for s in scenarios)

    result = {
        "benchmark": "exploration-cost",
        "requirement": "PRD §34.4",
        "requirement_text": (
            "Method: for each fixture, run exploration at k in {0,1,2,3} and record: "
            "schedules executed, schedules reduced away, wall time, and the smallest k at "
            "which each seeded defect is first found. Published as: a table showing where "
            "each defect is found."
        ),
        "function_under_test": "agentdx.explore.generate.explore",
        "scope_note": (
            "Runs against tests.integration.explore._harness's two synthetic, "
            "Scheduler-driven scenarios (research_fanout-shaped, code_pipeline-shaped), "
            "not the three real fixture graphs directly — see this module's own docstring "
            "and D-55/D-88 (CONTEXT.md §9) for why. support_triage has no synthetic "
            "explore-shaped scenario built (P13 built exactly two), so it is absent here, "
            "disclosed rather than silently generalized."
        ),
        "k_values": list(K_VALUES),
        "schedule_cap_n": SCHEDULE_CAP_N,
        "time_budget_s": TIME_BUDGET_S,
        "scenarios": [asdict(s) for s in scenarios],
        "met": overall_met,
        "method": (
            "explore(executor, delay_bound_k=k, schedule_cap_n=500, time_budget_s=30.0) run "
            "once per (scenario, k) pair; agentdx.analysis.race.detect_conflicts run against "
            "every executed schedule's own event log to determine finding presence. Wall "
            "time measured with time.perf_counter() around each explore() call."
        ),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
    }

    out = REPO_ROOT / "bench" / "results" / "exploration-cost.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    sys.stdout.write("exploration-cost:\n")
    for scenario in scenarios:
        sys.stdout.write(f"  {scenario.scenario_id} ({scenario.note}):\n")
        for row in scenario.rows:
            sys.stdout.write(
                f"    k={row.k}: executed={row.schedules_executed} "
                f"reduced_away={row.schedules_reduced_away} "
                f"wall={row.wall_time_s:.4f}s finding_present={row.finding_present}\n"
            )
    sys.stdout.write(f"written to {out}\n")

    return 0 if overall_met else 2


if __name__ == "__main__":
    raise SystemExit(main())
