# OP-3 REPAIR REPORT — second P11 `analysis/baseline` + `analysis/verdict` re-audit (`op2-audit-p11-second.md`)

**Repaired same day as the audit, by the orchestrating session (not the audit agent — the audit
agent used only `Read`/`Grep`/`Bash`, no `Edit`/`Write`).** Owner authorized fixing all five
findings in full (`AskUserQuestion`, "Fix all five now (recommended)"), applying the audit's own
suggested fix directions rather than novel designs.

## Finding #1 (CRITICAL) — `assess_comparability`'s model/tool-mismatch check was a tautology

**Repair.** `analysis/baseline.py::assess_comparability` no longer compares `multi_events`
against `baseline.spec` (a request `generate_baseline` derives *from* `multi_events` itself, so
comparing against it could never disagree). It now reads `baseline.events`' own recorded
`run_start`/`tool_call` events — what the injected `BaselineExecutor` actually ran — via the same
`_str_payload(run_start, "model")` / `multi_run_tools(...)` helpers already used for the
multi-agent side, giving `model_match`/`tools_match` genuine independent signal for the first
time. A baseline whose events carry no `run_start` at all now reports an empty observed model
(`""`), which correctly reads as a mismatch against any real model name rather than silently
matching by omission — the audit's own suggested fix direction, applied verbatim.

**Verified, real `generate_baseline`/`assess_comparability`, no mocks of the property under
test:**
- New test `test_assess_comparability_detects_a_real_model_mismatch_through_generate_baseline`
  (`tests/analysis/test_baseline.py`): builds real `multi_events` under model `"test-model"`,
  injects a `_FakeExecutor` whose returned events carry a genuinely different model
  (`"gpt-4-turbo"`) and a matching tool, calls `generate_baseline` for real (confirming
  `run.spec.model == "test-model"` still — documenting, not hiding, that `spec` remains a
  request rather than an observation), then calls `assess_comparability(multi_events, run)` and
  asserts `not result.model_match`, grade `C`, and `"model mismatch"` / `"gpt-4-turbo"` in
  `result.reason` — the exact false-`True`/grade-`A` result the audit demonstrated live is now a
  correctly-detected `False`/grade-`C`.
- Root-cause fix, not a superficial patch: fixing the comparison broke two pre-existing fixture
  helpers (`_fanout_baseline`, `_chain_baseline`) that had no `tool_call` event at all in their
  hand-built `baseline.events` — previously masked because the old code read
  `baseline.spec.tools` (hand-set to match) instead of the baseline's own actual events. Both
  fixtures were fixed by adding a matching `tool_call` event (with correctly renumbered
  `seq`/`causal_parents`/`event_count`), not treated as unrelated regressions — the two
  downstream tests that had been failing because of this
  (`test_compare_computes_speedup_against_the_independently_derived_ideal`,
  `test_format_scorecard_prints_the_week_6_demo_milestone_block`) now pass again.
- `_baseline_run()`'s own test helper rewritten to build real `events` (via `dataclasses.replace`
  on a `run_start`/`tool_call` factory) instead of leaving `events=()`, so every existing
  comparability test now exercises the corrected, events-based code path rather than a
  now-irrelevant `spec`-based one.
- `tests/analysis/test_baseline.py`: 23/23 pass. `tests/integration/cli/test_analyze_scorecard.py`
  + `test_compare_baseline.py`: 13/13 pass. `tests/acceptance/test_gates.py -k "test_g6_ or
  test_g7_"`: 2/2 pass — confirming the real, shipped `code_pipeline` single-agent baseline
  (which genuinely reuses the same tools as the multi-agent run) still grades correctly end to
  end, not just in the adversarial fixture above.

## Finding #2 (HIGH) — a scored fault, including a `SILENT_FAILURE`, never became a `VerdictFinding`

**Repair.** `analysis/verdict.py` gained `_resilience_findings(resilience, rules) ->
tuple[VerdictFinding, ...]`, mirroring `_redundancy_findings`'s shape exactly, per the audit's
own suggested fix direction: one `VerdictFinding` per `FaultScore` whose `degradation_class` is
`SILENT_FAILURE` (severity `CRITICAL`, matching PRD §18.3's severity table) or `HARD_FAILURE`
(severity `HIGH`); `NOT_FIRED`/`ABORTED` faults (`degradation_class is None`) are skipped, the
same exclusion `resilience.score()`'s own aggregation already applies. Evidence comes directly
from `FaultScore.evidence_seq`, which `resilience.score()` guarantees non-empty whenever a fault
is actually `SCORED` (I6 holds by construction, not by a new check here). Wired into `verdict()`
immediately after the existing `findings.extend(_redundancy_findings(...))` call, the same
pattern every other finding source already follows.

**Verified, real `resilience.score()`-shaped inputs, no mocks:**
- New tests in `tests/analysis/test_verdict.py`:
  `test_a_silent_failure_becomes_a_critical_verdict_finding` (reproduces the audit's own live
  demonstration almost exactly — `resilience_score=15`, `silent_failure_capped=True`, one
  `SILENT_FAILURE` `FaultScore` — and now asserts `len(result.findings) == 1`, `severity is
  Severity.CRITICAL`, `evidence.event_seqs == (1, 2, 3)`, and the fault's own id/label appear in
  the finding — where before the fix `result.findings` was empty),
  `test_a_hard_failure_becomes_a_high_severity_verdict_finding` (severity `HIGH`, correct
  evidence), `test_a_graceful_fault_produces_no_resilience_finding`,
  `test_a_not_fired_or_aborted_fault_produces_no_resilience_finding` (`degradation_class=None`
  must not crash the new code or fabricate a finding), and
  `test_resilience_none_produces_no_resilience_finding` (the `resilience=None`, "no chaos run"
  case stays finding-free, matching PRD §18.2's existing "25 if no chaos run" treatment).
- `_resilience()`'s test helper gained a `per_fault` parameter (defaulting to `()`, so every
  pre-existing test using it is unaffected) and a new `_fault_score()` factory was added for
  building minimally-valid `FaultScore` instances.
- Reachability, stated the same way the audit stated it: this remains not yet wired end-to-end
  through the shipped CLI (`cli/_analyze.py::analyze_events` is still only ever called with
  `fault_runs=()` today — a separate, pre-existing, undisclosed-as-new CLI gap this repair did
  not attempt to close). The fix closes the defect inside `verdict()` itself, which is real,
  already exercised by 34+ passing unit tests before this repair, and will surface correctly the
  moment that CLI wiring exists.

## Finding #3 (MEDIUM) — `BENEFICIAL`/`NEUTRAL` silently dropped from `secondary_classes` on a `HIGH` (not `CRITICAL`) conflict

**Repair.** `analysis/verdict.py` gained `_count_critical(findings)`, counting only
`CRITICAL`-severity `state_conflict` findings — deliberately narrower than the existing
`_count_high_or_critical`, which stays exactly as-is for `STATE_CONFLICT_RISK`'s own trigger and
`_coordination_score`'s conflict penalty (both of which PRD does specify as high-or-critical, per
the audit's own "What was checked and holds up" section). `_class_triggers`'s `beneficial`/
`neutral` computations now gate on `critical_conflicts == 0` instead of
`high_or_critical_conflicts == 0` — the audit's own suggested fix, applied verbatim.

**Verified, real `verdict()`, no mocks:**
- New test `test_a_high_severity_conflict_does_not_erase_beneficial_from_secondary_classes`:
  reproduces the audit's exact repro shape (`achieved_speedup=1.5`, one `HIGH` — not `CRITICAL`
  — `state_conflict` finding) and asserts `VerdictClass.BENEFICIAL in result.secondary_classes`
  (previously absent) while `verdict_class` is still correctly `STATE_CONFLICT_RISK`.
- New test `test_a_critical_severity_conflict_does_still_exclude_beneficial` confirms the other
  half of the fix: a genuinely `CRITICAL` finding still correctly excludes `BENEFICIAL` — the
  fix narrows the bar, it does not remove it.
- All prior `STATE_CONFLICT_RISK`/precedence tests (`test_state_conflict_risk_fires_on_a_high_
  severity_finding`, `test_precedence_state_conflict_risk_beats_coordination_bottleneck`, etc.)
  still pass unchanged — the headline class and `state_conflict_risk`'s own trigger were never
  affected by this finding or its fix.

## Finding #4 (MEDIUM) — `medium_max_instrumentation_gaps` was loaded but never consulted

**Repair.** `analysis/verdict.py::_confidence` now reads `rules.medium_max_instrumentation_gaps`
in its `low` branch (`instrumentation_gap_count > rules.medium_max_instrumentation_gaps`) instead
of leaving the ceiling unenforced — the magic-number-free pattern
`high_max_residual_fraction`/`medium_max_residual_fraction` already establish two lines above it
in the same function. Gap counts within the configured ceiling still contribute to `MEDIUM` (via
the pre-existing `instrumentation_gap_count > 0` check in the `medium` branch, unchanged); gap
counts past the ceiling now degrade confidence to `LOW`, per the audit's own suggested fix
direction.

**Verified, real `verdict()`/`load_verdict_rules()`, no mocks:**
- New test `test_confidence_drops_to_low_past_the_configured_instrumentation_gap_ceiling`:
  `instrumentation_gap_count=2` (== default ceiling) still reports `MEDIUM`;
  `instrumentation_gap_count=3` (> default ceiling) now reports `LOW` — directly refuting the
  audit's own "Proof 2" (1 gap through 1,000 gaps all reporting identical `MEDIUM`).
- New test `test_confidence_instrumentation_gap_ceiling_is_driven_by_the_configured_rule`:
  raising `medium_max_instrumentation_gaps` to `10` via `dataclasses.replace` makes
  `instrumentation_gap_count=3` report `MEDIUM` again — directly refuting the audit's own "Proof
  1" (changing the loaded config value had zero effect on the result). Same discipline as the
  existing `test_a_threshold_change_visibly_changes_the_verdict_class`.
- `test_confidence_medium_on_instrumentation_gaps` (pre-existing, `gap_count=1`) still passes
  unchanged — well within the ceiling either way.

## Finding #5 (MEDIUM) — `coordination_bottleneck_edge_cp_share` collided across two TOML subtables

**Repair.** Per the audit's second suggested option ("make `load_verdict_rules`'s merge loop
assert equality when the same key appears in two subtables with different values... this
project's existing 'assert it, report honestly' convention"), `load_verdict_rules()`'s merge loop
now tracks which `[verdict.*]` subtable first set each key (`first_seen`); a later subtable
setting the *same* key to a *different* value now raises the new `DuplicateThresholdKeyError`
(`E-VERD-002`), naming both conflicting subtables and their disagreeing values. Agreeing
duplicates — the committed file's current state, `0.40` in both `[verdict.classes]` and
`[verdict.severity]` — are unaffected and load exactly as before; only a future edit that lets
the two drift apart now fails loudly instead of silently picking whichever subtable is merged
last. The rename alternative (renaming one of the two TOML keys) was not taken, since the
assertion closes the general hazard class for *any* future accidental collision, not only this
one already-named key.

**Verified, real `load_verdict_rules()`, no mocks:**
- New test `test_load_verdict_rules_raises_when_a_duplicate_key_disagrees`
  (`tests/analysis/test_verdict_rules_toml.py`): monkeypatches `_find_rules_path` to point at a
  hand-written TOML file with `[verdict.classes]` at `0.40` and `[verdict.severity]` at `0.90` —
  the audit's own `repro4_toml_key_collision.py` shape — and asserts
  `DuplicateThresholdKeyError` is raised, `excinfo.value.code == "E-VERD-002"`, and both
  disagreeing values (`0.4`, `0.9`) appear in the message.
- New test `test_the_committed_files_duplicate_key_agrees_and_does_not_raise` confirms the real,
  shipped `verdict_rules.toml` still loads cleanly today (both entries agree at `0.40`) — the
  fix changes behaviour only on future drift, never on the file as committed.
- Every other `test_verdict_rules_toml.py`/`test_verdict.py` test that calls
  `load_verdict_rules()` continues to pass unchanged, confirming the new check adds zero false
  positives against the real file.

## What held, no repair needed

Per the audit's own "What was checked and holds up" section: the 2026-08-18 repair's
`_attribute_gap` branches, `run_start_seq`'s real `cli/_analyze.py` caller, `EmptyEvidenceError`/
I6's unbreakability, `race`/`redundancy`/`aggregates` composition into `verdict()`, determinism
(I1/NFR-14), `DEGRADED_FLAGGED`'s structural unreachability (D-51), the declared-but-unconsumed
`fake_fanout_*` TOML keys, the verdict-class precedence order, and `_coordination_score`'s
formula — none of these needed any change, and none were touched by this pass's fixes.

## Full verification, this pass

- `ruff check` / `ruff format --check` — clean on every touched file (`analysis/baseline.py`,
  `analysis/verdict.py`, `tests/analysis/test_baseline.py`, `tests/analysis/test_verdict.py`,
  `tests/analysis/test_verdict_rules_toml.py`).
- `mypy --strict` (scratch `--cache-dir`, this sandbox's standing stale-cache workaround) — clean
  on every touched source and test file.
- `pytest tests/analysis/` (`test_baseline.py`, `test_verdict.py`, `test_verdict_rules_toml.py`,
  `test_resilience.py`) — all pass; `test_verdict.py` grew from 34 to 43 tests,
  `test_verdict_rules_toml.py` from 6 to 8, `test_baseline.py` stayed at 23 (2 fixtures fixed,
  1 test added, net even after the fixture-repair pass described under Finding #1).
- `pytest tests/integration/cli/test_analyze_scorecard.py tests/integration/cli/
  test_compare_baseline.py` — 13/13 pass.
- `pytest tests/acceptance/test_gates.py -m acceptance -k "test_g6_ or test_g7_"` — 2/2 pass,
  real subprocess.
- Full suite (`tests/api` excluded, D-66): **2211 passed, 1 failed** — the one failure is
  `test_doctor_passes_on_a_healthy_setup`, the standing, pre-existing D-66 Python-3.10-sandbox
  diagnosis every prior ledger row carries, confirmed unrelated to any file this pass touched.
  Zero regressions relative to that baseline.
- 12 new/updated tests, all passing: `tests/analysis/test_baseline.py` (+1 test, 2 fixtures
  repaired, 1 helper rewritten), `tests/analysis/test_verdict.py` (+9 tests, 2 helpers extended/
  added), `tests/analysis/test_verdict_rules_toml.py` (+2 tests).

## Standing status

**A third independent re-audit remains owed**, same pattern every other repaired module in this
ledger carries — this repair is self-verified by the orchestrating session, not independently
re-checked by a fresh auditor. Not done, explicitly, matching the audit's own NOT DONE list where
it still applies: Finding #2's CLI reachability (`cli/_analyze.py`'s `fault_runs=()` gap) was
diagnosed but not closed — a separate, pre-existing gap, not part of this finding's own claim;
`resilience.py` itself was not re-audited beyond the one composition question this repair closed;
no re-verification on real Python 3.12 hardware (D-66 — nothing about any of these five fixes is
Python-version-specific in mechanism: pure event/dict/dataclass comparisons); `CliBaselineExecutor`'s
inability to produce `BaselineOutcome.CONTEXT_EXCEEDED` and the stale `cli/_baseline.py` docstring
citation (the audit's two "additional lower-severity observations") were disclosed by the audit
but not repaired here — neither was one of the audit's five numbered findings, and both remain
open for a future pass to pick up.
