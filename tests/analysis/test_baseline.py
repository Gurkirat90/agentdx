"""PRD §17 — single-agent baseline generation, comparability grading and the gap attribution.

**Why `BaselineRun`/`BaselineComparison` are mostly constructed directly, not always produced
by `generate_baseline`/`compare`.** Both are plain frozen dataclasses (Design Constraint 1:
`comparability` travels with every speedup number *because it is a required field*, not because
of how the object was built), so a test of `assess_comparability`'s grading logic, or of
`compare`'s arithmetic, can hand a purpose-built `BaselineRun` straight to the function under
test instead of round-tripping through a full generation pipeline it isn't exercising. Only
`generate_baseline` itself is tested through the injected `_FakeExecutor` (there is no real
`BaselineExecutor` in this codebase yet — see `baseline.py`'s module docstring).

**The multi-agent fixture (`_fanout_log`).** A genuine fan-out, not a chain: `alpha` runs a
10ms `tool_call`, `beta` runs a 4ms `llm_call`, both starting at the run's `virtual_ts_ms=0`
with no message dependency between them, both finishing by `run_end.virtual_ts_ms=10`. This
gives real (>1x) parallelism to work with — `total_work_ms = 10 + 4 = 14`,
`critical_path_length_ms = 10` (both branches complete by the makespan), so
`ideal_parallel_speedup = 14 / 10 = 1.4`. This file re-derives the expected
`ideal_parallel_speedup` from `timing.parallelism_metrics()` directly and checks `compare()`'s
wiring against it, and independently re-sums `comparison.attribution` to confirm Design
Constraint 2's closure holds.

**This fixture's six-bucket split is entirely degenerate, and that is a load-bearing fact,
not incidental.** Every leaf span here is `productive_work` (excluded from `GAP_BUCKET_ORDER`)
with zero critical-path duration in any of the five named overhead buckets — so
`_attribute_gap` always lands in its `total_marginal == 0.0` branch, where every bucket's
`gap_contribution` is a hardcoded `0.0` (or, for `unattributed`, `-gap`) rather than a value
computed from the marginal-speedup formula PRD §17.3 actually specifies. An independent OP-2
audit (2026-08-18) found that every `compare()`-based test in this file used only this
fixture, so **the formula's one genuinely novel piece of arithmetic — the normalised marginal
split across real, nonzero bucket durations — had no test that could have caught a sign error
reintroduced in it.** `_chain_log`/`_chain_baseline` below exist specifically to close that
gap; keep both fixtures, since between them they now exercise both of `_attribute_gap`'s two
non-error branches on purpose, not by accident.

**The chain fixture (`_chain_log`/`_chain_baseline`).** A genuine two-hop chain with no
parallelism at all: `alpha` runs a 5ms `llm_call` (`virtual_ts_ms` 2→7, a 2ms run-boundary
lead-in), sends a message to `beta`, who starts a 6ms `tool_call` 3ms later (a real handoff
gap) and finishes at `virtual_ts_ms=16`, with a further 3ms run-boundary trail-off before
`run_end` at `virtual_ts_ms=19`. Hand-computed critical path (unambiguous — a single chain, no
ties): `START -> Orch1 -> Work -> END`, length `19ms == virtual_makespan_ms` (residual `0`).
Hand-computed decomposition (cross-checked against `overhead.decompose_critical_path` directly
in `test_attribute_gap_general_branch_matches_hand_derived_fractions` below, not merely
asserted): `handoff = 3ms`
(`Work.start(10) - Orch1.end(7)`, matching the raw `message_recv.vts - message_send.vts` gap
exactly, so no `blocking_wait` remainder from the edge split itself), `blocking_wait = 5ms`
(`2ms` lead-in `+ 3ms` trail-off, both `run_boundary` edges per **C-19**), `productive_work =
11ms` (`5 + 6`), all other buckets `0`. `total_work_ms = 11` (no parallel branches to add),
`critical_path_length_ms = 19`, so `ideal_parallel_speedup = 11 / 19` — deliberately **less
than 1** (this fixture has no parallelism to exploit at all, a legitimate PRD-relevant shape:
multiple agents ran and coordinated, but gained nothing from doing so, which is closer to what
`COORDINATION_BOTTLENECK` looks like than the fan-out fixture's healthy case is). The baseline
is fixed at `virtual_makespan_ms=38` specifically so `achieved_speedup = 38/19 = 2.0` exactly,
keeping the hand arithmetic in the two new tests below tractable.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import pytest

from agentdx.analysis.baseline import (
    GAP_BUCKET_ORDER,
    BaselineAnalysisError,
    BaselineComparison,
    BaselineExecutionResult,
    BaselineOutcome,
    BaselineRun,
    BaselineRunSpec,
    ComparabilityGrade,
    _attribute_gap,
    assess_comparability,
    compare,
    compose_baseline_prompt,
    format_scorecard,
    generate_baseline,
    heuristic_step_budget,
    multi_run_tools,
)
from agentdx.analysis.overhead import OverheadDecomposition, decompose_critical_path
from agentdx.analysis.timing import build_timing_dag, critical_path, parallelism_metrics
from agentdx.events.schema import Event
from tests.analysis._events import (
    llm_call,
    message_recv,
    message_send,
    run_end,
    run_start,
    span_end,
    span_start,
    tool_call,
)

#: `baseline.py`'s own private `_BUCKET_LABELS` — mirrored here (not imported) so this test
#: doesn't reach across the module's public/private boundary; kept in sync by inspection since
#: `GAP_BUCKET_ORDER` is small and stable.
_EXPECTED_SCORECARD_LABELS = (
    "retry recovery",
    "redundant tool calls",
    "orchestration",
    "handoff latency",
    "blocking wait",
    "unattributed",
)

# -------------------------------------------------------------------------------------------
# A small log for the "read the multi-agent run" helpers — no DAG needed.
# -------------------------------------------------------------------------------------------


def _prompt_composition_log() -> list[Event]:
    return [
        run_start(seq=0, virtual_ts_ms=0),
        span_start(
            seq=1,
            virtual_ts_ms=0,
            vclock={"planner": 1},
            causal_parents=[0],
            agent_id="planner",
            span_id="P",
            kind="agent_step",
            name="planner",
            attributes={"role": "planner"},
        ),
        tool_call(
            seq=2,
            virtual_ts_ms=1,
            vclock={"planner": 2},
            causal_parents=[1],
            agent_id="planner",
            span_id="P",
            tool="search",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=1,
        ),
        span_end(
            seq=3,
            virtual_ts_ms=2,
            vclock={"planner": 3},
            causal_parents=[2],
            agent_id="planner",
            span_id="P",
            duration_virtual_ms=2,
        ),
        # worker's agent_step starts after planner's, and carries no `role` attribute — this
        # exercises compose_baseline_prompt's "defaults to worker" branch.
        span_start(
            seq=4,
            virtual_ts_ms=2,
            vclock={"worker": 1},
            causal_parents=[0],
            agent_id="worker",
            span_id="W",
            kind="agent_step",
            name="worker",
        ),
        tool_call(
            seq=5,
            virtual_ts_ms=3,
            vclock={"worker": 2},
            causal_parents=[4],
            agent_id="worker",
            span_id="W",
            tool="write",
            args_hash="blake2b:" + "b" * 64,
            duration_virtual_ms=1,
        ),
        # A second call to a tool "planner" already used — multi_run_tools must dedupe it.
        tool_call(
            seq=6,
            virtual_ts_ms=4,
            vclock={"worker": 3},
            causal_parents=[5],
            agent_id="worker",
            span_id="W",
            tool="search",
            args_hash="blake2b:" + "c" * 64,
            duration_virtual_ms=1,
        ),
        span_end(
            seq=7,
            virtual_ts_ms=5,
            vclock={"worker": 4},
            causal_parents=[6],
            agent_id="worker",
            span_id="W",
            duration_virtual_ms=3,
        ),
        run_end(
            seq=8,
            virtual_ts_ms=5,
            vclock={"_run": 2},
            causal_parents=[7],
            virtual_makespan_ms=5,
            event_count=9,
        ),
    ]


def test_multi_run_tools_is_sorted_and_deduplicated() -> None:
    tools = multi_run_tools(_prompt_composition_log())
    assert tools == ("search", "write")


def test_compose_baseline_prompt_orders_agents_by_first_agent_step_seq() -> None:
    prompt = compose_baseline_prompt(_prompt_composition_log())
    lines = prompt.splitlines()
    planner_line = next(line for line in lines if line.startswith("- planner"))
    worker_line = next(line for line in lines if line.startswith("- worker"))
    assert lines.index(planner_line) < lines.index(worker_line)
    assert "role: planner" in planner_line
    assert "Tools available to this role: search" in planner_line
    # worker gets no `role` attribute in the fixture — defaults to "worker".
    assert "role: worker" in worker_line
    assert "Tools available to this role: search, write" in worker_line


def test_compose_baseline_prompt_is_deterministic() -> None:
    events = _prompt_composition_log()
    assert compose_baseline_prompt(events) == compose_baseline_prompt(list(reversed(events)))


def test_heuristic_step_budget_uses_the_floor_when_the_multiplier_product_is_below_it() -> None:
    events = _prompt_composition_log()  # zero llm_call events
    assert heuristic_step_budget(events, multiplier=2.0, floor=4) == 4


def test_heuristic_step_budget_scales_with_llm_call_count() -> None:
    events = [
        run_start(seq=0, virtual_ts_ms=0),
        *[
            llm_call(
                seq=i,
                virtual_ts_ms=i,
                vclock={"a": i},
                causal_parents=[i - 1],
                agent_id="a",
                span_id="S",
                prompt_tokens=1,
                completion_tokens=1,
            )
            for i in range(1, 6)
        ],
    ]
    # 5 llm_call events * multiplier 2.0 = 10, above the floor of 4.
    assert heuristic_step_budget(events, multiplier=2.0, floor=4) == 10


# -------------------------------------------------------------------------------------------
# generate_baseline — via a hand-authored BaselineExecutor test double
# -------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FakeExecutor:
    """A minimal `BaselineExecutor` double — no real executor exists yet (P17)."""

    result: BaselineExecutionResult

    def execute(self, spec: BaselineRunSpec) -> BaselineExecutionResult:
        return self.result


def _generate_baseline_multi_events() -> list[Event]:
    return [
        run_start(seq=0, virtual_ts_ms=0),
        tool_call(
            seq=1,
            virtual_ts_ms=1,
            vclock={"a": 1},
            causal_parents=[0],
            agent_id="a",
            span_id="S",
            tool="search",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=1,
        ),
        tool_call(
            seq=2,
            virtual_ts_ms=2,
            vclock={"a": 2},
            causal_parents=[1],
            agent_id="a",
            span_id="S",
            tool="write",
            args_hash="blake2b:" + "b" * 64,
            duration_virtual_ms=1,
        ),
        run_end(
            seq=3,
            virtual_ts_ms=2,
            vclock={"_run": 2},
            causal_parents=[2],
            virtual_makespan_ms=2,
            event_count=4,
        ),
    ]


def test_generate_baseline_derives_spec_from_run_start_and_computes_cache_reuse_rates() -> None:
    multi_events = _generate_baseline_multi_events()
    baseline_events: tuple[Event, ...] = (
        run_start(seq=100, virtual_ts_ms=0),
        # Reuses "search"/hash "a"*64 from the multi-agent run — a hit.
        tool_call(
            seq=101,
            virtual_ts_ms=1,
            vclock={"solo": 1},
            causal_parents=[100],
            agent_id="solo",
            span_id="S",
            tool="search",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=1,
        ),
        # A brand-new args_hash — a miss.
        tool_call(
            seq=102,
            virtual_ts_ms=2,
            vclock={"solo": 2},
            causal_parents=[101],
            agent_id="solo",
            span_id="S",
            tool="write",
            args_hash="blake2b:" + "f" * 64,
            duration_virtual_ms=1,
        ),
        llm_call(
            seq=103,
            virtual_ts_ms=3,
            vclock={"solo": 3},
            causal_parents=[102],
            agent_id="solo",
            span_id="S",
            prompt_tokens=10,
            completion_tokens=5,
        ),
        run_end(
            seq=104,
            virtual_ts_ms=4,
            vclock={"_run": 2},
            causal_parents=[103],
            virtual_makespan_ms=4,
            event_count=5,
        ),
    )
    executor = _FakeExecutor(
        result=BaselineExecutionResult(events=baseline_events, outcome=BaselineOutcome.COMPLETED)
    )

    run = generate_baseline(multi_events, task="do the thing", executor=executor)

    assert run.spec.task == "do the thing"
    assert run.spec.tools == ("search", "write")
    assert run.spec.model == "test-model"  # from run_start's default payload
    assert run.spec.seed == 0
    assert run.spec.cache_mode == "replay"
    assert run.spec.max_steps == 4  # zero llm_call in multi_events -> the configured floor
    assert run.outcome is BaselineOutcome.COMPLETED
    assert run.cache_reuse_tool_rate == pytest.approx(0.5)  # 1 of 2 tool_calls reused
    assert run.cache_reuse_llm_rate == 1.0  # llm_call() builder defaults cache_status="hit"
    # call-count-weighted: (0.5 * 2 tool_calls + 1.0 * 1 llm_call) / 3 total calls
    assert run.cache_reuse_rate == pytest.approx((0.5 * 2 + 1.0 * 1) / 3)
    assert run.evidence_seq == (0, 100)


def test_generate_baseline_raises_e_base_001_when_multi_events_has_no_run_start() -> None:
    executor = _FakeExecutor(
        result=BaselineExecutionResult(events=(), outcome=BaselineOutcome.COMPLETED)
    )
    with pytest.raises(BaselineAnalysisError) as excinfo:
        generate_baseline([], task="x", executor=executor)
    assert excinfo.value.code == "E-BASE-001"


# -------------------------------------------------------------------------------------------
# assess_comparability — PRD §17.5's grading, all three grades
# -------------------------------------------------------------------------------------------


def _comparability_multi_events(*, status: str = "complete") -> list[Event]:
    return [
        run_start(seq=0, virtual_ts_ms=0),
        tool_call(
            seq=1,
            virtual_ts_ms=1,
            vclock={"a": 1},
            causal_parents=[0],
            agent_id="a",
            span_id="S",
            tool="search",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=1,
        ),
        run_end(
            seq=2,
            virtual_ts_ms=1,
            vclock={"_run": 2},
            causal_parents=[1],
            virtual_makespan_ms=1,
            event_count=3,
            status=status,
        ),
    ]


def _baseline_run(
    *,
    tools: tuple[str, ...] = ("search",),
    model: str = "test-model",
    cache_reuse_rate: float,
    outcome: BaselineOutcome = BaselineOutcome.COMPLETED,
) -> BaselineRun:
    return BaselineRun(
        events=(),
        baseline_of="r_test01",
        spec=BaselineRunSpec(
            task="t",
            tools=tools,
            model=model,
            system_prompt="p",
            max_steps=4,
            seed=0,
            cache_mode="replay",
            calibration_id=None,
        ),
        outcome=outcome,
        cache_reuse_tool_rate=cache_reuse_rate,
        cache_reuse_llm_rate=cache_reuse_rate,
        cache_reuse_rate=cache_reuse_rate,
        evidence_seq=(0,),
    )


def test_assess_comparability_grade_a_high_reuse_matching_model_and_tools() -> None:
    multi_events = _comparability_multi_events()
    baseline = _baseline_run(cache_reuse_rate=0.9)
    result = assess_comparability(multi_events, baseline)
    assert result.grade is ComparabilityGrade.A
    assert result.model_match
    assert result.tools_match
    assert result.both_succeeded
    assert result.reason  # never empty (Design Constraint 1)


def test_assess_comparability_grade_b_mid_reuse() -> None:
    multi_events = _comparability_multi_events()
    baseline = _baseline_run(cache_reuse_rate=0.5)
    result = assess_comparability(multi_events, baseline)
    assert result.grade is ComparabilityGrade.B


def test_assess_comparability_grade_c_low_reuse() -> None:
    multi_events = _comparability_multi_events()
    baseline = _baseline_run(cache_reuse_rate=0.1)
    result = assess_comparability(multi_events, baseline)
    assert result.grade is ComparabilityGrade.C
    assert "cache reuse" in result.reason


def test_assess_comparability_grade_c_model_mismatch() -> None:
    multi_events = _comparability_multi_events()
    baseline = _baseline_run(cache_reuse_rate=0.9, model="a-different-model")
    result = assess_comparability(multi_events, baseline)
    assert result.grade is ComparabilityGrade.C
    assert not result.model_match
    assert "model mismatch" in result.reason


def test_assess_comparability_grade_c_tool_set_mismatch() -> None:
    multi_events = _comparability_multi_events()
    baseline = _baseline_run(cache_reuse_rate=0.9, tools=("a-different-tool",))
    result = assess_comparability(multi_events, baseline)
    assert result.grade is ComparabilityGrade.C
    assert not result.tools_match
    assert "tool set mismatch" in result.reason


def test_assess_comparability_grade_c_baseline_failed_never_hidden() -> None:
    """PRD §17.6 limitation 1: a failed baseline is graded C, never silently a speedup number."""
    multi_events = _comparability_multi_events()
    baseline = _baseline_run(cache_reuse_rate=0.9, outcome=BaselineOutcome.FAILED)
    result = assess_comparability(multi_events, baseline)
    assert result.grade is ComparabilityGrade.C
    assert not result.both_succeeded
    assert "baseline did not complete" in result.reason


def test_assess_comparability_grade_c_multi_run_did_not_complete() -> None:
    multi_events = _comparability_multi_events(status="failed")
    baseline = _baseline_run(cache_reuse_rate=0.9)
    result = assess_comparability(multi_events, baseline)
    assert result.grade is ComparabilityGrade.C
    assert "multi-agent run did not complete" in result.reason


# -------------------------------------------------------------------------------------------
# compare() and format_scorecard() — the week-6 demo milestone (gates G6/G7)
# -------------------------------------------------------------------------------------------


def _fanout_log() -> list[Event]:
    """A genuine two-branch fan-out — see the module docstring."""
    return [
        run_start(seq=0, virtual_ts_ms=0),
        span_start(
            seq=1,
            virtual_ts_ms=0,
            vclock={"alpha": 1},
            causal_parents=[0],
            agent_id="alpha",
            span_id="A1",
            kind="tool_call",
            name="search",
        ),
        tool_call(
            seq=2,
            virtual_ts_ms=1,
            vclock={"alpha": 2},
            causal_parents=[1],
            agent_id="alpha",
            span_id="A1",
            tool="search",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=10,
        ),
        span_end(
            seq=3,
            virtual_ts_ms=10,
            vclock={"alpha": 3},
            causal_parents=[2],
            agent_id="alpha",
            span_id="A1",
            duration_virtual_ms=10,
        ),
        span_start(
            seq=4,
            virtual_ts_ms=0,
            vclock={"beta": 1},
            causal_parents=[0],
            agent_id="beta",
            span_id="B1",
            kind="llm_call",
            name="respond",
        ),
        llm_call(
            seq=5,
            virtual_ts_ms=1,
            vclock={"beta": 2},
            causal_parents=[4],
            agent_id="beta",
            span_id="B1",
            prompt_tokens=100,
            completion_tokens=50,
        ),
        span_end(
            seq=6,
            virtual_ts_ms=4,
            vclock={"beta": 3},
            causal_parents=[5],
            agent_id="beta",
            span_id="B1",
            duration_virtual_ms=4,
        ),
        run_end(
            seq=7,
            virtual_ts_ms=10,
            vclock={"alpha": 3, "beta": 3, "_run": 2},
            causal_parents=[3, 6],
            virtual_makespan_ms=10,
            event_count=8,
            total_tool_calls=1,
        ),
    ]


def _fanout_baseline() -> BaselineRun:
    multi_events = _fanout_log()
    baseline_events: tuple[Event, ...] = (
        run_start(seq=100, virtual_ts_ms=0),
        llm_call(
            seq=101,
            virtual_ts_ms=1,
            vclock={"solo": 1},
            causal_parents=[100],
            agent_id="solo",
            span_id="S1",
            prompt_tokens=150,
            completion_tokens=70,
        ),
        run_end(
            seq=102,
            virtual_ts_ms=22,
            vclock={"_run": 2},
            causal_parents=[101],
            virtual_makespan_ms=22,
            event_count=3,
        ),
    )
    return BaselineRun(
        events=baseline_events,
        baseline_of="r_test01",
        spec=BaselineRunSpec(
            task="t",
            tools=multi_run_tools(multi_events),
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


def _chain_log() -> list[Event]:
    """A genuine two-hop chain, no parallelism — see the module docstring's derivation."""
    return [
        run_start(seq=0, virtual_ts_ms=0),
        span_start(
            seq=1,
            virtual_ts_ms=2,
            vclock={"alpha": 1},
            causal_parents=[0],
            agent_id="alpha",
            span_id="Orch1",
            kind="llm_call",
            name="plan",
        ),
        llm_call(
            seq=2,
            virtual_ts_ms=3,
            vclock={"alpha": 2},
            causal_parents=[1],
            agent_id="alpha",
            span_id="Orch1",
            prompt_tokens=20,
            completion_tokens=10,
        ),
        span_end(
            seq=3,
            virtual_ts_ms=7,
            vclock={"alpha": 3},
            causal_parents=[2],
            agent_id="alpha",
            span_id="Orch1",
            duration_virtual_ms=5,
        ),
        message_send(
            seq=4,
            virtual_ts_ms=7,
            vclock={"alpha": 4},
            causal_parents=[3],
            agent_id="alpha",
            span_id="Orch1",
            message_id="m1",
            to="beta",
        ),
        span_start(
            seq=5,
            virtual_ts_ms=10,
            vclock={"beta": 1},
            causal_parents=[0],
            agent_id="beta",
            span_id="Work",
            kind="tool_call",
            name="execute",
        ),
        message_recv(
            seq=6,
            virtual_ts_ms=10,
            vclock={"beta": 2},
            causal_parents=[4, 5],
            agent_id="beta",
            span_id="Work",
            message_id="m1",
            from_="alpha",
            delivered_virtual_ts_ms=10,
        ),
        tool_call(
            seq=7,
            virtual_ts_ms=11,
            vclock={"beta": 3},
            causal_parents=[6],
            agent_id="beta",
            span_id="Work",
            tool="execute",
            args_hash="blake2b:" + "a" * 64,
            duration_virtual_ms=6,
        ),
        span_end(
            seq=8,
            virtual_ts_ms=16,
            vclock={"beta": 4},
            causal_parents=[7],
            agent_id="beta",
            span_id="Work",
            duration_virtual_ms=6,
        ),
        run_end(
            seq=9,
            virtual_ts_ms=19,
            vclock={"alpha": 4, "beta": 4, "_run": 2},
            causal_parents=[3, 8],
            virtual_makespan_ms=19,
            event_count=10,
            total_tool_calls=1,
        ),
    ]


def _chain_baseline() -> BaselineRun:
    multi_events = _chain_log()
    baseline_events: tuple[Event, ...] = (
        run_start(seq=100, virtual_ts_ms=0),
        llm_call(
            seq=101,
            virtual_ts_ms=1,
            vclock={"solo": 1},
            causal_parents=[100],
            agent_id="solo",
            span_id="S1",
            prompt_tokens=100,
            completion_tokens=50,
        ),
        run_end(
            seq=102,
            virtual_ts_ms=38,
            vclock={"_run": 2},
            causal_parents=[101],
            virtual_makespan_ms=38,
            event_count=3,
        ),
    )
    return BaselineRun(
        events=baseline_events,
        baseline_of="r_test02",
        spec=BaselineRunSpec(
            task="t",
            tools=multi_run_tools(multi_events),
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


def test_compare_computes_speedup_against_the_independently_derived_ideal() -> None:
    multi_events = _fanout_log()
    dag = build_timing_dag(multi_events)
    cp = critical_path(dag)
    baseline = _fanout_baseline()

    comparison = compare(multi_events, baseline, multi_dag=dag, multi_cp=cp)

    assert comparison.virtual_makespan_multi_ms == 10
    assert comparison.virtual_makespan_baseline_ms == 22
    assert comparison.achieved_speedup == pytest.approx(22 / 10)

    parallelism = parallelism_metrics(dag, cp)
    expected_ideal = parallelism.total_work_ms / parallelism.critical_path_length_ms
    assert comparison.ideal_parallel_speedup == pytest.approx(expected_ideal)
    assert comparison.gap == pytest.approx(expected_ideal - comparison.achieved_speedup)

    assert comparison.tokens_multi == 150
    assert comparison.tokens_baseline == 220
    assert comparison.token_cost_multiplier == pytest.approx(150 / 220)
    expected_cost_efficiency = comparison.achieved_speedup / comparison.token_cost_multiplier
    assert comparison.cost_efficiency == pytest.approx(expected_cost_efficiency)

    assert comparison.comparability.grade is ComparabilityGrade.A


def test_compare_signed_six_bucket_attribution_sums_to_the_overhead_cost() -> None:
    """Design Constraint 2, demonstrated directly (also asserted internally by `compare`).

    PRD §17.3, verbatim: "per-bucket contributions summing to the overhead cost" —
    `overhead_cost = achieved_speedup - ideal_parallel_speedup`, the negation of `comparison.
    gap` (`= ideal_parallel_speedup - achieved_speedup`). The two are related by a sign, not
    equal — see `baseline._attribute_gap`'s docstring for the derivation.

    **This exercises `_attribute_gap`'s `total_marginal == 0.0` (degenerate) branch only** —
    `_fanout_log`'s critical path has zero duration in every named overhead bucket (see the
    module docstring). That branch's contributions are hardcoded independently of the general
    formula below, so this test cannot catch a regression in the formula itself; see
    `test_attribute_gap_general_branch_matches_hand_derived_fractions` and
    `test_compare_general_branch_attribution_matches_hand_derived_values` for that.
    """
    multi_events = _fanout_log()
    baseline = _fanout_baseline()

    comparison = compare(multi_events, baseline)

    assert [b.bucket for b in comparison.attribution] == list(GAP_BUCKET_ORDER)
    total = sum(b.gap_contribution for b in comparison.attribution)
    assert total == pytest.approx(comparison.overhead_cost, abs=1e-6)
    assert total == pytest.approx(-comparison.gap, abs=1e-6)
    # The degenerate branch's own documented shape: every named bucket is exactly 0, the whole
    # overhead cost lands on `unattributed` (**C-21**). Pinned here so a future change to this
    # branch is a visible decision, not silent drift.
    by_bucket = {b.bucket: b.gap_contribution for b in comparison.attribution}
    assert by_bucket["unattributed"] == pytest.approx(comparison.overhead_cost, abs=1e-6)
    for bucket in GAP_BUCKET_ORDER:
        if bucket != "unattributed":
            assert by_bucket[bucket] == 0.0


def test_attribute_gap_general_branch_matches_hand_derived_fractions() -> None:
    """The formula PRD §17.3 actually specifies, exercised for real (not the degenerate branch).

    Independently derived by hand (see the module docstring for the fixture), using
    `fractions.Fraction` so the expected values are exact rationals, not a second float
    computation that could share a bug with the code under test:

        achieved_speedup = 38 / 19 = 2
        ideal_parallel_speedup = total_work_ms / critical_path_length_ms = 11 / 19
        gap = 11/19 - 2 = -27/19;  overhead_cost = 27/19

        handoff (d=3):        t_without_b = 16;  speedup_wo_b = 38/16 = 19/8
                               marginal = 19/8 - 2 = 3/8
        blocking_wait (d=5):  t_without_b = 14;  speedup_wo_b = 38/14 = 19/7
                               marginal = 19/7 - 2 = 5/7
        total_marginal = 3/8 + 5/7 = 21/56 + 40/56 = 61/56

        attribution(handoff)       = (27/19) * (3/8) / (61/56) = 567/1159
        attribution(blocking_wait) = (27/19) * (5/7) / (61/56) = 1080/1159
        sum = 1647/1159 = 27/19 = overhead_cost  ✓ (closes exactly, not just approximately)

    This is the test an OP-2 audit (2026-08-18) found missing: every other `compare()`-based
    test in this file used `_fanout_log`, whose decomposition never leaves this function's
    `total_marginal == 0.0` branch — see that branch's own hardcoded contributions below this
    function in `baseline.py`. A sign error reintroduced in the `for bucket in
    GAP_BUCKET_ORDER: contribution = -gap * (...)` loop would not have been caught by any test
    before this one.
    """
    multi_events = _chain_log()
    dag = build_timing_dag(multi_events)
    cp = critical_path(dag)
    decomposition = decompose_critical_path(dag, cp, multi_events)

    # Cross-check the hand-derived decomposition before trusting it in the arithmetic below —
    # `overhead.py` is already independently tested; this just confirms this fixture produces
    # what the module docstring claims.
    assert dict(decomposition.bucket_ms) == {
        "retry_recovery": 0,
        "redundant_work": 0,
        "orchestration": 0,
        "productive_work": 11,
        "handoff": 3,
        "blocking_wait": 5,
    }
    assert decomposition.residual_ms == 0
    assert decomposition.virtual_makespan_ms == 19

    achieved_speedup = Fraction(38, 19)
    ideal_parallel_speedup = Fraction(11, 19)
    gap = ideal_parallel_speedup - achieved_speedup
    assert gap == Fraction(-27, 19)

    attribution = _attribute_gap(
        t_multi_ms=19,
        t_single_ms=38,
        achieved_speedup=float(achieved_speedup),
        gap=float(gap),
        decomposition=decomposition,
    )

    by_bucket = {a.bucket: a.gap_contribution for a in attribution}
    expected = {
        "retry_recovery": Fraction(0),
        "redundant_work": Fraction(0),
        "orchestration": Fraction(0),
        "handoff": Fraction(567, 1159),
        "blocking_wait": Fraction(1080, 1159),
        "unattributed": Fraction(0),
    }
    for bucket, expected_fraction in expected.items():
        assert by_bucket[bucket] == pytest.approx(float(expected_fraction), abs=1e-9), bucket

    total = sum(by_bucket.values())
    assert total == pytest.approx(float(-gap), abs=1e-9)
    assert total == pytest.approx(27 / 19, abs=1e-9)


def test_attribute_gap_infinite_marginal_branch_zeroes_every_other_bucket() -> None:
    """Pins today's documented behaviour of the `total_marginal == inf` branch.

    Provably unreachable through the real `compare()` pipeline (see `_attribute_gap`'s
    docstring), but the function's own signature does not itself prevent a hand-built
    `OverheadDecomposition` from reaching it — an OP-2 audit (2026-08-18) found this branch had
    no test at all. `blocking_wait` here consumes the *entire* multi-run makespan (`d_b ==
    t_multi_ms`), so `t_without_b <= 0` and its marginal is treated as infinite; `handoff`'s own
    real, finite 3ms duration is currently zeroed rather than credited, which this test exists
    to make a visible, deliberate fact rather than a silent implementation detail.
    """
    decomposition = OverheadDecomposition(
        bucket_ms={
            "retry_recovery": 0,
            "redundant_work": 0,
            "orchestration": 0,
            "productive_work": 0,
            "handoff": 3,
            "blocking_wait": 10,
        },
        bucket_evidence_seq={
            "retry_recovery": (),
            "redundant_work": (),
            "orchestration": (),
            "productive_work": (),
            "handoff": (1, 2),
            "blocking_wait": (3, 4),
        },
        residual_ms=0,
        residual_fraction=0.0,
        residual_flagged=False,
        residual_tolerance=0.02,
        virtual_makespan_ms=10,
        critical_path_length_ms=10,
    )

    attribution = _attribute_gap(
        t_multi_ms=10,
        t_single_ms=20,
        achieved_speedup=2.0,
        gap=-0.5,
        decomposition=decomposition,
    )
    by_bucket = {a.bucket: a.gap_contribution for a in attribution}

    assert by_bucket["blocking_wait"] == pytest.approx(0.5, abs=1e-9)
    assert by_bucket["handoff"] == 0.0  # real 3ms duration, currently credited nothing
    for bucket in ("retry_recovery", "redundant_work", "orchestration", "unattributed"):
        assert by_bucket[bucket] == 0.0
    total = sum(by_bucket.values())
    assert total == pytest.approx(0.5, abs=1e-9)  # still closes to overhead_cost


def test_compare_general_branch_attribution_matches_hand_derived_values() -> None:
    """The `compare()`-level counterpart of the unit test above.

    Proves the wiring, not just the arithmetic in isolation: that `compare()` correctly
    extracts `t_multi_ms`, `t_single_ms` and the decomposition and hands them to
    `_attribute_gap` unmodified.
    """
    multi_events = _chain_log()
    baseline = _chain_baseline()

    comparison = compare(multi_events, baseline)

    assert comparison.achieved_speedup == pytest.approx(2.0, abs=1e-9)
    assert comparison.ideal_parallel_speedup == pytest.approx(11 / 19, abs=1e-9)
    assert comparison.gap == pytest.approx(-27 / 19, abs=1e-9)
    assert comparison.overhead_cost == pytest.approx(27 / 19, abs=1e-9)

    by_bucket = {b.bucket: b.gap_contribution for b in comparison.attribution}
    assert by_bucket["handoff"] == pytest.approx(567 / 1159, abs=1e-9)
    assert by_bucket["blocking_wait"] == pytest.approx(1080 / 1159, abs=1e-9)
    for bucket in ("retry_recovery", "redundant_work", "orchestration", "unattributed"):
        assert by_bucket[bucket] == 0.0

    total = sum(by_bucket.values())
    assert total == pytest.approx(comparison.overhead_cost, abs=1e-6)


def test_compare_never_produces_a_speedup_without_its_comparability_grade() -> None:
    """Design Constraint 1, structurally: there is no code path that omits the grade."""
    comparison = compare(_fanout_log(), _fanout_baseline())
    assert isinstance(comparison, BaselineComparison)
    assert comparison.comparability is not None
    all_grades = (ComparabilityGrade.A, ComparabilityGrade.B, ComparabilityGrade.C)
    assert comparison.comparability.grade in all_grades


def test_compare_raises_e_base_001_when_multi_events_has_no_run_end() -> None:
    multi_events = _fanout_log()[:-1]  # drop run_end
    with pytest.raises(BaselineAnalysisError) as excinfo:
        compare(multi_events, _fanout_baseline())
    assert excinfo.value.code == "E-BASE-001"


def test_format_scorecard_prints_the_week_6_demo_milestone_block() -> None:
    """The week-6 demo milestone, gates G6/G7.

    `agentdx compare <run_id> --baseline` / `agentdx analyze <run_id> --scorecard`,
    demonstrated via the pytest harness precedent P06/P09 already set (no CLI exists yet —
    see `baseline.py`'s module docstring's SELF-AUDIT note).

    **Uses `_chain_log`/`_chain_baseline`, not `_fanout_log`.** An OP-2 audit (2026-08-18)
    found the original version of this test used the fan-out fixture, whose six-bucket
    attribution is entirely degenerate (see the module docstring) — so the printed block it
    produced showed `+0.00×   0ms` for every named bucket except `unattributed`, a visibly
    thinner demonstration than PRD §17.4's own worked example, and every assertion below only
    checked that a label string appeared *somewhere* in the text, never next to its correct
    value — a mislabelled bucket (e.g. swapping `handoff`/`blocking wait`) would not have been
    caught. This version asserts exact printed lines for the two real, nonzero buckets.
    """
    comparison = compare(_chain_log(), _chain_baseline())
    text = format_scorecard(comparison)
    lines = text.splitlines()

    assert "Coordination Efficiency:  2.00×   faster than single-agent" in lines[0]
    assert "Ideal parallel speedup      0.58×   (total work 11ms / critical path 19ms)" in lines
    assert "Achieved speedup            2.00×   (baseline 38ms / multi-agent 19ms)" in lines
    assert "Overhead cost               +1.42×" in lines
    assert any(
        line.startswith("  handoff latency") and "+0.49×" in line and "3ms" in line
        for line in lines
    )
    assert any(
        line.startswith("  blocking wait") and "+0.93×" in line and "5ms" in line for line in lines
    )
    for zero_bucket in ("retry recovery", "redundant tool calls", "orchestration"):
        assert any(
            line.startswith(f"  {zero_bucket}") and "+0.00×" in line and "0ms" in line
            for line in lines
        )
    assert "Comparability" in text
    assert "A" in lines[-2]  # grade line, printed beside the reuse breakdown
    for label in _EXPECTED_SCORECARD_LABELS:
        assert label in text

    print("\n" + text)  # noqa: T201 — the pasted block the mission brief asks for
