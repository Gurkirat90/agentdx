# OP-2 INDEPENDENT AUDIT — G3 (deterministic replay) and G5 (decomposition invariant)

**Auditor:** independent session, no prior involvement in this project (per `CONTEXT.md` §0's
OP-2 definition — "best run by a different model than built the module").

**Scope.** `tests/determinism/test_replay_equality.py`, `tests/determinism/_subprocess_runner.py`,
`tests/determinism/_harness.py`, `src/agentdx/runtime/scheduler.py` (the `run()`/`_choose()`
control flow and `DeterminismGuard` install site), `src/agentdx/runtime/determinism.py`
(`DeterminismGuard`, `trap`), `src/agentdx/events/canonical.py` (`canonical_log_hash`,
`canonical_projection`), `src/agentdx/events/schema.py` (`Volatility`/`EVENT_FIELDS`),
`docs/determinism-guarantees.md`; `tests/analysis/test_decomposition_invariant.py`,
`src/agentdx/analysis/overhead.py` (`decompose_critical_path`), `src/agentdx/analysis/timing.py`
(`build_timing_dag`, `_build_edges`, `virtual_makespan_ms`), `agentdx.toml`'s
`[analysis] residual_tolerance`. Also read, for the mechanised layer both gates sit under:
`tests/acceptance/test_gates.py::_run_gate`. Cross-checked but not independently re-audited:
`src/agentdx/analysis/redundancy.py`, `src/agentdx/runtime/faults/`, and every module CONTEXT.md
already records as independently audited elsewhere (`op2-audit-p06-second.md`,
`op2-audit-p07-second.md`, etc.) — those findings are taken as given, not re-derived here.

**Method.** Read `CONTEXT.md` §0 (OP-1/2/3 role definitions), the relevant parts of §5 (rows 6 and
10), §6 (the G3/G5 rows and the never-waived list), §8 (ADR-001, C-1, C-19), §9 (D-50, D-52, D-89),
§11 (tripwires, especially 19), and §13, before reading any product code. Read every file above
end to end, tracing control flow rather than trusting docstrings, per the assignment. Read
`op2-audit-docker-cold-start.md` as the format/rigor template this report follows. Attempted live
execution (see below).

**Environment constraint, confirmed directly.** `python3 --version` in this audit's own sandbox
reports `Python 3.10.12`; the project's `pyproject.toml`/`.python-version` require `>=3.12,<3.13`.
`uv python install 3.12` was attempted once and failed with a network error reaching
`github.com` (`astral-sh/python-build-standalone`) — `tunnel error: unsuccessful` — matching the
already-established constraint exactly. No further attempts were made, per the assignment's own
instruction. A real attempt to collect both gates' test files under the available `python3.10`
was made anyway (see "NOT DONE" below): both fail identically at import time,
`ModuleNotFoundError: No module named 'agentdx'` — the package itself is not installed in this
sandbox, so even partial collection is blocked. **This audit is therefore a code read, not a live
re-run of either gate**, exactly the scope boundary the G10 audit (`op2-audit-docker-cold-start.md`)
already established for this project.

---

## VERDICT: G3 — **PASS WITH NOTES**

The literal gate test genuinely does what PRD §10.1/§44.1 and its own docstring claim: it spawns
≥10 real, fresh OS processes; the artifact compared is a real, comprehensive canonical-log hash,
not a narrow proxy; the seed is genuinely and non-decoratively threaded into the scheduler's sole
scheduling-decision point; `DeterminismGuard` genuinely wraps the entire replay path; and the
project's own named tripwire-19 failure mode (trusting a subprocess exit code alone) does **not**
apply to this test as written, and was separately found and closed at the mechanised
acceptance-harness layer. No code path was found where this test could report PASS despite a real
divergence.

The notes that keep this from a bare PASS: (1) the mechanised acceptance-gate wrapper's floor for
G3 is weaker than "all three DoD assertions hold" — it only requires 1 of 3; (2) the literal gate
test itself exercises a hand-built synthetic scenario, not a real reference fixture or the real
`agentdx run` path — a materially stronger, real-fixture determinism result exists, but lives
elsewhere, self-reported only, and is not cross-referenced from the G3 row a reader would consult.
Given G3 is one of PRD §44.3's six never-waived gates, both notes are worth a deliberate,
recorded decision rather than silent acceptance — see Findings #1 and #2.

## VERDICT: G5 — **PASS WITH NOTES**

The test genuinely loads all three required real golden fixtures (not synthetic or contrived
data) and genuinely computes `virtual_makespan_ms` from an independent, VirtualClock-derived
source (`cli/host.py`'s `self._clock.virtual_ms()`, stamped once at run-seal time) — not from the
same arithmetic being validated, and not from wall-clock time, correctly following `CONTEXT.md`
C-1's ruling. However, two things materially qualify what "G5 PASS on all three fixtures" proves
today: the top-level summing identity the test re-asserts is guaranteed to hold by construction
(honestly disclosed in the module's own docstring, not a hidden defect, but not independent
evidence either), and the golden fixtures the test loads are — by the project's own 2026-09-07
investigation (D-89) — stamped under a scheduler stub that "makes no determinism claim," not the
real `Scheduler` `agentdx run` uses today, with an empirically confirmed structural difference.
The genuinely substantive part of the gate (`residual_fraction < 2%`) is real and non-trivial, but
its current margin rests on a specific, disclosed interpretive ruling (C-19) adopted specifically
because the PRD-literal reading fails this same gate on all three fixtures. G5 is not on PRD
§44.3's never-waived list, which lowers the stakes relative to G3, but the same findings would
recur unchanged the next time this gate is evaluated until the underlying fixtures are
regenerated against the real scheduler.

---

# PART A — G3 findings

## FINDING G3-1 (MEDIUM) — the mechanised acceptance gate's `min_pytest_passed=1` floor for G3 is weaker than "the DoD holds"; a regression in 2 of the file's 3 tests would still report PASS

**Where:** `tests/acceptance/test_gates.py:242` — `_run_gate("G3", ["pytest",
"tests/determinism/test_replay_equality.py"], min_pytest_passed=1)`.

`tests/determinism/test_replay_equality.py` contains three test functions, each asserting one of
the file's own stated "DEFINITION OF DONE" clauses: (a) 100 runs at seed 42 (≥10 fresh processes)
byte-identical, (b) two same-seed runs print an identical `schedule_decision` sequence, (c)
different seeds produce different interleavings. `_run_gate`'s `min_pytest_passed` mechanism —
built specifically to close CONTEXT.md §11 tripwire 19 ("an acceptance-gate test that shells out
to a subprocess trusts the exit code alone," D-70) — parses the subprocess's own JUnit XML and
requires `total - failures - errors - skipped >= min_pytest_passed`. For G3 this is set to `1`,
not `3`. Concretely: if test (b) or test (c) regressed (e.g., a future change accidentally
desynchronises the scheduler's seeded RNG from the DoD's declared behaviour, or a merge conflict
silently drops the `causes=` edge that exercises the vclock-merge path — exactly the class of bug
`_harness.py`'s own docstring says this scenario exists to catch) while test (a) kept passing, the
JUnit `<testsuite>` would report `tests="3" failures="1"`, `passed_count = 2`, and
`2 >= 1` is `True` — the mechanised G3 gate would still assert PASS and write `met: true`-shaped
output to `.results/G3.json`.

This does not mean G3 is currently failing anything — it means the specific safety net tripwire 19
was built to install (D-70: "an exit code alone cannot distinguish 'the property held' from
'nothing was actually checked'") is present but calibrated to the weakest floor that clears the
"not literally zero" bar, rather than to this file's own, already-enumerated three-clause DoD.
Since `min_pytest_passed` already threads a real number through per-gate (G1/G4 use it too,
elsewhere in the same file, at their own values), raising G3's specifically to `3` is a one-line
change with no discovered downside.

**Severity:** MEDIUM. No live divergence was found to be currently masked — this is a latent gap
in the gate's own strictness, not a demonstrated false PASS. Given G3's never-waived status, I
would treat this as worth closing before the next gate mechanisation change, not as urgent as a
live defect.

## FINDING G3-2 (MEDIUM, disclosure/traceability) — the literal G3 test proves scheduler-level determinism on a synthetic scenario; the stronger per-fixture, real-CLI-path result exists but is not cross-referenced from the G3 row, is self-reported only, and was measured on the wrong Python version

**Where:** `tests/determinism/_harness.py` (module docstring: "no LLM, no graph, no fixture" —
"design constraint 6"); `bench/harness/replay_determinism.py:17-23` (its own docstring, quoted
below); `bench/results/replay-determinism.json`; `CONTEXT.md` §6 row for G3 (line 185).

`tests/determinism/test_replay_equality.py` — the literal command PRD §44.1/CONTEXT.md §6 cites
for G3 — runs against a hand-built scenario (`_harness.py`: four fake agents, direct
`Scheduler.spawn()` calls, no LLM, no LangGraph, no fixture). This is a real, honest, and
non-trivial test of the scheduler's own `_choose()`/seeding mechanism (see Positive Controls
below) — but it is not a test of whether a real reference fixture, run twice through the actual
`agentdx run` CLI path, replays byte-identically. That stronger claim is measured separately, in
`bench/harness/replay_determinism.py`, whose own docstring states the distinction plainly:

> "That test already proves gate G3 (100/100 identical) against a synthetic four-fake-agent
> scenario... it is a scheduler-level determinism proof, not a per-fixture PRD §34.2 measurement."

The committed result (`bench/results/replay-determinism.json`) does show `"met": true` for all
three real fixtures (`code_pipeline`, `support_triage`, `research_fanout`), 100/100 identical each,
via the real production path (`cli.commands.run._run_direct_target`) — a genuinely stronger result
than G3's own literal test. But: (a) it is self-reported only, from the same 2026-09-08 P18.4
session that also self-reports G5 — no independent OP-2 has touched it; (b) its own committed
`"environment"` block reads `"python": "3.10.12"` — i.e., this evidence was itself produced outside
the project's own required `>=3.12,<3.13` interpreter range, the identical environment gap this
audit's own sandbox has; and (c) `CONTEXT.md`'s G3 row (§6, line 185) cites only
`pytest tests/determinism/test_replay_equality.py` and says nothing about this broader,
per-fixture result — a reader relying on the G3 row alone would not learn that a stronger, real-CLI
claim exists, nor that it carries its own, separate self-report/Python-version caveats.

None of this is hidden by the code — `replay_determinism.py`'s docstring is explicit about the
relationship — but PRD §44.3 calls G3 never-waived, and the row that is supposed to be the single
place a reader checks its status does not surface either the scope-narrowness of its own literal
test or the existence/caveats of the stronger evidence sitting one directory over.

**Severity:** MEDIUM. Not a correctness defect in either test — both do what they claim — but a
gap in what the never-waived gate's own status row communicates, on a project whose stated culture
is exactly "surface it in your response either way" (`CONTEXT.md` §0.6).

## FINDING G3-3 (LOW, doc drift) — `docs/determinism-guarantees.md` §8 cites a test file that does not exist

**Where:** `docs/determinism-guarantees.md:163` — `PYTHONHASHSEED=0 pytest
tests/determinism/test_leak_detection.py -v`.

`tests/determinism/test_leak_detection.py` does not exist anywhere in the repository (confirmed by
a repo-wide grep — the only hit for the string `test_leak_detection` is this doc line itself).
Leak-detection tests do exist, under `tests/unit/runtime/test_determinism.py`
(`test_a_leak_raises_...`, `test_the_same_leak_only_warns_in_non_strict_mode`, etc.) — the doc's
own claim about *what is tested* is accurate, only the *path* is wrong. The document is also
stamped "Last updated: P06" and still describes `agentdx run` as "not built yet," which was true
at P06 but is superseded (with its own real, disclosed caveats) by P17's later CLI work.

**Severity:** LOW. Does not affect gate correctness; a reader following this doc's own runnable
snippet verbatim would get a `pytest` collection error, not a false result.

## Positive controls checked — G3

- **≥10 genuinely fresh OS processes, not reused or mocked.** `test_replay_equality.py:55-65`
  calls `subprocess.run([sys.executable, "-m", "tests.determinism._subprocess_runner",
  str(_SEED)], ..., check=True)` inside a `for _ in range(_SUBPROCESS_REPLAYS)` loop
  (`_SUBPROCESS_REPLAYS = 10`) — each iteration is a fresh `subprocess.run` call, i.e. a new
  interpreter process, not one process invoked once and inspected ten times. `check=True` means a
  non-zero exit raises `CalledProcessError` and fails the test outright, not silently.
- **The comparison is a full canonical event-log hash, not a narrow proxy.**
  `events/canonical.py::canonical_log_hash` (lines 278-292) is a rolling blake2b-256 over every
  event's `canonical_bytes`, in order — order-sensitive, one hash per event, chained with a
  separator specifically to prevent event re-partitioning. `canonical_projection`
  (lines 241-265) includes every field whose `Volatility` is `STABLE` — confirmed by reading
  `events/schema.py`'s `EVENT_FIELDS`: only `run_id` (`IDENTITY` — excluded because two replays
  *must* differ here, with the exclusion rationale stated directly in the field's own `doc=`) and
  `wall_ts_ms` (`VOLATILE` — real elapsed time) are excluded from the fields checked; `seq`,
  `sched_step`, `virtual_ts_ms`, `vclock`, `type`, `span_id`, `causal_parents`, `fault_id`, and
  every payload field marked canonical are all included. This is not a narrow comparison that
  could pass while real nondeterminism exists elsewhere in the log.
- **The seed is genuinely, non-decoratively threaded through, not merely accepted and ignored.**
  `_harness.py::build_scenario_scheduler(seed)` constructs `Scheduler(..., seed=seed, ...)`.
  `scheduler.py::run()` (lines 838-846) builds `guard = trap(seed=self._seed, ...)` and, inside
  `with guard:`, sets `self._rng = guard.seeded_random` (line 874). `_choose()` (the scheduler's
  **only** scheduling-decision point, per its own docstring) uses
  `runnable[self._rng.randrange(len(runnable))]` (line 1379) whenever policy is `"random"` (the
  harness's setting). This was independently, non-tautologically confirmed by
  `test_different_seeds_produce_different_interleavings` (lines 121-149): it runs 8 distinct seeds
  and asserts `len(distinct_sequences) > 1` — i.e., it would fail loudly if the seed had no real
  effect on scheduling, not merely assert that a seed parameter was accepted.
- **`DeterminismGuard` genuinely covers the entire replay path being tested, not a subset of it.**
  `scheduler.py::run()` wraps `await self._scheduler_loop()` — the entire task-dispatch loop, from
  the `RUNNING` transition to the root task's completion — inside `with guard:` (lines 871-875).
  `run_scenario_async` (the function both the in-process and (indirectly, via
  `_subprocess_runner.py`) fresh-process replays call) does nothing but
  `await scheduler.run(_root(scheduler))` — there is no code path in this test that constructs a
  scheduler and drives it without going through `run()`, so there is no way to bypass the guard
  from within this test.
- **Tripwire 19 (exit-code-only trust) does not apply to the literal test, and was found and
  closed at the mechanised-acceptance layer.** In `test_replay_equality.py` itself, subprocess
  success is checked two ways, not one: `check=True` (raises on non-zero exit) *and* the actual
  printed stdout content (`result.stdout.strip()`) is what feeds the real equality comparison
  (`len(set(hashes)) == 1`) — a subprocess that silently exited 0 having printed nothing, or the
  wrong thing, would produce a hash mismatch and a loud, informative assertion failure, not a
  silent pass. Separately, at the mechanised acceptance-harness layer,
  `tests/acceptance/test_gates.py`'s own module comments (lines 57-66) name tripwire 19 explicitly
  and describe the exact D-70 fix (`min_pytest_passed` + JUnit XML, not terminal-text regex) —
  confirmed present and wired for G3 (Finding G3-1 concerns its calibration, not its absence).
- **Fail-closed on an unpinned hash seed, not silently tolerant.** `strict_determinism=True` is set
  in the harness's `SchedulerConfig` (`_harness.py:116`); `DeterminismGuard.install()`
  (`determinism.py:571-579`) raises `DeterminismLeakError` in strict mode if `PYTHONHASHSEED` is
  not pinned to `"0"`, rather than warning. Running the literal gate command bare (no `just`, no
  `PYTHONHASHSEED=0` in the environment) therefore errors loudly rather than risking a false
  determinism claim — confirmed independently by `tests/acceptance/test_gates.py`'s own comment
  (lines 101-111), which records exactly this being discovered by the repo owner.

---

# PART B — G5 findings

## FINDING G5-1 (LOW-MEDIUM, disclosed-but-worth-restating) — the test's top-level summing assertion is guaranteed to hold by construction; it cannot detect the failure mode it appears to test

**Where:** `tests/analysis/test_decomposition_invariant.py:46-47` (`total = sum(dec.bucket_ms.
values()) + dec.residual_ms; assert abs(total - dec.virtual_makespan_ms) <= 1`);
`src/agentdx/analysis/overhead.py:334-342`.

`decompose_critical_path` computes `residual_ms = dag.virtual_makespan_ms - cp.length_ms` (line
334), then checks `if abs(total - dag.virtual_makespan_ms) > 1: raise OverheadAnalysisError(...)`
(lines 336-342) — **before** constructing or returning the `OverheadDecomposition` object the test
later inspects. Because the function itself raises on this exact condition, and the test does not
catch that exception, any `dec` object the test's line 47 can ever see has already, necessarily,
passed this check inside the function that produced it. The test's own re-assertion of the same
inequality (line 47) is therefore dead code for this specific failure mode: it can never observe
a `dec` where the identity is violated, because such a `dec` is never returned in the first place
— the test would instead fail with an unhandled `OverheadAnalysisError` at line 41 (the call to
`decompose_critical_path`), not with an `AssertionError` at line 47.

This is the same *shape* of risk the assignment names via the project's own D-50/C-19/P11-finding
precedent (a computed value compared against itself rather than an independent expectation) —
though here it is not hidden: the module's own docstring states plainly that "the first identity
holds by construction... and is asserted at the bottom of `decompose_critical_path`, not merely
hoped for (Design Constraint 2)," and the test file's own comment (line 44) says the same:
"already raised inside `decompose_critical_path` if this were violated — re-asserted here as the
test's own independent check, not just trusting the analyser didn't raise" — which is honest about
*intent* but does not change that the re-assertion cannot independently catch anything the
production code's own internal raise did not already catch. The internal raise is real
defense-in-depth (it protects every caller of `decompose_critical_path`, not just this test), so
this is not a false-PASS risk in practice — a real classification bug in `_classify_node`/
`_classify_edge` would still surface, just as a test *error*, not a test *failure*, at a different
line than the one that looks like the check. Also observe: `dec.residual_flagged`
(asserted `False` at test line 54) is itself defined as `residual_fraction >= tolerance`
(`overhead.py:352`) — i.e., line 54 re-tests the exact same boolean as line 50
(`residual_fraction < residual_tolerance`), via a derived field, not a second independent signal.

The genuinely substantive, non-tautological part of the gate is the `< 2%` threshold itself
(Finding G5-2 below), which is **not** guaranteed to hold by construction — `residual_fraction`
depends on the real ratio of `cp.length_ms` to `dag.virtual_makespan_ms` on real fixture data, and
has previously actually failed this exact threshold under a different, real code path (see below).

**Severity:** LOW-MEDIUM. No evidence this masks a live defect — the identity is real algebra that
does hold given the classification code is correct, and the internal raise means a violation is
still caught (as an error) rather than silently passed. Flagged because a reader taking "the test
asserts the invariant" at face value would reasonably assume the test *itself* is the thing
providing that guarantee, when the guarantee actually lives one layer down, unconditionally, in
production code the test cannot meaningfully add to for this specific line.

## FINDING G5-2 (MEDIUM, re-affirms and sharpens the already-ruled C-19) — the current <2% PASS margin on all three fixtures depends on a specific, disclosed interpretive reclassification that was adopted specifically because the PRD-literal reading fails this same gate

**Where:** `CONTEXT.md` D-50/C-19 (§9/§10); `src/agentdx/analysis/timing.py` (`run_boundary` edge
construction, ~line 760, citing D-50/C-19 directly in its own comment); `src/agentdx/analysis/
overhead.py`'s `_classify_edge` (routes `run_boundary` weight to `blocking_wait`).

Verified directly in code (not merely taken from the ledger's own account): `timing.py`'s
`_build_edges` computes `run_boundary` edge weight as a real, non-zero gap to `run_start`/
`run_end` (`max(0, node.start - run_start.vts)` / `max(0, run_end.vts - node.end)`), and its own
comment cites CONTEXT.md D-50 and C-19 by name as the source of this deliberate divergence from
PRD §16.1.1's literal edge table, which specifies weight `0` for this edge. Per C-19's own
recorded text, reverting to the PRD-literal `weight_ms=0` reading was "checked directly" and
produces residual fractions of **8.5% / 6.3% / 10.0%** on the three golden fixtures — all several
times over the 2% gate. The currently shipped behaviour (non-zero weight, routed to
`blocking_wait`) is what brings all three fixtures under 2% today.

This is not a new discovery — it is already ruled (C-19) and disclosed in the code's own comments
— but it means "residual < 2%, on all three fixtures" is not an interpretation-free measurement of
how completely the run is instrumented; it is contingent on a specific, still-PRD-unreconciled
choice about where a run's own lead-in/trail-off time is booked, one that was adopted *because* the
alternative reading fails this exact gate. A reader of the bare "G5 PASS" claim, without this
context, would reasonably assume residual is uniformly small because the system is well
instrumented, not because of where one category of edge weight is classified.

**Severity:** MEDIUM. Re-stated here (not newly discovered) because the assignment specifically
asked this history be checked against the current test, and because it directly and materially
determines today's pass/fail margin on every fixture this gate covers.

## FINDING G5-3 (MEDIUM-HIGH) — the golden fixtures this gate tests are stamped by a scheduler stub that "makes no determinism claim," not the real `Scheduler` the product actually runs today; the project's own investigation found a structurally different real-scheduler log

**Where:** `CONTEXT.md` D-89 (§9); `tests/golden/{code_pipeline,research_fanout,
support_triage}.jsonl` (the exact files `test_decomposition_invariant.py:26-28` loads);
`fixtures/_harness.py::FixtureRunHost.open_run()`; `sdk/generic.py:866`
(`RunContext.create`'s `scheduler=None` default → `ImmediateScheduler()`).

`test_decomposition_invariant.py` loads `tests/golden/*.jsonl` directly (`_load`, lines 25-28) —
these are the same committed files D-89 investigated. D-89 (2026-09-07) traced, empirically, that
`fixtures/_harness.py` never passes `scheduler=` to `RunContext.create`, so every committed golden
log was stamped under `ImmediateScheduler()` — whose own docstring, per D-89's account, calls
itself "the no-scheduler default... makes no determinism claim" — never the real
`runtime.scheduler.Scheduler` that `cli/host.py::CliRunHost` (the real `agentdx run` path)
explicitly constructs and passes instead. D-89's own live comparison (re-running
`code_pipeline` through the real, unmodified CLI twice) found a **structural**, not cosmetic,
difference: 61 events under the real scheduler vs. 48 in the golden file, with the extra 13 being
`schedule_decision` events the fixture harness never emits at all, `sched_step` genuinely grouping
multiple events per real decision (vs. the golden file's synthetic `sched_step = seq`), and a
different canonical log hash entirely.

`decompose_critical_path`'s six-bucket classification is directly sensitive to exactly this kind of
structure — node/edge construction in `timing.py` depends on span nesting, `agent_step_segment`
boundaries, and edge weights all derived from the sequence and grouping of real events. Since the
golden fixtures G5 tests against do not reflect what the real, current scheduler produces for the
same fixtures, "G5 passes on all three fixtures" is a true statement about a synthetic,
provisional corpus (already labelled `"PROVISIONAL... Regenerate at P07"` in the fixtures' own
`golden_findings.json`, per D-89), not (yet) a statement about what the current production
`agentdx run` path would produce for the same inputs. D-89 itself records this as "open —
investigated, not built, deliberately," recommending real-scheduler regeneration as its own scoped
follow-up given the blast radius (every consumer of `evidence.seq`, every hand-checked expectation
in `checks.py`, etc.) — this audit did not find evidence that follow-up has since landed.

**Severity:** MEDIUM-HIGH. This is the most consequential G5 finding: it is not about whether the
arithmetic is correct (it is, given the inputs) but about whether the inputs the gate is graded
against are still representative of what the gate's own criterion ("on all three fixtures") is
understood to mean by a reader — the real fixtures, run for real. It is fully disclosed in the
ledger (D-89), which is a mitigating factor, but it sits one layer away from the G5 row itself and
would change the residual computation (in an unknown direction, per fixture) if closed.

## FINDING G5-4 (LOW, doc drift) — CONTEXT.md's "15/15 across `tests/analysis/`" figure is stale by an order of magnitude; the narrower "4/4 in that file" figure still checks out

**Where:** `CONTEXT.md` §6 (G5 row, line 187: "4/4 in that file, 15/15 across
`tests/analysis/`"), dated to the 2026-08-18 P10 build session.

Counted directly: `tests/analysis/test_decomposition_invariant.py` collects exactly 4 test items
(3 parametrize cases of `test_decomposition_invariant_holds` over `_FIXTURES`, plus
`test_every_bucket_traces_to_evidence_seq`) — the "4/4" figure is accurate today. The broader "15/15
across `tests/analysis/`" figure is not: the top-level `tests/analysis/*.py` files alone (excluding
the `race/` subdirectory) now contain 111 `def test_*` functions across 11 files (`test_verdict.py`
alone has 43); including `tests/analysis/race/` the total is 171. This is expected given the
codebase has grown through P11/P12/P18 since 2026-08-18, and the ledger's own text scopes the claim
to "this session" — but the row has not been updated or annotated as historical in the ~3 weeks and
several build sessions since, on a file that is otherwise updated same-day for far smaller changes.

**Severity:** LOW. Does not affect gate correctness — purely a stale self-report figure a careless
reader could mistake for current test-suite size.

## Positive controls checked — G5

- **All three required fixtures are genuinely covered, from real (not synthetic) data.**
  `_FIXTURES = ("code_pipeline", "research_fanout", "support_triage")` (line 22),
  `@pytest.mark.parametrize("fixture_name", _FIXTURES)` (line 31), `_load` (lines 25-28) reads
  `tests/golden/{name}.jsonl` and decodes every line through the real `events.canonical.
  decode_event` — not a hand-constructed or trivially-contrived event set.
- **`virtual_makespan_ms` is an independent ground-truth value, not self-referentially computed by
  the code under test.** `timing.py::build_timing_dag` reads it directly off the log:
  `makespan = _int_field(run_end.payload, "virtual_makespan_ms")` (line 431) — a field the schema
  marks `derived=True` and stamps once, at run-seal time, in `cli/host.py:401` as
  `virtual_makespan_ms = self._clock.virtual_ms()`. This is a real `VirtualClock` read, genuinely
  separate from `overhead.py`'s bucket/residual arithmetic, and genuinely distinct from the
  schema's separate `wall_makespan_ms` field (confirmed both fields exist independently in
  `events/schema.py`), correctly implementing C-1's "virtual, not wall clock" ruling.
- **Evidence traceability (I6) is a real, non-vacuous check, not merely present.**
  `test_every_bucket_traces_to_evidence_seq` (lines 60-71) asserts every bucket with `ms > 0`
  carries a non-empty `bucket_evidence_seq` of real integer `seq`s — checked against
  `support_triage`, one of the three fixtures (not all three, a minor scope note, not a separate
  finding).
- **The <2% threshold is read from configuration, not hardcoded inline** (tripwire 5):
  `agentdx.toml:64` sets `residual_tolerance = 0.02`, and `overhead.py::_load_residual_tolerance`
  reads it at call time rather than a magic number appearing in `decompose_critical_path` itself.

---

## NOT DONE / RISKS

- **No live execution of either gate.** This sandbox has Python 3.10.12; the project requires
  `>=3.12,<3.13`. `uv python install 3.12` was attempted once and failed on a blocked network path
  to `github.com` — confirmed, not assumed. A direct attempt to collect (not even run) either test
  file under the available `python3.10` was made and failed identically for both:
  `ModuleNotFoundError: No module named 'agentdx'` (the package is not installed in this sandbox).
  Installing the full dependency stack (LangGraph, FastAPI, etc.) to get further was judged out of
  this audit's scope, consistent with the G10 audit's own precedent of not chasing a live re-run
  once blocked once. Everything in this report is a static trace of source against its own
  committed docs/results, not a fresh, independent execution of either gate.
- **`bench/results/replay-determinism.json`'s real-fixture, real-CLI-path determinism claim
  (Finding G3-2) was read and traced to its producing code, but not re-executed** — it is cited as
  existing, self-reported, better evidence than G3's own literal test covers, not independently
  re-verified numerically.
- **D-89's own empirical comparison (Finding G5-3) was read and traced, not reproduced.** This
  audit did not attempt to run `code_pipeline` through the real scheduler a third time to confirm
  the 61-vs-48-event divergence independently; D-89's account is detailed enough (exact counts,
  exact hash prefixes, the specific new event type) to be taken as a credible, already-cited
  primary source rather than something needing re-derivation for this audit's purposes.
- **`research_fanout` and `support_triage` were not individually checked against the real
  scheduler** — D-89 itself discloses this same narrower scope (only `code_pipeline` was
  empirically re-run); Finding G5-3 above inherits that same boundary rather than overstating it to
  all three fixtures.
- **G3-1's predicted gap (a 2-of-3-tests-pass scenario still reporting mechanised PASS) was not
  demonstrated live** — e.g., by deliberately breaking `test_two_same_seed_runs_...` and confirming
  `_run_gate` still asserts G3 PASS. This is a direct trace of `_run_gate`'s own arithmetic
  (`passed_count >= min_pytest_passed`, `min_pytest_passed=1`), not an empirical reproduction,
  for the same reason live execution was unavailable throughout this audit.
- **Every other acceptance gate (G1, G2, G4, G6-G10) and every other `tests/analysis/` file are out
  of this audit's scope**, per the assignment — this report makes no claim about them.
- **This audit's own findings have not themselves been independently re-audited** — per this
  project's own stated culture (`CONTEXT.md` §0), that re-check is a separate, future OP-2's job.
