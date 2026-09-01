#!/usr/bin/env bash
# ---------------------------------------------------------------------------------------
# commit-d78.sh — record the D-78 ruling and the two design specs.
#
# One commit. Everything here is ledger and documentation; no source file is touched, so
# the whole-repo hooks (mypy, import-linter, determinism-hygiene) have nothing to check and
# the ordering problem that broke commit-p19.sh's first run cannot recur.
#
# Run from the checkout root:   bash commit-d78.sh
# Nothing is pushed.
# ---------------------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

echo "=== pre-flight: the three checkers this commit must not break ==="
uv run python scripts/check_ledger.py
uv run python scripts/check_bench_markers.py
uv run python scripts/check_fixture_finding_evidence.py

echo
echo "=== commit ==="
git add CONTEXT.md docs/journal/2026-33.md d78-plan.md d62-design.md commit-d78.sh
git commit -F - <<'MSG'
P19(ledger): rule D-78 reuse-and-print -- C-34, C-33, D-80

Records the owner's D-78 ruling and two design specs. No source changes.

C-34 -- D-78's resolution. A run_id collision against a SEALED prior run is reused and
printed with that run's own exit code; a collision against an UNSEALED row is replaced.
The split matters and was not in the four candidate resolutions: the run row is written
before execution (cli/host.py:213, status="running"), so every machine that has hit D-62
holds an unsealed orphan. Reusing one would print nothing and exit 0 -- reporting success
for a run that never happened. Replacing it does not violate I2, which protects a sealed
run's event log; PRD §27.3 already reasons that an interrupted run is not analysable.

Exit codes stay a MAJOR contract: reuse replays the prior verdict's code, never a blanket
0. A re-invocation must not turn a previously-red CI run green.

run_id stays purely content-derived (I1) and the store stays append-only (I2). No --force.

D-80 -- records the selection in §9 without altering D-78's row, which append-only forbids.

C-33 -- the PRD §38.3 doc-naming ruling, recorded LATE. It was drafted in the P19 build
session's handover patch and never applied by the session that took it up, so
docs/architecture.md shipped in 0cac940 with no ruling behind it. Found by grepping for
C-33 rather than trusting the handover. The existing filenames are authoritative; §38.3's
value is its table's coverage, not its filenames.

d78-plan.md -- implementation spec. Not implemented; its own prompt. One open sub-question
needs an ADR: whether --ci/JSON output marks a reused run distinguishably from a fresh one.

d62-design.md -- design only, three options costed, plus a framing correction this ledger
has carried since P17. D-62 is recorded as "nothing calls spawn()", but sdk.generic.Scheduler
-- the Protocol the whole SDK programs against -- has only yield_point and no spawn at all
(sdk/generic.py:261-271; the string "spawn" appears nowhere in sdk/). The SDK cannot spawn.
Closing D-62 therefore starts with a public-interface change and needs an ADR.

The doc also states, as a HYPOTHESIS WITH AN EXPERIMENT rather than a finding, that the
deadlock is not caused by parallel fan-out: _resume_task grants one event-loop tick per
resumption and only recognises scheduler-created Futures as suspension, so any await on
real async machinery needing more than one tick deadlocks regardless of concurrency. The
observed empty wait_reason is consistent with this. §3 names the one test that settles it.
Run it before choosing a design.

Twenty-seventh §13 rollover: three oldest rows (2026-08-22 P15, 2026-08-24 P14+OP-2+OP-3,
2026-08-24 P16) moved to docs/journal/2026-33.md verbatim to make room for C-33/C-34/D-80.
CONTEXT.md is at 499 of its 500-line cap.

Gate status unchanged: 4/10. C-34 is a decision, not an implementation -- D-78 still blocks
G9 independently of D-62 until the spec is built.

ADRs: none. Two are owed -- one for the --ci reuse representation, one for widening
sdk.generic.Scheduler.
MSG

echo
echo "=== DONE ==="
git log --oneline -3
echo
echo "Then:  just ci && git push origin main"
