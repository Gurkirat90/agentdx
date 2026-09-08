#!/usr/bin/env python3
"""PRD §34.5: speedup accuracy — analytic fixtures, plus virtual/wall calibration.

**What is measured, stated precisely (Rule E1, AGENTS.md §6).** Two independent
validations, exactly as the PRD names them (there is no external ground truth for either):

1. **Analytic fixtures.** Four hand-authored event logs (`_ANALYTIC_FIXTURES` below), each
   with a `achieved_speedup`/`ideal_parallel_speedup` derivable on paper from the fixture's
   own declared span durations — no scheduler, no fixture graph, no LLM. `agentdx.analysis.
   baseline.compare` (the exact function `cli.commands.run`'s `--baseline`/`compare
   --baseline` reach for) is run against each, and its two speedup numbers are checked
   against the hand-derived value to within the PRD's own ±1% tolerance.
2. **Virtual-versus-wall calibration.** For each of the three real P05 fixtures, several
   fresh live runs (`bench.harness._real_fixture_run.run_fixture_fresh`, the real `Scheduler`/
   `CliRunHost` path — see that module and D-89 for why not `fixtures/_harness.py`) report
   both `run_end.payload.virtual_makespan_ms` and `.wall_makespan_ms`; the divergence
   `(wall - virtual) / virtual` is computed per run and its distribution (min/median/max)
   published. **No gate here** — the PRD states a gate only for the analytic-fixtures half
   ("assert the computed value matches to ±1%"); the calibration half is descriptive
   ("report the divergence distribution"), and this project's own fixtures make zero real
   LLM/network calls (`ResponsePool`, D-83), so a large *percentage* divergence on a
   sub-100ms *absolute* scale is an expected, honestly-disclosed property of these
   particular fixtures, not evidence of a defect — see the published JSON's own `note` field.

**Why hand-authored analytic fixtures, not `tests/analysis/test_baseline.py`'s own
`_fanout_log`/`_chain_log`.** Those are real, already-verified fixtures for that test file's
own purposes, but they are private (`_`-prefixed) module-level functions of a test module,
not shared infrastructure — `bench/` importing them would reach across a test file's own
internal/public boundary the way `race_accuracy_corpus.py`'s use of `tests.analysis.race.
_causal_log.CausalLog` (a module *designed* as reusable infrastructure, per its own
docstring) deliberately does not. This harness instead builds its own four fixtures directly
from `tests.analysis._events`' builders (the same "minimal, fully-explicit `Event` builder"
`test_timing_hand_computed.py`/`test_baseline.py` both already use), each with an in-file
critical-path derivation a reader can check on paper, and each verified against the real
`agentdx.analysis.timing.build_timing_dag`/`critical_path`/`parallelism_metrics` functions
before being trusted (not merely asserted) — see the derivation comment on each fixture.

Usage: `python3.12 bench/harness/speedup_accuracy.py`
Exit codes: 0 — every analytic fixture met ±1% on both speedup numbers · 2 — at least one did not.
"""

from __future__ import annotations

import json
import platform
import statistics
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from tests.analysis._events import (  # noqa: E402
    message_recv,
    message_send,
    run_end,
    run_start,
    span_end,
    span_start,
    tool_call,
)

from agentdx.analysis.baseline import (  # noqa: E402
    BaselineOutcome,
    BaselineRun,
    BaselineRunSpec,
    compare,
)
from agentdx.events.schema import Event  # noqa: E402
from bench.harness._real_fixture_run import REAL_FIXTURE_NAMES, run_fixture_fresh  # noqa: E402

TOLERANCE = 0.01  # PRD §34.5 item 1: "assert the computed value matches to ±1%"
CALIBRATION_SEEDS = (42, 43, 44, 45, 46)


class AnalyticFixture(NamedTuple):
    """One hand-derivable speedup fixture: its multi-agent log, baseline makespan, and answer."""

    fixture_id: str
    derivation: str
    multi_events: tuple[Event, ...]
    baseline_virtual_makespan_ms: int
    expected_ideal_parallel_speedup: float
    expected_achieved_speedup: float


def _sequential_fixture() -> AnalyticFixture:
    """One agent, two 50ms tool calls back to back — zero parallelism, zero overhead.

    critical_path = START->T1(50)->T2(50)->END = 100 (program_order gap 0). total_work =
    50+50 = 100. ideal_parallel_speedup = 100/100 = 1.0. Baseline set to the identical 100ms
    (a single agent doing the same two calls has nothing to parallelise either) so
    achieved_speedup = 100/100 = 1.0 too — the degenerate "no concurrency anywhere" case.
    """
    events = (
        run_start(seq=0, virtual_ts_ms=0),
        span_start(
            seq=1,
            virtual_ts_ms=0,
            vclock={"solo": 1},
            causal_parents=[0],
            agent_id="solo",
            span_id="T1",
            kind="tool_call",
            name="a",
        ),
        tool_call(
            seq=2,
            virtual_ts_ms=1,
            vclock={"solo": 2},
            causal_parents=[1],
            agent_id="solo",
            span_id="T1",
            tool="a",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=50,
        ),
        span_end(
            seq=3,
            virtual_ts_ms=50,
            vclock={"solo": 3},
            causal_parents=[2],
            agent_id="solo",
            span_id="T1",
            duration_virtual_ms=50,
        ),
        span_start(
            seq=4,
            virtual_ts_ms=50,
            vclock={"solo": 4},
            causal_parents=[3],
            agent_id="solo",
            span_id="T2",
            kind="tool_call",
            name="b",
        ),
        tool_call(
            seq=5,
            virtual_ts_ms=51,
            vclock={"solo": 5},
            causal_parents=[4],
            agent_id="solo",
            span_id="T2",
            tool="b",
            args_hash="blake2b:" + "b" * 64,
            duration_virtual_ms=50,
        ),
        span_end(
            seq=6,
            virtual_ts_ms=100,
            vclock={"solo": 6},
            causal_parents=[5],
            agent_id="solo",
            span_id="T2",
            duration_virtual_ms=50,
        ),
        run_end(
            seq=7,
            virtual_ts_ms=100,
            vclock={"solo": 6, "_run": 2},
            causal_parents=[6],
            virtual_makespan_ms=100,
            event_count=7,
        ),
    )
    return AnalyticFixture(
        fixture_id="sequential_no_parallelism",
        derivation=(
            "critical_path=START->T1(50)->T2(50)->END=100; total_work=50+50=100; "
            "ideal=100/100=1.0; baseline=100 -> achieved=100/100=1.0"
        ),
        multi_events=events,
        baseline_virtual_makespan_ms=100,
        expected_ideal_parallel_speedup=1.0,
        expected_achieved_speedup=1.0,
    )


def _two_way_parallel_fixture() -> AnalyticFixture:
    """Two agents, both 100ms tool calls, both starting at t=0 — perfect 2-way parallelism.

    critical_path = max(100, 100) = 100 (both branches complete by the makespan). total_work
    = 100+100 = 200. ideal_parallel_speedup = 200/100 = 2.0. Baseline (single agent doing
    both sequentially) = 200ms -> achieved_speedup = 200/100 = 2.0 — matches ideal exactly,
    the "zero coordination overhead" case.
    """
    events = (
        run_start(seq=0, virtual_ts_ms=0),
        span_start(
            seq=1,
            virtual_ts_ms=0,
            vclock={"a": 1},
            causal_parents=[0],
            agent_id="a",
            span_id="A1",
            kind="tool_call",
            name="x",
        ),
        tool_call(
            seq=2,
            virtual_ts_ms=1,
            vclock={"a": 2},
            causal_parents=[1],
            agent_id="a",
            span_id="A1",
            tool="x",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=100,
        ),
        span_end(
            seq=3,
            virtual_ts_ms=100,
            vclock={"a": 3},
            causal_parents=[2],
            agent_id="a",
            span_id="A1",
            duration_virtual_ms=100,
        ),
        span_start(
            seq=4,
            virtual_ts_ms=0,
            vclock={"b": 1},
            causal_parents=[0],
            agent_id="b",
            span_id="B1",
            kind="tool_call",
            name="y",
        ),
        tool_call(
            seq=5,
            virtual_ts_ms=1,
            vclock={"b": 2},
            causal_parents=[4],
            agent_id="b",
            span_id="B1",
            tool="y",
            args_hash="blake2b:" + "b" * 64,
            duration_virtual_ms=100,
        ),
        span_end(
            seq=6,
            virtual_ts_ms=100,
            vclock={"b": 3},
            causal_parents=[5],
            agent_id="b",
            span_id="B1",
            duration_virtual_ms=100,
        ),
        run_end(
            seq=7,
            virtual_ts_ms=100,
            vclock={"a": 3, "b": 3, "_run": 2},
            causal_parents=[3, 6],
            virtual_makespan_ms=100,
            event_count=7,
        ),
    )
    return AnalyticFixture(
        fixture_id="two_way_parallel",
        derivation=(
            "critical_path=max(100,100)=100; total_work=100+100=200; ideal=200/100=2.0; "
            "baseline=200 -> achieved=200/100=2.0"
        ),
        multi_events=events,
        baseline_virtual_makespan_ms=200,
        expected_ideal_parallel_speedup=2.0,
        expected_achieved_speedup=2.0,
    )


def _three_way_uneven_fixture() -> AnalyticFixture:
    """Three agents, 33/37/41ms tool calls, all starting at t=0 — uneven, non-round numbers.

    critical_path = max(33,37,41) = 41. total_work = 33+37+41 = 111.
    ideal_parallel_speedup = 111/41 = 2.707317073170732 (verified against the real
    `timing.build_timing_dag`/`critical_path`/`parallelism_metrics` before being trusted, not
    merely computed by hand — this fixture's arithmetic doesn't reduce to a round number,
    which is deliberate: it exercises the ±1% tolerance check on genuine floating-point
    division, not just integer ratios). Baseline (single agent, all three sequentially) =
    111ms -> achieved_speedup = 111/41 = 2.707317073170732, matching ideal exactly again
    (still a zero-overhead fan-out).
    """
    events = (
        run_start(seq=0, virtual_ts_ms=0),
        span_start(
            seq=1,
            virtual_ts_ms=0,
            vclock={"a": 1},
            causal_parents=[0],
            agent_id="a",
            span_id="A1",
            kind="tool_call",
            name="x",
        ),
        tool_call(
            seq=2,
            virtual_ts_ms=1,
            vclock={"a": 2},
            causal_parents=[1],
            agent_id="a",
            span_id="A1",
            tool="x",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=33,
        ),
        span_end(
            seq=3,
            virtual_ts_ms=33,
            vclock={"a": 3},
            causal_parents=[2],
            agent_id="a",
            span_id="A1",
            duration_virtual_ms=33,
        ),
        span_start(
            seq=4,
            virtual_ts_ms=0,
            vclock={"b": 1},
            causal_parents=[0],
            agent_id="b",
            span_id="B1",
            kind="tool_call",
            name="y",
        ),
        tool_call(
            seq=5,
            virtual_ts_ms=1,
            vclock={"b": 2},
            causal_parents=[4],
            agent_id="b",
            span_id="B1",
            tool="y",
            args_hash="blake2b:" + "b" * 64,
            duration_virtual_ms=37,
        ),
        span_end(
            seq=6,
            virtual_ts_ms=37,
            vclock={"b": 3},
            causal_parents=[5],
            agent_id="b",
            span_id="B1",
            duration_virtual_ms=37,
        ),
        span_start(
            seq=7,
            virtual_ts_ms=0,
            vclock={"c": 1},
            causal_parents=[0],
            agent_id="c",
            span_id="C1",
            kind="tool_call",
            name="z",
        ),
        tool_call(
            seq=8,
            virtual_ts_ms=1,
            vclock={"c": 2},
            causal_parents=[7],
            agent_id="c",
            span_id="C1",
            tool="z",
            args_hash="blake2b:" + "c" * 64,
            duration_virtual_ms=41,
        ),
        span_end(
            seq=9,
            virtual_ts_ms=41,
            vclock={"c": 3},
            causal_parents=[8],
            agent_id="c",
            span_id="C1",
            duration_virtual_ms=41,
        ),
        run_end(
            seq=10,
            virtual_ts_ms=41,
            vclock={"a": 3, "b": 3, "c": 3, "_run": 2},
            causal_parents=[3, 6, 9],
            virtual_makespan_ms=41,
            event_count=10,
        ),
    )
    ideal = 111 / 41
    return AnalyticFixture(
        fixture_id="three_way_uneven_parallel",
        derivation="critical_path=max(33,37,41)=41; total_work=33+37+41=111; ideal=111/41",
        multi_events=events,
        baseline_virtual_makespan_ms=111,
        expected_ideal_parallel_speedup=ideal,
        expected_achieved_speedup=ideal,
    )


def _chain_with_handoff_fixture() -> AnalyticFixture:
    """Two agents in a message chain with a real handoff gap — coordination overhead.

    alpha: 20ms tool call (0->20), sends a message. beta: receives 5ms later (25), runs a
    15ms tool call (25->40). critical_path = START->A1(20)->message(5)->B1(15)->END = 40.
    total_work = 20+15 = 35 (handoff time is not "work"). ideal_parallel_speedup = 35/40 =
    0.875 — deliberately *below* 1.0 (two agents coordinated but gained nothing, PRD's
    COORDINATION_BOTTLENECK shape). Baseline fixed at 42ms (independent of total_work, on
    purpose) so achieved_speedup = 42/40 = 1.05 — deliberately *different* from
    ideal_parallel_speedup, exercising the two formulas independently rather than always
    producing the same number the way the first three fixtures do.
    """
    events = (
        run_start(seq=0, virtual_ts_ms=0),
        span_start(
            seq=1,
            virtual_ts_ms=0,
            vclock={"alpha": 1},
            causal_parents=[0],
            agent_id="alpha",
            span_id="A1",
            kind="tool_call",
            name="p1",
        ),
        tool_call(
            seq=2,
            virtual_ts_ms=1,
            vclock={"alpha": 2},
            causal_parents=[1],
            agent_id="alpha",
            span_id="A1",
            tool="p1",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=20,
        ),
        span_end(
            seq=3,
            virtual_ts_ms=20,
            vclock={"alpha": 3},
            causal_parents=[2],
            agent_id="alpha",
            span_id="A1",
            duration_virtual_ms=20,
        ),
        message_send(
            seq=4,
            virtual_ts_ms=20,
            vclock={"alpha": 4},
            causal_parents=[3],
            agent_id="alpha",
            span_id="A1",
            message_id="m1",
            to="beta",
        ),
        span_start(
            seq=5,
            virtual_ts_ms=25,
            vclock={"beta": 1},
            causal_parents=[4],
            agent_id="beta",
            span_id="B1",
            kind="tool_call",
            name="p2",
        ),
        message_recv(
            seq=6,
            virtual_ts_ms=25,
            vclock={"beta": 2},
            causal_parents=[4],
            agent_id="beta",
            span_id="B1",
            message_id="m1",
            from_="alpha",
            delivered_virtual_ts_ms=25,
        ),
        tool_call(
            seq=7,
            virtual_ts_ms=26,
            vclock={"beta": 3},
            causal_parents=[6],
            agent_id="beta",
            span_id="B1",
            tool="p2",
            args_hash="blake2b:" + "b" * 64,
            duration_virtual_ms=15,
        ),
        span_end(
            seq=8,
            virtual_ts_ms=40,
            vclock={"beta": 4},
            causal_parents=[7],
            agent_id="beta",
            span_id="B1",
            duration_virtual_ms=15,
        ),
        run_end(
            seq=9,
            virtual_ts_ms=40,
            vclock={"alpha": 4, "beta": 4, "_run": 2},
            causal_parents=[8],
            virtual_makespan_ms=40,
            event_count=9,
        ),
    )
    return AnalyticFixture(
        fixture_id="chain_with_handoff_gap",
        derivation=(
            "critical_path=START->A1(20)->message(5)->B1(15)->END=40; total_work=20+15=35; "
            "ideal=35/40=0.875; baseline fixed at 42 (independent) -> achieved=42/40=1.05"
        ),
        multi_events=events,
        baseline_virtual_makespan_ms=42,
        expected_ideal_parallel_speedup=0.875,
        expected_achieved_speedup=1.05,
    )


_ANALYTIC_FIXTURES: tuple[AnalyticFixture, ...] = (
    _sequential_fixture(),
    _two_way_parallel_fixture(),
    _three_way_uneven_fixture(),
    _chain_with_handoff_fixture(),
)


def _make_baseline_run(fixture: AnalyticFixture) -> BaselineRun:
    """Build a minimal, valid `BaselineRun` whose only load-bearing number is its makespan."""
    events = (
        run_start(seq=100, virtual_ts_ms=0),
        run_end(
            seq=101,
            virtual_ts_ms=fixture.baseline_virtual_makespan_ms,
            vclock={"_run": 2},
            causal_parents=[100],
            virtual_makespan_ms=fixture.baseline_virtual_makespan_ms,
            event_count=2,
        ),
    )
    return BaselineRun(
        events=events,
        baseline_of="r_test01",
        spec=BaselineRunSpec(
            task="t",
            tools=(),
            model="test-model",
            system_prompt="p",
            max_steps=4,
            seed=0,
            cache_mode="replay",
            calibration_id=None,
        ),
        outcome=BaselineOutcome.COMPLETED,
        cache_reuse_tool_rate=1.0,
        cache_reuse_llm_rate=1.0,
        cache_reuse_rate=1.0,
        evidence_seq=(100,),
    )


def _within_tolerance(measured: float, expected: float, tolerance: float) -> bool:
    if expected == 0.0:
        return measured == 0.0
    return abs(measured - expected) / abs(expected) <= tolerance


def _run_analytic_fixtures() -> tuple[list[dict[str, object]], bool]:
    rows: list[dict[str, object]] = []
    all_met = True
    for fixture in _ANALYTIC_FIXTURES:
        baseline = _make_baseline_run(fixture)
        comparison = compare(fixture.multi_events, baseline)
        ideal_met = _within_tolerance(
            comparison.ideal_parallel_speedup, fixture.expected_ideal_parallel_speedup, TOLERANCE
        )
        achieved_met = _within_tolerance(
            comparison.achieved_speedup, fixture.expected_achieved_speedup, TOLERANCE
        )
        met = ideal_met and achieved_met
        all_met = all_met and met
        rows.append(
            {
                "fixture_id": fixture.fixture_id,
                "derivation": fixture.derivation,
                "expected_ideal_parallel_speedup": fixture.expected_ideal_parallel_speedup,
                "measured_ideal_parallel_speedup": comparison.ideal_parallel_speedup,
                "ideal_parallel_speedup_met": ideal_met,
                "expected_achieved_speedup": fixture.expected_achieved_speedup,
                "measured_achieved_speedup": comparison.achieved_speedup,
                "achieved_speedup_met": achieved_met,
                "met": met,
            }
        )
    return rows, all_met


def _run_calibration() -> list[dict[str, object]]:
    """Measure wall/virtual divergence per real fixture — honestly, including a null result.

    **Live finding, not assumed.** `runtime.scheduler.Scheduler._scheduler_loop` only calls
    `self._clock.advance_to(earliest)` — the *one* call site anywhere in `src/` that moves
    the virtual clock at all — inside the `not runnable` branch, i.e. only when the
    scheduler would otherwise have nothing to dispatch
    and a timer is pending. None of the three real fixtures ever registers a timer (no
    `agentdx.sleep()`, no injected fault latency, no calibration profile driving simulated
    tool/LLM duration into the scheduler's own clock — the still-open Q-43.2.3 gap
    `fixtures/_harness.py`'s own docstring already names) — every task is always immediately
    runnable, so the clock is *never* advanced. Verified directly this run, not inferred:
    every event in every real fixture run below carries `virtual_ts_ms == 0`, including
    `run_end.payload.virtual_makespan_ms`. A divergence formula computed against a virtual
    makespan of exactly 0 is undefined (0/0), and reporting it as `0.0` would misread as "wall
    and virtual matched perfectly" when the honest fact is "there is currently no virtual
    duration in these fixtures' real execution to compare against at all" — a materially
    different, and more consequential, statement. This function reports which one is true
    per sample rather than silently defaulting to the flattering-looking number.
    """
    rows: list[dict[str, object]] = []
    for fixture_name in REAL_FIXTURE_NAMES:
        divergences: list[float] = []
        samples: list[dict[str, object]] = []
        for seed in CALIBRATION_SEEDS:
            _run_id, events = run_fixture_fresh(fixture_name, seed=seed)
            run_end_event = next(e for e in reversed(events) if e.type.value == "run_end")
            virtual_ms = int(run_end_event.payload["virtual_makespan_ms"])  # type: ignore[arg-type]
            wall_ms = int(run_end_event.payload["wall_makespan_ms"])  # type: ignore[arg-type]
            measurable = virtual_ms > 0
            divergence = (wall_ms - virtual_ms) / virtual_ms if measurable else None
            if divergence is not None:
                divergences.append(divergence)
            samples.append(
                {
                    "seed": seed,
                    "virtual_makespan_ms": virtual_ms,
                    "wall_makespan_ms": wall_ms,
                    "measurable": measurable,
                    "divergence": round(divergence, 6) if divergence is not None else None,
                }
            )
        rows.append(
            {
                "fixture": fixture_name,
                "samples": samples,
                "measurable_sample_count": len(divergences),
                "divergence_median": round(statistics.median(divergences), 6)
                if divergences
                else None,
                "divergence_min": round(min(divergences), 6) if divergences else None,
                "divergence_max": round(max(divergences), 6) if divergences else None,
                "not_measurable_reason": (
                    None
                    if divergences
                    else (
                        "virtual_makespan_ms was 0 in every sample — this fixture's real "
                        "Scheduler-driven execution never advances the virtual clock (no "
                        "timer/sleep/injected-latency call site was ever hit), so "
                        "wall/virtual divergence is undefined (0/0), not zero"
                    )
                ),
            }
        )
    return rows


def main() -> int:
    """Run both §34.5 validations; gate on the analytic half only, publish, report."""
    analytic_rows, analytic_met = _run_analytic_fixtures()
    calibration_rows = _run_calibration()

    result = {
        "benchmark": "speedup-accuracy",
        "requirement": "PRD §34.5",
        "requirement_text": (
            "Method: (1) Analytic fixtures. Synthetic graphs with known durations where the "
            "correct speedup is derivable by hand; assert the computed value matches to "
            "±1%. (2) Virtual-versus-wall calibration. For a live-recorded run, compare "
            "virtual makespan to measured wall makespan; report the divergence "
            "distribution. Published as: 'computed speedup matches the analytic value "
            "within X% on N synthetic graphs; virtual/wall divergence median Y%.'"
        ),
        "function_under_test": "agentdx.analysis.baseline.compare(multi_events, baseline)",
        "tolerance": TOLERANCE,
        "analytic_fixtures": {
            "count": len(_ANALYTIC_FIXTURES),
            "rows": analytic_rows,
            "met": analytic_met,
        },
        "virtual_wall_calibration": {
            "note": (
                "No PRD gate on this half (descriptive only). Live finding, this run: all "
                "three real fixtures report virtual_makespan_ms == 0 for every sample — "
                "runtime.scheduler.Scheduler only advances its virtual clock inside its "
                "'nothing runnable, a timer is pending' branch (the sole "
                "self._clock.advance_to(...) call site), and none of these fixtures ever "
                "registers a timer (no agentdx.sleep(), no injected fault latency, no "
                "calibration profile — the still-open Q-43.2.3 gap). Divergence is "
                "therefore NOT MEASURABLE for any of the three today, not '0.0' — see each "
                "row's own not_measurable_reason. This is a genuine, newly-observed gap in "
                "these fixtures' realism, disclosed rather than papered over with a "
                "flattering-looking zero; not previously documented in CONTEXT.md."
            ),
            "seeds": list(CALIBRATION_SEEDS),
            "rows": calibration_rows,
        },
        "met": analytic_met,
        "method": (
            "Analytic half: four hand-authored event logs (tests.analysis._events "
            "builders), each with an in-file critical-path/total-work derivation verified "
            "against the real timing.build_timing_dag/critical_path/parallelism_metrics "
            "functions before being trusted; analysis.baseline.compare's two speedup "
            "numbers are checked against the hand-derived value to within ±1%. Calibration "
            "half: bench.harness._real_fixture_run.run_fixture_fresh (real Scheduler/"
            "CliRunHost) executes each real fixture 5 times (seeds 42-46); divergence = "
            "(wall_makespan_ms - virtual_makespan_ms) / virtual_makespan_ms per run."
        ),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
    }

    out = REPO_ROOT / "bench" / "results" / "speedup-accuracy.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    sys.stdout.write(f"speedup-accuracy: {len(_ANALYTIC_FIXTURES)} analytic fixtures\n")
    for row in analytic_rows:
        status = "MET" if row["met"] else "NOT MET"
        sys.stdout.write(
            f"  {row['fixture_id']}: ideal {row['measured_ideal_parallel_speedup']:.6f} "
            f"(expect {row['expected_ideal_parallel_speedup']:.6f}), "
            f"achieved {row['measured_achieved_speedup']:.6f} "
            f"(expect {row['expected_achieved_speedup']:.6f}) [{status}]\n"
        )
    sys.stdout.write("virtual/wall calibration (descriptive, no gate):\n")
    for row in calibration_rows:
        if row["measurable_sample_count"]:
            sys.stdout.write(
                f"  {row['fixture']}: divergence median={row['divergence_median']:.4f} "
                f"min={row['divergence_min']:.4f} max={row['divergence_max']:.4f} "
                f"({row['measurable_sample_count']}/{len(CALIBRATION_SEEDS)} measurable)\n"
            )
        else:
            sys.stdout.write(
                f"  {row['fixture']}: NOT MEASURABLE — {row['not_measurable_reason']}\n"
            )
    sys.stdout.write(f"written to {out}\n")

    return 0 if analytic_met else 2


if __name__ == "__main__":
    raise SystemExit(main())
