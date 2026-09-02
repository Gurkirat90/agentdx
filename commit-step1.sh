#!/usr/bin/env bash
# ---------------------------------------------------------------------------------------
# commit-step1.sh — D-62 step 1: the experiment, its result, and the framing correction.
# Run from the checkout root:   bash commit-step1.sh    (nothing is pushed)
# ---------------------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

echo "=== pre-flight — no bypasses ==="
uv run ruff check tests/integration/runtime/
uv run ruff format --check tests/integration/runtime/
uv run python scripts/check_bench_markers.py
uv run python scripts/check_ledger.py

echo
echo "=== the experiment must still produce its result before it is committed ==="
echo "(3 controls pass, the decisive probe deadlocks -> exit 1 is CORRECT here)"
set +e
uv run pytest tests/integration/runtime/test_d62_suspension_contract.py -q
rc=$?
set -e
if [ "$rc" -eq 0 ]; then
  echo "UNEXPECTED: the suite passed cleanly. The decisive probe is supposed to FAIL with"
  echo "'HYPOTHESIS CONFIRMED'. If it now passes, the sequential graph COMPLETED and the"
  echo "finding has reversed -- do not commit; re-read the result first."
  exit 1
fi
echo "-> non-zero as expected; the decisive probe still deadlocks."

echo
echo "=== commit ==="
git add tests/integration/runtime/ d62-design.md commit-step1.sh
git commit -F - <<'MSG'
P20(runtime): D-62 is not caused by fan-out -- measured, and the framing corrected

An experiment, not a regression suite. It settles which design D-62 needs before any wiring
is written, because the ledger recorded the wrong reason and two of the three candidate
designs were built on it.

RESULT (Darwin/arm64, CPython 3.12.2, tests/integration/runtime/):

    root coroutine that never suspends                    completes
    root awaiting an already-resolved Future              completes
    root awaiting an Event set by another real task       DeadlockError
    single-node SEQUENTIAL LangGraph graph, no fan-out    DeadlockError

The boundary, stated only as far as observed: the scheduler tolerates `await`, but not an
`await` whose resolution requires the event loop to run ANOTHER TASK. The two passing
controls are what make that precise; without them "the scheduler rejects suspension" would
have been the obvious and wrong reading.

A graph with nothing to parallelise cannot be deadlocked by parallelism. CONTEXT.md §7/§9,
the Dockerfile header and this session's own earlier notes were all wrong to say fan-out
causes D-62. Fan-out is incidental.

RETRACTION, mine: the first revision of this file and of d62-design.md §3 asserted a "one
event-loop tick" budget. `_resume_task` does grant exactly one `asyncio.sleep(0)` -- that is
readable in the source -- but the tick COUNT was never measured, and the first version of the
experiment that claimed to measure it was broken. Its "one tick" control used
`ensure_future` + `Event.wait()`; `ensure_future` only schedules, so that case always
required another task to run and could never have passed. The control failed, which is what
controls are for. The tick number is retracted throughout.

NEW OPTION D, which the measurement produced and the original option set did not contain:
`_scheduler_loop` declares deadlock the moment no SCHEDULER task is runnable, while the real
event loop may still have pending work -- the Event probe completes fine under a plain
`asyncio.run`. So the fix may be far smaller than options A/B: do not declare deadlock while
the loop has pending work; yield, re-check, and only then raise. That would live entirely in
runtime/scheduler.py, touch no sdk/, and require no Protocol change.

Option D is NOT recommended here. Letting the real loop run work the scheduler cannot see is
precisely what I1 exists to prevent, and it is only safe if that work emits no events -- true
for Pregel's plumbing, not guaranteed in general. It needs its own experiment. It is recorded
because A and B both pay a large cost to solve a problem that may not be the one that exists.

Runs with strict_determinism=False, deliberately and not as a bypass: `strict` also gates
_patch_time and _patch_thread_spawn, so a clock read inside Pregel raises DeterminismLeakError
BEFORE the scheduler loop is reached -- the file's first run did exactly that and learned
nothing. Leaving strict on would let a different failure impersonate the one under test. I1 is
tested in tests/determinism/, not here.

The decisive test FAILS BY DESIGN with "HYPOTHESIS CONFIRMED" in its message. It is excluded
from no suite and hidden behind no marker: a red test whose failure text is the finding is
more honest than a green one asserting a bug it cannot fix. Whoever closes D-62 deletes or
inverts it, and that inversion is the acceptance criterion.

Gate status: unchanged, 4/10. This changes no product behaviour -- it changes what the next
prompt should build.

ADRs: none yet. Closing D-62 still needs one for sdk.generic.Scheduler if options A/B win;
option D would not need it, which is part of its appeal and part of why it needs scrutiny.
MSG

echo
echo "=== DONE ==="
git log --oneline -3
echo
echo "Next: CONTEXT.md §7/§9's fan-out framing and the Dockerfile header both still carry the"
echo "corrected claim. Those are ledger edits -- 497/500 lines, so check headroom first."
