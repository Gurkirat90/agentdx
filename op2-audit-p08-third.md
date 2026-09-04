# OP-2 INDEPENDENT AUDIT (THIRD PASS) — P08 `scenario/`

**Auditor note on method.** Fresh read, no memory of the build or the two prior OP-2s beyond
what `CONTEXT.md` §5 row 8 and the two summarized audit narratives state. Every claim below was
re-run by the auditor, not read off a prior report: `pytest`, `ruff`, throwaway repro scripts
against the real `agentdx` console script (`/tmp/optb_venv`), and one direct comparison of a
correctly-spelled vs. a plausibly-mistyped `--assert` expression against the same real fixture
run. No source file was edited; this session used only `Read`/`Grep`/`Bash` against the
checked-in tree, plus disposable scripts under `/tmp`.

**Scope.** `src/agentdx/scenario/` in full, with hard focus on the ~38 lines `assertions.py`
gained this session (2026-09-04, commit `d9ac1e8`, D-82) implementing `eval_findings_type_count`
and `_FINDING_TYPE_ALIASES` for the new `agentdx run --assert PATH OP VALUE` CLI flag. Verified
the diff is exactly those 38 lines via `git show d9ac1e8 -- src/agentdx/scenario/assertions.py`
— nothing else in `scenario/` changed this session.

---

## VERDICT

**FAIL — on the new code specifically.** One high-severity, fully demonstrated defect in
`eval_findings_type_count` (the function this session added): it accepts and silently evaluates
an assertion against **any** string as a finding type, with no check that the type is one the
analysis layer can ever actually produce. A single mistyped `--assert` expression — including
the single most predictable typo a user could make, confusing the finding-type name with the
existing built-in assertion name it resembles — reports a false `PASSED`/exit-0 on a run that
has the exact defect the assertion was meant to catch. Demonstrated live below, not inferred.

**The two prior repairs (fixture-existence / `E-SCEN-012`, and the corrected `C-13` positive
fixture-inference check) both hold** under the adversarial re-tests this pass ran against them —
see §2. No regression found in previously-audited code.

Everything else in `scenario/` examined this pass (schema, matrix, loader defaults, the
pre-existing `evaluate_assertion` dispatcher) is unchanged from the prior two audits' territory
and was not re-litigated from scratch, per the effort budget in the brief — this is not a claim
that no further defect exists there, only that this pass found none while doing the adversarial
checks it did run.

---

## 1. FINDING #1 (HIGH) — `--assert findings.<type>` accepts unknown/mistyped finding types and silently reports a vacuous PASS

**Where:** `src/agentdx/scenario/assertions.py:425-450` (`eval_findings_type_count`,
`_FINDING_TYPE_ALIASES` at line 422 — the exact new code this session added). Compounded by
`src/agentdx/cli/commands/run.py:192-222` (`_parse_assert_expr`), the only gate that runs
*before* the run executes, which validates the `findings.` prefix and the `OP VALUE` shape but
never the type name itself.

**The defect.** `eval_findings_type_count` does:
```python
resolved_type = _FINDING_TYPE_ALIASES.get(finding_type, finding_type)
count = sum(1 for f in run.findings if f.type == resolved_type)
```
There is no check anywhere — not in this function, not in `_parse_assert_expr`, not anywhere
else in the call path — that `resolved_type` is one of the finding types this codebase's
analysis layer can actually produce (`state_conflict` — `analysis/race.py:128`; `silent_failure`
— `analysis/resilience.py:111`; `coordination_bottleneck`/`redundancy` —
`analysis/verdict.py:433,456,489`). Any other string — a typo, a fictional type, or (the
demonstrated case below) a name that reads like a valid concept but isn't the actual `Finding.type`
value — silently counts zero matching findings and evaluates the comparison against `0`, which
for the extremely natural assertion shape `<= 0` ("assert no findings of this type") is **always
true**, regardless of what the run actually found.

**This is a direct violation of the module's own established pattern.** Every other name-lookup
this module performs is validated against an enumerated, known set before it is trusted:
`E-SCEN-005` checks a fault's `agent`/`tool`/`edge` target against `GraphIdentity`'s statically
discovered set and lists valid targets on failure; the pre-existing `evaluate_assertion`
dispatcher (`assertions.py:453-506`) raises on any assertion `name` not in its `match` statement
(and `validate.py`'s load-time twin checks scenario-YAML assertion names against
`schema.BUILT_IN_ASSERTION_NAMES`, `E-SCEN-007`). The new `--assert` path is the only
name-lookup in `scenario/` with zero enumeration — an open string flows straight to an equality
check with no failure mode at all.

**Demonstrated, real subprocess, real fixture (`fixtures/code_pipeline`, seed 42, which has a
real, seeded, golden `state_conflict` finding — PRD §23.1):**

```
$ agentdx --data-dir /tmp/opt_data run fixtures/code_pipeline --seed 42 \
    --assert "findings.no_state_conflicts <= 0"
code_pipeline: passed
  run_id: r_90b839e6
  verdict: state_conflict_risk (confidence low)
  ✓ findings.no_state_conflicts: 0 finding(s) of type 'no_state_conflicts' vs. <= 0
REAL EXIT CODE: 0

$ agentdx --data-dir /tmp/opt_data run fixtures/code_pipeline --seed 42 \
    --assert "findings.state_conflict <= 0"
code_pipeline: failed
  run_id: r_90b839e6
  verdict: state_conflict_risk (confidence low)
  ✗ findings.state_conflict: 1 finding(s) of type 'state_conflict' vs. <= 0
REAL EXIT CODE: 1
```

Both commands run against the identical sealed log (`r_90b839e6`, D-80 reuse). The first,
spelled after the module's own built-in assertion name `no_state_conflicts` (a completely
plausible thing to type — it is the literal name PRD §21.7 gives this exact check for the YAML
path), silently passes with exit 0. The second, spelled correctly, correctly fails with exit 1.
A CI pipeline using the first spelling gets false green on a run with a real, critical,
already-golden-fixture-documented state conflict.

This is not limited to that one name — any string works the same way:

```
$ agentdx --data-dir /tmp/opt_data run fixtures/code_pipeline --seed 42 \
    --assert "findings.this_type_is_not_real >= 0"
  ✓ findings.this_type_is_not_real: 0 finding(s) of type 'this_type_is_not_real' vs. >= 0
REAL EXIT CODE: 0
```

**Why this matters at the severity the project's own convention assigns it:** this is the exact
defect shape both prior `scenario/` OP-2s made their headline finding — a typo or a fictional
name silently passing validation instead of being rejected (`target.fixture` not checked for
existence; `fixtures/perturbations/` mis-inferred the same way `fixtures/tasks/` was). The
second audit's own language — "the exact defect shape the second P08 audit already recorded
twice" — describes this new instance precisely, in code written after both of those audits and
never independently reviewed until now.

**Suggested fix direction (not mandatory):** add a small `Final[frozenset[str]]` of the finding
types this build's analysis layer actually emits (cross-check `analysis/race.py`,
`analysis/resilience.py`, `analysis/verdict.py` before hardcoding it, since `resilience.py`'s
`SILENT_FAILURE` enum member may not currently reach a real `Finding` object at all — worth
confirming before writing the set) next to `_FINDING_TYPE_ALIASES` in `assertions.py`, and
reject `resolved_type` values outside it. Ideally the check happens in `_parse_assert_expr`
(`cli/commands/run.py`) too, so an unknown finding type is a `USAGE_ERROR` before any run starts
— consistent with PRD §21.3's stated rationale ("failing after a 40-second run because of a
typo is unacceptable in CI"), which this CLI-invented flag has no PRD grammar of its own to
inherit that rule from, but there's no reason not to hold it to the same standard the rest of
this module already does.

**Test-coverage note, same root cause.** `eval_findings_type_count`/`_FINDING_TYPE_ALIASES` have
**zero** direct unit tests — confirmed by `grep -rn "eval_findings_type_count\|FINDING_TYPE_ALIASES"
tests/`, which returns only the one docstring cross-reference in
`tests/integration/cli/test_assert_flag.py`'s module docstring, no actual test invocation. Every
test that touches this function goes through the full CLI, a real `Scheduler`, and one real
fixture — never a stub `RunSummary`, never an unrecognized-type case, never more than the one
`state_conflict`/`race` pairing. This is a real gap against `AGENTS.md` §5's own stated model for
this class of code ("hand-authored event logs with hand-computed expected outputs... most of the
product is testable without running an agent") and is exactly why Finding #1 went unnoticed at
commit time — the only test path exercising this function never tries a bad input. A fix should
add unit tests in `tests/unit/scenario/test_assertions.py` with a minimal stub satisfying the
`RunSummary`/`Finding` protocols, covering: exact-type match, alias resolution (`race` →
`state_conflict`), and (once the validation above exists) an unrecognized-type case asserting a
clear error rather than a silent pass.

---

## 2. Prior repairs re-tested adversarially — both hold

**`E-SCEN-012` (target.fixture existence, second OP-2 finding #3, `C-15`).** Re-tested against
an explicit fictional fixture name and confirmed rejected:

```
target: {fixture: totally_bogus_fixture}
→ E-SCEN-012  `target.fixture: 'totally_bogus_fixture'` does not name a real fixture
              (no `fixtures/totally_bogus_fixture/graph.py` on disk)
```

**`C-13` corrected fixture-inference (first OP-2's original finding, corrected by the second
OP-2 after the first fix proved too narrow).** Re-tested against both the originally-buggy
directory and the second directory the first fix missed:

```
task: fixtures/tasks/refactor_module.md
→ E-SCEN-003  ...`task`'s path is under `fixtures/tasks/`, a shared directory
              (not itself a fixture)...

task: fixtures/perturbations/whatever.md
→ E-SCEN-003  ...`task`'s path is under `fixtures/perturbations/`, a shared directory
              (not itself a fixture)...
```

Both correctly refuse to infer a fixture. `loader._is_real_fixture` (`loader.py:466-481`) is
still the positive `fixtures/<name>/graph.py`-exists check the second repair replaced the
denylist with — confirmed by reading it, not just by these two directories continuing to work
(a third, still-undiscovered non-fixture directory under `fixtures/` would also be caught, since
the check is positive rather than enumerated).

**Fault-parameter boundary (`message_reorder.window <= 16`, second OP-2 finding #4 — the
monkeypatch that dropped a ceiling from 16→6 without failing a single existing test).**
Re-tested at the exact boundary directly against `validate()`, independent of the existing test
suite:

```
window=1:  only base/unrelated errors (accepted)
window=16: only base/unrelated errors (accepted)
window=17: + E-SCEN-011 (rejected)
window=0:  + E-SCEN-011 (rejected)
window=-1: + E-SCEN-011 (rejected)
```

Ceiling and floor both hold at the exact PRD-cited boundary, not just at an extreme sentinel.

No regression found in any of the three previously-repaired areas under this pass's adversarial
re-tests.

---

## 3. Noted, not a `scenario/`-scoped finding — flagging for whoever looks at `cli/` next

**`CliRunSummary.findings` only ever contains `state_conflict` findings in production.**
`src/agentdx/cli/commands/run.py:365,428` builds every `CliRunSummary` with
`findings=tuple(analysis.race_findings)` (`cli/_analyze.py:120,167` — `race_findings` comes from
`detect_conflicts(events)`, i.e. `analysis/race.py` only). `coordination_bottleneck`/`redundancy`
findings computed by `analysis/verdict.py` never reach `RunSummary.findings` through the real
CLI path. This means `eval_findings_type_count`'s generality (any finding type, not just
`state_conflict`/`race`) is currently untestable against real output for any type other than the
one this session's own tests use, and the **pre-existing** built-ins `eval_no_silent_failures`/
`eval_max_findings` are equally limited when evaluated via `agentdx run` — a `silent_failure`
finding, if the analysis layer ever produces one as a real `Finding` object, would not be visible
to either check today. This lives entirely in `cli/_runsummary.py` and `cli/commands/run.py`,
predates this session (not part of the audited 38-line diff), and is outside `src/agentdx/scenario/`
— noted for context per the brief's "anything else you notice" clause, not counted as a
`scenario/` defect requiring repair in this report, and not mutation-tested or further chased
given the effort budget for this pass.

---

## 4. What was checked and found clean

- `ruff check src/agentdx/scenario/ src/agentdx/cli/commands/run.py` — all checks passed.
- `ruff format --check` on the same files — already formatted.
- `PYTHONHASHSEED=0 python -m pytest tests/unit/scenario` — 108 passed (matches `CONTEXT.md`'s
  stated count, no drift).
- `PYTHONHASHSEED=0 python -m pytest tests/unit/scenario tests/integration/cli/test_assert_flag.py
  tests/integration/cli/test_scenario_run.py tests/integration/cli/test_scenario_commands.py` —
  123 passed.
- `--assert` composes correctly with `--ci`/`--format json`: verified a real `--ci --assert
  "findings.race >= 1" --out ...` run and inspected `summary.json` — the ad-hoc assertion result
  is present in the `scenarios[].assertions[]` array alongside the (in this run, empty)
  scenario-file assertions, and the run's overall `status`/exit code reflect it. (Not re-audited
  in depth: the emitted `expected` field is `null` rather than a real value — this traces to
  `AssertionResult`'s pre-existing three-field shape, `assertions.py:129-142`, unchanged this
  session, and is a pre-existing PRD §22.3 conformance gap ("every assertion outcome records
  expected, actual...") rather than something the new code introduced or worsened. Flagged here
  only so it isn't mistaken for new-code fallout; not scored as a finding of this pass since it
  predates the audited diff.)
- `_parse_assert_expr`'s `expr.split()` + exactly-3-tokens shape check: tried malformed input
  (`""`, 1-token, 4-token, no-space-around-operator) — all correctly rejected with
  `E-TARGET-009` before any run starts, no crash.
- Repeated `--assert` (multiple flags): confirmed via the existing
  `test_assert_is_repeatable` — every expression evaluates independently and the run fails
  overall if any one does, matching the documented "every `--assert` must hold" contract.

---

## SUMMARY FOR A REPAIR SESSION WITH NO CONTEXT

One real, demonstrated, high-severity defect: `eval_findings_type_count`
(`src/agentdx/scenario/assertions.py:425`) and its only caller,
`_parse_assert_expr`/`_eval_assert_exprs` (`src/agentdx/cli/commands/run.py:192,225`), accept any
string as a `--assert findings.<type>` finding type with no validation against the finding types
this build actually produces. Fix by enumerating the known set and rejecting anything outside it
— ideally at CLI parse time, before the run executes. Add direct unit tests in
`tests/unit/scenario/test_assertions.py` for `eval_findings_type_count` with a stub `RunSummary`
(currently has none at all, only end-to-end CLI tests against one fixture and one finding type).
Everything else examined — including both defects the prior two audits found and repaired — held
under this pass's adversarial re-tests.
