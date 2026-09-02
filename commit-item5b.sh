#!/usr/bin/env bash
# ---------------------------------------------------------------------------------------
# commit-item5b.sh — record the measured G10 cold-start result.
#
# One commit. Ledger, the results file, and one stale-string fix in the harness.
# Run from the checkout root:   bash commit-item5b.sh    (nothing is pushed)
# ---------------------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

echo "=== pre-flight ==="
uv run python scripts/check_ledger.py
uv run python scripts/check_bench_markers.py
uv run python scripts/check_fixture_finding_evidence.py
uv run ruff check bench/harness/docker_cold_start.py
uv run ruff format --check bench/harness/docker_cold_start.py

echo
echo "=== commit ==="
git add CONTEXT.md bench/results/docker-cold-start.json bench/harness/docker_cold_start.py commit-item5b.sh
git commit -F - <<'MSG'
P19(bench): G10 cold start 181.092s -> 140.273s, under threshold -- measured

PRD sections: §39.2, §39.4, §44.1 G10, NFR-5. Invariants: I9 (the number cites its file),
I11 (the harness's wall-time fields carry the _wall_s suffix).

First cold, gate-conformant re-measurement after the .dockerignore fix (2c0090f):

    build_and_up_wall_s : 140.273     (was 181.092)
    threshold_s         : 180.0
    cold_cache          : true
    is_gate_conformant  : true
    arch                : arm64, conformant

Under the threshold with headroom, where the previous cold run was over it. That closes
G10's TIMING half -- the second, independent failure this gate carried alongside D-62.

Cause, for the record: there was no .dockerignore, so every build transferred the entire
1.2 GB checkout to the daemon before the first layer ran -- 1,135.1 MB across 39,001 files,
cut to 3.4 MB across 245 by the filter.

HONESTY ON THE NUMBERS: both 181.092 and 140.273 are single wall-clock samples on one
Darwin/arm64 machine. The direction is established; the exact delta is not a constant. Same
discipline as D-68 -- cite bench/results/docker-cold-start.json, never quote a fixed digit
as a permanent fact. The result file's own gate_status field says so.

STILL FAILS. `populated run list` is unreachable while D-62 stands: seed exits 5
(E-SCHED-003), run_count 0, /api/health never reached because the server is gated on
seeding. And the headroom against three REAL fixture runs, once they work, is untested --
today's runs fail immediately and contribute nothing to the 140.273 s.

Also fixes a stale claim in _diagnose_seed's exit-5 text, which asserted "LangGraph's
parallel fan-out has no runnable task" as fact. d62-design.md §3 downgrades that to a
hypothesis: _resume_task grants one event-loop tick per resumption and recognises only
scheduler-created Futures as suspension, so any await needing more than one tick deadlocks
regardless of concurrency. The string now says so and points at the log_tail's wait_reason
as the discriminator. Third instance of this class today (the Dockerfile header and
CONTEXT.md §6 carried the same claim); all three now corrected.

Gate status: 4/10 unchanged. G10 keeps exactly one blocker instead of two.
ADRs: none.
MSG

echo
echo "=== DONE ==="
git log --oneline -3
echo
echo "Then:  just ci && git push origin main"
