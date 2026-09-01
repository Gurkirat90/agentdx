#!/usr/bin/env bash
# ---------------------------------------------------------------------------------------
# verify-p19-repair.sh — everything the review sandbox could NOT verify.
#
# The sandbox had Python 3.10 and no network (so no 3.12), no ruff, no mypy, no docker,
# no just. Every claim in the review came from executing the real modules under 3.10 or
# from the repo's own 3.10-clean checkers. Nothing below has ever been run.
#
# Run from the checkout root:   bash verify-p19-repair.sh
# Nothing here writes to the repo except step 6, which reverts a file and puts it back.
# Steps are independent; none aborts the script. Read the SUMMARY at the end.
# ---------------------------------------------------------------------------------------
set -u
cd "$(dirname "$0")" || exit 1

PASS=(); FAIL=(); SKIP=()
step() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }
record() { if [ "$1" -eq 0 ]; then PASS+=("$2"); else FAIL+=("$2 (exit $1)"); fi; }
have()  { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------------------
# A. STATIC — the three checks I could not run at all
# ---------------------------------------------------------------------------------------

step "A1  ruff — never run against my edits"
# Proves: _target.py's rewritten branch and the new tests satisfy the D docstring rules and
# the 100-col limit. I measured line lengths by hand (max 94 / 96) but ran no linter.
if have uv; then
  uv run ruff check . ; record $? "A1 ruff check"
  uv run ruff format --check . ; record $? "A1 ruff format"
else SKIP+=("A1 ruff — no uv"); fi

step "A2  mypy --strict — never run"
# Proves: the new `relative_to` / ValueError branch types cleanly. `find_repo_root` returns
# `Path | None` and I chain it through `or`; mypy is the thing that confirms that narrows.
if have uv; then uv run mypy --strict src/agentdx ; record $? "A2 mypy"; else SKIP+=("A2 mypy — no uv"); fi

step "A3  import-linter — never run"
# Proves: no layering rule broke. Low risk (cli/ only) but it is one of the 10 contracts.
if have uv; then uv run lint-imports ; record $? "A3 import-linter"; else SKIP+=("A3 — no uv"); fi

# ---------------------------------------------------------------------------------------
# B. THE NEW TESTS — proven by executing the module, never by pytest
# ---------------------------------------------------------------------------------------

step "B1  the D-77 regression file, verbose"
# Proves: the 12 tests COLLECT and pass under real pytest. I verified the logic by importing
# _target.py verbatim under 3.10 (16/16 cases) but never exercised the `repo_root` fixture,
# the new autouse `_pin_cwd` fixture, or monkeypatch.chdir interaction between them.
if have uv; then
  uv run pytest tests/integration/cli/test_target_resolution.py -v
  record $? "B1 test_target_resolution.py"
else SKIP+=("B1 — no uv"); fi

step "B2  the CLI integration tests most likely to be disturbed"
# Proves: changing is_fixture_name's semantics did not move an exit code. These pass bare
# names (`code_pipeline`, `no_such_fixture_ever_exists`) and one absolute empty dir — all
# three shapes are in my 16-case matrix and all three were unchanged, but that is reasoning,
# not a run. Exit codes 0-7 are a MAJOR contract (CHANGELOG policy); this is the check.
if have uv; then
  uv run pytest tests/integration/cli/ -v ; record $? "B2 tests/integration/cli/"
else SKIP+=("B2 — no uv"); fi

step "B3  agentdx instrument — the second caller, fixed by the same change, untested"
# Proves: `instrument` still resolves a bare fixture name. It shares is_fixture_name and had
# the identical basename-hijack defect. NO test in the repo covers `instrument` with a
# fixture argument, so nothing in B1/B2 will catch a regression here. This is the largest
# uncovered surface of the repair.
if have uv; then
  echo "--- expect: static discovery of a real graph.py, exit 0"
  uv run agentdx instrument code_pipeline ; record $? "B3 instrument <bare name>"
  echo "--- expect: same result, exit 0"
  uv run agentdx instrument fixtures/code_pipeline ; record $? "B3 instrument <fixture path>"
  echo "--- expect: exit 2, NOT a preview of the code_pipeline fixture"
  mkdir -p /tmp/adx-collide/code_pipeline && : > /tmp/adx-collide/code_pipeline/x.yaml
  uv run agentdx instrument /tmp/adx-collide/code_pipeline
  [ $? -eq 2 ] && record 0 "B3 instrument <collision> refused" || record 1 "B3 instrument <collision> NOT refused"
else SKIP+=("B3 — no uv"); fi

step "B4  full suite — the real regression question"
# Proves: nothing else in 2116 tests depended on the old basename behaviour. No existing test
# imports _target symbols (I grepped), but the CLI is exercised end-to-end in several places.
#
# PYTHONHASHSEED=0 is REQUIRED project-wide (AGENTS.md §4.1). `just` exports it (justfile:11)
# and tests/acceptance/ sets it explicitly (D-67); a bare `uv run pytest` gets neither, and
# ~49 runtime/determinism/faults tests then fail with E-SCHED-004 DeterminismLeakError — the
# guard correctly refusing to run rather than risk a false determinism claim. That is the
# suite working, not a regression. The first version of this script omitted it.
if have uv; then PYTHONHASHSEED=0 uv run pytest -q ; record $? "B4 full suite"; else SKIP+=("B4 — no uv"); fi

# ---------------------------------------------------------------------------------------
# C. DISCRIMINATING POWER — the mutation test, on real pytest
# ---------------------------------------------------------------------------------------

step "C1  revert is_fixture_name to the shipped version; the new tests must FAIL"
# Proves: the three new tests have real power under pytest, not just under my 3.10 harness.
# I proved this by re-implementing both versions and calling them directly. This does it the
# honest way. The file is restored either way.
if have uv && have git; then
  cp src/agentdx/cli/_target.py /tmp/_target.py.bak
  python3 - <<'PY'
import re, pathlib
p = pathlib.Path("src/agentdx/cli/_target.py"); s = p.read_text()
start = s.index("    stripped = candidate.rstrip(\"/\")\n    if not stripped:")
end   = s.index("def load_fixture_graph")
shipped = '''    repo_root = root or find_repo_root()
    if repo_root is None:
        return None
    stripped = candidate.rstrip("/")
    name = Path(stripped).name
    if (repo_root / "fixtures" / name / "graph.py").is_file():
        return name
    return None


'''
p.write_text(s[:start] + shipped + s[end:])
print("reverted is_fixture_name to the version the diff shipped")
PY
  uv run pytest tests/integration/cli/test_target_resolution.py -q
  rc=$?
  cp /tmp/_target.py.bak src/agentdx/cli/_target.py
  echo "--- restored; confirm clean:"; git diff --stat src/agentdx/cli/_target.py
  if [ $rc -ne 0 ]; then record 0 "C1 tests correctly FAIL against the old code"
  else record 1 "C1 TESTS PASSED AGAINST THE OLD CODE — they do not discriminate"; fi
else SKIP+=("C1 — needs uv + git"); fi

# ---------------------------------------------------------------------------------------
# D. GATES — the point of the whole repair
# ---------------------------------------------------------------------------------------

step "D1  G9 — must move from exit 7 to exit 5"
# Proves the repair did what it was for. BEFORE: exit 7, "no scenario files found under
# fixtures/code_pipeline". AFTER: exit 5, a DeadlockError from D-62. Exit 5 is SUCCESS for
# this check — it means the target resolved and an agent actually started. Exit 7 means the
# repair did not take in the real CLI path.
if have just; then just demo-offline; echo "^ exit $? — want 5, NOT 7"; else SKIP+=("D1 — no just"); fi

step "D2  just acceptance — the mechanised gate table"
# Proves: the 4/10 baseline (G2, G3, G5, G8) did not regress. G9/G10 stay red on D-62.
if have just; then just acceptance ; record $? "D2 acceptance (expect FAIL on 6)"; else SKIP+=("D2 — no just"); fi

step "D3  just ci — both blockers now fixed; expect GREEN"
# History: `just ci` runs lint FIRST and it exited 1 on 12 pre-existing ruff errors in
# `gen/make_fixtures.py` (10) and `scripts/print_acceptance_table.py` (2) — E501, D205, D209,
# D103, both files unmodified since b043b47 — so lint had been red since that commit and
# check-ledger's 539/500 (D-61) was never even reached. Two blockers masking each other.
# Both are now closed: the lint errors are fixed (formatting only) and D-61 by the first §7
# rollover (D-79), leaving CONTEXT.md at 499. Every step of `just ci` should now pass.
if have just; then just ci ; record $? "D3 just ci (expect GREEN)"; else SKIP+=("D3 — no just"); fi

# ---------------------------------------------------------------------------------------
# E. DOCKER — no daemon in the sandbox; none of this has been run since the repair
# ---------------------------------------------------------------------------------------

step "E1  cold G10 — the number that does not exist yet"
# The committed bench/results/docker-cold-start.json is WARM (`cold_cache: false`, taken with
# --no-prune, build_and_up 9.856s). No G10-conformant measurement exists. THIS is the run.
# NOTE: `_go_cold` runs `docker builder prune -af`, which is DAEMON-GLOBAL despite a docstring
# claiming project isolation (open finding F6) — it will wipe every other project's build
# cache on this machine. Do not run it on a machine mid-build.
echo "SKIPPED BY DEFAULT — read the warning above, then run:"
echo "    just bench-docker-cold"
SKIP+=("E1 cold G10 — must be run deliberately, prunes the whole builder cache")

step "E2  in-container target resolution — the environment that motivated the fix"
# Proves the repair works where it actually mattered. The seed service should now reach a
# DeadlockError instead of 'no scenario files found'. Reads the seed container's own log.
echo "Run:  docker compose up --build ; docker compose logs seed"
echo "  want: E-SCHED-003 DeadlockError, seed exit 5"
echo "  bad : 'no scenario files found under fixtures/code_pipeline', exit 7"
SKIP+=("E2 in-container resolution — needs a docker daemon")

step "E3  the two Docker facts nobody has measured"
echo "  docker build . && docker images agentdx:local --format '{{.Size}}'   # PRD 39.4 wants < 500MB"
echo "  ...and the same on LINUX: ./.agentdx-data:/data is written by uid 10001; the build-time"
echo "  chown is shadowed by the runtime mount. Docker Desktop on macOS remaps ownership and"
echo "  hides this. The one real run was on Darwin. Linux is a CONTEXT.md 3 supported platform."
SKIP+=("E3 image size + Linux bind mount — never measured, never run")

# ---------------------------------------------------------------------------------------
step "SUMMARY"
printf '\n\033[32mPASS\033[0m\n'; printf '  %s\n' "${PASS[@]:-(none)}"
printf '\n\033[31mFAIL\033[0m\n'; printf '  %s\n' "${FAIL[@]:-(none)}"
printf '\n\033[33mSKIPPED / MANUAL\033[0m\n'; printf '  %s\n' "${SKIP[@]:-(none)}"
printf '\nD3 red on check-ledger is expected. Everything else red is a real finding.\n'
