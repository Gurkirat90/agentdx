#!/usr/bin/env python3
"""PRD §34.6: resilience scoring — fault matrix vs documented expectation, cross-seed stability.

**What is measured, stated precisely (Rule E1, AGENTS.md §6).** `agentdx.analysis.resilience.
score` — the real PRD §19 aggregator, run through no proxy — is exercised against
`bench.harness._resilience_scenario`'s own `code_pipeline`-shaped, `Scheduler`-driven scenario
(built this cycle specifically because it is the one construction in this repo that both fires a
real `CrashInjector` fault *and* emits a genuine `success_check` `assertion_result` event —
`resilience.score` requires both and neither the real fixtures nor P09's own G4 test harness
supply them; see `_resilience_scenario.py`'s own module docstring for the full chain). For each
of 3 seeds, three variants are run (`crash_target` in `{None, 'reviewer', 'coder'}`), and
`resilience.score(baseline_events, fault_runs)` is called once per seed against the two crash
variants. Recorded per `(fault, seed)`: the resulting `DegradationClass` and per-fault `score`.
Two things are checked against the PRD's own §34.6 text:

1. **"assert the computed classification matches the documented expectation"** — the expected
   class for both `agent_crash(reviewer)` and `agent_crash(coder)` is `SILENT_FAILURE`, decided
   *before* looking at output (see "Documenting the expectation, before running anything" below),
   not fitted to whatever came out.
2. **"scores are stable across seeds within ±3 points where the outcome class is identical"** —
   checked both per-fault and for the aggregate `resilience_score`.

**Documenting the expectation, before running anything.** `_resilience_scenario.py`'s
`_build_registry` arms every crash as `recoverable=False`. `CrashInjector`'s own docstring
states a non-recoverable crash "leaves the agent's future tasks PENDING forever from this
injector's point of view" — it does not fail the *run* itself; only the crashed agent's own task
ends early. So `scheduler.run(_root())` returns normally regardless of which agent crashed
(live-verified: `n_events` differs by crash target, but `_run_async` always completes without
raising), meaning `run_end.status` is always `"complete"`. Meanwhile `_tester`'s `success_check`
fails whenever its crashed upstream never appended to `received`. Per
`classify_degradation`: `success_check_passed=False` and `run_end_status=="complete"` is the
*literal* PRD §19.5 definition of `SILENT_FAILURE` ("reported success while `success_check`
failed") — not a benchmark artifact, the expected shape of "an agent crashes silently and
nothing downstream notices," which is exactly the case this scenario models. `GRACEFUL` (no
crash) is baseline_events, `HARD_FAILURE`/`DEGRADED_FLAGGED` are not reachable from this
scenario's own construction (no code path sets `run_end.status` to anything but `"complete"`)
and so are not documented as expected outcomes for any cell in this matrix.

**Why one synthetic scenario, not "3 fixtures", and one fault type, not "4 fault types" — the
PRD §34.6 method line names both dimensions this benchmark narrows, stated once, here.**

- *Fault types.* `cli/host.py`'s own module docstring (verified, quoted in
  `_resilience_scenario.py`'s own docstring): only `runtime.faults.process.CrashInjector`
  (`agent_crash`) is ever registered as `Scheduler(fault_hook=...)` in this build —
  `TransportFaultInjector` (`latency`, `message_drop`) and `DependencyFaultInjector`
  (`tool_failure`) are pure decision logic with no live call site. Scoring them here would mean
  fabricating a result for a fault that cannot actually fire — the exact failure mode PRD §12.5
  itself warns against ("A chaos experiment whose fault silently did not apply produces a
  falsely reassuring result"). `agent_crash` is the only one of the four MVP fault types this
  cycle can genuinely score, and this file says so instead of quietly reporting on 1 of 4.
- *Fixtures.* Verified this cycle (P18.3, D-89) and reconfirmed while building this file: a real
  fixture run through `CliRunHost`/`_run_direct_target` never emits a `success_check`
  `assertion_result` event at all (`grep` across `cli/host.py`/`cli/commands/run.py` finds no
  `assertion_result`/`emit_assertion` call site) — `resilience.score` raises `E-RES-001` on any
  such log, by design (PRD §19.2 "has nothing to divide"). None of the three real fixtures can
  be scored by this module *at all* today, independent of which fault type is attempted. Rather
  than block this benchmark on that gap or fabricate a `success_check` for a fixture that never
  emits one, this file substitutes the one scenario in this repo that is genuinely wired for
  `resilience.score` end to end, and documents the substitution rather than silently reporting
  "3 fixtures" when it ran one.

Usage: `python3.12 bench/harness/resilience_scoring.py`
Exit codes: 0 if every documented classification matches and every stability check within
tolerance passes; 2 otherwise. PRD §34.6 states no numeric score gate (unlike §34.7's explicit
`Gate` line) — the "gate" here is entirely the two assertions the PRD's own method line names.
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from agentdx.analysis.resilience import (  # noqa: E402
    DegradationClass,
    FaultRunInput,
    score,
)
from bench.harness._resilience_scenario import run_scenario  # noqa: E402

SEEDS: tuple[int, ...] = (1, 2, 3)
CRASH_TARGETS: tuple[str, ...] = ("reviewer", "coder")
FAULT_ID = "f_00"
"""`FaultRegistry.from_resolved_scenario` assigns `fault_id`s as `f_{index:02d}` in declaration
order (`runtime/faults/registry.py`); `_resilience_scenario._build_registry` always declares
exactly one fault, so this is always `"f_00"` — verified live, not assumed."""

EXPECTED_DEGRADATION: dict[str, DegradationClass] = {
    "reviewer": DegradationClass.SILENT_FAILURE,
    "coder": DegradationClass.SILENT_FAILURE,
}
STABILITY_TOLERANCE = 3
"""PRD §34.6's own literal number: "stable across seeds within ±3 points"."""


@dataclass(frozen=True, slots=True)
class PerFaultSeedRow:
    """One `(fault, seed)` cell's score and classification."""

    seed: int
    degradation_class: str | None
    score: float | None
    status: str


@dataclass(frozen=True, slots=True)
class FaultCell:
    """One fault's full row across every seed, plus the two PRD §34.6 checks for it."""

    crash_target: str
    fault_label: str
    expected_degradation: str
    rows: tuple[PerFaultSeedRow, ...]
    classification_matches_expected: bool
    outcome_class_identical_across_seeds: bool
    score_spread: float | None
    stable_within_tolerance: bool
    met: bool
    note: str


@dataclass(frozen=True, slots=True)
class SeedAggregate:
    """One seed's aggregate `resilience_score`/`worst_fault_score`."""

    seed: int
    resilience_score: int | None
    worst_fault_score: int | None


def _run_seed(seed: int) -> tuple[SeedAggregate, dict[str, PerFaultSeedRow]]:
    """Run baseline + both crash variants at `seed`; return the aggregate and per-fault rows."""
    baseline = run_scenario(seed=seed, crash_target=None)
    fault_runs = [
        FaultRunInput(
            fault_id=FAULT_ID,
            fault_label=f"agent_crash({target})",
            events=run_scenario(seed=seed, crash_target=target).events,
        )
        for target in CRASH_TARGETS
    ]
    result = score(baseline.events, fault_runs)

    rows: dict[str, PerFaultSeedRow] = {}
    for target, fault_score in zip(CRASH_TARGETS, result.per_fault, strict=True):
        rows[target] = PerFaultSeedRow(
            seed=seed,
            degradation_class=(
                fault_score.degradation_class.value
                if fault_score.degradation_class is not None
                else None
            ),
            score=fault_score.score,
            status=fault_score.status.value,
        )
    aggregate = SeedAggregate(
        seed=seed,
        resilience_score=result.resilience_score,
        worst_fault_score=result.worst_fault_score,
    )
    return aggregate, rows


def _build_fault_cell(crash_target: str, rows: tuple[PerFaultSeedRow, ...]) -> FaultCell:
    expected = EXPECTED_DEGRADATION[crash_target]
    observed_classes = {r.degradation_class for r in rows}
    classification_matches_expected = all(r.degradation_class == expected.value for r in rows)
    outcome_class_identical = len(observed_classes) == 1

    scores = [r.score for r in rows if r.score is not None]
    score_spread = (max(scores) - min(scores)) if len(scores) == len(rows) and scores else None

    # PRD §34.6: stability is checked "where the outcome class is identical" — if seeds
    # disagree on classification, the ±3-point check does not apply (a classification change is
    # already the sharper, more serious failure, caught by `classification_matches_expected`).
    if not outcome_class_identical:
        stable_within_tolerance = True
    elif score_spread is None:
        stable_within_tolerance = False
    else:
        stable_within_tolerance = score_spread <= STABILITY_TOLERANCE

    met = classification_matches_expected and stable_within_tolerance
    if not classification_matches_expected:
        seen = sorted({r.degradation_class or "None" for r in rows})
        note = f"expected {expected.value!r} at every seed, observed {seen} — real failure"
    elif not stable_within_tolerance:
        note = (
            f"classification stable at {expected.value!r} but score spread "
            f"{score_spread} exceeds ±{STABILITY_TOLERANCE} — real failure"
        )
    else:
        note = (
            f"classification matches expected {expected.value!r} at every seed; "
            f"score spread {score_spread} within ±{STABILITY_TOLERANCE}"
        )

    return FaultCell(
        crash_target=crash_target,
        fault_label=f"agent_crash({crash_target})",
        expected_degradation=expected.value,
        rows=rows,
        classification_matches_expected=classification_matches_expected,
        outcome_class_identical_across_seeds=outcome_class_identical,
        score_spread=score_spread,
        stable_within_tolerance=stable_within_tolerance,
        met=met,
        note=note,
    )


def main() -> int:
    """Run the fault matrix across `SEEDS`; publish the classification/stability table."""
    seed_aggregates: list[SeedAggregate] = []
    rows_by_target: dict[str, list[PerFaultSeedRow]] = {t: [] for t in CRASH_TARGETS}

    for seed in SEEDS:
        aggregate, rows = _run_seed(seed)
        seed_aggregates.append(aggregate)
        for target in CRASH_TARGETS:
            rows_by_target[target].append(rows[target])

    fault_cells = tuple(
        _build_fault_cell(target, tuple(rows_by_target[target])) for target in CRASH_TARGETS
    )

    agg_scores = [a.resilience_score for a in seed_aggregates if a.resilience_score is not None]
    aggregate_score_spread = (
        (max(agg_scores) - min(agg_scores))
        if len(agg_scores) == len(seed_aggregates) and agg_scores
        else None
    )
    aggregate_stable = (
        aggregate_score_spread is not None and aggregate_score_spread <= STABILITY_TOLERANCE
    )

    overall_met = all(c.met for c in fault_cells) and aggregate_stable

    result_json = {
        "benchmark": "resilience-scoring",
        "requirement": "PRD §34.6",
        "requirement_text": (
            "Method: a fault matrix (4 fault types x 3 fixtures x 3 seeds) with expected "
            "qualitative outcomes documented per cell; assert the computed classification "
            "matches the documented expectation, and that scores are stable across seeds "
            "within +/-3 points where the outcome class is identical."
        ),
        "function_under_test": "agentdx.analysis.resilience.score",
        "scope_note": (
            "Narrowed on both PRD-named axes, disclosed rather than silently generalized. "
            "Fault types: only agent_crash (1 of 4 MVP types) is ever registered as a live "
            "Scheduler(fault_hook=...) in this build (cli/host.py's own docstring, quoted in "
            "_resilience_scenario.py) -- latency/message_drop/tool_failure are pure decision "
            "logic with no live call site and are not scored here, rather than fabricated. "
            "Fixtures: none of the three real fixtures can be scored by resilience.score at "
            "all today -- a real run through CliRunHost/_run_direct_target never emits a "
            "success_check assertion_result event (verified this cycle, D-89-adjacent finding), "
            "and score() raises E-RES-001 without one. This benchmark instead runs "
            "bench.harness._resilience_scenario's own code_pipeline-shaped synthetic scenario, "
            "the one construction in this repo genuinely wired for CrashInjector fault firing "
            "and success_check emission together -- one scenario, not three fixtures, disclosed "
            'as a scope boundary, not generalized to "the fixtures pass this check".'
        ),
        "expected_degradation_rationale": (
            "Every armed crash is recoverable=False (CrashInjector: a non-recoverable crash "
            "ends only the crashed agent's own task, never the run itself), so run_end.status "
            "is always 'complete' regardless of which agent crashes. _tester's success_check "
            "fails whenever its crashed upstream never delivered. classify_degradation's own "
            "rule (success_check_passed=False, run_end_status=='complete') is PRD Sec19.5's "
            "literal SILENT_FAILURE definition -- decided from the scenario's construction "
            "before any run, not fitted to the output."
        ),
        "seeds": list(SEEDS),
        "stability_tolerance_points": STABILITY_TOLERANCE,
        "seed_aggregates": [asdict(a) for a in seed_aggregates],
        "aggregate_score_spread": aggregate_score_spread,
        "aggregate_stable_within_tolerance": aggregate_stable,
        "fault_cells": [asdict(c) for c in fault_cells],
        "met": overall_met,
        "method": (
            "For each seed in {1,2,3}: run the baseline (crash_target=None) and both crash "
            "variants (reviewer, coder) via bench.harness._resilience_scenario.run_scenario; "
            "call agentdx.analysis.resilience.score(baseline.events, [FaultRunInput(...), "
            "FaultRunInput(...)]) once per seed. Per fault: check every seed's "
            "degradation_class equals the documented expectation, and (only when the observed "
            "class is identical across all seeds) that max(score) - min(score) <= 3. Also "
            "checked for the aggregate resilience_score across seeds."
        ),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
    }

    out = REPO_ROOT / "bench" / "results" / "resilience-scoring.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result_json, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    sys.stdout.write("resilience-scoring:\n")
    sys.stdout.write(
        "  scope: 1 fault type (agent_crash, of 4 MVP types) x 1 synthetic scenario "
        "(code_pipeline-shaped, not 3 real fixtures -- see this module's own docstring)\n\n"
    )
    for cell in fault_cells:
        sys.stdout.write(f"  {cell.fault_label} (expected {cell.expected_degradation}):\n")
        for row in cell.rows:
            sys.stdout.write(
                f"    seed={row.seed}: class={row.degradation_class} score={row.score} "
                f"status={row.status}\n"
            )
        sys.stdout.write(f"    -> {cell.note}\n\n")
    sys.stdout.write("  aggregate resilience_score per seed:\n")
    for a in seed_aggregates:
        sys.stdout.write(f"    seed={a.seed}: resilience_score={a.resilience_score}\n")
    sys.stdout.write(
        f"  aggregate spread={aggregate_score_spread} "
        f"stable={aggregate_stable} (tolerance +/-{STABILITY_TOLERANCE})\n"
    )
    sys.stdout.write(f"  overall met={overall_met}\n")
    sys.stdout.write(f"written to {out}\n")

    return 0 if overall_met else 2


if __name__ == "__main__":
    raise SystemExit(main())
