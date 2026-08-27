"""tests/acceptance/ — one executable test per PRD §44.1 global MVP acceptance gate.

**What this package is not.** It does not re-implement gate logic. Each test below shells
out to the *exact* verification command PRD §44.1's table names for that gate and asserts
the process exits 0 — nothing more clever than that. The binding definition of "gate met" is
CONTEXT.md's own: "Gates are met only when the command exits 0 in CI on a clean checkout."
A test in this package that passed for any other reason (parsing stdout for a hopeful
substring, calling an internal function instead of the CLI, catching and downgrading a
non-zero exit) would be exactly the kind of test §7 of the owning prompt exists to distrust.

**Why several of these are expected to fail right now, honestly.** Per CONTEXT.md §5/§6/§7
(as of the 2026-08-27 P18/19 QA-acceptance session that wrote this package):

- G1 — `agentdx run fixtures/code_pipeline ...` hits a documented, undstubbed `sdk/`-layer
  bug: nothing in `sdk/` ever calls `runtime.scheduler.Scheduler.spawn()`, so LangGraph's
  parallel fan-out deadlocks the scheduler (exit 5). The underlying detection logic is
  covered by `tests/analysis/race/test_gate_g1.py` against the golden fixture log, but that
  is not this gate's literal §44.1 command, and this package does not substitute it.
- G4 — the scenario YAML's literal `scenario run ... --repeat 20` invocation does not exist
  as a CLI surface; `scenario`'s real subcommands are `validate`/`list`/`expand`/`new` (P17).
- G6, G7 — `agentdx compare` and `agentdx analyze` are explicit P17 stubs (exit 2,
  "not yet implemented"), correctly out of P17's declared scope, not silently no-op'd.
- G9 — `just demo-offline` calls `agentdx run fixtures/...` directly, so it inherits G1's
  `sdk/`-spawn deadlock the moment a real (non-golden-log) run is attempted.
- G10 — `bench/harness/docker_cold_start.py`, the script `just bench-docker-cold` calls,
  does not exist yet. No Docker build has been attempted by this prompt (out of scope: no
  new features).

G2, G3, G5, G8 have real, already-built verification paths and are expected to pass in a
real CI environment with Python 3.12 and Node installed. This sandbox itself has neither
(see CONTEXT.md's NOT DONE section for the P18/19 session) — its own local run of this
package is therefore not a substitute for the real CI job `just acceptance` becomes wired
into (`.github/workflows/ci.yml`).
"""
