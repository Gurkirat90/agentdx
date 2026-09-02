#!/usr/bin/env bash
# ---------------------------------------------------------------------------------------
# commit-item5d.sh — fix the mypy error my own pre-flight suppressed, and remove the bypass.
# Run from the checkout root:   bash commit-item5d.sh    (nothing is pushed)
# ---------------------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

echo "=== pre-flight — no bypasses this time ==="
uv run ruff check bench/harness/docker_cold_start.py
uv run ruff format --check bench/harness/docker_cold_start.py
uv run mypy --strict bench/harness/docker_cold_start.py
uv run python scripts/check_ledger.py

echo
echo "=== commit ==="
git add bench/harness/docker_cold_start.py commit-item5c.sh commit-item5d.sh
git commit -F - <<'MSG'
P19(bench): fix no-any-return, and remove the `|| true` that hid it

_http_json returned `json.loads(...)` -- typed Any -- from a function declared
`object | None`, which `mypy --strict` rejects with no-any-return. Fixed by binding the
decoded value to `object` before returning, so the declared type is honest rather than a
promise `Any` leaks through.

HOW IT SURVIVED, which matters more than the error:

commit-item5c.sh's pre-flight ran `mypy --strict bench/harness/docker_cold_start.py
|| true`. I wrote that `|| true`. It reported the error to the terminal and let the commit
proceed anyway -- a bypass built into a check, in a harness whose entire purpose is refusing
to report a result it did not verify. The `|| true` is removed; that line now gates.

COVERAGE GAP THIS EXPOSED, reported not fixed:

    ruff check . / ruff format --check .   covers bench/      YES
    mypy --strict src/agentdx              covers bench/      NO
    check_determinism_hygiene.py           covers bench/      NO  (scans src/agentdx/ only)
    lint-imports                           covers bench/      NO  (contracts are on agentdx.*)

The last two are correct: benchmarks legitimately read the wall clock, and bench/ is not a
layer. mypy is the real gap -- the harness that gates G10 and writes the file every Rule E1
citation resolves to has never been type-checked by CI, which is why a no-any-return sat
there until a hand-written pre-flight happened to look.

NOT extending `just typecheck`, and this is now evidence rather than caution.
`uv run mypy --strict bench/` was run: 12 errors across 6 files.

    bench/harness/scrub_reconstruction.py:88  return-value  Store vs SnapshottingStore
    bench/harness/api_latency.py:190          explicit-any  banned by this project's config
    bench/harness/sdk_overhead.py:128         attr-defined  "object" has no attribute ainvoke
    tests/unit/events/factories.py:148        arg-type      x7, dataclasses.replace(**kwargs)
    tests/unit/sdk/fakes.py:135               unused-ignore
    tests/unit/store/conftest.py:45           misc          yield type mismatch

Two things that makes clear. First, adding bench/ to `just typecheck` would turn `just ci`
red immediately -- and `just ci` went green for the first time in this project's history
earlier today (02f8538). Second, it would not be adding one directory: scrub_reconstruction
imports test helpers, so mypy follows into tests/ and half the errors are there. That is a
scoped repair with its own prompt, not a one-line scope change.

docker_cold_start.py appears nowhere in that list -- this commit's fix is clean.

THIRD SCOPE SURPRISE OF THE DAY, worth naming as a pattern rather than three incidents:
a check's coverage is routinely narrower than its name suggests. `mypy --strict src/agentdx`
does not cover bench/. `check_bench_markers.py` scans only README.md and docs/. The
otel-is-a-projection import contract is vacuously KEPT over a docstring-only package. Each
reads as broad protection and is not. Before citing any check as evidence, read what it
actually walks.

Gate status: unchanged, 4/10.
ADRs: none.
MSG

echo
echo "=== DONE ==="
git log --oneline -3
echo
echo "To decide the coverage gap:  uv run mypy --strict bench/"
echo "Then:  just ci && git push origin main"
