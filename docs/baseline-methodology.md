# The headline feature — baseline comparison, the verdict engine, and resilience scoring

> Implements PRD §17 (single-agent baseline generation, comparability grading, speedup
> formulas and gap attribution), §18 (the verdict engine), and §19 (resilience scoring).
> Companion to `docs/performance-analysis.md` (P10 — the timing DAG, critical path, overhead
> decomposition, and aggregates this module consumes as already-computed inputs) and
> `docs/event-schema.md` (the event contract every number here traces back to, I6). Every
> error code below is a stable part of the public contract.

---

## 1. What this is, and what it is not

`src/agentdx/analysis/{baseline,verdict,resilience}.py` (P11) are three modules, not one
pipeline glued together:

- **`baseline.py`** generates a single-agent baseline for a multi-agent run (via an injected
  executor — §2), grades its comparability (§3), and computes the speedup formulas and the
  signed six-bucket gap attribution (§4).
- **`verdict.py`** is a pure function of already-computed analysis outputs — `baseline.
  BaselineComparison`, `overhead.OverheadDecomposition`, `resilience.ResilienceResult`,
  `aggregates.EdgeAggregate`/`AgentAggregate`, `redundancy.RedundancyGroup` — into a verdict
  class, a 0–100 coordination score, a confidence level, and a set of evidence-backed findings
  and recommendations (§5).
- **`resilience.py`** scores chaos-fault runs against a no-fault control run of the same
  scenario (§6).

**Race detection (`analysis.race`, P12), the CLI, and the UI are all out of scope** — `verdict
()` accepts `state_conflict_findings` as an open seam for the first of those (§5.2); the other
two are P17+ work.

**A naming collision, resolved by never letting the two meanings touch.** `analysis.baseline`'s
"baseline" is PRD §17's single-agent replay of a multi-agent run. `analysis.resilience`'s
"baseline" (`score(baseline_events, fault_runs)`'s first parameter) is PRD §19.1's **no-fault
control run of the same scenario** — an unrelated concept that happens to share the English
word. The two modules do not import each other, `resilience.py`'s own module docstring calls
this out explicitly, and every internal name in `resilience.py` says `baseline_events`, never
bare `baseline`, to keep the two apart in code as well as in prose.

---

## 2. `BaselineExecutor` — I3's one, precisely-scoped exception (§24.3)

`analysis/` may import only `agentdx.events`/`agentdx.store` (CONTEXT.md §4's layer table);
`.importlinter`'s `analysis-is-pure` contract has no allowlist entry for `baseline.py` either.
But generating a baseline means *executing* a single-agent run, which means touching the
runtime — the one place PRD §24.3 grants an exception, precisely bounded: `BaselineExecutor`
is a `Protocol` declared in `analysis.baseline`, implemented by whatever module is allowed to
import the runtime (`cli`, per §24.3 — not yet built, P17). `generate_baseline`'s every
parameter is a plain value, an `Event` sequence, or the injected executor, so the import graph
cannot route around the Protocol even by accident.

**No `BaselineExecutor` implementation exists in this codebase yet.** `sdk.generic.RunHost` —
the thing a real implementation would wrap — is still the open P06 gap CONTEXT.md's handoff
brief names. `tests/analysis/test_baseline.py` exercises `generate_baseline`/`compare` against
a hand-authored `_FakeExecutor` test double, not a real one; `agentdx compare <run_id>
--baseline` (gate G6) is consequently unrunnable as a literal CLI invocation until P17 exists —
the same precedent P06 (gate G3) and P09 (gate G4) already set. See §9 for the pytest
demonstration that stands in for it today.

### 2.1 What `generate_baseline` derives from the multi-agent log, and what it needs as a parameter

| `BaselineRunSpec` field | Source |
|---|---|
| `task` | **A caller-supplied parameter, not derived.** No event type in this build's schema carries a run's task text (`docs/event-schema.md`, checked exhaustively) — it lives on the `Scenario`, which `analysis/` may not import (I3). |
| `tools` | `multi_run_tools(multi_events)` — the sorted, deduplicated set of every `tool_call.tool` name in the log |
| `model` | `run_start.payload.model`, verbatim |
| `system_prompt` | `compose_baseline_prompt(multi_events)` (§2.2), or a caller override (§8 limitation 2) |
| `max_steps` | `heuristic_step_budget(multi_events)` (§2.3) |
| `seed` | `run_start.payload.seed`, verbatim |
| `cache_mode` | `run_start.payload.cache_mode`, verbatim — **never hardcoded** (Design Constraint 7, §2.4) |
| `calibration_id` | `run_start.payload.calibration_id`, verbatim |

### 2.2 `compose_baseline_prompt` — operationalised, not guessed at

PRD §17.2 says the function "concatenates the multi-agent system prompts... in topological
order... preserving the tool descriptions verbatim." Neither a per-agent *system prompt*
string nor a tool's *argument schema* is a field this build's event schema ever persists:
`llm_call.payload.prompt` exists only under `capture_bodies=True` (I8's default-off privacy
gate), and no event type carries a tool's parameter schema at all — only `tool_call.args_hash`,
a hash of already-canonicalised arguments (`redundancy.py`'s module docstring makes the
identical point). Reading §17.2 literally would make baseline generation silently degrade to
an empty or missing prompt the moment a project runs with default privacy settings — worse
than an honest alternative.

**Ruling.** The prompt is composed mechanically from what the log always carries: each agent's
id, its declared `role` (`agent_step.attributes.role`, defaulting to `"worker"`), and the
sorted set of tool names it called (names only, never argument schemas). Agents are ordered by
the `seq` of their first `agent_step` `span_start` — a stable proxy for "topological order"
that needs no causality graph. This satisfies §17.6's own limitation 2 exactly ("the baseline
prompt is a mechanical composition, not an optimised single-agent prompt... `--baseline-prompt
<file>` lets a user supply their own") and is fully computable under I8's default
configuration.

### 2.3 `heuristic_step_budget` — a versioned formula where the PRD names none

PRD §17.2 names `heuristic_step_budget(multi_run)` without a formula. This build's formula,
versioned in `verdict_rules.toml`'s `[baseline]` table:

```
heuristic_step_budget = max(step_budget_floor, step_budget_multiplier * total_llm_calls_in_multi_run)
```

Rationale: a single agent replaying a multi-agent workflow needs at least as many reasoning
turns as the busiest specialised path took calls, plus headroom for the coordination work it
must now also do itself. `total_llm_calls` (not `average_parallelism`-scaled) is a hard floor
on how much reasoning the task took *somewhere* in the multi-agent run — floors are the honest
choice for a budget a baseline should not silently starve against.

### 2.4 Sandbox, cache and offline inheritance (Design Constraint 7, PRD §13.9)

`analysis.baseline` never executes anything itself — it only calls the injected executor — so
it cannot enforce "free and offline" directly. What it does do: `BaselineRunSpec.cache_mode` is
always read from the multi-agent run's own `run_start.payload.cache_mode`, never hardcoded, so
a caller that constructs its `BaselineExecutor` correctly (honouring §13.9's sandbox/blast-
radius inheritance and wiring the same cache instance) has everything it needs from the spec.
The sandbox/blast-radius inheritance itself is declared as a contract on the `BaselineExecutor.
execute` docstring rather than enforced in code, because no `Sandbox` type or `RunHost` exists
anywhere in this codebase yet to inherit from — the same "declared capability, not yet wired to
a real call site" shape CONTEXT.md's D-37/D-47 already record for other P06-adjacent seams.

---

## 3. Comparability grading (PRD §17.5) — Design Constraint 1, structurally

A speedup number without its comparability grade is, per the mission brief, structurally
impossible here: `BaselineComparison` — the only object this module returns a speedup inside
of — carries `comparability: ComparabilityAssessment` as a **required** field, and every
formatter (`format_scorecard`) prints the grade on the same block as every speedup figure it
labels. There is no code path that hands out `achieved_speedup` without `comparability` sitting
beside it in the same frozen object.

| Grade | Condition |
|---|---|
| **A** | `cache_reuse_rate >= grade_a_min_cache_reuse` (default `0.80`), model matches, tool set matches, both runs succeeded |
| **B** | `grade_b_min_cache_reuse <= cache_reuse_rate < grade_a_min_cache_reuse` (default `0.40`–`0.80`), same match/success conditions |
| **C** | Anything else — low reuse, a model or tool-set mismatch, or either run not completing. **A failed baseline is graded C, never silently turned into a speedup number** (§17.6 limitation 1) |

`cache_reuse_rate` is `generate_baseline`'s call-count-weighted mean of the baseline run's own
`cache_reuse_tool_rate` (fraction of baseline `tool_call`s whose `(tool, args_hash)` also
appears in the multi-agent log) and `cache_reuse_llm_rate` (fraction of baseline `llm_call`s
with `cache_status` in `{"hit", "perturbed"}`).

---

## 4. Metrics, formulas, and the signed six-bucket gap attribution (PRD §17.3/§17.4)

```
T_multi        = multi-agent run's virtual makespan
T_single       = baseline run's virtual makespan
achieved_speedup        = T_single / T_multi
ideal_parallel_speedup  = total_work_ms / critical_path_length_ms   (from timing/overhead, unmodified)
overhead_cost            = achieved_speedup - ideal_parallel_speedup
gap                      = ideal_parallel_speedup - achieved_speedup     (= -overhead_cost)
token_cost_multiplier    = tokens_multi / tokens_baseline
cost_efficiency          = achieved_speedup / token_cost_multiplier
```

### 4.1 A defect this module's own tests caught, and fixed, before any caller was wired to it

PRD §17.3, verbatim: "the source shows per-bucket contributions summing to **the overhead
cost**" — `overhead_cost = achieved_speedup - ideal_parallel_speedup`, the *negation* of `gap`
(`= ideal_parallel_speedup - achieved_speedup`). The normalised-marginal-attribution formula
that computes each bucket's `attribution_b = -gap * (marginal_b / Σmarginal)` sums, by
construction, to exactly `-gap == overhead_cost` whenever `Σmarginal != 0` — algebraically
forced, not an empirical property. `_attribute_gap`'s own internal self-check originally
compared the sum against `gap` directly instead of `-gap`; since the two differ by a sign for
any nonzero gap, that check raised `E-BASE-002` on essentially every real comparison, including
straightforward "beneficial" cases with zero retry/redundant/orchestration/handoff/blocking
overhead. `tests/analysis/test_baseline.py::
test_compare_signed_six_bucket_attribution_sums_to_the_overhead_cost` caught this before any
caller was wired to `compare()`; the fix corrects the self-check's target to `overhead_cost`
(the actual attribution numbers were already correct — only the assertion's sign was wrong).

**An independent OP-2 audit (2026-08-18) found that catch was narrower than it looked.** The
fixture that test used (`_fanout_log`) has zero duration in every named overhead bucket, so
`_attribute_gap` always lands in its degenerate `total_marginal == 0.0` branch (§4.2 below) —
whose contributions are hardcoded independently of the general marginal-attribution formula.
Every `compare()`-based test in the suite used only that fixture, so a *second* sign error
reintroduced in the general formula's own `for bucket in GAP_BUCKET_ORDER: contribution =
-gap * (...)` loop would not have been caught by anything. The repair added a second fixture
(`_chain_log`/`_chain_baseline`, a genuine two-hop chain with real, distinct handoff and
blocking-wait critical-path time) and three new tests —
`test_attribute_gap_general_branch_matches_hand_derived_fractions` (direct, hand-derived via
`fractions.Fraction`), `test_compare_general_branch_attribution_matches_hand_derived_values`
(the same numbers through the real `compare()` pipeline, proving the wiring), and
`test_attribute_gap_infinite_marginal_branch_zeroes_every_other_bucket` (pinning the third,
previously-untested branch, where one bucket's duration alone consumes the whole makespan) —
so all three of `_attribute_gap`'s branches now have a test that would fail if their arithmetic
were subtly wrong, not merely one that would fail if the function stopped returning at all.

### 4.2 The zero-overhead edge case, ruled rather than raised

A second, related case: a run can have **zero duration in all five tracked overhead buckets**
(no retry, no redundant work, no orchestration, no handoff, no blocking wait — and no residual
either) and still have `gap != 0`, simply because `achieved_speedup` is measured against an
independently-generated baseline while `ideal_parallel_speedup` is intrinsic to this run's own
decomposition; the two have no forced relationship. In this case every bucket's marginal
contribution is `0`, so the normalised split (`marginal_b / Σmarginal`) is undefined. **Ruling:**
the entire `overhead_cost` is assigned to the `unattributed` bucket — the bucket that exists
exactly for "not explained by a tracked category" — rather than raising `E-BASE-002`. Design
Constraint 2 says "report honestly when it does not close"; reporting the whole cost as
unattributed *is* the honest report for a genuinely overhead-free run, not a crash. Both cases
are covered by `tests/analysis/test_baseline.py`.

### 4.3 The six buckets

`GAP_BUCKET_ORDER = (retry_recovery, redundant_work, orchestration, handoff, blocking_wait,
unattributed)` — five of `overhead.py`'s own critical-path buckets (all but `productive_work`,
which is not overhead) plus `unattributed` (the critical-path residual). For each bucket `b`
with critical-path duration `d_b`: `T_without_b = T_multi - d_b`, `speedup_wo_b = T_single /
T_without_b`, `marginal_b = max(0, speedup_wo_b - achieved_speedup)`,
`attribution_b = -gap * (marginal_b / Σmarginal)`. `_attribute_gap` asserts
`Σattribution_b == overhead_cost` within floating-point tolerance and raises `E-BASE-002` if it
does not close — never expected to fire on a real comparison, per §4.1/§4.2's fixes.

### 4.4 `format_scorecard`

Renders `BaselineComparison` as PRD §17.4's canonical scorecard — the week-6 demo milestone.
Buckets print in `GAP_BUCKET_ORDER`; the comparability grade and its cache-reuse breakdown
always appear on the same block as every speedup figure (Design Constraint 1); every line's
evidence seqs print beside it (I6). See §9 for a real, pasted example.

---

## 5. The verdict engine (PRD §18)

> PRD §18, verbatim: "The verdict must never be a black-box LLM opinion. Prefer deterministic
> calculations wherever possible. All rules below are pure functions of analysis outputs."

`verdict()` takes **already-computed analysis outputs** — never raw events, and never
recomputes anything a sibling module already owns. PRD §24.3's module map lists
`agentdx.analysis.verdict`'s one dependency as "all analysers," and a pure function of their
outputs is exactly what that looks like in code. Because every input is a plain dataclass,
`tests/analysis/test_verdict.py` builds them directly rather than deriving them from event
logs — more direct, and independent of `timing`/`overhead`/`redundancy`/`aggregates`'
already-tested internals.

### 5.1 Evidence, enforced in the type (Design Constraint 3, I6)

`Evidence.__post_init__` raises `EmptyEvidenceError` (`E-VERD-001`) the instant an `Evidence`
is constructed with an empty `event_seqs` tuple. Every `VerdictFinding`/`Recommendation`/
`Verdict` embeds a required (non-`Optional`) `Evidence` field, so there is no code path — not a
missed `if`, not a review oversight — that can produce a finding, recommendation, or verdict
with no evidence. `tests/analysis/test_verdict.py::
test_an_empty_evidence_array_is_rejected_by_the_type_itself` demonstrates it directly; the
Definition of Done's schema test.

**A softer gap in the same guarantee, found by an independent OP-2 audit (2026-08-18) and only
partly closeable here.** `verdict()`'s `INSUFFICIENT_DATA`-with-no-comparison-and-no-findings
fallback previously hardcoded `event_seqs=(0,)` unconditionally — a *documented* placeholder
("a real run always has at least a run_start at seq 0 or 1"), not a fabricated-and-hidden one,
but an assumption rather than a checked fact: `verdict()` receives no raw event log in this
code path, so it has no way to confirm seq `0` actually exists in the run being analysed. The
repair adds an optional `run_start_seq: int | None` parameter — a caller that has the run's own
event log (P17's future CLI, chiefly) should pass the real `run_start.seq`, and gets genuine,
traceable I6 evidence; a caller that doesn't still gets the same placeholder, now labelled
`UNVERIFIED` in `Evidence.computation` rather than silently indistinguishable from a real seq.
This is a real improvement, not just a re-worded comment, but it does not fully close the gap:
no caller in this codebase supplies `run_start_seq` yet, because none exists (P17 isn't built).
`tests/analysis/test_verdict.py::test_insufficient_data_fallback_uses_a_real_run_start_seq_
when_the_caller_has_one` and its sibling `..._is_the_guaranteed_fallback_with_no_comparison_
at_all` cover both paths.

### 5.2 `state_conflict_findings` — an open seam for P12, not a gap

PRD §18.1's `STATE_CONFLICT_RISK` class and §18.2's `conflict_penalty` both need
`state_conflict` findings, which only `analysis.race` (P12, `NOT STARTED`) can produce.
`verdict()` accepts them as `state_conflict_findings: Sequence[StateConflictFinding] = ()` — a
`Protocol` matching `scenario.assertions.Finding`'s exact shape (`type`, `severity`,
`evidence_seq`), so the day P12 ships a real `Finding` type, no signature here needs to change.
Called with the default empty sequence (every caller until P12 exists), `STATE_CONFLICT_RISK`
structurally never fires — `tests/analysis/test_verdict.py::
test_state_conflict_risk_never_fires_without_findings` covers it.

### 5.3 Verdict classes and precedence (PRD §18.1)

Nine classes, in precedence order (highest first — the headline class is the first whose
trigger evaluates `True`; every other true trigger is retained in `secondary_classes`, never
lost): `UNRELIABLE_TOPOLOGY`, `STATE_CONFLICT_RISK`, `NEGATIVE_CAPABILITY`,
`NEGATIVE_SPEEDUP`, `COORDINATION_BOTTLENECK`, `BASELINE_FAILED`,
`BASELINE_CONTEXT_EXCEEDED`, `NEUTRAL`, `BENEFICIAL`, `INSUFFICIENT_DATA`.

**`INSUFFICIENT_DATA` is deliberately lowest-precedence, per the PRD's own literal ordering** —
a run with too few agents/spans but a strong `BENEFICIAL` signal still headlines `BENEFICIAL`,
with `INSUFFICIENT_DATA` demoted to a secondary class (the honest caveat, not the headline).
Its trigger includes `comparison is None`, so it is the guaranteed fallback when nothing else
is even computable — `tests/analysis/test_verdict.py::
test_insufficient_data_is_the_guaranteed_fallback_with_no_comparison_at_all` covers the
fallback case; the "demoted to secondary" cases are covered separately.

### 5.4 The coordination score (PRD §18.2)

```
speedup_component     = clamp(achieved_speedup / max(1, ideal_parallel_speedup), 0, 1) * speedup_weight        (40)
efficiency_component  = clamp(productive_work_ms / virtual_makespan_ms, 0, 1) * efficiency_weight              (25)
reliability_component = (resilience_score / 100) * reliability_weight, or reliability_weight if no chaos run   (25)
conflict_penalty      = min(conflict_penalty_max, conflict_penalty_per_finding * count(high/critical conflicts))
coordination_score    = round(speedup_component + efficiency_component + reliability_component - conflict_penalty)
```

Weights are the PRD's own literal values (`40 + 25 + 25`, minus up to `25`) — not rebalanced
to sum to `100`; tuning these against real fixture data is separate, open work (CONTEXT.md
Q-43.2.6). `coordination_score` is `None` only when no `BaselineComparison` was available —
never a fabricated `0`.

### 5.5 Confidence (PRD §18.5) — never quietly rounded up (Design Constraint 5)

`LOW` if `residual_fraction > medium_max_residual_fraction`, or comparability grade is `C`, or
there is no comparison at all. Else `MEDIUM` if `residual_fraction >
high_max_residual_fraction`, or grade is `B`, or there is at least one instrumentation gap.
Else `HIGH`. A low-confidence verdict is stated as `LOW`, never silently reported as `MEDIUM`
or `HIGH` because the headline number still looked fine.

### 5.6 Recommendations (PRD §18.6) — four of seven rules implemented

Implemented: merge-agents (a handoff edge's `cp_share` above threshold, with the destination
agent's own productive-work share below a ceiling), static-routing (orchestration overhead
above threshold), memoise-or-consolidate (any detected redundancy group), and single-agent-is-
cheaper (token cost multiplier high with achieved speedup below a ceiling). **Not implemented**:
the fake-fan-out rule (needs a `parallelism`-derived branch-count signal this build's
`ParallelismMetrics` does not carry per-branch) and the write-write-conflict rule (needs
`analysis.race`, §5.2's open seam). Both are declared gaps, not silent omissions.

---

## 6. Resilience scoring (PRD §19)

### 6.1 Every §19.1 input, checked against what the event schema actually carries

| Input | Source | Available? |
|---|---|---|
| `baseline_success`/`fault_success` | `assertion_result` (`kind == "success_check"`, `passed`) | **Yes** |
| `recovery_time_virtual_ms` | Operationalised via `Event.fault_id` taint (§6.2) | Yes, ruled |
| `retries_base`/`retries_fault` | `span_start.attributes.retry_of` count (`timing.py`'s own field) | **Yes** |
| `degradation_class` | PRD §19.5's four classes | **Partial — §6.3** |

### 6.2 Recovery time, operationalised

PRD §19.1 defines `recovery_time_virtual_ms` as "virtual ms from `fault_injected` to the first
successful completion of **the affected subgraph**." No event type names "the affected
subgraph" as a first-class concept, but `Event.fault_id` does: `runtime/faults/taint.py`'s
`FaultTaintTracker` already stamps every event downstream of a fault with that fault's id, and
it is a real, persisted field on every event in this build's schema — not something this
module has to recompute. **Ruling:** recovery time is the virtual-ms gap from the fault's own
`fault_injected` event to the first `span_end` carrying that fault's `fault_id` with
`payload.status == "ok"`. If no such event exists, recovery is `None` and scores `0` for that
component (PRD §19.3: "never recovering... scores 0").

### 6.3 Degradation classification — a genuine, partial STOP-CONDITION-3 hit

PRD §19.5 needs a *system-emitted signal* to place a run into `degraded_flagged` ("succeeded
with reduced quality **and** the system emitted a signal") and to distinguish `graceful`'s
"failed and reported the failure" from `hard_failure`'s "failed loudly with a clear error." **No
event type in this build's frozen schema carries such a signal** — `instrumentation_gap`
records a *capture* defect, not a task-quality one, and `nondeterminism_warning`'s closed enum
is about ambient non-determinism, not a fallback or partial-result marker (checked
exhaustively against `docs/event-schema.md`, not assumed). This is the shape STOP CONDITION 3
names — but it disqualifies only *part* of one classifier, not the deliverable: `success_ratio`,
`recovery_component`, `amplification_component`, and `silent_failure` detection (§19.5's own
definition, "reported success while `success_check` failed," needs only `run_end.status` plus
`assertion_result`, both real) are all fully available and implemented in full.

**Ruling, conservative and fully tested:**

- Passed (`success_check` true): always `GRACEFUL`.
- Not passed, `run_end.status == "complete"` (the system claimed success anyway): `SILENT_
  FAILURE` — the literal §19.5 definition, fully determined, no signal needed.
- Not passed, `run_end.status != "complete"` (the system itself reported non-success):
  `HARD_FAILURE` rather than the higher-scored `GRACEFUL`, because this build cannot confirm
  the "declared fallback, partial result marked partial" condition `GRACEFUL`'s failed branch
  requires — the lower score is the safer direction for a number a user acts on
  (`redundancy.py`'s "over-disqualify rather than under-report" precedent, applied here to
  degradation instead of redundancy).

`DEGRADED_FLAGGED` is declared on the `DegradationClass` enum (for a future PRD-amendment-
driven classifier) but is **structurally unreachable** from `classify_degradation` in this
build — `tests/analysis/test_resilience.py::
test_degraded_flagged_is_never_produced_by_this_build` sweeps every input combination and
asserts it, so the gap is a visible, tested property, not a silent one.

**Flagged for a PRD amendment**: either `degraded_flagged`/`graceful`-vs-`hard_failure` need a
concrete event-schema signal (a `fallback_taken`/`partial_result` marker), or §19.5 should say
explicitly that a build without one collapses to the conservative reading above.

### 6.4 Per-fault score and aggregation (PRD §19.6/§19.7)

```
per_fault_score = 100 * (0.50 * success_ratio + 0.20 * recovery_component
                          + 0.15 * amplification_component + 0.15 * degradation_weight)
resilience_score = weighted_mean(per_fault_score, weight = fault's own weight, or 1.0 if unset)
```

**§19.7's non-negotiable aggregation rules, encoded as hard rules with their own tests, not as
formulas that happen to land there:**

1. **A fault that never fired is excluded**, not scored `0` and not scored `100` — `fault_
   injected` is emitted only on a fault's first fire (`runtime/faults/safety.py`: "no fault
   fires, no `fault_injected` event is ever written"), so its absence for a given `fault_id` is
   a precise, direct signal, not an inference. `FaultRunStatus.NOT_FIRED`.
2. **A run aborted by the safety guard (`run_end.status == "aborted_guard"`) is excluded**, not
   scored. `FaultRunStatus.ABORTED`.
3. **Any `silent_failure` present hard-caps the aggregate at `silent_failure_cap`** (default
   `49`) — regardless of how high the weighted mean of the other components would otherwise
   land. `tests/analysis/test_resilience.py::
   test_a_single_silent_failure_caps_the_aggregate_at_49` is the Definition of Done's explicit
   test for this rule.
4. `per_fault` always has exactly one entry per input `FaultRunInput`, in input order — the
   aggregate never appears without its own breakdown, structurally.
5. `resilience_score`/`worst_fault_score` are `None`, never a fabricated `0`/`100`, when zero
   faults were scored (all excluded, or the input list was empty).

### 6.5 `format_resilience_table`

Renders PRD §19.8's table shape: one row per fault (or `[not_fired]`/`[aborted]` for excluded
ones), the `not fired`/`aborted` summary line (always printed, even when empty), and the cap
warning line when §6.4 rule 3 fired.

---

## 7. `verdict_rules.toml` — every threshold and weight, versioned and printable

All thresholds/weights the three modules compare a measurement against live in
`src/agentdx/analysis/verdict_rules.toml`, and only there — never inline (AGENTS.md §4,
CONTEXT.md §11 tripwire 5). `verdict.load_verdict_rules()`/`resilience.
load_resilience_rules()` parse it; `verdict.format_rules(rules)` returns the byte-identical
verbatim text, which is what a future `agentdx analyze --explain` (P17, not yet built) will
print. `tests/analysis/test_verdict_rules_toml.py` proves the file round-trips through both
loaders today, gate or no gate, and spot-checks the parsed values against the committed
numbers (catching a drift a byte-identity check on the raw text alone would not). `tests/
analysis/test_verdict.py::test_a_threshold_change_visibly_changes_the_verdict_class`
demonstrates the Definition of Done's explicit requirement: raising `beneficial_min_speedup`
turns a previously-`BENEFICIAL` comparison into `NEUTRAL`, with no other input changed.

---

## 8. Published limitations (PRD §17.6, stated plainly)

1. **A failed or context-exceeded baseline is reported as such — never folded into a speedup
   number.** `BaselineOutcome.FAILED`/`CONTEXT_EXCEEDED` are kept distinct (so a caller can
   tell "the model refused" from "the task doesn't fit one context"), and `assess_comparability`
   always grades a non-`COMPLETED` baseline `C`.
2. **The baseline prompt is a mechanical composition, not an optimised single-agent prompt**
   (§2.2). A caller can supply `system_prompt` directly to `generate_baseline` to override it
   (the `--baseline-prompt <file>` CLI flag PRD §17.6 names is P17, not yet built).
3. **LLM cache reuse between a multi-agent run and its single-agent baseline is necessarily
   partial**, because the prompts genuinely differ — tool-call reuse is typically high,
   LLM-call reuse typically low. This is exactly why the comparability grade exists, and why a
   first baseline generation typically needs one `--record` pass (P17 concern).
4. **The `degraded_flagged` degradation class is structurally unreachable in this build**
   (§6.3) — a real gap against a genuinely-missing event-schema signal, not a design choice,
   flagged for a PRD amendment.
5. **Two of PRD §18.6's seven recommendation rules are not implemented** (§5.6) — fake-fan-out
   and write-write-conflict, both blocked on inputs this build's other modules do not yet
   produce.
6. **The `_attribute_gap`/`gap`/`overhead_cost` sign relationship is easy to get backwards**
   (§4.1) — this build got it backwards once, and a test caught it before any caller was wired
   to it. Anyone extending `baseline.py` should re-read §4.1 before touching `_attribute_gap`.
7. **`_attribute_gap`'s general (non-degenerate) branch — the formula's one genuinely novel
   piece of arithmetic — initially had no test that exercised it at all** (§4.1). An independent
   OP-2 audit found every existing test used a fixture whose decomposition always landed in the
   degenerate `total_marginal == 0.0` branch instead. Closed by adding a second fixture and
   three hand-derived tests covering all three of the function's branches; the lesson (a fixture
   chosen for one property can silently fail to exercise the property a test claims to check) is
   worth re-reading before adding a fourth branch to this function.
8. **`INSUFFICIENT_DATA`'s no-comparison-and-no-findings evidence fallback is a documented,
   unverified placeholder unless the caller supplies `run_start_seq`** (§5.1) — `verdict()` has
   no raw event log to check a seq against, and no caller in this codebase supplies one yet
   (P17 isn't built). This is I6's one remaining soft spot in this delivery.

---

## 9. A worked example (the week-6 demo milestone, gates G6/G7)

**Deliberately not pasted here as literal figures.** Rule E1 (AGENTS.md §6, invariant I9) — "no
published statistic without a reproducible measurement," mechanised by `scripts/
check_bench_markers.py` — applies to every unit-bearing number under `docs/`, and a
`[bench:<file>]` marker must resolve to a committed file in `bench/results/`. The numbers a
`format_scorecard` demo run produces are a deterministic test's assertion output, not a
`bench/results/`-style measured benchmark, so citing them here as bare figures would either
trip that gate honestly or require committing a test's stdout as if it were a benchmark result
— a category error this project does not make (`bench/results/`'s own README: "Committed
*benchmark* output").

Run the real demonstration instead:

```
uv run pytest tests/analysis/test_baseline.py::test_format_scorecard_prints_the_week_6_demo_milestone_block -q -s
```

It builds a genuine two-branch fan-out multi-agent run (`alpha` running one `tool_call`,
`beta` running one `llm_call`, both starting at the run's own t=0 with no message dependency
between them) against a hand-authored single-agent baseline, and prints `format_scorecard`'s
full block: the achieved and ideal speedups, the signed six-bucket attribution (every tracked
bucket at `0` in this particular fixture, with the entire overhead cost honestly reported as
`unattributed` — §4.2's zero-overhead case, exercised directly), the token cost multiplier and
cost efficiency, and the comparability grade on the same block as every speedup figure (Design
Constraint 1). This is the same pytest-harness-in-place-of-an-unbuilt-CLI precedent P06 (gate
G3) and P09 (gate G4) already established.

---

## 10. Error code reference

| Code | Raised by | Meaning |
|---|---|---|
| `E-BASE-001` | `baseline.generate_baseline`, `baseline.compare` | `multi_events` has no `run_start` (nothing to derive a spec from), or the multi-agent log has no `run_end` |
| `E-BASE-002` | `baseline._attribute_gap` (via `compare`) | The six-bucket attribution does not sum to `overhead_cost` within floating-point tolerance — a code-correctness assertion (§4.1/§4.2's fixes), never expected on a real comparison |
| `E-VERD-001` | `verdict.Evidence.__post_init__` | An `Evidence` was constructed with an empty `event_seqs` — I6, mechanised in the type (§5.1) |
| `E-RES-001` | `resilience.score`, `resilience._score_one_fault` | `baseline_events` has no `success_check` assertion (§19.2 has nothing to divide by), `baseline_success == 0`, or a *fired, non-aborted* fault run is missing a `success_check` result or `run_end.status` |

---

## 11. Determinism (NFR-14)

Every collection any of the three modules builds is a `tuple` in a stable sort order, or a
`dict` inserted in a fixed key sequence — no bare `set` iteration anywhere in `baseline.py`,
`verdict.py`, or `resilience.py` (`scripts/check_determinism_hygiene.py` clean, including two
places where a raw `set()`/ternary-`set()` pattern was rewritten to a `list` + one final
`sorted(set(...))` hop specifically to satisfy the static check without changing behaviour —
see the modules' own inline comments at each site).
