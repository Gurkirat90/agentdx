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

- G1 — the deadlock this note used to cite (`sdk/` never calling `Scheduler.spawn()`) is
  fixed: ADR-017 wired `spawn()`/`join()` in, and D-62 task #25's candidate beta
  (`d62-design.md` §8.7, ADR pending as of 2026-09-03) closed the dispatch gap that kept
  `code_pipeline` deadlocking after that. `agentdx run fixtures/code_pipeline` now completes
  end-to-end (verified via `CliRunner`, in-process, in this sandbox). The gate's own literal
  command is still blocked, for an unrelated, pre-existing reason found while checking this:
  `agentdx run` has no `--assert` option at all (`--assert findings.race >= 1` is PRD §44.1's
  literal invocation) — the same class of gap G4 already documents below (a §44.1 command
  that does not exist as a real CLI surface). Whether this gate would pass once `--assert`
  exists is unverified here — the underlying race-detection algorithm is covered by
  `tests/analysis/race/test_gate_g1.py` against the golden fixture log, a different, narrower
  claim than this gate's literal command, and this package does not substitute one for the
  other.
- G4 — the scenario YAML's literal `scenario run ... --repeat 20` invocation does not exist
  as a CLI surface; `scenario`'s real subcommands are `validate`/`list`/`expand`/`new` (P17).
- G6, G7 — `agentdx compare` and `agentdx analyze` are explicit P17 stubs (exit 2,
  "not yet implemented"), correctly out of P17's declared scope, not silently no-op'd.
- G9 — never waived (I7). Re-run for real 2026-09-03 (Python 3.12, real `just`, repo owner's
  machine) now that D-62 task #25's dispatch gap is closed (candidate beta): `just
  demo-offline` reaches `agentdx run fixtures/code_pipeline` for real and fails at
  `exit 5, StoreError [E-STORE-010] run 'r_50f3b68c' already exists` — **D-78/D-80**, not a
  new bug. `run_id` is a pure content hash of `(seed, scenario_hash, graph_hash)` (§6.1), the
  store is append-only (I2), and re-running the identical fixture at the identical seed
  collides with a row this same machine already wrote. The owner ruled the resolution on
  2026-09-01 (D-80, **C-34**: reuse-and-print against a sealed row, replace against an
  unsealed one) but it was never built — D-80's own row says so explicitly ("implementation
  owed... blocks G9 independently of D-62"). This is the first real run to reach this path:
  every earlier attempt hit either D-62's deadlock or D-77's since-fixed fixture-resolution
  gap first, so D-80's ruling sat unimplemented but also un-blocking nothing visibly, until
  candidate beta closed the one gap standing in front of it. G9 cannot pass until D-80 is
  implemented, independently of everything else in this list.
- G10 — `bench/harness/docker_cold_start.py`, the script `just bench-docker-cold` calls,
  does not exist yet. No Docker build has been attempted by this prompt (out of scope: no
  new features).

G2, G3, G5, G8 have real, already-built verification paths and are expected to pass in a
real CI environment with Python 3.12 and Node installed. This sandbox itself has neither
(see CONTEXT.md's NOT DONE section for the P18/19 session) — its own local run of this
package is therefore not a substitute for the real CI job `just acceptance` becomes wired
into (`.github/workflows/ci.yml`).
"""
