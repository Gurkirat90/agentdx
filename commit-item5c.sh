#!/usr/bin/env bash
# ---------------------------------------------------------------------------------------
# commit-item5c.sh — correct the G10 attribution after a second cold run disagreed.
# Run from the checkout root:   bash commit-item5c.sh    (nothing is pushed)
# ---------------------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

echo "=== pre-flight ==="
uv run python scripts/check_ledger.py
uv run python scripts/check_bench_markers.py
uv run ruff check bench/harness/docker_cold_start.py
uv run ruff format --check bench/harness/docker_cold_start.py
uv run mypy --strict bench/harness/docker_cold_start.py || true   # bench/ is outside src/

echo
echo "=== commit ==="
git add CONTEXT.md bench/harness/docker_cold_start.py bench/results/docker-cold-start.json commit-item5c.sh
git commit -F - <<'MSG'
P19(bench): a second cold run disagreed by 34s -- record what "cold" actually means

PRD sections: §44.1 G10, §39.2. Invariants: I9.

Two cold, gate-conformant runs of the SAME commit on the same machine:

    140.273 s   then   106.342 s

A 34 s / 24% spread. The previous commit (fd02ce4) attributed the 181.092 -> 140.273
improvement to the .dockerignore fix. With n=2 that attribution does not hold: the
run-to-run spread is comparable to the delta being claimed.

ROOT CAUSE OF THE SPREAD, found by reading _go_cold rather than assumed:
`docker builder prune -af` empties the BUILD CACHE. The three base images the Dockerfile
pulls -- node:20-bookworm-slim, python:3.12-slim-bookworm, ghcr.io/astral-sh/uv:0.5.11 --
live in the IMAGE STORE and survive it. `compose down --rmi local` removes only the
locally-built agentdx:local. So the first run paid a registry pull the second did not.

`cold_cache: true` has therefore been overstating what it controls: it means "no build
cache", not "nothing cached". The harness now records base_images_present and
base_images_pulled_this_run in every result, and takes --pull-cold to delete them so the
build pays a real pull.

The DEFAULT IS DELIBERATELY UNCHANGED. PRD §44.1 says only "cold cache" and does not settle
whether a fresh CI runner's image pull counts toward the gate. Changing the default would
move a gate's goalposts silently, which is the opposite of what this harness is for. The
stricter reading is available behind a flag and recorded either way.

WHAT IS ESTABLISHED: both cold runs are under the 180 s threshold, so G10's timing half is
supported by two independent measurements rather than one. And the .dockerignore fix's own
effect was measured directly and is not in question: build context 1,135.1 MB / 39,001 files
-> 3.4 MB / 245 files.

WHAT IS NOT: how much of the wall-clock improvement that context reduction accounts for.
Direction established, magnitude not. Same discipline D-68 already imposed on
scrub-reconstruction after three same-machine runs produced three different p95 values --
this project has now learned the identical lesson twice, in two different harnesses.

Gate status: 4/10 unchanged. G10 keeps one blocker (D-62), not two. Headroom against three
real fixture runs is still untested -- today's runs deadlock instantly and contribute
nothing to either measurement.

ADRs: none.
MSG

echo
echo "=== DONE ==="
git log --oneline -3
echo
echo "Optional, for the stricter number:  uv run bench/harness/docker_cold_start.py --pull-cold"
echo "Then:  just ci && git push origin main"
