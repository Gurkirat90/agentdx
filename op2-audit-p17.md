# OP-2 INDEPENDENT AUDIT (FIRST PASS) — P17 `cli/`

**Auditor note on method.** Fresh read, no memory of the build. Read `AGENTS.md`, PRD §37
(CLI grammar), §44.1 (acceptance gates), §37.3 (output conventions), `CONTEXT.md` §5 row 17
and §9's D-82/D-83, and `docs/cli.md` (treated as a claim, not ground truth). Every claim
below was independently re-run against the real `agentdx` console script
(`/tmp/optb_venv`, `PYTHONHASHSEED=0`), not read off a prior report or CONTEXT.md's own
self-assessment. No source file was edited; `Read`/`Grep`/`Bash` only, plus disposable
scripts/output redirected to `/tmp`.

**Scope.** `src/agentdx/cli/` in full, heaviest scrutiny on the code named in the brief:
`_baseline.py`, `commands/compare.py`, `commands/analyze.py`, `commands/scenario_run.py`,
`commands/run.py`'s `--assert`/`direct_target_name` additions (all added/changed
2026-09-04, D-82/D-83), plus the handed-off `CliRunSummary.findings` lead from the same-day
`scenario/` OP-2. Sampled `host.py`, `main.py`, `_output.py`, `_exitcodes.py`, `_target.py`,
`doctor.py` at the depth the effort budget allows, cross-checking one path into `api/` where
the CLI's own new field-population touches a store column the API layer also reads.

---

## VERDICT

**FAIL.** Four real, independently demonstrated defects, two of them severe:

1. **(CRITICAL, demonstrated live)** The global `--json` option — PRD §37.3's own
   scriptability contract, and `_output.py`'s own module-docstring example
   (`agentdx run fixtures/code_pipeline --json | jq .verdict.class`) — silently does nothing
   on `run`, `analyze`, `compare`, and `scenario run`. Under `--json`, all four commands
   print their normal human prose to stderr and leave **stdout completely empty**. No JSON
   object is ever emitted. This is not a partial gap; it is a total contract failure across
   four of the CLI's highest-traffic commands, with zero test coverage.
2. **(HIGH, demonstrated live — the handed-off lead, confirmed real)** `CliRunSummary.findings`
   is built exclusively from `analysis.race_findings` (`state_conflict` type only) in both
   `_score_one` and `_score_reused` (`cli/commands/run.py:382,445`). `coordination_bottleneck`
   and `redundancy` findings — computed by `analysis/verdict.py` and explicitly listed as
   "known, real finding types" in `scenario/assertions.py::_KNOWN_FINDING_TYPES` — can never
   reach any assertion evaluated through the real CLI path. `--assert findings.redundancy`/
   `findings.coordination_bottleneck` are structurally dead: `>=` comparisons always fail,
   `<=`-style comparisons always vacuously pass, regardless of the run's real analysis
   output. Demonstrated against a real, already-sealed run whose `redundancy` finding
   `agentdx analyze` independently confirms exists.
3. **(MEDIUM)** `agentdx compare RUN_A RUN_B` (the two-run form) can never exit 1. There is
   no regression/tolerance logic in that code path at all — it unconditionally `return`s
   `OK`. PRD §37.1's own exit-code contract for `compare` ("0 no regression · 1 regression
   beyond tolerance") is unreachable for this form, and the CLI's own exit-code test suite
   never exercises `compare` at all.
4. **(MEDIUM)** `agentdx doctor` implements 3 of the ~9 checks PRD §37 (line 4633)
   enumerates by name (missing: data-dir writability, SQLite WAL support, DuckDB
   availability, cache integrity, last-run determinism/instrumentation-gap reporting, and
   the security-relevant "API key present in a committed file" check PRD separately calls
   out at line 4269) and never uses PRD's three-tier doctor-specific exit contract
   ("0 all pass · 1 warnings · 2 failures") — every failing check maps to exit 2, exit 1 is
   unreachable. Unlike this codebase's other stubs (which self-declare "not implemented"),
   `doctor` presents its six checks as complete, with no disclosure of the gap anywhere in
   `docs/cli.md` or `CONTEXT.md`'s P17 self-report.

**What holds.** The `--assert`/`is_known_finding_type` repair from the same-day `scenario/`
OP-2 composes correctly with `run.py` (`_parse_assert_expr` rejects unknown types as
`USAGE_ERROR` before any run starts, exactly as designed — verified live). Exit codes 0/1/2/3
/4/5/6/7 as a *table* are each individually reachable somewhere in the CLI (mostly via `run`),
`--faults`/`--assert` composition, `scenario run --repeat`'s isolated-store re-execution
semantics, `CliBaselineExecutor`'s reuse of `_execute_one`, and `doctor`'s three implemented
checks are all sound, honestly composed, and match their own documented behavior. `cli/`'s
"zero business logic" architectural rule holds structurally — no finding-detection or
verdict-computation logic was found living in `cli/` itself; every defect below is a wiring
gap (a field never populated, a branch never taken), not new analysis logic gone wrong.

---

## 1. FINDING #1 (CRITICAL) — Global `--json` silently does nothing on `run`, `analyze`, `compare`, `scenario run`

**Where:** `src/agentdx/cli/commands/run.py:921-974` (`_finish`, JSON emission only inside
`if ci:` at line 935; the plain-human branch at 972-974 runs unconditionally otherwise, with
no `out.json_mode` check anywhere in the function or its callers); `src/agentdx/cli/commands/
analyze.py:60-144` (`analyze()` — no `out.json_mode`/`out.emit_json` reference anywhere in
the file); `src/agentdx/cli/commands/compare.py:145-213` (`_print_two_run_diff`/
`_print_baseline_diff` — same, no `json_mode` check in the file); `src/agentdx/cli/commands/
scenario_run.py:82-187` (`scenario_run()` — same).

**The contract this violates.** PRD §37.3: *"`--json` on any command emits a
machine-readable object to stdout with all human output suppressed to stderr, so every
command is scriptable."* `cli/_output.py`'s own module docstring states the identical rule
and gives the canonical example: `agentdx run fixtures/code_pipeline --json | jq
.verdict.class` "must never see a progress line mixed into its stdout." `Output.line()`
correctly routes to stderr when `json_mode` is set (`_output.py:94-101`) — so the *routing*
half of the contract works. What is missing, in exactly these four commands, is the other
half: an actual JSON object ever being written to stdout at all.

**Contrast with the rest of the CLI.** `instrument.py:50-51`, `scenario.py:50-51,77-78,103-
104`, `doctor.py:234-242`, and `version.py:18-19` all correctly branch on `out.json_mode` and
call `out.emit_json(...)`. The four commands that are new or heavily rewritten today
(`compare`, `analyze`, `scenario_run`) plus `run` itself — the composition root — are the
ones missing it. This is not a partial gap sprinkled across the module; it is a clean, total
miss confined to exactly the code this audit was asked to scrutinize hardest.

**Demonstrated, real subprocess, real binary:**

```
$ agentdx --data-dir /tmp/adx_data1 --json run fixtures/code_pipeline --cache-mode replay --seed 555 \
    > /tmp/stdout.txt 2> /tmp/stderr.txt
$ echo $?
0
$ cat /tmp/stdout.txt
                                  # <-- completely empty
$ cat /tmp/stderr.txt
code_pipeline: passed
  run_id: r_d91ea3ee
  verdict: state_conflict_risk (confidence low)
Bounded search: absence of findings is not proof of absence.
```

Identical result for `analyze` and `compare`:

```
$ agentdx --data-dir /tmp/adx_data1 --json analyze r_90b839e6 >stdout 2>stderr; echo $?
0
$ cat stdout   # empty
$ cat stderr   # the full human report

$ agentdx --data-dir /tmp/adx_data1 --json compare r_90b839e6 r_6b5fd594 >stdout 2>stderr; echo $?
0
$ cat stdout   # empty
$ cat stderr   # the full human report
```

`agentdx run fixtures/code_pipeline --json | jq .verdict.class` — the exact example
`_output.py`'s own docstring uses to explain why `--json` exists — returns `jq: error
(at <stdin>:0): Cannot iterate over null` today, because stdin to `jq` is empty.

**Not the same thing as `--ci --format json`.** `run --ci --format json` does produce a
`summary.json`, but it *writes a file* under `--out` (`ci_mod.write_json_summary`,
`run.py:961-962`) — a different, correctly-documented PRD §22.4 contract. It does not
address the global `--json` option, which PRD §37 lists as applying to every command and
which this codebase's own output module treats as a first-class, universal contract.

**Test-coverage confirmation.** `grep -rln '"--json"' tests/integration/cli/` returns only
`test_scenario_commands.py` and `test_doctor.py` — zero tests exercise global `--json` on
`run`, `analyze`, `compare`, or `scenario run` anywhere in the suite. The gap has never been
exercised, let alone caught.

**Suggested fix direction (not mandatory).** Each of the four commands needs an early
`if out.json_mode: out.emit_json({...}); raise typer.Exit(...)` branch, mirroring
`doctor.py`'s pattern, emitting the same information the human branch already computes
(`_RunOutcome`/`AnalysisResult`/`CliRunSummary` for `run`; `Verdict`/`BaselineComparison` for
`analyze`/`compare`). For `run` specifically, the existing `ci_mod.ScenarioOutcome`/
`CiSummary` shape (`_finish`'s `if ci:` branch) is a ready-made JSON schema that could be
reused for plain `--json` too (write to stdout via `out.emit_json` instead of to a file),
rather than inventing a second one.

---

## 2. FINDING #2 (HIGH) — `CliRunSummary.findings` never includes `coordination_bottleneck`/`redundancy` findings; `--assert findings.<those types>` is structurally dead

**Where:** `src/agentdx/cli/_runsummary.py:47` (`findings: Sequence[RaceFinding] =
field(default_factory=tuple)` — typed to `analysis.race.Finding` specifically, not the
broader `scenario.assertions.Finding` Protocol shape); `src/agentdx/cli/commands/run.py:382`
(`_score_one`) and `:445` (`_score_reused`) — both construct `CliRunSummary` with
`findings=tuple(analysis.race_findings)` and nothing else. `analysis.race_findings` comes
from `detect_conflicts(events)` (`cli/_analyze.py:120,167`) — `analysis/race.py` exclusively.

**What's missing.** `analysis/verdict.py`'s `verdict()` computes a *separate* set of
findings — `Verdict.findings: tuple[VerdictFinding, ...]` (`analysis/verdict.py:246`),
populated from `_coordination_bottleneck_findings` (types `"coordination_bottleneck"`,
lines 421-468) and `_redundancy_findings` (type `"redundancy"`, lines 471-506). This is
reachable on `AnalysisResult.verdict.findings` (`cli/_analyze.py:70`, `analyze_events`'s
return) — but nothing in `cli/commands/run.py` ever merges it into `CliRunSummary.findings`.
`scenario/assertions.py::_KNOWN_FINDING_TYPES` (line 438-440) explicitly lists
`{"state_conflict", "coordination_bottleneck", "redundancy"}` as the three real, producible
finding types this build has — i.e. the CLI's own type-validation layer *advertises*
`coordination_bottleneck`/`redundancy` as legitimate `--assert findings.<type>` targets
(and `known_finding_type_spellings()` surfaces them in the CLI's own error message when a
user mistypes something). The producer for two of those three types simply never reaches the
object the check runs against.

**Demonstrated, real subprocess, same sealed run, both commands:**

```
$ agentdx --data-dir /tmp/adx_data1 run fixtures/code_pipeline --cache-mode replay
code_pipeline: passed
  run_id: r_90b839e6
  ...

$ agentdx --data-dir /tmp/adx_data1 analyze r_90b839e6
r_90b839e6: state_conflict_risk (confidence low)
  [low] read_file executed 2x concurrently; 0ms and 0 tokens wasted
  -> `read_file` executed 2x concurrently; 0ms and 0 tokens wasted. Memoise the tool or
     assign it to one agent.
```

`analyze` (which reads `analysis.verdict.findings` directly, bypassing `CliRunSummary`
entirely — see "Not affected" below) confirms a real `redundancy` finding exists on this run.
Now assert against the identical, reused run via `run --assert`:

```
$ agentdx --data-dir /tmp/adx_data1 run fixtures/code_pipeline --cache-mode replay \
    --assert "findings.redundancy >= 1"
code_pipeline: failed
  run_id: r_90b839e6
  (reused: identical seed/scenario/graph already ran; not re-executed — D-80)
  verdict: state_conflict_risk (confidence low)
  ✗ findings.redundancy: 0 finding(s) of type 'redundancy' vs. >= 1
$ echo $?
1
```

`0 finding(s) of type 'redundancy'` against a run that `analyze` independently proves has
one. The same structural bug means `--assert "findings.redundancy <= 0"` (or any
upper-bound-style check) would silently, vacuously **pass** on the same run — the exact
false-green shape the same-day `scenario/` OP-2 already flagged and fixed once for
*unrecognized* types (`op2-audit-p08-third.md`); this is the identical failure mode one
layer up, for a type the validator correctly recognizes as real, because the type's own
*producer* was never wired to the object the validator checks.

**Blast radius, worked through concretely (not left as a hand-wave):**

- **`--assert findings.coordination_bottleneck`/`findings.redundancy`** (`cli/commands/
  run.py`): always dead, per above. `>=` assertions always fail; `<=`/count-based assertions
  against these two types always vacuously pass.
- **Scenario-file built-in `max_findings`** (`scenario/assertions.py::eval_max_findings`,
  reads `run.findings` — the same `CliRunSummary.findings`): a scenario declaring
  `max_findings: {severity: critical, count: 0}` will pass even if the run has real
  critical/high `coordination_bottleneck` or `redundancy` findings, because those findings
  are invisible to `run.findings` through the real CLI path. This is a silent false negative
  on exactly the assertion PRD §21.7 designs to catch a coordination regression.
- **`scenario run --repeat`'s reproducibility signature** (`cli/commands/
  scenario_run.py:72-73`, `_reproducibility_signature` reads `outcome.summary.findings`):
  G4's "same failure classification, same cascade shape" claim is, in practice, blind to any
  change in `coordination_bottleneck`/`redundancy` finding shape across repeats — a
  narrower reproducibility check than the command's own docstring claims.
- **`compare RUN_A RUN_B`'s printed findings count** (`compare.py:164`,
  `len(analysis_a.race_findings)`/`len(analysis_b.race_findings)` directly): same
  undercount, informational-display-only in this one spot.
- **Not affected:** `agentdx analyze`'s own printed verdict (`analyze.py:130`, `for finding
  in verdict.findings:` — reads `AnalysisResult.verdict.findings` directly, never goes
  through `CliRunSummary`) is correct and does show `coordination_bottleneck`/`redundancy`
  findings, as demonstrated above. `no_state_conflicts`/`no_silent_failures` built-ins are
  also unaffected in practice (they only ever match `state_conflict`/`silent_failure`
  respectively, neither of which this gap touches — though `silent_failure` has its own,
  separate, pre-existing "no `Finding` ever carries that type" issue in `scenario/`, out of
  this module's scope, already implicitly documented by `_KNOWN_FINDING_TYPES`'s own
  comment).

**Test-coverage confirmation.** `tests/integration/cli/test_assert_flag.py` exercises
`findings.race`/`findings.state_conflict` only (`grep -n "findings\." tests/integration/cli/
test_assert_flag.py` — no occurrence of `coordination_bottleneck` or `redundancy` anywhere in
the file, or anywhere else under `tests/integration/cli/`). Zero coverage for two of the
three "known" finding types the codebase itself advertises as real.

**Why this isn't a trivial one-line fix.** `VerdictFinding` (`analysis/verdict.py:209-219`)
does not structurally satisfy `scenario.assertions.Finding` as-is: `severity` is typed
`Severity` (an enum), not `str`, and the evidence is `evidence: Evidence` (`Evidence.
event_seqs: tuple[int, ...]`), not a flat `evidence_seq` attribute — unlike
`analysis.race.Finding`, whose docstring explicitly notes it structurally satisfies the
Protocol. A correct fix needs a small adapter (e.g. a thin wrapper or a `evidence_seq`
property + `.severity.value`), not a naive
`tuple(analysis.race_findings) + tuple(analysis.verdict.findings)` concatenation, which
would fail the `Finding` Protocol's `runtime_checkable` shape check the moment anything
actually iterates a merged sequence expecting `.evidence_seq`.

**Suggested fix direction (not mandatory).** In `_score_one`/`_score_reused`
(`cli/commands/run.py:376-408,439-455`), build `CliRunSummary.findings` from both
`analysis.race_findings` and an adapted view of `analysis.verdict.findings`, converting each
`VerdictFinding` to something exposing `type: str`, `severity: str`,
`evidence_seq: Sequence[int]` (e.g. `finding.evidence.event_seqs`,
`finding.severity.value`). Add direct tests asserting `--assert
findings.coordination_bottleneck`/`findings.redundancy` actually see a nonzero count on a
run known to produce one (this run, `r_90b839e6`, already does, via `redundancy`).

---

## 3. FINDING #3 (MEDIUM) — `agentdx compare RUN_A RUN_B` can never exit 1, regardless of an actual regression

**Where:** `src/agentdx/cli/commands/compare.py:145-171` (`_print_two_run_diff`) —
unconditionally `return OK` at line 171. No call to `cli.ci.check_regression`, no tolerance
comparison, no branch that could produce any exit code other than `OK`/`NOT_FOUND` for this
form.

**The contract this violates.** PRD §37.1: *"`agentdx compare RUN_A RUN_B [--json]
[--tolerance-file FILE] [--force]` — Metric deltas, findings added/removed, verdict change.
Refuses when scenario hashes differ unless `--force`. Exit: 0 no regression · 1 regression
beyond tolerance."* The two-run form's exit code is documented as a real pass/fail signal.
In this build it is always 0.

**Partially, but not fully, disclosed.** The module's own docstring and `docs/cli.md:166-167`
both say `--tolerance-file`/`--force` are "not yet wired... passing either prints a warning
and is otherwise a no-op" — so the *flags* being no-ops is disclosed. What is not stated
anywhere (not in the docstring, not in `docs/cli.md`, not in `CONTEXT.md`'s D-82/G6 rows) is
the direct consequence: the command's exit code can *never* reflect a regression at all, with
or without those flags, because no regression-detection logic exists in this code path
regardless of flags. A CI job written straight from PRD §37.1's own exit-code table
(`agentdx compare "$BASE" "$HEAD"; if [ $? -eq 1 ]; then fail; fi`) would silently never fire,
with no warning printed to explain why.

**Confirmed, real subprocess** (two distinct runs, same fixture, different seeds — not a
degenerate self-comparison):

```
$ agentdx --data-dir /tmp/adx_data1 compare r_90b839e6 r_6b5fd594; echo $?
r_90b839e6  vs.  r_6b5fd594
  verdict       state_conflict_risk      state_conflict_risk
  score         None                     None
  findings      1                        1
  makespan_ms   0                        0
0
```

`_print_two_run_diff`'s own source (read directly, not just inferred from this one test) has
no code path capable of returning anything but `OK` — this holds for *any* two run ids, not
just this pair; the live run above is a spot-check of what the source already proves.

**Also worth noting (not scored as a second sub-finding, same code path):** the two-run
form's own printed `findings` row (`compare.py:164`) is subject to Finding #2's undercount
(`len(analysis.race_findings)` only) — a smaller, informational-only instance of the same
root cause, listed here rather than as its own numbered item since the display, not a
pass/fail decision, is what's affected.

**Test-coverage confirmation.** `tests/integration/cli/test_exit_codes.py` — the suite
`CONTEXT.md` cites as proving "all 8 §37.2 exit codes each proven by ≥1 test asserting real
process exit status" — never invokes `compare` at all (`grep -n "compare" tests/integration/
cli/test_exit_codes.py` returns nothing); the ASSERTION_FAILURE=1 test that does exist
exercises `run` only. The ledger's claim is true for the exit-code table as a whole but does
not cover `compare`'s own PRD-specified exit semantics, which are untested and, per the
source, unimplementable as currently written.

**Suggested fix direction (not mandatory).** Either wire `_print_two_run_diff` to
`cli.ci.check_regression`'s tolerance engine (using `REGRESSION_TOLERANCES` defaults when no
`--tolerance-file` is given) and return `ASSERTION_FAILURE` on a real violation, or — the
smaller change — print an explicit warning in the no-flags case too ("no regression
tolerance configured; this comparison is informational only, exit code is always 0"), the
same self-disclosure pattern `run.py`'s own `--jobs`/`--fail-on` stubs already use, so the
gap in the exit-code contract is visible rather than silently inherited from PRD's text.

---

## 4. FINDING #4 (MEDIUM) — `agentdx doctor` implements a minority of PRD's named checks and never uses the documented "warnings" exit tier, with no disclosure of either gap

**Where:** `src/agentdx/cli/commands/doctor.py:208-219` (`run_checks` — exactly six checks:
`python-version`, `langgraph-version`, `hash-seed`, `cache-db`, `store-migration`, `port`);
`:34-41` (`Check` dataclass — a plain boolean `ok`, no severity tier); `:250`
(`raise typer.Exit(code=OK if not failed else USAGE_ERROR)` — every failure maps to exit 2,
exit 1 is unreachable from this command).

**The contract this falls short of.** PRD §37 (line 4633): *"Checks: Python version;
`PYTHONHASHSEED`; data dir writability; SQLite version and WAL support; DuckDB availability;
port availability; cache integrity; last run's determinism quality, instrumentation gaps and
residual; whether an API key is present in a committed file. Prints a fix for every failure.
Exit: 0 all pass · 1 warnings · 2 failures."* PRD line 4269 separately calls out the same
API-key check as a named Design Constraint 4 requirement: *"`agentdx doctor` warns if an API
key appears in `agentdx.toml` or in a committed file."*

Implemented: Python version ✓, `PYTHONHASHSEED` ✓, port availability ✓ (plus two reasonable,
PRD-unnamed extras: `langgraph-version`, `store-migration`). **Missing entirely:** data-dir
writability, SQLite WAL-mode support, DuckDB availability, cache *integrity* (the existing
`cache-db` check only tests file existence, never integrity), last-run determinism
quality/instrumentation-gap/residual reporting, and the API-key-in-a-committed-file check —
six of PRD's nine named items, including the one security-relevant one PRD calls out twice.
The three-tier exit contract specific to `doctor` is also collapsed to two tiers: nothing in
`Check`'s shape or `doctor()`'s exit logic can ever produce exit 1.

**Not disclosed, unlike this codebase's own stub convention.** Every other unbuilt command in
`cli/` (`replay`, `export`, `import`, `cache *`, `baseline update`, `bench`, `scenario new`)
self-declares via `_not_implemented(...)`, prints "not implemented — owned by prompt X", and
exits 2 naming the reason (`main.py:75-87`) — the codebase's own established honesty pattern
for an incomplete command (AGENTS.md §2: "If something cannot be completed, it is reported as
NOT DONE, not shipped as a stub"). `doctor` instead presents its six checks as the finished
article: its own module docstring calls itself a check suite that "makes the first five
minutes survivable," `docs/cli.md:109-113` describes "Six checks, each reading a fact this
module did not invent" with no comparison to PRD's longer list, and `CONTEXT.md`'s P17
self-report says only "`agentdx doctor` verified against three deliberately broken setups...
4/4 passing" — no mention anywhere of the six missing PRD-named checks or the missing
warning tier.

**Severity note.** This is lower-stakes than Findings #1-2 (doctor is a diagnostic aid, not a
correctness-critical execution path), but it is a real, silent gap against both the PRD text
and this project's own stated honesty standard, and the missing API-key check specifically
has a genuine security angle PRD calls out by name.

**Suggested fix direction (not mandatory).** Either implement the remaining PRD-named checks
(the API-key-in-committed-file scan is the highest-value one to prioritize, being both
explicitly named twice in the PRD and a real security hygiene gap) and add a `severity:
"warning" | "failure"` field to `Check` so `doctor()` can actually reach exit 1, or — the
smaller, honest change matching this codebase's own convention — state plainly in
`docs/cli.md` and `CONTEXT.md`'s ledger which of PRD's named checks are implemented and which
are deferred, the same way every other unbuilt surface in `cli/` already does.

---

## 5. Checked and found clean / composes correctly

- **The `scenario/` OP-2 repair composes correctly with `run.py`.** `_parse_assert_expr`
  (`run.py:210-239`) calls `is_known_finding_type` and raises `TargetError`/`USAGE_ERROR`
  before any run executes — verified live: `--assert "findings.bogus_type >= 1"` and
  `--assert "findings.no_state_conflicts <= 0"` (the exact typo the prior audit demonstrated)
  both correctly reject with `E-TARGET-009` at parse time, never reaching the run at all.
- **`CliBaselineExecutor` (`cli/_baseline.py`)** composes `cli.commands.run._execute_one`
  directly (no reimplementation), uses a genuine throwaway `Store` (`tempfile.
  TemporaryDirectory`), and correctly raises `UnsupportedBaselineTargetError` for any target
  other than `code_pipeline` rather than fabricating a comparison — confirmed by reading the
  full 155-line file; no shortcuts found.
- **`scenario_run.py --repeat`** correctly forces a fresh, isolated `Store` per repeat
  (avoiding the D-80 reuse path that would make a "N/N identical" claim vacuous) — the design
  reasoning in its own docstring is accurate to what the code does.
- **`cli/host.py` (`CliRunHost`)** — read in full; the previously-fixed `cache=` threading and
  D-80 sealed-run-collision handling are both present and match their documented behavior;
  no new defect found in the time budgeted.
- **Exit codes 0/2/3/4/5/6/7** as individual values are each reachable and correctly mapped
  from `_exitcodes.py`'s single authoritative table (no drift from PRD §37.2's literal
  numbers found anywhere in `cli/`).
- **`--json`/`direct_target_name` cross-check into `api/`:** `cli.commands.run._run_and_score`'s
  new `direct_target_name` threading (D-83) now populates `RunRecord.scenario_id` for
  direct-target CLI runs with a fixture name rather than a real stored scenario id. This makes
  `api/routes/runs.py`'s own documented assumption ("`POST /api/runs` always sets
  `scenario_id`, so [an unresolvable one] is not reachable through the shipped API today")
  stale in a narrow sense. Traced through: `inject_fault`'s I12 chaos-authorization check
  (the one place an unresolvable `scenario_id` would raise `ScenarioUnresolvableForChaosError`,
  a 409) is gated by `record.status != "running"` earlier in the same function
  (`api/routes/runs.py:560-561`) — and a CLI-created run is always already sealed
  (`complete`/`failed`) by the time it could be queried, since the CLI executes synchronously.
  **This is a suspicion, not a confirmed finding**: I could not construct a live failure
  through the shipped API given that gate, only a latent inconsistency between two modules'
  assumptions about the same store column. Worth a one-line note to whoever next touches
  either `cli/`'s `scenario_id` population or `api/`'s I12 check, not a repair item on its own.

---

## SUMMARY FOR A REPAIR SESSION WITH NO CONTEXT

Four real defects, ranked by severity:

1. **`run`/`analyze`/`compare`/`scenario_run` all ignore global `--json`** — stdout is
   completely empty, all output goes to stderr regardless. Fix: add an `if out.json_mode:
   out.emit_json(...)` branch to each, mirroring `doctor.py`'s pattern. Zero existing test
   coverage for this — add it.
2. **`CliRunSummary.findings` (`cli/commands/run.py:382,445`, `cli/_runsummary.py:47`) only
   ever contains `state_conflict` findings**, never `coordination_bottleneck`/`redundancy`
   from `analysis.verdict.findings` — making `--assert findings.coordination_bottleneck`/
   `findings.redundancy` structurally dead (always-fail or always-vacuous-pass) despite being
   advertised as valid, known finding types. Also silently narrows the scenario-file
   `max_findings` built-in and `scenario run --repeat`'s reproducibility signature. Fix
   needs a small `VerdictFinding` → `Finding`-Protocol adapter (severity enum → `.value`,
   `evidence.event_seqs` → `evidence_seq`), not a naive concatenation.
3. **`compare RUN_A RUN_B` can never exit 1** — no regression/tolerance logic exists in that
   code path; it always returns `OK`. PRD's own exit-code contract for `compare` is
   unimplemented, and untested.
4. **`doctor` implements 3 of PRD's ~9 named checks** (missing, notably, the API-key-in-a-
   committed-file check PRD names twice) and never reaches its own documented "1 = warnings"
   exit tier — undisclosed anywhere, unlike this codebase's other honestly-labeled stubs.

Everything else sampled — the same-day `scenario/` repair's composition with `run.py`,
`CliBaselineExecutor`, `scenario_run --repeat`'s isolation design, `host.py`, the exit-code
table's individual values — held under adversarial re-test. The module's "zero business
logic in `cli/`" architectural rule holds: every defect above is a wiring gap, not new
analysis/decision logic gone wrong.
