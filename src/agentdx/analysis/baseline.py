r"""Single-agent baseline generation and comparison (PRD §17) — the headline feature.

**I3, the one exception, executed precisely.** `analysis.*` may not import `runtime.*`/`sdk.*`
(no allowlist entry — `.importlinter`'s `analysis-is-pure` contract has none for this file
either). Baseline is the one analyser that must *execute* a run, so it does so behind
`BaselineExecutor`, a `Protocol` declared here and implemented by whatever module is allowed to
import the runtime (`cli`, per PRD §24.3 — not yet built, P17). This module never imports
`agentdx.runtime`, `agentdx.sdk`, `agentdx.scenario` or any model client; every one of
`generate_baseline`/`compare`'s parameters is either a plain value, an `Event` sequence, or the
injected `BaselineExecutor` — so the import graph literally cannot route around the Protocol.

**No `RunHost`/`BaselineExecutor` implementation exists in this codebase yet** (`sdk.generic.
RunHost` is still the open P06 gap CONTEXT.md's handoff brief names). `tests/analysis/
test_baseline.py` exercises `generate_baseline`/`compare` against a hand-authored fake executor
(`_FakeExecutor`, same shape as `fixtures/_harness.py`'s provisional stamping — ADR-012's
precedent), not a real one. `agentdx compare <run_id> --baseline` (gate G6) is therefore
unrunnable as a literal CLI invocation until P17 exists, the same precedent P06 (gate G3) and
P09 (gate G4) already set — see this module's closing SELF-AUDIT for the pytest command that
demonstrates the same behaviour today.

## Design Constraint 1 — comparability travels with the number, structurally

A speedup number without its comparability grade is, per the mission brief, structurally
impossible here: `BaselineComparison` (the only object this module returns a speedup inside of)
carries `comparability: ComparabilityAssessment` as a required field, not an optional one, and
every formatter (`format_scorecard`) prints the grade on the same line as every speedup figure
it labels, never separately. There is no code path that hands out `achieved_speedup` without
`comparability` sitting beside it in the same frozen object.

## Two PRD-silent points, ruled rather than guessed at (AGENTS.md §1)

**`compose_baseline_prompt`, operationalised.** PRD §17.2 says the function "concatenates the
multi-agent system prompts... in topological order... preserving the tool descriptions
verbatim." Neither a per-agent *system prompt* string nor a tool's *argument schema* is a field
this build's event schema ever persists (`docs/event-schema.md`): `llm_call.payload.prompt`
exists only under `capture_bodies=True` (I8's default-off privacy gate, PRD §9.5), and no event
type carries a tool's parameter schema at all — only `tool_call.args_hash`, a hash of the
already-canonicalised arguments (`redundancy.py`'s module docstring makes the identical point
about `args_hash` vs. raw `args`). Reading §17.2 literally would make baseline generation
silently degrade to an empty or missing prompt the moment a project runs with default privacy
settings — worse than the honest alternative. **This module instead composes the prompt
mechanically from what the log always carries**: each agent's id, its declared `role`
(`agent_step.attributes.role`, defaulting to `worker`), and the sorted set of tool names it
called (`tool_call.tool` — names only, never argument schemas, for the same reason). Agents are
ordered by the `seq` of their first `agent_step` `span_start` (a stable proxy for "topological
order" that needs no causality graph). This satisfies PRD §17.6 limitation 2 exactly ("the
baseline prompt is a mechanical composition, not an optimised single-agent prompt... `--baseline-
prompt <file>` lets a user supply their own") and is fully computable under I8's default
configuration. Surfaced here, in `docs/baseline-methodology.md`, and in the closing SELF-AUDIT.

**Step budget, ruled.** PRD §17.2 names `heuristic_step_budget(multi_run)` without a formula.
`heuristic_step_budget` here is `max(step_budget_floor, step_budget_multiplier *
total_llm_calls_in_multi_run)`, both values in `verdict_rules.toml`'s `[baseline]` table (never
inline — AGENTS.md §4). Rationale: a single agent replaying a multi-agent workflow needs at
least as many reasoning turns as the busiest specialised path took calls, plus headroom for the
coordination work it must now also do itself without a second agent's help; `total_llm_calls`
(not `average_parallelism`-scaled) is used because it is a hard floor on how much reasoning the
task took *somewhere* in the multi-agent run, and floors are the honest choice for a budget a
baseline should not silently starve against.

## Design Constraint 7 — sandbox, cache and offline inheritance (PRD §13.9)

This module cannot enforce "free and offline" itself — it never executes anything; it only
calls the injected `BaselineExecutor`. What it *can* and does do: `BaselineRunSpec.cache_mode`
is always read from the multi-agent run's own `run_start.payload.cache_mode` (never hardcoded to
`"record"` or any other value), so a caller that constructs its `BaselineExecutor` correctly
(honouring §13.9's sandbox/blast-radius inheritance and wiring the same cache instance) has
everything it needs from the spec to keep the baseline run free and offline too. The sandbox/
blast-radius inheritance itself is the concrete executor's responsibility, declared here as a
contract on the Protocol rather than enforced in code, because no `Sandbox` type or `RunHost`
exists anywhere in this codebase yet to inherit from — the same "declared capability, not yet
wired to a real call site" shape CONTEXT.md's D-37/D-47 already record for other P06-adjacent
seams.

PRD §17.1 (comparability requirements), §17.2 (generation algorithm), §17.3 (metrics, the six-
bucket gap attribution), §17.4 (the canonical scorecard), §17.5 (comparability grading), §17.6
(published limitations), §24.3 (the `BaselineExecutor` injection point), §13.9 (sandbox
inheritance).

**Determinism (NFR-14).** Every collection this module builds is either a `tuple` in a stable
sort order or a `dict` inserted in a fixed key sequence; no bare `set` iteration.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from agentdx.analysis.overhead import (
    OverheadDecomposition,
    decompose_critical_path,
)
from agentdx.analysis.redundancy import detect_redundancy
from agentdx.analysis.timing import (
    CriticalPathResult,
    TimingDAG,
    build_timing_dag,
    critical_path,
    parallelism_metrics,
)
from agentdx.events.schema import Event, EventType

_DOCS: Final = "docs/baseline-methodology.md"
_RULES_FILENAME: Final = "verdict_rules.toml"

#: The six overhead-attribution buckets the speedup gap is split across (PRD §17.3), in fixed
#: display/insertion order. Five are `overhead.py` critical-path buckets minus `productive_work`
#: (not overhead, per PRD §16.2.2); the sixth, `unattributed`, is the critical-path residual —
#: exactly the six lines PRD §17.4's scorecard shows (`retry_recovery` reads `0.00x` and is
#: still present, matching `overhead.format_decomposition_table`'s own "print every bucket, even
#: at zero" convention).
GAP_BUCKET_ORDER: Final = (
    "retry_recovery",
    "redundant_work",
    "orchestration",
    "handoff",
    "blocking_wait",
    "unattributed",
)


class ComparabilityGrade(StrEnum):
    """Baseline comparability grade (PRD §17.5).

    **Deliberately duplicated from `agentdx.scenario.schema.ComparabilityGrade`, not imported.**
    `analysis/` may import only `events`/`store` (CONTEXT.md §4's layer table; `.importlinter`'s
    `analysis-is-pure` contract has no allowlist entry for `scenario`, and adding one is exactly
    the kind of layer change AGENTS.md §2 requires an ADR for, not a P11 side effect). Kept
    identical by value (`"A"`/`"B"`/`"C"`) to the scenario-side definition on purpose, following
    `timing.py`'s already-established precedent for `_happens_before` (module docstring, "some
    small pieces of causal logic are deliberately duplicated across layers rather than
    imported").
    """

    A = "A"
    B = "B"
    C = "C"


class BaselineOutcome(StrEnum):
    """What happened when the injected `BaselineExecutor` ran the single-agent graph.

    PRD §17.6 limitation 1: a baseline failure is reported as `FAILED`/`CONTEXT_EXCEEDED`,
    **never** silently turned into a speedup number. `compare` refuses to compute
    `achieved_speedup` when either run's outcome is not `COMPLETED` (see `compare`'s docstring).
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CONTEXT_EXCEEDED = "context_exceeded"


class BaselineAnalysisError(RuntimeError):
    """A hard baseline-generation or comparison invariant failure — `E-BASE-0NN`.

    Distinct from a *low-comparability* or *failed-baseline* result, neither of which is an
    error (PRD §17.6: both are reported honestly). This class exists for the same reason
    `overhead.OverheadAnalysisError` does: a self-consistency assertion this module's own
    arithmetic must satisfy by construction (Design Constraint 2's "assert it"), never expected
    to fire on a real comparison.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a description of what went wrong."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


# ---------------------------------------------------------------------------------------------
# PRD §17.2 — the BaselineExecutor injection point (I3's one exception, mechanised)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BaselineRunSpec:
    """Everything a `BaselineExecutor` needs to build and run the single-agent graph (§17.2).

    Guarantees: `tools` is sorted and deduplicated; `system_prompt` is `compose_baseline_
    prompt`'s deterministic output (or a caller-supplied override, PRD §17.6 limitation 2's
    `--baseline-prompt <file>`); `cache_mode` is always copied verbatim from the multi-agent
    run's own `run_start.payload.cache_mode` (Design Constraint 7 — never hardcoded here).
    """

    task: str
    tools: tuple[str, ...]
    model: str
    system_prompt: str
    max_steps: int
    seed: int
    cache_mode: str
    calibration_id: str | None


@dataclass(frozen=True, slots=True)
class BaselineExecutionResult:
    """What a concrete `BaselineExecutor` hands back to `generate_baseline`.

    Guarantees: `events` is `seq`-ascending and, when `outcome is COMPLETED`, contains a
    `run_start`/`run_end` pair (the same precondition `timing.build_timing_dag` already
    enforces — `compare` relies on that, not on re-checking it here).
    """

    events: tuple[Event, ...]
    outcome: BaselineOutcome


@runtime_checkable
class BaselineExecutor(Protocol):
    """The injection seam that lets `analysis.baseline` execute a run without importing the runtime.

    I3's one, precisely-scoped exception — PRD §24.3, CONTEXT.md invariant I3. A concrete
    implementation is constructed by `cli` (not yet built, P17) from the same scenario/sandbox/
    cache configuration the multi-agent run itself used, per §13.9. This module declares the
    contract; it does not, and structurally cannot, satisfy it.
    """

    def execute(self, spec: BaselineRunSpec) -> BaselineExecutionResult:
        """Build and run the single-agent graph `spec` describes; return its log and outcome.

        Guarantees the implementation must uphold (documented here since this module cannot
        enforce them across an injected boundary): the run executes under the same sandbox,
        blast radius and destructive-tool stubbing as the parent multi-agent run (§13.9); it
        is free and offline (I7); a context-window failure is reported as `CONTEXT_EXCEEDED`,
        never folded into `FAILED` (§17.6 limitation 1's two are kept distinct so a caller can
        tell "the model refused" from "the task doesn't fit one context").
        """
        ...


# ---------------------------------------------------------------------------------------------
# Extracting what the baseline needs from the multi-agent run's own log
# ---------------------------------------------------------------------------------------------


def _str_payload(event: Event, key: str) -> str | None:
    value = event.payload.get(key)
    return value if isinstance(value, str) else None


def _int_payload(event: Event, key: str) -> int | None:
    value = event.payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _run_start(events: Sequence[Event]) -> Event:
    found = next((e for e in events if e.type is EventType.RUN_START), None)
    if found is None:
        raise BaselineAnalysisError("E-BASE-001", "the log has no run_start event")
    return found


def _run_end(events: Sequence[Event]) -> Event | None:
    return next((e for e in events if e.type is EventType.RUN_END), None)


def _attributes(event: Event) -> Mapping[str, object]:
    value = event.payload.get("attributes")
    return value if isinstance(value, Mapping) else {}


def multi_run_tools(events: Sequence[Event]) -> tuple[str, ...]:
    """Return the sorted, deduplicated set of tool names `events` called (PRD §17.2 step 1)."""
    names = {
        tool
        for e in events
        if e.type is EventType.TOOL_CALL
        for tool in (_str_payload(e, "tool"),)
        if tool is not None
    }
    return tuple(sorted(names))


def _agent_order(events: Sequence[Event]) -> tuple[str, ...]:
    """Return every `agent_id` with an `agent_step` span, ordered by that span's first `seq`."""
    first_seq: dict[str, int] = {}
    for event in events:
        if (
            event.type is EventType.SPAN_START
            and _str_payload(event, "kind") == "agent_step"
            and event.agent_id is not None
        ):
            first_seq.setdefault(event.agent_id, event.seq)
    return tuple(sorted(first_seq, key=lambda a: (first_seq[a], a)))


def _agent_roles(events: Sequence[Event]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for event in events:
        if (
            event.type is EventType.SPAN_START
            and _str_payload(event, "kind") == "agent_step"
            and event.agent_id is not None
        ):
            role = _str_payload_attr(event, "role")
            if role is not None:
                roles.setdefault(event.agent_id, role)
    return roles


def _str_payload_attr(event: Event, key: str) -> str | None:
    value = _attributes(event).get(key)
    return value if isinstance(value, str) else None


def _agent_tools(events: Sequence[Event]) -> dict[str, tuple[str, ...]]:
    by_agent: dict[str, list[str]] = {}
    for event in events:
        if event.type is EventType.TOOL_CALL and event.agent_id is not None:
            tool = _str_payload(event, "tool")
            if tool is not None:
                by_agent.setdefault(event.agent_id, []).append(tool)
    return {agent: tuple(sorted(set(tools))) for agent, tools in by_agent.items()}


def compose_baseline_prompt(events: Sequence[Event]) -> str:
    """Deterministically compose the single-agent system prompt (PRD §17.2, ruled).

    See the module docstring's "compose_baseline_prompt, operationalised" for the reading.

    Guarantees: byte-identical output for the same `events` (NFR-14); every agent named in
    `events` appears exactly once, in first-`seq`-of-`agent_step` order; every tool name any
    agent called appears under that agent's line, sorted.
    """
    roles = _agent_roles(events)
    tools_by_agent = _agent_tools(events)
    lines = [
        "You are a single agent performing the work of the following coordinated agents.",
        "Act in the order below only where a real dependency requires it; otherwise use your "
        "own judgement about sequencing.",
        "",
    ]
    for agent_id in _agent_order(events):
        role = roles.get(agent_id, "worker")
        tools = tools_by_agent.get(agent_id, ())
        tool_note = f" Tools available to this role: {', '.join(tools)}." if tools else ""
        lines.append(f"- {agent_id} (role: {role}).{tool_note}")
    return "\n".join(lines)


def _load_baseline_defaults() -> tuple[float, int]:
    """Return `(step_budget_multiplier, step_budget_floor)` from `verdict_rules.toml`."""
    for parent in Path(__file__).resolve().parents:
        toml_path = parent / _RULES_FILENAME
        if toml_path.is_file():
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            section = data.get("baseline", {})
            multiplier = section.get("step_budget_multiplier", 2.0)
            floor = section.get("step_budget_floor", 4)
            return float(multiplier), int(floor)
        candidate = parent.parent / "src" / "agentdx" / "analysis" / _RULES_FILENAME
        if candidate.is_file():
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            section = data.get("baseline", {})
            return float(section.get("step_budget_multiplier", 2.0)), int(
                section.get("step_budget_floor", 4)
            )
    return 2.0, 4


def heuristic_step_budget(
    events: Sequence[Event], *, multiplier: float | None = None, floor: int | None = None
) -> int:
    """Return PRD §17.2's `heuristic_step_budget(multi_run)` (ruled — see module docstring)."""
    default_multiplier, default_floor = _load_baseline_defaults()
    m = multiplier if multiplier is not None else default_multiplier
    f = floor if floor is not None else default_floor
    total_llm_calls = sum(1 for e in events if e.type is EventType.LLM_CALL)
    return max(f, round(m * total_llm_calls))


# ---------------------------------------------------------------------------------------------
# PRD §17.2 — generation
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BaselineRun:
    """The result of generating a baseline (PRD §17.2's `generate_baseline` return value).

    Guarantees: `cache_reuse_rate` is the call-count-weighted mean of `cache_reuse_tool_rate`/
    `cache_reuse_llm_rate` (`0.0` when there were no calls of either kind to reuse); `outcome`
    is never silently assumed `COMPLETED` — a caller must check it before trusting `events` to
    represent a finished run (§17.6 limitation 1).
    """

    events: tuple[Event, ...]
    baseline_of: str
    spec: BaselineRunSpec
    outcome: BaselineOutcome
    cache_reuse_tool_rate: float
    cache_reuse_llm_rate: float
    cache_reuse_rate: float
    evidence_seq: tuple[int, ...]


def _tool_reuse_rate(multi_events: Sequence[Event], baseline_events: Sequence[Event]) -> float:
    """Fraction of baseline `tool_call`s whose `(tool, args_hash)` also appears in `multi_events`.

    Operationalises PRD §17.2 step 4's "tool calls with identical (tool,args) hashes reuse
    recorded results" as a structural comparison between the two logs — computable without any
    signal from the executor, and exactly what "reuse" means for a tool call (the event schema
    has no tool-call cache_status field; `llm_call.cache_status` is llm-specific).
    """
    multi_keys = {
        (_str_payload(e, "tool"), _str_payload(e, "args_hash"))
        for e in multi_events
        if e.type is EventType.TOOL_CALL
    }
    baseline_calls = [e for e in baseline_events if e.type is EventType.TOOL_CALL]
    if not baseline_calls:
        return 0.0
    reused = sum(
        1
        for e in baseline_calls
        if (_str_payload(e, "tool"), _str_payload(e, "args_hash")) in multi_keys
    )
    return reused / len(baseline_calls)


def _llm_reuse_rate(baseline_events: Sequence[Event]) -> float:
    """Fraction of baseline `llm_call`s whose `cache_status` indicates a served-from-cache hit.

    `"hit"` and `"perturbed"` both mean the call was answered from the cache rather than a live
    provider call; `"miss_recorded"`/`"miss_error"` mean it was not (PRD §11's four cache-status
    values, `docs/event-schema.md`).
    """
    calls = [e for e in baseline_events if e.type is EventType.LLM_CALL]
    if not calls:
        return 0.0
    hits = sum(1 for e in calls if _str_payload(e, "cache_status") in ("hit", "perturbed"))
    return hits / len(calls)


def generate_baseline(
    multi_events: Sequence[Event],
    task: str,
    executor: BaselineExecutor,
    *,
    system_prompt: str | None = None,
    step_budget_multiplier: float | None = None,
    step_budget_floor: int | None = None,
) -> BaselineRun:
    """Generate a single-agent baseline for `multi_events` (PRD §17.2), via `executor`.

    Args:
        multi_events: The sealed multi-agent run's own event log. `task` is deliberately a
            separate parameter, not derived from `multi_events`: no event type in this build's
            schema carries a run's task text (`docs/event-schema.md`) — it lives on the
            scenario, which `analysis/` may not import (I3). The caller (eventually `cli`,
            which already has the `Scenario` in hand) supplies it directly.
        task: The identical task definition the multi-agent run executed (PRD §17.1's "same
            task" requirement — verbatim, not re-derived, so it is trivially satisfied).
        executor: The injected `BaselineExecutor`.
        system_prompt: Override for `compose_baseline_prompt`'s mechanical composition (PRD
            §17.6 limitation 2's `--baseline-prompt <file>`). `None` uses the mechanical one.
        step_budget_multiplier: Override for `verdict_rules.toml`'s `[baseline]
            .step_budget_multiplier` (mainly for tests).
        step_budget_floor: Override for `verdict_rules.toml`'s `[baseline].step_budget_floor`
            (mainly for tests).

    Returns:
        The `BaselineRun`. Deterministic given the same inputs and executor behaviour (NFR-14):
        this function performs no clock read, no randomness, and no unordered iteration.

    Raises:
        BaselineAnalysisError: `E-BASE-001` — `multi_events` has no `run_start` (nothing to
            derive a spec from).
    """
    run_start = _run_start(multi_events)
    prompt = system_prompt if system_prompt is not None else compose_baseline_prompt(multi_events)
    tools = multi_run_tools(multi_events)
    model = _str_payload(run_start, "model") or ""
    seed = _int_payload(run_start, "seed") or 0
    cache_mode = _str_payload(run_start, "cache_mode") or "replay"
    calibration_id = _str_payload(run_start, "calibration_id")
    budget = heuristic_step_budget(
        multi_events, multiplier=step_budget_multiplier, floor=step_budget_floor
    )

    spec = BaselineRunSpec(
        task=task,
        tools=tools,
        model=model,
        system_prompt=prompt,
        max_steps=budget,
        seed=seed,
        cache_mode=cache_mode,
        calibration_id=calibration_id,
    )
    result = executor.execute(spec)

    tool_rate = _tool_reuse_rate(multi_events, result.events)
    llm_rate = _llm_reuse_rate(result.events)
    tool_calls = sum(1 for e in result.events if e.type is EventType.TOOL_CALL)
    llm_calls = sum(1 for e in result.events if e.type is EventType.LLM_CALL)
    total_calls = tool_calls + llm_calls
    overall_rate = (
        (tool_rate * tool_calls + llm_rate * llm_calls) / total_calls if total_calls > 0 else 0.0
    )

    baseline_run_start = next((e for e in result.events if e.type is EventType.RUN_START), None)
    evidence_seqs = [run_start.seq]
    if baseline_run_start is not None:
        evidence_seqs.append(baseline_run_start.seq)
    evidence = tuple(sorted(set(evidence_seqs)))

    return BaselineRun(
        events=tuple(result.events),
        baseline_of=run_start.run_id,
        spec=spec,
        outcome=result.outcome,
        cache_reuse_tool_rate=tool_rate,
        cache_reuse_llm_rate=llm_rate,
        cache_reuse_rate=overall_rate,
        evidence_seq=evidence,
    )


# ---------------------------------------------------------------------------------------------
# PRD §17.5 — comparability grading
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ComparabilityAssessment:
    """PRD §17.5's grade, bundled with exactly what justifies it (Design Constraint 1).

    Guarantees: `reason` is always populated (never an empty string) and names the deciding
    condition, so a grade is never displayed without a human-readable justification next to it.
    """

    grade: ComparabilityGrade
    cache_reuse_rate: float
    cache_reuse_tool_rate: float
    cache_reuse_llm_rate: float
    model_match: bool
    tools_match: bool
    both_succeeded: bool
    reason: str


def _load_comparability_thresholds() -> tuple[float, float]:
    """Return `(grade_a_min_cache_reuse, grade_b_min_cache_reuse)` from `verdict_rules.toml`."""
    for parent in Path(__file__).resolve().parents:
        toml_path = parent / _RULES_FILENAME
        if toml_path.is_file():
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            section = data.get("comparability", {})
            return (
                float(section.get("grade_a_min_cache_reuse", 0.80)),
                float(section.get("grade_b_min_cache_reuse", 0.40)),
            )
        candidate = parent.parent / "src" / "agentdx" / "analysis" / _RULES_FILENAME
        if candidate.is_file():
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            section = data.get("comparability", {})
            return (
                float(section.get("grade_a_min_cache_reuse", 0.80)),
                float(section.get("grade_b_min_cache_reuse", 0.40)),
            )
    return 0.80, 0.40


def assess_comparability(
    multi_events: Sequence[Event],
    baseline: BaselineRun,
) -> ComparabilityAssessment:
    """Return PRD §17.5's comparability grade for `baseline` against `multi_events`.

    Task match is not assessed: `generate_baseline` always passes the caller's `task` through
    to `BaselineRunSpec` verbatim (PRD §17.1's "same task" requirement is satisfied by
    construction, not by comparison).
    """
    grade_a_min, grade_b_min = _load_comparability_thresholds()
    run_start = _run_start(multi_events)
    multi_model = _str_payload(run_start, "model") or ""
    model_match = multi_model == baseline.spec.model
    tools_match = sorted(multi_run_tools(multi_events)) == sorted(baseline.spec.tools)

    multi_end = _run_end(multi_events)
    multi_succeeded = multi_end is not None and _str_payload(multi_end, "status") == "complete"
    baseline_succeeded = baseline.outcome is BaselineOutcome.COMPLETED
    both_succeeded = multi_succeeded and baseline_succeeded

    reuse = baseline.cache_reuse_rate
    if not both_succeeded:
        grade = ComparabilityGrade.C
        reason = (
            "baseline did not complete the task"
            if not baseline_succeeded
            else "the multi-agent run did not complete the task"
        )
    elif not model_match:
        grade = ComparabilityGrade.C
        reason = f"model mismatch: multi-agent {multi_model!r} vs. baseline {baseline.spec.model!r}"
    elif not tools_match:
        grade = ComparabilityGrade.C
        reason = "tool set mismatch between the multi-agent run and the baseline"
    elif reuse < grade_b_min:
        grade = ComparabilityGrade.C
        reason = f"cache reuse {reuse:.0%} is below the {grade_b_min:.0%} floor for grade B"
    elif reuse < grade_a_min:
        grade = ComparabilityGrade.B
        reason = f"cache reuse {reuse:.0%} is below the {grade_a_min:.0%} floor for grade A"
    else:
        grade = ComparabilityGrade.A
        reason = f"cache reuse {reuse:.0%}, identical model/tools/task, both runs succeeded"

    return ComparabilityAssessment(
        grade=grade,
        cache_reuse_rate=reuse,
        cache_reuse_tool_rate=baseline.cache_reuse_tool_rate,
        cache_reuse_llm_rate=baseline.cache_reuse_llm_rate,
        model_match=model_match,
        tools_match=tools_match,
        both_succeeded=both_succeeded,
        reason=reason,
    )


# ---------------------------------------------------------------------------------------------
# PRD §17.3 — metrics and the signed six-bucket gap attribution
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BucketAttribution:
    """One line of PRD §17.3's normalised marginal attribution.

    Guarantees: `gap_contribution` is `<= 0` for every real overhead bucket with `d_b > 0`
    (removing overhead never makes the comparison worse); `evidence_seq` is the CP bucket's own
    evidence, sorted (I6).
    """

    bucket: str
    critical_path_ms: int
    gap_contribution: float
    evidence_seq: tuple[int, ...]


def _tokens(events: Sequence[Event]) -> int:
    total = 0
    for e in events:
        if e.type is EventType.LLM_CALL:
            prompt = _int_payload(e, "prompt_tokens") or 0
            completion = _int_payload(e, "completion_tokens") or 0
            total += prompt + completion
    return total


def _attribute_gap(
    *,
    t_multi_ms: int,
    t_single_ms: int,
    achieved_speedup: float,
    gap: float,
    decomposition: OverheadDecomposition,
) -> tuple[BucketAttribution, ...]:
    """Return the six `BucketAttribution`s of PRD §17.3's normalised marginal attribution.

    **Sums to `overhead_cost` (`= achieved_speedup - ideal_parallel_speedup = -gap`), not to
    `gap` itself** — PRD §17.3 says it plainly ("per-bucket contributions summing to the
    overhead cost") and the formula (`attribution_b = -gap * marginal_b / Σmarginal`) makes it
    algebraically forced: `Σ attribution_b = -gap * (Σmarginal / Σmarginal) = -gap ==
    overhead_cost` by construction, whenever `Σmarginal != 0`. Comparing the sum against `gap`
    itself (rather than `-gap`) would reject essentially every real comparison, since the two
    differ by a sign for any nonzero gap — a defect this module's own
    `test_baseline.py::test_compare_signed_six_bucket_attribution_sums_to_the_overhead_cost`
    caught before P17 ever wired a caller to it. **That original catch, however, only exercised
    the `total_marginal == 0.0` branch below (a fan-out fixture with no real bucket durations),
    which hardcodes its contributions independently of this function's general-branch formula
    — it could not have caught a *second* sign error reintroduced at the `for bucket in
    GAP_BUCKET_ORDER:` loop a few lines below. An independent OP-2 audit (2026-08-18) found
    this gap directly; `test_compare_signed_six_bucket_attribution_general_branch_matches_
    hand_derived_fractions` now exercises that loop with real, distinct, nonzero bucket
    durations and exact hand-derived (`fractions.Fraction`) expected values per bucket, closing
    it.**

    The `total_marginal == float("inf")` branch (a single bucket's own duration consumes the
    entire multi-run makespan) zeroes every other bucket's contribution, even one with a real,
    finite marginal contribution of its own — this is provably unreachable through the real
    `compare()` pipeline today, because `overhead.py`'s own decomposition invariant
    (`Σ(six buckets) + residual == virtual_makespan_ms`, asserted in `decompose_critical_path`)
    forces every other bucket to `0` duration whenever one bucket already consumes the whole
    makespan. It is not proven unreachable for a hand-built `OverheadDecomposition` passed
    directly to this function (its signature does not itself enforce that invariant) —
    `test_attribute_gap_infinite_marginal_branch_zeroes_every_other_bucket` pins today's
    documented behaviour so a future change here is a deliberate decision, not a silent drift.

    Raises:
        BaselineAnalysisError: `E-BASE-002` — the six attributions do not sum to
            `overhead_cost` within floating-point tolerance. By construction this is a
            code-correctness assertion, exactly `overhead.py`'s `E-OVHD-001` pattern (Design
            Constraint 2's "assert it, report honestly when it does not close") — never
            expected to fire on a real comparison.
    """
    d_b: dict[str, int] = {}
    evidence: dict[str, tuple[int, ...]] = {}
    for bucket in GAP_BUCKET_ORDER:
        if bucket == "unattributed":
            d_b[bucket] = decomposition.residual_ms
            evidence[bucket] = ()
        else:
            d_b[bucket] = decomposition.bucket_ms[bucket]
            evidence[bucket] = decomposition.bucket_evidence_seq[bucket]

    marginal: dict[str, float] = {}
    for bucket in GAP_BUCKET_ORDER:
        duration = d_b[bucket]
        if duration <= 0:
            marginal[bucket] = 0.0
            continue
        t_without_b = t_multi_ms - duration
        if t_without_b <= 0:
            # The bucket alone accounts for the entire makespan — an extreme, undefined-ratio
            # edge case no real run reaches (see module docstring's assertion note). Treated as
            # "all of the gap", the honest limit of the marginal formula as t_without_b -> 0+.
            marginal[bucket] = float("inf")
            continue
        speedup_wo_b = t_single_ms / t_without_b if t_without_b > 0 else float("inf")
        marginal[bucket] = max(0.0, speedup_wo_b - achieved_speedup)

    total_marginal = sum(v for v in marginal.values() if v != float("inf"))
    has_infinite = any(v == float("inf") for v in marginal.values())

    results: list[BucketAttribution] = []
    if has_infinite:
        infinite_buckets = [b for b in GAP_BUCKET_ORDER if marginal[b] == float("inf")]
        share = gap / len(infinite_buckets) if infinite_buckets else 0.0
        for bucket in GAP_BUCKET_ORDER:
            contribution = -share if bucket in infinite_buckets else 0.0
            results.append(
                BucketAttribution(
                    bucket=bucket,
                    critical_path_ms=d_b[bucket],
                    gap_contribution=contribution,
                    evidence_seq=evidence[bucket],
                )
            )
        return tuple(results)

    if total_marginal == 0.0:
        # None of the five tracked overhead categories (nor the residual) has any duration on
        # the critical path, yet `gap != 0` — `achieved_speedup` (measured against an
        # independently-generated baseline) and `ideal_parallel_speedup` (intrinsic to this
        # run's own decomposition) are simply two different numbers with no forced relationship
        # (a run can be genuinely overhead-free and still beat, or fall short of, its own
        # decomposition-derived "ideal" once compared against a real single-agent baseline).
        # There is nothing to blame among the five named buckets, so the whole gap goes to
        # `unattributed` — the bucket that exists exactly for "not explained by a tracked
        # category" — rather than raising: Design Constraint 2 says "report honestly when it
        # does not close," and reporting 100% unattributed *is* the honest report, not a crash.
        for bucket in GAP_BUCKET_ORDER:
            contribution = -gap if bucket == "unattributed" else 0.0
            results.append(
                BucketAttribution(
                    bucket=bucket,
                    critical_path_ms=d_b[bucket],
                    gap_contribution=contribution,
                    evidence_seq=evidence[bucket],
                )
            )
        return tuple(results)

    for bucket in GAP_BUCKET_ORDER:
        contribution = -gap * (marginal[bucket] / total_marginal)
        results.append(
            BucketAttribution(
                bucket=bucket,
                critical_path_ms=d_b[bucket],
                gap_contribution=contribution,
                evidence_seq=evidence[bucket],
            )
        )

    total = sum(r.gap_contribution for r in results)
    overhead_cost = -gap
    if abs(total - overhead_cost) > 1e-6:
        raise BaselineAnalysisError(
            "E-BASE-002",
            f"Σ(six-bucket attribution)={total!r} does not equal overhead_cost="
            f"{overhead_cost!r} within tolerance (PRD §17.3: 'per-bucket contributions "
            "summing to the overhead cost')",
        )
    return tuple(results)


@dataclass(frozen=True, slots=True)
class BaselineComparison:
    """PRD §17.3/§17.4's full comparison — the object every speedup number lives inside.

    Guarantees (Design Constraint 1): `comparability` is a required field, not `Optional` —
    there is no `BaselineComparison` without a grade attached. `attribution` sums to
    `overhead_cost` (`= -gap`, not `gap` itself — see `_attribute_gap`'s docstring for the
    derivation; asserted in `_attribute_gap`, not merely intended). All figures are **virtual**
    time (I11) — `virtual_makespan_multi_ms`/`virtual_makespan_baseline_ms` are the only
    durations here, never a wall-clock one.
    """

    multi_run_id: str
    baseline_of: str
    achieved_speedup: float
    ideal_parallel_speedup: float
    overhead_cost: float
    token_cost_multiplier: float
    cost_efficiency: float
    gap: float
    attribution: tuple[BucketAttribution, ...]
    comparability: ComparabilityAssessment
    virtual_makespan_multi_ms: int
    virtual_makespan_baseline_ms: int
    total_work_ms: int
    critical_path_ms: int
    tokens_multi: int
    tokens_baseline: int
    outcome_multi: str
    outcome_baseline: BaselineOutcome
    evidence_seq: tuple[int, ...]


def compare(
    multi_events: Sequence[Event],
    baseline: BaselineRun,
    *,
    multi_dag: TimingDAG | None = None,
    multi_cp: CriticalPathResult | None = None,
) -> BaselineComparison:
    """Compare `multi_events` against `baseline` (PRD §17.3/§17.4/§17.5), bundled with evidence.

    Args:
        multi_events: The sealed multi-agent run's event log.
        baseline: `generate_baseline`'s result.
        multi_dag: Pre-computed `timing.build_timing_dag(multi_events)`, or `None` to compute
            it here (lets a caller that already built one for `overhead`/`aggregates` avoid
            recomputing it).
        multi_cp: Pre-computed `timing.critical_path(multi_dag)`, or `None` to compute it here.

    Returns:
        The `BaselineComparison`. When either run did not complete (`outcome != "complete"`/
        `COMPLETED`), speedup and attribution are still computed arithmetically (never a
        `ZeroDivisionError` — `T_single`/`T_multi` are always the real virtual makespans of
        whatever each run did record) but `comparability.grade` is always `C` and
        `comparability.reason` names which side failed — PRD §17.6 limitation 1's "never as a
        speedup" is honoured by de-emphasis (grade C), not by omitting the number, matching
        §17.5's own presentation rule for grade C exactly.

    Raises:
        BaselineAnalysisError: `E-BASE-001` propagated from a missing `run_start`/`run_end` on
            either side; `E-BASE-002` if the six-bucket attribution does not close (Design
            Constraint 2).
    """
    multi_run_start = _run_start(multi_events)
    multi_end = _run_end(multi_events)
    if multi_end is None:
        raise BaselineAnalysisError("E-BASE-001", "the multi-agent log has no run_end event")

    dag = multi_dag if multi_dag is not None else build_timing_dag(multi_events)
    cp = multi_cp if multi_cp is not None else critical_path(dag)
    parallelism = parallelism_metrics(dag, cp)
    redundancy_groups = detect_redundancy(dag)
    decomposition = decompose_critical_path(
        dag, cp, multi_events, redundancy_groups=redundancy_groups
    )

    baseline_end = _run_end(baseline.events)
    t_single_ms = (
        _int_payload(baseline_end, "virtual_makespan_ms") or 0 if baseline_end is not None else 0
    )
    t_multi_ms = dag.virtual_makespan_ms

    achieved_speedup = t_single_ms / t_multi_ms if t_multi_ms > 0 else 0.0
    ideal_parallel_speedup = (
        parallelism.total_work_ms / parallelism.critical_path_length_ms
        if parallelism.critical_path_length_ms > 0
        else 0.0
    )
    overhead_cost = achieved_speedup - ideal_parallel_speedup
    gap = ideal_parallel_speedup - achieved_speedup

    tokens_multi = _tokens(multi_events)
    tokens_baseline = _tokens(baseline.events)
    token_cost_multiplier = tokens_multi / tokens_baseline if tokens_baseline > 0 else 0.0
    cost_efficiency = achieved_speedup / token_cost_multiplier if token_cost_multiplier > 0 else 0.0

    attribution = _attribute_gap(
        t_multi_ms=t_multi_ms,
        t_single_ms=t_single_ms,
        achieved_speedup=achieved_speedup,
        gap=gap,
        decomposition=decomposition,
    )

    comparability = assess_comparability(multi_events, baseline)

    evidence_seqs = [multi_run_start.seq, multi_end.seq]
    if baseline_end is not None:
        evidence_seqs.append(baseline_end.seq)
    evidence_seqs.extend(baseline.evidence_seq)
    evidence = tuple(sorted(set(evidence_seqs)))

    return BaselineComparison(
        multi_run_id=multi_run_start.run_id,
        baseline_of=baseline.baseline_of,
        achieved_speedup=achieved_speedup,
        ideal_parallel_speedup=ideal_parallel_speedup,
        overhead_cost=overhead_cost,
        token_cost_multiplier=token_cost_multiplier,
        cost_efficiency=cost_efficiency,
        gap=gap,
        attribution=attribution,
        comparability=comparability,
        virtual_makespan_multi_ms=t_multi_ms,
        virtual_makespan_baseline_ms=t_single_ms,
        total_work_ms=parallelism.total_work_ms,
        critical_path_ms=parallelism.critical_path_length_ms,
        tokens_multi=tokens_multi,
        tokens_baseline=tokens_baseline,
        outcome_multi=_str_payload(multi_end, "status") or "unknown",
        outcome_baseline=baseline.outcome,
        evidence_seq=evidence,
    )


# ---------------------------------------------------------------------------------------------
# PRD §17.4 — the canonical scorecard
# ---------------------------------------------------------------------------------------------

_BUCKET_LABELS: Final[Mapping[str, str]] = {
    "retry_recovery": "retry recovery",
    "redundant_work": "redundant tool calls",
    "orchestration": "orchestration",
    "handoff": "handoff latency",
    "blocking_wait": "blocking wait",
    "unattributed": "unattributed",
}


def format_scorecard(comparison: BaselineComparison) -> str:
    """Render `comparison` as PRD §17.4's canonical scorecard — the week-6 demo milestone.

    Deterministic: buckets print in `GAP_BUCKET_ORDER`, the comparability grade and its reuse
    breakdown always appear (Design Constraint 1), every numeric line's evidence seqs are
    printed beside it (I6).
    """
    achieved = comparison.achieved_speedup
    verdict_word = "slower than single-agent" if achieved < 1.0 else "faster than single-agent"
    flag = " ⚠" if achieved < 1.0 else ""
    lines = [
        f"Coordination Efficiency:  {achieved:.2f}×{flag}   {verdict_word}",
        "─" * 60,
        f"Ideal parallel speedup      {comparison.ideal_parallel_speedup:.2f}×   "
        f"(total work {comparison.total_work_ms}ms / "
        f"critical path {comparison.critical_path_ms}ms)",
        f"Achieved speedup            {achieved:.2f}×   "
        f"(baseline {comparison.virtual_makespan_baseline_ms}ms / multi-agent "
        f"{comparison.virtual_makespan_multi_ms}ms)",
        f"Overhead cost               {comparison.overhead_cost:+.2f}×",
    ]
    for item in comparison.attribution:
        label = _BUCKET_LABELS[item.bucket]
        seqs = item.evidence_seq
        seq_note = f"[seq {seqs[0]}→{seqs[-1]}]" if seqs else ""
        lines.append(
            f"  {label:<22} {item.gap_contribution:+.2f}×   {item.critical_path_ms}ms   {seq_note}"
        )
    lines.append("")
    lines.append(
        f"Token cost multiplier       {comparison.token_cost_multiplier:.1f}×    "
        f"vs single-agent ({comparison.tokens_multi} vs {comparison.tokens_baseline})"
    )
    lines.append(f"Cost efficiency             {comparison.cost_efficiency:.2f}")
    lines.append("")
    grade = comparison.comparability
    lines.append(
        f"Comparability               {grade.grade.value}       "
        f"cache reuse {grade.cache_reuse_rate:.0%} "
        f"(tools {grade.cache_reuse_tool_rate:.0%}, llm {grade.cache_reuse_llm_rate:.0%})"
    )
    lines.append(f"  {grade.reason}")
    return "\n".join(lines)


__all__ = [
    "GAP_BUCKET_ORDER",
    "BaselineAnalysisError",
    "BaselineComparison",
    "BaselineExecutionResult",
    "BaselineExecutor",
    "BaselineOutcome",
    "BaselineRun",
    "BaselineRunSpec",
    "BucketAttribution",
    "ComparabilityAssessment",
    "ComparabilityGrade",
    "assess_comparability",
    "compare",
    "compose_baseline_prompt",
    "format_scorecard",
    "generate_baseline",
    "heuristic_step_budget",
    "multi_run_tools",
]
