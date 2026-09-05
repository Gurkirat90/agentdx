# OP-3 REPAIR REPORT — first P17 `cli/` audit (`op2-audit-p17.md`)

**Repaired same day as the audit, by the orchestrating session (not the audit agent — the
audit agent used only `Read`/`Grep`/`Bash`, no `Edit`/`Write`).**

## Finding #1 (CRITICAL) — global `--json` silently did nothing on `run`/`analyze`/`compare`/`scenario run`

**Repair.** `cli/commands/run.py::_finish` now builds the same `ci_mod.CiSummary` object
whenever `ci` **or** `out.json_mode` is set (previously only under `ci`), and calls
`out.emit_json(summary.as_dict())` under `--json` regardless of `--ci` — the two are
independent contracts (`--ci` writes files, `--json` writes to stdout) and can now coexist.
`analyze.py`, `compare.py` (both diff functions), and `scenario_run.py` each gained an
`if out.json_mode: out.emit_json({...})` branch emitting the same information their human
branch already prints, matching `doctor.py`'s existing pattern. Human output is unaffected —
`Output.line()` already correctly routed it to stderr under `json_mode`; only the missing
stdout object was the defect.

**Verified, real subprocess:** `agentdx --json run fixtures/code_pipeline --seed 42 | jq
.scenarios[0].status` and equivalents for `analyze`/`compare`/`scenario run` all now produce
valid JSON on stdout, human prose on stderr. 5 new integration tests
(`tests/integration/cli/test_json_output.py`) assert `json.loads(result.stdout)` succeeds and
`result.stderr` is non-empty, for all four commands (`compare` tested in both forms).

## Finding #2 (HIGH) — `CliRunSummary.findings` never included `coordination_bottleneck`/`redundancy`

**Repair.** `cli/_runsummary.py` gained `_AdaptedVerdictFinding` (a frozen dataclass adapting
`analysis.verdict.VerdictFinding`'s `severity: Severity` enum and `evidence.event_seqs` shape
to the `scenario.assertions.Finding` Protocol's `severity: str`/`evidence_seq` shape) and
`combined_findings(result: AnalysisResult) -> tuple[Finding, ...]`, merging
`analysis.race_findings` with an adapted view of `analysis.verdict.findings`. Both
`cli.commands.run._score_one`/`_score_reused` now build `CliRunSummary.findings` via
`combined_findings(analysis)` instead of `tuple(analysis.race_findings)` alone.
`compare.py::_print_two_run_diff`'s printed findings count (an informational-display instance
of the same root cause, per the audit's §2) was fixed the same way.

**Blast radius closed, all three consumers named in the audit:**
- `--assert findings.coordination_bottleneck`/`findings.redundancy` now see real counts.
- The scenario-file `max_findings` built-in (`scenario/assertions.py::eval_max_findings`,
  reads `run.findings`) is no longer blind to these two finding types.
- `scenario run --repeat`'s reproducibility signature (`_reproducibility_signature`, reads
  `outcome.summary.findings`) now genuinely covers all three finding types, not just races.

**mypy note.** `analysis.race.Finding` and `_AdaptedVerdictFinding` are both frozen
dataclasses; the `Finding` Protocol's bare-attribute declarations imply settable variables to
mypy's static checker even though a frozen field satisfies the Protocol at runtime
(`runtime_checkable`, read-only usage only). Resolved via `cast`, matching
`cli/_analyze.py`'s own existing precedent for the identical Protocol-vs-frozen-dataclass gap
— not a design change, just telling the static checker what the runtime shape check already
guarantees.

**Verified, real subprocess:** `--assert "findings.redundancy >= 1"` against
`fixtures/code_pipeline --seed 42` (a run `analyze` independently confirms has a real
`redundancy` finding) now passes with count 1, where it previously silently reported 0.
`--assert "findings.redundancy <= 0"` now correctly fails (previously vacuously passed). 2 new
tests in `test_assert_flag.py`, 2 new tests in `test_compare_baseline.py` (count fix +
disclosure below).

## Finding #3 (MEDIUM) — `compare RUN_A RUN_B` could never exit 1

**Repair, the smaller/disclosed option** (the audit offered either a full regression-engine
wiring or an honest disclosure; a full wiring would need `VerdictFinding`/severity-adapted
metrics for two arbitrary, possibly-unrelated runs with no shared "scenario" identity to key a
tolerance table on — real scope beyond this pass's budget, and speculative without a concrete
worked design). `_print_two_run_diff` now prints an explicit warning on every invocation:
*"compare RUN_A RUN_B is informational only in this build — no regression tolerance is
evaluated, so the exit code is always 0 regardless of the deltas above."* Matches this
codebase's own established pattern (`--tolerance-file`/`--force`'s existing disclosure in the
same function). Module docstring updated with the same disclosure. **Not closed as a defect,
disclosed as a documented, honest gap** — a real regression-tolerance engine for this form
remains a fair thing for a future prompt to build.

**Verified, real subprocess:** the warning prints on every `compare RUN_A RUN_B` invocation,
confirmed live. 1 new test.

## Finding #4 (MEDIUM) — `doctor` implements 3 of ~9 PRD-named checks, undisclosed

**Repair, the smaller/disclosed option** (same reasoning as Finding #3 — implementing the six
missing checks, especially the API-key-in-a-committed-file scan, is a fair-sized real feature,
not a wiring fix). Added a "Coverage gap, stated plainly" section to `doctor.py`'s module
docstring and to `docs/cli.md`'s `agentdx doctor` section, naming exactly which PRD checks
exist (6) and which don't (6, including the security-relevant API-key scan PRD names twice),
and noting the exit-code contract is two-tier, not the documented three-tier. **Not closed as
a defect, disclosed as a documented, honest gap** — matching this codebase's own convention
for every other unbuilt CLI surface (`replay`, `export`, `cache *`, etc., which self-declare
via `_not_implemented(...)`).

## What held, no repair needed

Per the audit's own §5: the same-day `scenario/` `--assert` repair composing correctly with
`run.py`; `CliBaselineExecutor`; `scenario_run --repeat`'s isolation design; `host.py`; the
individual exit-code table values; `cli/`'s "zero business logic" architectural rule. The
`direct_target_name`/`api/` I12 cross-check was explicitly flagged by the audit as "a
suspicion, not a confirmed finding" — no live failure was constructed, only a latent
inconsistency between two modules' assumptions about the same store column, gated by a
condition (`record.status != "running"`) that a synchronous CLI-created run always satisfies
by the time it's queryable. Left as a one-line note for whoever next touches either `cli/`'s
`scenario_id` population or `api/`'s I12 check — not repaired here, no reproduction exists.

## Full verification, this pass

- `pytest tests/integration/cli tests/unit/scenario` — all pass except the pre-existing,
  unrelated `test_doctor_passes_on_a_healthy_setup` (D-66, Python 3.10 sandbox).
- Full suite (`tests/api` excluded, D-66) — same single pre-existing failure, no new
  regressions.
- `pytest tests/acceptance/test_gates.py -m acceptance -k "g1 or g4 or g6 or g7"` — 4/4 still
  pass, real subprocess.
- `ruff check`/`ruff format --check` — clean on every touched file (also opportunistically
  fixed 3 pre-existing, unrelated lint findings in `test_assert_flag.py`'s docstrings,
  discovered while checking this pass's own new tests in the same file — not part of this
  audit's scope, but real and cheap to fix while already there).
- `mypy --strict` — clean on every individually-checked touched file (`cli/commands/run.py`,
  `analyze.py`, `compare.py`, `scenario_run.py`, `doctor.py`, `cli/_runsummary.py`).
  `mypy --strict src/agentdx/cli/` as a whole package still hits the pre-existing D-66
  `api/models.py` Python-3.10-vs-PEP-695 parse crash, unrelated to this repair, same standing
  gap every prior row this session carries.
- `check_determinism_hygiene.py`/`lint-imports` — clean (only the same D-66 `api/models.py`
  parse gap; `scenario/`/`cli/` contracts still kept, 10/10).
- 10 new/updated integration tests, all passing: `test_json_output.py` (new, 5 tests),
  `test_assert_flag.py` (+2), `test_compare_baseline.py` (+2 finding-count/disclosure tests).

## Standing status

**A second independent re-audit remains owed**, same pattern every other repaired module in
this ledger carries — this repair is self-verified by the orchestrating session, not
independently re-checked by a fresh auditor. Findings #3 and #4 were deliberately disclosed
rather than fully built out, given the effort budget for a 10-module audit queue this session
is working through — a future prompt could reasonably close either as real feature work, not
just a repair.
