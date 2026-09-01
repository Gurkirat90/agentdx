#!/usr/bin/env bash
# ---------------------------------------------------------------------------------------
# commit-item5.sh — the .dockerignore fix, the OP-2 findings report, and the ledger
# rollover that was already made.
#
# Run from the checkout root:   bash commit-item5.sh
# Nothing is pushed.
#
# AFTER committing, the measurement that decides whether G10's timing half is closed:
#
#     just bench-docker-cold
#
# WARNING: that prunes the machine-wide Docker builder cache (docker builder prune -af).
# For a first look without the prune, run the harness directly with --no-prune; it will
# tell you the result is not G10.
# ---------------------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

echo "=== pre-flight ==="
uv run python scripts/check_ledger.py
uv run python scripts/check_bench_markers.py
uv run python scripts/check_fixture_finding_evidence.py

echo
echo "=== 1. the .dockerignore fix ==="
git add .dockerignore Dockerfile
git commit -F - <<'MSG'
P19(packaging): add .dockerignore -- 1,135 MB of build context, and a latent clobber

PRD sections: §39.2, §39.4, §44.1 G10.

There was no .dockerignore. Docker transfers the whole build context to the daemon before
the first layer runs, so every `docker build` shipped the entire 1.2 GB checkout across the
Docker Desktop virtualization boundary before anything compiled.

Measured against the real tree:

    context WITHOUT .dockerignore : 1,135.1 MB   39,001 files
    context WITH    .dockerignore :     3.4 MB      245 files
    removed                       : 1,131.7 MB   38,756 files  (99.70%)

The bulk was _to_delete/ (418M), .venv-review/ (226M), .venv/ (215M),
frontend/node_modules/ (212M) and .mypy_cache/ (55M) -- none of which the image reads.

This matters for G10 specifically. Its only cold measurement is 181.092 s against a 180 s
threshold: a second, independent failure alongside D-62. Context transfer is a plausible
large share of that. NOT CLAIMED AS FIXED -- `just bench-docker-cold` is the measurement,
and it has not been re-run.

SECOND, MORE SERIOUS: this also closes a correctness bug that was passing by luck.
`COPY frontend/ ./` runs AFTER `RUN npm ci`, so without node_modules excluded the host's
macOS tree overwrote the container's freshly installed Linux one. The build succeeded only
because the host tree carries all 23 @esbuild platform variants including linux-arm64, so
esbuild still resolved a usable binary. `npm ci`'s entire output was being discarded, and
the image carried every platform's binaries plus all devDependencies -- relevant to §39.4's
sub-500MB target, which has never been measured. The Dockerfile now states this dependency
explicitly, because deleting .dockerignore reintroduces it silently.

Also corrects a claim in the Dockerfile header. It stated as fact that "LangGraph's
parallel fan-out deadlocks the single-task scheduler loop". d62-design.md §3 downgrades
that to a hypothesis with an experiment attached: the scheduler grants one event-loop tick
per resumption and recognises only scheduler-created Futures as suspension, so any await on
real async machinery needing more than one tick deadlocks regardless of concurrency. Fan-out
may be incidental. Unrun either way.

Verified: every path the Dockerfile COPYs survives the filter (14/14 checked
programmatically); node_modules, .venv, _to_delete, .git and .mypy_cache are all excluded.
docs/ is deliberately NOT excluded -- the Dockerfile needs docs/openapi.json, and relying on
a `!` re-inclusion after a directory exclusion is an ordering subtlety that breaks quietly.

Gate status: G10 still FAILS. This attacks its timing half only; the populated-run-list half
remains blocked on D-62.

ADRs: none.
MSG

echo
echo "=== 2. the OP-2 findings and the ledger rollover ==="
git add op2-findings-p19.md CONTEXT.md docs/journal/2026-33.md commit-item5.sh
git commit -F - <<'MSG'
P19(audit): OP-2 findings F-12/F-13/F-14 against this session's own work

A partial, NON-INDEPENDENT audit. CONTEXT.md §0 requires an OP-2 be run by a session that
did not build the module; this one wrote every artefact it examines. A real OP-2 is still
owed. Read-only per §0: no code changed, no ruling edited.

F-12 (high) -- C-34's "replace the orphaned run" branch cannot be implemented. C-34 was
ruled and committed in 99beb11 an hour before this audit, justified by "I2 protects a
SEALED run's event log". That is false. events_no_delete
(store/migrations/m0001_initial.py:75) is an unconditional BEFORE DELETE trigger; it never
consults sealed_at. store/sqlite.py's own docstring names the failure mode: the guarantee
holds "from this process, from the API reader, from sqlite3 on the command line, or from a
later prompt that forgets". Escapes checked and closed: no trigger on `runs` (deleting only
the row orphans its events, and findings REFERENCES runs(run_id)); PRD §27.5's
`agentdx prune` does not exist (only `cache prune`, an exit-2 stub); the migration
trigger-drop window is a precedent, not a licence.

F-13 (high) -- D-78 was never an open product decision. run_id is Volatility.IDENTITY
(events/schema.py:262-271), a mark that exists specifically for it, documented as "differs
by construction between two otherwise identical executions". The field doc is explicit:
"two replays of the same run must differ here or they collide on runs.run_id ... Ruling R1."
Two consequences: (a) run_id is excluded from the canonical projection, so an attempt
component costs I1 NOTHING -- the I1 objection this session raised against the
attempt-counter option, and used to rank it last of four, was wrong; (b) make_run_id
(runtime/scheduler.py:1177), being purely content-derived, contradicts a week-1 schema
ruling. G3 never catches it because the determinism suite drives Scheduler directly and
never reaches CliRunHost.open_run -> store.create_run. Worth its own test.

F-14 (medium) -- the R-series rulings are cited as authority and defined nowhere. R1 gates
run_id's volatility mark (3 source + 2 doc sites); R4 gates float rejection (4 sites across
events/ and runtime/cache/). `grep -c "R1" CONTEXT.md` -> 0. No R-series table exists in
CONTEXT.md §10, docs/event-schema.md or docs/journal/. Substance is restated inline at each
citation so nothing is wrong today, but §0.5's precedence order cannot rank a ruling absent
from the ledger.

Consequences, NOT yet repaired: d78-plan.md §2-§3 is superseded; C-34 needs an appended
amendment (C-11/C-13 precedent -- never a silent edit); the attempt-counter resolution is
now corroborated by the codebase rather than a compromise.

Twenty-eighth §13 rollover: two oldest rows (2026-08-25 P17, 2026-08-25 P16+OP-2+OP-3)
moved to docs/journal/2026-33.md verbatim, clearing room for C-35 and D-81. Those two
rulings are NOT written yet -- the space is cleared and unused, deliberately, pending a
decision on whether a third session should perform the repair rather than the session that
wrote, audited and would now be fixing its own work.

ADRs: none.
MSG

echo
echo "=== DONE ==="
git log --oneline -4
echo
echo "Then, to measure what this bought:"
echo "    just ci"
echo "    just bench-docker-cold      # prunes the machine-wide builder cache"
