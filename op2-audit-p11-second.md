# OP-2 INDEPENDENT AUDIT (SECOND PASS) — P11 `analysis/baseline`, `analysis/verdict`

**Scope.** `src/agentdx/analysis/baseline.py`, `src/agentdx/analysis/verdict.py`, their tests
(`tests/analysis/test_baseline.py`, `tests/analysis/test_verdict.py`,
`tests/analysis/test_verdict_rules_toml.py`), `src/agentdx/analysis/verdict_rules.toml`, and —
per this audit's brief — the real, recently-built consumers that never got adversarial scrutiny
of their own: `src/agentdx/cli/_baseline.py` (`CliBaselineExecutor`), `src/agentdx/cli/_analyze.py`
(`analyze_events`, the composition point that wires `race`/`resilience`/`aggregates` into
`verdict()`), and `src/agentdx/cli/commands/{analyze,compare}.py`. `src/agentdx/analysis/
resilience.py` is read as context only (row 11b, already audited in scope with no findings
against that file specifically — not re-litigated here except where it interacts with
`verdict()`'s own composition logic).

This is P11's **second** independent audit. The first (2026-08-18, same-day OP-2/OP-3) found a
sign-bug whose only regression test never exercised the general formula it claimed to protect, a
soft I6 gap in `verdict()`'s `INSUFFICIENT_DATA` fallback, two stale docstring citations, and two
unmarked dead TOML keys — all repaired same day (`docs/journal/2026-33.md`'s 2026-08-18 `P10
repair` row carries the full account; `CONTEXT.md` §5 row 11). That repair is **not** assumed
correct here; it is re-tested adversarially, and it holds (see "What was checked and holds up").
Two new, real defects were found instead, plus three more of lesser severity — none of them
restatements of the first audit's findings.

**Method.** Fresh read of `CONTEXT.md` (§2, §5 rows 11/11b/12, §8's ADR-017 line on `analysis.
baseline`'s I3 exception, §13's P11 session-log rows in `docs/journal/2026-33.md`), `AGENTS.md`,
PRD §17 (baseline), §18 (verdict), §19.5/§19.7 (resilience, for the composition question), and
the full text of `baseline.py` (1008 lines), `verdict.py` (954 lines), `cli/_baseline.py` (154
lines), `cli/_analyze.py` (175 lines) end to end. No source file was edited. Every claim below
was executed against the real, unmocked functions — nine throwaway repro scripts under
`/tmp/audit_repro/` calling the actual `generate_baseline`/`assess_comparability`/`compare`/
`verdict`/`load_verdict_rules`/`resilience.score` — using this project's own sandbox convention
(`PATH=/tmp/optb_venv/bin`, `PYTHONHASHSEED=0`). The existing test suite was run as a baseline
first: `pytest tests/analysis/test_baseline.py tests/analysis/test_verdict.py tests/analysis/
test_resilience.py tests/integration/cli/test_analyze_scorecard.py tests/integration/cli/
test_compare_baseline.py -q` — **96/96 pass**, no changes needed to reach that baseline.

---

## VERDICT: **FAIL**

The 2026-08-18 repair holds up under adversarial re-test: `_attribute_gap`'s three branches
(degenerate/general/infinite-marginal) still behave exactly as that repair's hand-derived tests
pin them, `run_start_seq` now has a real caller (`cli/_analyze.py::_run_start_seq`, closing that
audit's "no caller yet" gap), and `EmptyEvidenceError`/I6 is unbreakable by construction. But two
new, real defects were found and demonstrated live against the actual code — not against a mock
or a synthetic stand-in for the property under test:

1. **(CRITICAL)** `assess_comparability`'s `model_match`/`tools_match` checks are structurally
   tautological. The only real code path that produces a `BaselineRun`
   (`generate_baseline`) derives `BaselineRunSpec.model`/`.tools` **directly from the multi-agent
   run's own events**, then `assess_comparability` compares that derived value back against the
   same multi-agent run — so it is comparing the multi-run's model against itself, always. A
   `BaselineExecutor` that silently runs the single-agent baseline under a completely different
   model can never cause `model_match` to go `False`, and PRD §17.5's own literal grade-C trigger
   ("Reuse < 40%, **or a model/tool mismatch**, or the baseline failed the task") can never fire
   through this path — demonstrated live with a 100%-cache-reuse, genuinely-different-model
   comparison that is graded **A**, with the printed reason literally reading "identical
   model/tools/task."
2. **(HIGH)** `verdict()` has no mechanism analogous to `_coordination_bottleneck_findings`/
   `_redundancy_findings` for resilience data: a scored fault — including a genuine
   `SILENT_FAILURE`, the single worst outcome PRD §19.5 names and the one PRD §18.3's severity
   table explicitly assigns `critical` severity to, in the same row as a lost-update finding —
   never becomes a `VerdictFinding`. `verdict.findings` is empty even when the headline class is
   correctly `UNRELIABLE_TOPOLOGY`. Demonstrated live against the real `resilience.score()` (not
   a stand-in). Caveat, stated plainly: this is not yet end-to-end reachable via the shipped CLI
   either, because `cli/_analyze.py::analyze_events` is never called with `fault_runs` populated
   today — a separate, shallower gap, not part of this finding's own claim, which is about
   `verdict()`'s own logic.
3. **(MEDIUM)** `_class_triggers` reuses one shared `high_or_critical_conflicts` count for two
   PRD-distinct exclusion bars: `STATE_CONFLICT_RISK`'s own trigger (correctly high-or-critical)
   and `BENEFICIAL`/`NEUTRAL`'s "no critical findings" exclusion (PRD names *critical only*). A
   single `HIGH`-severity (not `CRITICAL`) state-conflict finding silently suppresses `BENEFICIAL`
   from `secondary_classes` even though its own PRD-literal trigger is still true. Headline choice
   is unaffected (`STATE_CONFLICT_RISK` already outranks `BENEFICIAL` whenever this happens), but
   PRD §18.1's explicit "secondary classes... so nothing is lost" guarantee is violated.
4. **(MEDIUM)** `verdict_rules.toml`'s `[verdict.confidence].medium_max_instrumentation_gaps`
   (default `2`) is loaded into `VerdictRules` but never read by `_confidence()`, which instead
   hardcodes `instrumentation_gap_count > 0` — a magic number this file's own header comment and
   AGENTS.md §4 both forbid. Consequence: there is no code path in which `_confidence()` ever
   returns `LOW` purely because of instrumentation-gap severity — 1 gap and 1,000 gaps report the
   identical `MEDIUM`.
5. **(MEDIUM)** `load_verdict_rules()` flattens every `[verdict.*]` subtable into one dict keyed
   by field name. `coordination_bottleneck_edge_cp_share` is declared in **both**
   `[verdict.classes]` and `[verdict.severity]` (PRD §18.1 and §18.3 respectively) — two
   logically separate thresholds that happen to share a name and, today, an identical value
   (`0.40`). Because `VerdictRules` has one flat field for it, whichever subtable is merged last
   (`severity`, per the fixed iteration order) silently wins; the other subtable's entry becomes
   a complete no-op with no validation and no test to catch a future edit that sets them
   differently.

None of the five is a restatement of the 2026-08-18 audit's findings, and none required editing
any source file to demonstrate.

---

## FINDING #1 (CRITICAL) — `assess_comparability`'s model/tool-mismatch check is a tautology; it can never detect what PRD §17.5 explicitly requires it to detect

**Where:** `src/agentdx/analysis/baseline.py:570-624` (`assess_comparability`), specifically
lines 582-584:

```python
run_start = _run_start(multi_events)
multi_model = _str_payload(run_start, "model") or ""
model_match = multi_model == baseline.spec.model
tools_match = sorted(multi_run_tools(multi_events)) == sorted(baseline.spec.tools)
```

and `generate_baseline` (lines 475-495), which is the **only** place a `BaselineRunSpec` is ever
constructed in this codebase:

```python
model = _str_payload(run_start, "model") or ""      # `run_start` is `_run_start(multi_events)`
...
spec = BaselineRunSpec(..., model=model, tools=tools, ...)   # `tools = multi_run_tools(multi_events)`
```

`baseline.spec.model` is derived from `multi_events` itself, and `assess_comparability` then
compares it back against `multi_events`' own model. The two sides of the comparison are
computed from the identical source, by construction — `model_match`/`tools_match` are `True` for
every `BaselineRun` this codebase can produce, regardless of what the injected
`BaselineExecutor` actually ran.

**Why this is reachable, not contrived.** PRD §17.5's literal table (line 2745) names the grade-C
trigger as *"Reuse < 40%, or a model/tool mismatch, or the baseline failed the task"* — three
independent conditions, one of which is a real, named requirement this module is supposed to
implement, not a hypothetical the audit invented. The real, shipped `BaselineExecutor`
(`cli/_baseline.py::CliBaselineExecutor.execute`) does not thread `spec.model`/`spec.tools`/
`spec.system_prompt`/`spec.max_steps` through to the real execution at all — it calls
`run_cmd._execute_one(graph=..., task_text=spec.task, ..., seed=spec.seed, ..., cache_mode=
spec.cache_mode, ...)`, passing only `task`, `seed` and `cache_mode`. Today this is masked
because `CliRunHost` stamps `run_start.payload.model` from `self._model`, which defaults to the
literal string `"unspecified"` at every one of `_execute_one`'s call sites (`cli/host.py:234`,
`cli/commands/run.py:319` never passes `model=`) — so both sides currently read `"unspecified"`
regardless. But the comparability check's job is precisely to catch the day this stops being
true — a user re-running `agentdx compare RUN_ID --baseline` after changing their configured
model, or a future live-model baseline target (explicitly anticipated:
`UnsupportedBaselineTargetError`'s own docstring says "or, one day, live-model") that genuinely
executes under a different model than the original run. In both of those completely ordinary
scenarios, the check cannot catch the mismatch, because it was never comparing the executor's
actual behaviour — only a value that was echoed back to itself.

**Demonstrated, real `generate_baseline`/`assess_comparability`, no mocks of the property under
test** (`/tmp/audit_repro/repro1_model_tools_tautology.py`):

```
multi-agent run's actual model      : llama-3.1-8b
baseline's ACTUAL executed model    : gpt-4-turbo   <-- genuinely different model
baseline.spec.model (what assess_comparability checks against) : llama-3.1-8b
cache_reuse_rate                    : 1.0

model_match : True
tools_match : True
grade       : A
reason      : cache reuse 100%, identical model/tools/task, both runs succeeded
```

The `_FakeExecutor` here does not lie about anything `assess_comparability` inspects — it
returns real `Event`s whose own `run_start.payload.model` is `"gpt-4-turbo"` and whose own
`tool_call` reuses the same tool as the multi-run. That is exactly the shape a real,
misconfigured or drifted executor would produce. `assess_comparability` never looks at
`baseline.events`' own recorded `run_start` at all — only at `baseline.spec`, which is not
independent information.

**Confirmed not already covered by any existing test.**
`tests/analysis/test_baseline.py::test_assess_comparability_grade_c_model_mismatch` and
`test_assess_comparability_grade_c_tool_set_mismatch` do exist and do pass — but both construct
a `BaselineRun` **directly** via the test-local `_baseline_run()` helper
(`tests/analysis/test_baseline.py:408-433`), which builds a `BaselineRunSpec` with an
arbitrary, hand-set `model="a-different-model"` that bypasses `generate_baseline` entirely. They
prove `assess_comparability`'s own comparison logic is internally consistent; they do not (and,
given how the test is written, cannot) prove that a real `BaselineRun` can ever actually reach
`assess_comparability` in a mismatched state. The one test that does exercise
`generate_baseline` (`test_generate_baseline_derives_spec_from_run_start_and_computes_cache_
reuse_rates`, line 301) asserts, in its own words, `run.spec.model == "test-model"  # from
run_start's default payload` — i.e. it documents the tautology as an expected property without
ever following through to `assess_comparability` to see what that means for the comparability
grade. No test in the suite calls both functions together with an executor whose returned events
disagree with the multi-run.

**Severity.** Critical. This is a scorecard-facing metric that reaches the user verbatim (`agentdx
compare --baseline`/`agentdx analyze --scorecard`, gates G6/G7) with a printed sentence claiming
"identical model/tools/task" that the code has no way to falsify. It directly defeats a named,
non-optional PRD §17.5 requirement, and it fabricates confidence in exactly the shape invariant I9
exists to forbid.

**Suggested fix direction (not mandatory).** `assess_comparability` should compare `multi_model`/
`multi_run_tools(multi_events)` against what `baseline.events`' **own** `run_start`/`tool_call`
events actually recorded — not against `baseline.spec`, which is a request, not an observation.
`BaselineRun` already carries `events`; a small helper mirroring `_str_payload(run_start,
"model")` and `multi_run_tools` applied to `baseline.events` would give `assess_comparability`
independent signal. Where the baseline target genuinely never emits a `model` field at all (as
`code_pipeline`'s scripted graph does today, always `"unspecified"`), that should itself be
visible — comparing `"unspecified"` against a real model name should not silently read as a match.

---

## FINDING #2 (HIGH) — a scored fault, including a `SILENT_FAILURE`, never becomes a `VerdictFinding`; `verdict()` has no per-fault findings path at all

**Where:** `src/agentdx/analysis/verdict.py` — there is no function analogous to
`_coordination_bottleneck_findings` (line 421) or `_redundancy_findings` (line 471) for
`resilience.ResilienceResult`/`FaultScore`. `resilience: ResilienceResult | None` is consumed in
exactly two places in the whole module: `_class_triggers`'s `unreliable` trigger (line 650-656,
reads only `resilience.silent_failure_capped`/`.resilience_score`) and `_coordination_score`'s
`reliability_component` (line 742-745, reads only `.resilience_score`). `verdict()`'s own
`findings: list[VerdictFinding]` is built at lines 861-867 from exactly two sources
(`_coordination_bottleneck_findings`, `_redundancy_findings`) — `resilience`/`state_conflict_
findings` never appear on the right-hand side of a `findings.extend(...)` call for resilience
data (state-conflict findings feed the class trigger and the coordination-score penalty, same as
resilience, but never a `VerdictFinding` either — this is a genuine, structural gap in the
module, not specific to one input type).

**Why this matters, and PRD basis.** PRD §19.5 states plainly: *"`silent_failure` is the worst
outcome in the model because it is the one that reaches users undetected."* PRD §18.3's severity
table lists, in one row: *"`critical` | Lost update with divergent values; silent failure under
fault; total failure"* — the identical severity bucket a real `state_conflict` finding
(`race.py`) already uses to become a `VerdictFinding`. `resilience.FaultScore` (the per-fault
record `resilience.score()` already produces, fully real and already exercised by
`tests/analysis/test_resilience.py`'s 18 tests) carries everything a finding needs —
`fault_id`, `fault_label`, `degradation_class`, `evidence_seq` — and none of it is used to build
one.

**Demonstrated, real `resilience.score()` and real `verdict()`, no mocks**
(`/tmp/audit_repro/repro5_silent_failure_no_finding.py`):

```
resilience.resilience_score      : 15
resilience.silent_failure_capped : True
per_fault[0].degradation_class   : silent_failure
per_fault[0].fault_id            : f-silent
per_fault[0].evidence_seq        : (1, 2, 3)

verdict_class : unreliable_topology
findings      : []
```

The headline class is correctly `UNRELIABLE_TOPOLOGY` — that part of the composition works. But
`verdict.findings` is empty. A caller rendering `verdict.findings` (the API's
`GET /api/runs/{id}/findings`-style surface, or the CLI's `for finding in verdict.findings:
out.line(...)` loop in `cli/commands/analyze.py:130-131`) has nothing at all to show naming
*which* fault silently failed, even though that exact information (`fault_id="f-silent"`,
evidence seqs `(1, 2, 3)`) was sitting in `resilience.per_fault[0]` the whole time.

**Confirmed not already covered by any existing test.** Every `test_verdict.py` call site that
passes `resilience=` uses the module's own `_resilience(score=..., silent_failure_capped=...)`
helper (lines 157-166), which always constructs `per_fault=()` — an empty tuple. No test in
`test_verdict.py` ever passes a `resilience` result with a populated `per_fault` sequence, so no
test could have caught this: the test suite's own resilience fixture structurally cannot carry
the information whose absence this finding is about.

**Reachability, stated honestly.** This is not yet reachable end-to-end through the shipped CLI:
`cli/_analyze.py::analyze_events` is only ever called with `fault_runs=()` today (`cli/commands/
analyze.py`/`compare.py` never construct or pass any `FaultRunInput`s — chaos-run composition
into `analyze`/`compare` is a separate, not-yet-built CLI feature). But the defect this finding
names is inside `verdict()` itself, which is fully real, already covered by 34 passing unit
tests, and would surface the exact moment that CLI wiring exists — it is not a hypothetical
input shape, since `resilience.score()` (P11's own sibling module) already produces exactly this
`FaultScore` shape in its own real, exercised test suite.

**Severity.** High, not critical: the headline verdict class and the coordination-score penalty
are both correct today, so a user is not told the wrong thing — they are told an incomplete
thing, missing the one itemised, evidence-backed line PRD §18.3's severity table implies should
exist for the single outcome this project's own resilience design (§2.7, referenced from §19.5)
calls the most dangerous one to miss.

**Suggested fix direction (not mandatory).** A `_resilience_findings(resilience, rules) ->
tuple[VerdictFinding, ...]` mirroring `_redundancy_findings`'s shape: one `VerdictFinding` per
`FaultScore` with `degradation_class in (SILENT_FAILURE, HARD_FAILURE)`, severity `CRITICAL`
for `SILENT_FAILURE` (matching PRD §18.3) and `HIGH` for `HARD_FAILURE`, evidence from
`FaultScore.evidence_seq`, wired into `verdict()`'s `findings.extend(...)` chain the same way
`_redundancy_findings` already is.

---

## FINDING #3 (MEDIUM) — `BENEFICIAL`/`NEUTRAL` silently drop out of `secondary_classes` on a `HIGH` (not `CRITICAL`) state-conflict finding

**Where:** `src/agentdx/analysis/verdict.py:648-697`. `high_or_critical_conflicts =
_count_high_or_critical(state_conflict_findings)` (line 648) counts both `HIGH` and `CRITICAL`
severities. That one value is reused, unchanged, for two PRD-distinct exclusion bars:

```python
state_conflict_risk = high_or_critical_conflicts > 0                       # PRD: high OR critical — correct
...
neutral = (... and high_or_critical_conflicts == 0)                        # PRD: "no critical findings" — should be critical-only
beneficial = (... and high_or_critical_conflicts == 0)                     # PRD: "no critical findings" — should be critical-only
```

PRD §18.1's table gives `BENEFICIAL`/`NEUTRAL` a narrower exclusion ("no `critical` findings")
than `STATE_CONFLICT_RISK`'s own trigger ("≥1 `state_conflict` finding at `high`/`critical`") —
two different bars, spelled differently in the same table, sharing one variable in the code.

**Why this is reachable, not contrived.** Any real run with exactly one `read_write` or
`write_read` finding (both `HIGH` severity per `race.py`'s own `_BASE_SEVERITY` table — only
`write_write` is `CRITICAL`) alongside a genuinely good speedup hits this. A stale/dirty read
that never destroyed data is a real, non-catastrophic finding class this project's own race
detector reports routinely; it should not silently erase the fact that the topology is otherwise
beneficial from the secondary-classes badge list.

**Demonstrated, real `verdict()`, no mocks** (`/tmp/audit_repro/
repro3_beneficial_secondary_high_vs_critical.py`):

```
verdict_class     : state_conflict_risk
secondary_classes : []
```

Input: `achieved_speedup=1.5` (comfortably above `beneficial_min_speedup=1.15`) plus one `HIGH`
(not `CRITICAL`) `state_conflict` finding. Headline is correctly `STATE_CONFLICT_RISK`
(outranks `BENEFICIAL` regardless — this part is fine), but `BENEFICIAL` is absent from
`secondary_classes` even though its own PRD-literal trigger ("`achieved_speedup >= 1.15` and no
`critical` findings") is still true.

**Confirmed not already covered by any existing test.**
`test_state_conflict_risk_fires_on_a_high_severity_finding` (line 245) uses the identical input
shape (a `HIGH` finding plus `achieved_speedup=1.5`) but only asserts
`result.verdict_class is VerdictClass.STATE_CONFLICT_RISK` — it never inspects
`secondary_classes`, so it cannot have caught this.

**Severity.** Medium. The headline class is never affected (`STATE_CONFLICT_RISK` is always
correctly true and always correctly outranks `BENEFICIAL`/`NEUTRAL` whenever this variable
conflation would matter), so no user is told a wrong top-line verdict. But PRD §18.1's own stated
rationale for secondary classes — "shown as badges beneath the headline so nothing is lost" — is
violated for a real, reachable input shape.

**Suggested fix direction (not mandatory).** Compute a second, `CRITICAL`-only count (e.g.
`critical_conflicts = sum(1 for f in state_conflict_findings if f.type == "state_conflict" and
f.severity == Severity.CRITICAL.value)`) and use that for `beneficial`/`neutral`'s exclusion,
leaving `high_or_critical_conflicts` for `state_conflict_risk` and the coordination-score penalty
(both of which PRD does specify as high-or-critical) unchanged.

---

## FINDING #4 (MEDIUM) — `medium_max_instrumentation_gaps` is loaded from `verdict_rules.toml` but never consulted; confidence has no ceiling on instrumentation-gap severity

**Where:** `src/agentdx/analysis/verdict.py:756-780` (`_confidence`). `instrumentation_gap_count
> 0` (line 776) is a hardcoded literal `0`. `VerdictRules.medium_max_instrumentation_gaps` (field
declared line 282, TOML default `2`, loaded at line 389) is never referenced anywhere in
`_confidence` or elsewhere in the file (`grep -c medium_max_instrumentation_gaps
verdict.py` → 3 occurrences, all in the field declaration/default/loader — zero in the function
that is supposed to use it).

**Why this is reachable, not contrived.** Any real run whose event log has more than a couple of
`instrumentation_gap` events (an unrecognised LangGraph internal, per PRD line 5127 — a real,
documented, non-exotic occurrence) hits this. `verdict_rules.toml`'s own header comment states:
*"every number those three files compare a measurement against lives here, and only here"* — a
project-wide, AGENTS.md §4-backed guarantee this specific comparison violates directly, using an
inline magic number instead of the config value sitting two lines above it in the same TOML
table (`high_max_residual_fraction`/`medium_max_residual_fraction`, both correctly read at lines
767/774, are the pattern this one silently deviates from).

**Demonstrated, real `verdict()`/`load_verdict_rules()`, no mocks**
(`/tmp/audit_repro/repro2_instrumentation_gap_threshold_dead.py`):

```
--- Proof 1: the configured threshold has ZERO effect on the result ---
  medium_max_instrumentation_gaps=default(=2)    gap_count=1  -> confidence=medium
  medium_max_instrumentation_gaps=zero(=0)       gap_count=1  -> confidence=medium
  medium_max_instrumentation_gaps=huge(=1000000) gap_count=1  -> confidence=medium

--- Proof 2: no ceiling at all on instrumentation_gap_count's severity ---
  instrumentation_gap_count=    1 -> confidence=medium
  instrumentation_gap_count=    2 -> confidence=medium
  instrumentation_gap_count=    3 -> confidence=medium
  instrumentation_gap_count=   50 -> confidence=medium
  instrumentation_gap_count= 1000 -> confidence=medium
```

Proof 1 changes the loaded config value across three orders of magnitude with zero effect on the
output. Proof 2 shows 1,000 instrumentation gaps — a log riddled with unrecognised internals,
i.e. very low actual data quality — reports the exact same `MEDIUM` confidence as a single gap.
PRD §18.5's own table structure (`high`: "no instrumentation gaps"; `medium`: "≤2 instrumentation
gaps") implies a real ceiling was intended past which confidence should degrade further; nothing
in the shipped code enforces one.

**Confirmed not already covered by any existing test.**
`test_confidence_medium_on_instrumentation_gaps` (line 532) only exercises
`instrumentation_gap_count=1` — well inside the configured threshold — and never varies the
threshold itself. No test in the file constructs `dataclasses.replace(rules,
medium_max_instrumentation_gaps=...)`, and no test uses a gap count anywhere near or past the
configured boundary.

**Severity.** Medium. `_confidence` is used for display/CI-gating de-emphasis (PRD §18.5: "Low
confidence... excludes it from CI assertions by default"), not for the headline verdict class or
score — but a run whose log has severe instrumentation gaps should arguably never report
`MEDIUM` confidence (implying only a modest caveat) when the actual data quality is far worse,
and the config file actively invites an editor to believe changing this number has an effect.

**Suggested fix direction (not mandatory).** Read `rules.medium_max_instrumentation_gaps` in
place of the literal `0`, and add a genuine ceiling — e.g. gaps within the threshold contribute
to `medium` (as today, but via the config value), gaps beyond it contribute to `low` alongside
the residual/grade conditions already there.

---

## FINDING #5 (MEDIUM) — `coordination_bottleneck_edge_cp_share` is declared in two TOML subtables sharing one flat dataclass field; one silently shadows the other

**Where:** `src/agentdx/analysis/verdict_rules.toml:42` (`[verdict.classes]`) and `:66`
(`[verdict.severity]`) both declare `coordination_bottleneck_edge_cp_share`. `VerdictRules`
(`verdict.py:256-292`) has exactly one field of that name. `load_verdict_rules`'s merge loop
(lines 356-361) iterates `(classes, score_section, severity, confidence, recs)` in that fixed
order and overwrites `merged[key]` unconditionally on every match — so whichever subtable is
iterated last for a given shared key wins, silently, with no warning and no validation that the
two subtables agree.

**Why this is reachable, not contrived.** Both entries currently read `0.40`, so there is no
live misbehaviour in the shipped file today — this is a config-authoring trap, not a currently
wrong number. But the two entries sit under two different, PRD-numbered section headers in the
same file (§18.1 "verdict class triggers" vs. §18.3 "severity assignment") that visually and
textually invite an editor to believe they are two independent knobs — exactly the situation
`verdict_rules.toml`'s own stated purpose ("versioned... so a score whose formula changes
silently is worthless for regression comparison," PRD §18.2) exists to prevent, and exactly the
class of silent-drift risk CONTEXT.md tripwire 17 was created for ("a change that flips a gate's
pass/fail status without a same-commit ledger entry").

**Demonstrated, real `load_verdict_rules()`, no mocks** (`/tmp/audit_repro/
repro4_toml_key_collision.py`, pointed at a hand-written TOML file with the two subtables set to
different values):

```
[verdict.classes].coordination_bottleneck_edge_cp_share  in file = 0.40
[verdict.severity].coordination_bottleneck_edge_cp_share in file = 0.90
VerdictRules.coordination_bottleneck_edge_cp_share (loaded)      = 0.9
```

`[verdict.classes]`'s entry (0.40) is completely discarded; `[verdict.severity]`'s value (0.90)
is used for **both** the `COORDINATION_BOTTLENECK` class trigger (`_class_triggers`, line 670)
and the `coordination_bottleneck` finding's own detection threshold
(`_coordination_bottleneck_findings`, line 429) — an editor who changed only `[verdict.severity]`
believing it controlled finding-severity independently of the class trigger would silently
change both, or vice versa, with zero indication anywhere (no test failure, no lint, no
`--explain` divergence, since `--explain` just prints the raw TOML text verbatim, not the
resolved dataclass).

**Confirmed not already covered by any existing test.**
`tests/analysis/test_verdict_rules_toml.py` round-trips the committed file and spot-checks
individual field values against the committed numbers — it has no test that sets the two
subtables to *different* values and checks which one wins, because doing so requires the exact
kind of hand-crafted adversarial TOML fixture this audit built.

**Severity.** Medium. Currently inert (both values agree), so no run is scored wrongly today —
but the mechanism is a latent defect, not a hypothetical one: the collision is real and
demonstrated, and the file's own layout actively encourages the mistake that would trigger it.

**Suggested fix direction (not mandatory).** Either rename one of the two TOML keys (e.g.
`severity_edge_cp_share` under `[verdict.severity]`, since it is conceptually the same number
reused rather than two, this is at minimum a naming/documentation fix) so they cannot collide, or
make `load_verdict_rules`'s merge loop assert equality when the same key appears in two subtables
with different values, per this project's existing "assert it, report honestly" convention
(`overhead.py`'s `E-OVHD-001` / `baseline.py`'s `E-BASE-002`).

---

## Additional lower-severity observations (not full findings — noted for completeness)

- **`CliBaselineExecutor.execute()` can never produce `BaselineOutcome.CONTEXT_EXCEEDED`**
  (`cli/_baseline.py:142-144`: `COMPLETED if result.status == "complete" else FAILED` — a
  binary branch with no third case). This mirrors D-51's already-declared `DEGRADED_FLAGGED`
  gap in shape (a real enum value the shipped code has no path to produce) but is **not**
  declared anywhere in `docs/baseline-methodology.md`/`docs/cli.md`. Confirmed the underlying
  runtime has no representation of a context-window-exceeded outcome at all
  (`sdk/generic.py:1888-1898`'s `status` is only ever `"complete"`/`"failed"`; `run_end.status`'s
  schema enum has no such member either) — so this is currently unreachable through the one
  registered target (`code_pipeline`, which never calls a live model), not a live bug today.
  Worth a one-line disclosure the same way D-51 was disclosed, before a live-model baseline
  target is registered.
- **Stale docstring citation**: `cli/_baseline.py`'s closing comment (lines 148-154) cites
  `tests/unit/cli/test_baseline_executor.py` as the place `isinstance(executor, BaselineExecutor)`
  is asserted against a real instance. That file does not exist (`find tests -iname
  '*baseline_executor*'` → no results). The real coverage lives in `tests/integration/cli/
  test_analyze_scorecard.py`/`test_compare_baseline.py`/`tests/acceptance/test_gates.py`. Same
  class of defect the first P11 audit found and fixed elsewhere in this same file family
  (a stale test-name citation in `baseline.py`'s own docstring) — recurred here, undetected,
  in code written after that repair.

---

## What was checked and holds up (no defect found)

- **The 2026-08-18 repair's own regression tests, re-verified adversarially.**
  `_attribute_gap`'s three branches (`total_marginal == 0.0` degenerate case,
  `total_marginal == float("inf")` case, and the general case the original sign-bug lived in)
  were each re-run against their existing hand-derived-fraction tests; all pass. The repair's
  claim that a reintroduced sign error at the general branch's `contribution = -gap * (...)`
  line would now be caught is consistent with that branch's own dedicated test
  (`test_compare_signed_six_bucket_attribution_general_branch_matches_hand_derived_fractions`)
  using genuinely distinct, non-degenerate bucket durations — not re-litigated by mutation here
  since the repair's own account already demonstrates it, but the fixture shape was read in full
  and confirmed non-degenerate.
- **`run_start_seq`'s "no real caller yet" gap, closed for real.** The first audit's soft I6 gap
  (an unverified placeholder `event_seqs=(0,)`) now has a genuine caller:
  `cli/_analyze.py::_run_start_seq(events)` reads the real log's own `run_start.seq` and threads
  it into `verdict(run_start_seq=...)` on every real `analyze`/`compare` invocation — confirmed
  by reading `cli/_analyze.py:80-84` and `:154` together, not merely trusting the docstring.
- **I6/`EmptyEvidenceError` is unbreakable by construction**, confirmed by direct construction
  attempts against `Evidence(event_seqs=(), ...)` — raises `E-VERD-001` every time, exactly as
  designed; no way found to route around `__post_init__`.
- **`race.Finding`/`analysis.race` composition into `verdict()` is correctly wired** —
  `cli/_analyze.py:149` passes `race_findings` (real `detect_conflicts(events)` output, `type=
  "state_conflict"`, string severities matching `_count_high_or_critical`'s comparison) as
  `state_conflict_findings`; `redundancy_groups`/`edge_aggregates`/`agent_aggregates` are
  likewise correctly threaded and each produces its own `VerdictFinding`
  (`_redundancy_findings`, `_coordination_bottleneck_findings`) — the "one layer down" version of
  the `cli/`-audit's `--assert`-composition gap does **not** recur for these three finding
  sources; it does recur for resilience (Finding #2 above).
- **Determinism (I1/NFR-14).** No bare `set` iteration in either file (`grep -n "set("` in both
  files: every occurrence is `sorted(set(...))`); no `time.time`/`random`/`uuid` calls; every
  dict this module builds is either iterated in a fixed `GAP_BUCKET_ORDER`/`_PRECEDENCE` tuple
  order or keyed by values already sorted upstream. `heuristic_step_budget`/`compose_baseline_
  prompt`'s agent-ordering logic (`_agent_order`) sorts by `(first_seq, agent_id)`, a stable,
  input-order-independent key.
- **`DEGRADED_FLAGGED`'s structural unreachability (D-51)** re-confirmed still accurately
  described — `classify_degradation`'s three reachable branches are unchanged since the first
  audit; this is a known, disclosed gap, not re-reported as new.
- **`fake_fanout_max_parallelism`/`fake_fanout_min_branches`** (`verdict_rules.toml:89-90`) are
  still correctly marked, in-file, as declared-but-unconsumed — consistent with `verdict.py`'s
  module docstring; unlike `medium_max_instrumentation_gaps` (Finding #4), these two carry no
  false expectation of being live.
- **The verdict-class precedence order itself** (`_PRECEDENCE`, line 111) was checked against
  every one of PRD §18.1's nine classes and matches exactly; `headline = next((c for c in
  _PRECEDENCE if triggers[c]), VerdictClass.INSUFFICIENT_DATA)` correctly guarantees a headline
  always exists (`insufficient_data`'s own trigger includes `comparison is None`).
- **`_coordination_score`'s formula** (speedup/efficiency/reliability weights, conflict penalty
  cap) matches PRD §18.2's literal formula term-for-term, including the `clamp`/`round`
  semantics and the "25 if no chaos run" fallback.

---

## NOT DONE / RISKS

- **Finding #2's end-to-end CLI reachability was not built or forced** — no scenario/fixture in
  this repo currently produces a real `SILENT_FAILURE` through `agentdx run`+`agentdx analyze`
  (chaos-run composition into `analyze`/`compare` isn't wired yet, a separate, pre-existing gap
  this audit did not attempt to close or fully characterise). The defect is proven at the
  `verdict()`/`resilience.score()` level, which is real and already used in production by
  `resilience.py`'s own callers — but a literal `agentdx analyze RUN_ID` reproduction was not
  attempted since no CLI path exists yet to feed it fault runs.
- **`resilience.py` itself was not re-audited** beyond the one composition question (Finding
  #2) — row 11b's prior "no findings against this file specifically" is taken as still current;
  this pass did not re-run an adversarial pass over `resilience.py`'s own internals (recovery
  time, amplification, the aggregation cap) since the brief scoped this audit to `baseline`/
  `verdict`.
- **Finding #5's TOML collision was demonstrated against a hand-written fixture file**, not the
  committed `verdict_rules.toml` (whose two values currently agree, by construction) — the
  mechanism is proven, not a live wrong number in the shipped file today.
- **Did not re-verify on real Python 3.12 hardware** — this sandbox is Python 3.10 (documented,
  pre-existing D-66 gap, not re-reported here). Nothing about any of the five findings is
  Python-version-specific in mechanism (pure dict/dataclass/string comparisons); real-hardware
  confirmation is still owed, same as every other module's audit in this project's history.
- **`docs/AgentDX-PRD-v2.md`'s §18.6 recommendation-rule gaps** (fake-fan-out, write-write-
  conflict — already declared, not re-investigated) and **`assess_comparability`'s omission of
  `cache_mode` as an explicit comparability factor** (not required by PRD §17.5's literal table,
  which names only reuse/model/tools/task-success — flagged here only as a suspicion, not a
  finding, since no PRD text requires it and cache-mode divergence should already show up
  indirectly through a depressed `cache_reuse_rate`) were both considered and consciously not
  pursued further, for lack of a PRD basis to call either one a defect rather than a design
  choice.
- Repro scripts live at `/tmp/audit_repro/repro1..repro5*.py` plus `custom_rules.toml` (this
  sandbox) — disposable, not part of the repository; re-run any of them against a repaired
  `analysis/baseline.py`/`verdict.py` to confirm a fix.
