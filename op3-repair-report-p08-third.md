# OP-3 REPAIR REPORT — third P08 `scenario/` audit (`op2-audit-p08-third.md`)

**Repaired same day as the audit, by the orchestrating session (not the audit agent — the
audit agent used only `Read`/`Grep`/`Bash`, no `Edit`/`Write`, per this project's own audit/
repair separation).**

## Finding #1 (HIGH) — `--assert findings.<type>` accepted unknown/mistyped finding types

**Repair.** `src/agentdx/scenario/assertions.py` gained:

- `_KNOWN_FINDING_TYPES: Final[frozenset[str]]` — `{"state_conflict", "coordination_bottleneck",
  "redundancy"}`, cross-checked directly against every `Finding`/`VerdictFinding` constructor
  in `analysis/` (`race.py:128`, `verdict.py:433,456,489`) — not against `resilience.py`'s
  `SILENT_FAILURE`, confirmed to be a `DegradationClass` member (PRD §19.5 fault-run
  classification), never a `Finding.type` value; no `analysis/` code constructs a `Finding`
  with that type. A hardcoded literal, not an import from `analysis/` — `scenario/` has no
  layer dependencies (`lint-imports`'s own contract), the same reason `Finding`/`RunSummary`
  in this module are `Protocol`s rather than concrete imports.
- `is_known_finding_type(finding_type: str) -> bool` — the public check, alias-resolving first.
- `known_finding_type_spellings() -> tuple[str, ...]` — sorted valid spellings, for error
  messages only.
- `eval_findings_type_count` now raises `ValueError` for an unrecognized type (defense in
  depth for any future caller reaching this function some other way).

`src/agentdx/cli/commands/run.py`'s `_parse_assert_expr` now calls `is_known_finding_type`
right after confirming the `findings.` prefix, and raises `TargetError("E-TARGET-009", ...)` —
the same error family already used for this flag's other malformed shapes — **before the run
executes**, so a typo is a `USAGE_ERROR` immediately rather than discovered only after a run
completes (matching PRD §21.3's own stated CI rationale, which this CLI-invented flag has no
grammar of its own to inherit from, but there's no reason not to hold it to the same standard).

**Verification, real subprocess, this sandbox:**

```
$ agentdx run fixtures/code_pipeline --seed 42 --assert "findings.no_state_conflicts <= 0"
✗ [E-TARGET-009] --assert 'findings.no_state_conflicts <= 0': 'no_state_conflicts' is not a
  finding type this build's analysis layer produces — use one of ('coordination_bottleneck',
  'race', 'redundancy', 'state_conflict')
EXIT: 2 (USAGE_ERROR)

$ agentdx run fixtures/code_pipeline --seed 42 --assert "findings.this_type_is_not_real >= 0"
✗ [E-TARGET-009] ... same rejection
EXIT: 2

$ agentdx run fixtures/code_pipeline --seed 42 --assert "findings.state_conflict <= 0"
✗ findings.state_conflict: 1 finding(s) of type 'state_conflict' vs. <= 0
EXIT: 1 (correctly still fails against the real seeded conflict)
```

Both audit-demonstrated false-pass cases now reject before the run executes; the correct
spelling still fails against the real, seeded `state_conflict` finding exactly as before.

**Test-coverage gap closed.** `tests/unit/scenario/test_assertions.py` gained 8 new direct unit
tests (0 existed before): exact-type match, `race` alias resolution, the exact demonstrated
typo rejected via `pytest.raises`, a fictional type rejected, a parametrized
`is_known_finding_type` table (6 real cases including the `silent_failure` non-member and a
case-sensitivity check), and `known_finding_type_spellings`'s sort/membership contract.

**Note found and fixed during repair, not in the original edit.** The first edit to
`test_assertions.py` mis-scoped an `old_string` match and orphaned the last line of the
pre-existing `test_run_shell_success_check_timeout_is_config_driven` test (`assert "timed out"
in result.detail`) outside any function, causing a `NameError` at collection/run time. Caught
by running the test file immediately after editing it (not assumed clean), fixed by restoring
the line to its original test and removing the stray duplicate.

## Prior repairs (E-SCEN-012, corrected C-13, fault-parameter boundaries)

No regression — the audit re-tested all three adversarially and confirmed they hold. No
repair action needed.

## Full verification, this pass

- `pytest tests/unit/scenario/test_assertions.py` — 40 passed (32 pre-existing + 8 new).
- `pytest tests/unit/scenario tests/integration/cli/test_assert_flag.py
  tests/integration/cli/test_scenario_run.py tests/integration/cli/test_scenario_commands.py`
  — 148 passed.
- `pytest --ignore=tests/api` (full suite) — only the pre-existing, unrelated
  `test_doctor_passes_on_a_healthy_setup` failure (D-66, Python 3.10 sandbox vs. the `>=3.12`
  pin).
- `pytest tests/acceptance/test_gates.py -m acceptance -k g1` — G1 real subprocess gate still
  passes.
- `ruff check` / `ruff format --check` — clean on all three touched files.
- `mypy --strict` — clean on `scenario/assertions.py` and `cli/commands/run.py`.
- `check_determinism_hygiene.py` / `lint-imports` — clean (only the pre-existing D-66
  `api/models.py` parse gap remains; `scenario/` contract still kept, 10/10).

## Standing status

**A fourth independent re-audit remains owed**, same pattern this project holds every other
repaired module to — this repair is self-verified by the orchestrating session, not
independently re-checked by a fresh auditor. Given this is the third consecutive audit and the
first two real findings (across the first two audits) plus this one were each closed the same
day they were found, and this pass's adversarial re-test of the two prior repairs found no
regression, the module's trend is toward stability — but that is an observation, not a
substitute for the re-audit itself.
