"""tests/acceptance/ — one executable test per PRD §44.1 global MVP acceptance gate.

**What this package is not.** It does not re-implement gate logic. Each test below shells
out to the *exact* verification command PRD §44.1's table names for that gate and asserts
the process exits 0 — nothing more clever than that. The binding definition of "gate met" is
CONTEXT.md's own: "Gates are met only when the command exits 0 in CI on a clean checkout."
A test in this package that passed for any other reason (parsing stdout for a hopeful
substring, calling an internal function instead of the CLI, catching and downgrading a
non-zero exit) would be exactly the kind of test §7 of the owning prompt exists to distrust.

**Status as of 2026-09-04 (updated from the 2026-08-27 P18/19 QA-acceptance session that
originally wrote this package — see CONTEXT.md §6 for the current row-by-row ledger, the
single source of truth this note summarises rather than duplicates).**

- G1, G4, G6, G7 — **green.** All four were blocked on the same root cause: PRD §44.1's own
  literal verification commands name CLI surfaces (`run --assert`, `scenario run --repeat`,
  `compare RUN_ID --baseline`, `analyze RUN_ID --scorecard`) that P17 had deliberately
  stubbed, not broken. All four are now real (`cli/commands/run.py`, `cli/commands/
  scenario_run.py`, `cli/commands/compare.py`, `cli/commands/analyze.py`, and — for G6/G7 —
  `cli/_baseline.py`'s `CliBaselineExecutor`, a real scripted single-agent execution for
  `fixtures/code_pipeline` through the real `Scheduler`/`CliRunHost`, registered by target
  name so an unsupported target fails honestly rather than fabricating a comparison). Each
  gate's own test function carries the fuller account and the integration tests that cover
  it off the acceptance-suite path.
- G9 — never waived (I7), and **green**: re-run for real 2026-09-03 (Python 3.12, real
  `just`, repo owner's machine) after D-62 task #25's dispatch gap (candidate beta) and
  D-80/ADR-020 (the `run_id` reuse-and-print ruling for a sealed-row collision) both landed.
  `just demo-offline` exits 0 end-to-end for the first time in this project's history.
- G10 — still red: `bench/harness/docker_cold_start.py`, the script `just bench-docker-cold`
  calls, does not exist yet. No Docker build has been attempted (out of scope: no new
  features, and this sandbox has no Docker daemon to build against either).

G2, G3, G5, G8 have real, already-built verification paths and are expected to pass in a
real CI environment with Python 3.12 and Node installed. This sandbox itself has neither
Python 3.12 nor, for G8, a browser Playwright can drive — its own local run of this package
is therefore not a substitute for the real CI job `just acceptance` becomes wired into
(`.github/workflows/ci.yml`); every claim above of "real hardware" or "green" traces to an
actual run on the repo owner's own Python-3.12 machine, cited by date, not to this sandbox.
"""
