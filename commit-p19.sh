#!/usr/bin/env bash
# ---------------------------------------------------------------------------------------
# commit-p19.sh — commit the P19 working tree as a stacked series (AGENTS.md §9).
#
# WHY THIS IS A SCRIPT AND NOT SOMETHING THE ASSISTANT DID:
#   1. The Cowork sandbox mounts this repo without delete permission inside `.git`. A commit
#      attempt from there succeeded at writing objects but could not unlink its own lock
#      files, stranding `.git/HEAD.lock` and `.git/refs/heads/main.lock`. Step 0 removes
#      them. Without that, every `git commit` on this machine fails with
#      "Unable to create '.git/refs/heads/main.lock': File exists".
#   2. `pre-commit` is not installed in that sandbox, so the repo's own pre-commit hook
#      (which runs check_ledger locally — AGENTS.md §10) could not run. It runs here.
#
# Run from the checkout root:   bash commit-p19.sh
# Nothing is pushed. Review with `git log --stat` and push yourself.
# ---------------------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

echo "=== 0. clear stranded locks from the sandbox's failed commit attempt ==="
rm -f .git/HEAD.lock .git/refs/heads/main.lock
# 328 stranded `.git/objects/*/tmp_obj_*` files also exist; 317 predate this session
# (2026-08-14 through 08-27 — earlier sessions hit the same wall and nobody noticed).
# They are inert, but `git gc` will not remove them while they look like in-progress
# writes. This is safe and clears them:
find .git/objects -name 'tmp_obj_*' -delete 2>/dev/null || true
git fsck --no-progress --connectivity-only >/dev/null 2>&1 || true
echo "locks cleared; git status:"; git status --short | head -25

echo

# ---------------------------------------------------------------------------------------
# ORDER MATTERS, AND THE FIRST ATTEMPT GOT IT WRONG.
#
# `pre-commit` stashes unstaged changes before running hooks, so each commit is checked
# against HEAD + that commit's staged files only. Most hooks are file-scoped and do not
# care. Four are whole-repo: mypy, import-linter, determinism-hygiene, check-bench-markers.
#
# check-bench-markers is the one that bites. Committing packaging first meant the Rule E1
# fix (docs/cli.md, docs/limitations.md) was still stashed, so the hook scanned the ORIGINAL
# files and failed on the eight violations this series exists to close. Nothing was wrong
# with the fix; the commit ORDER was wrong.
#
# Rule E1 therefore goes first. Everything after it is checked against a tree where
# check-bench already passes.
#
# Verified, not assumed: on the failed run `ruff format` PASSED while tests/api/test_runs.py
# was stashed in its unfixed state, which proves that hook is file-scoped and that the
# hygiene commit does not also need to come first.
# ---------------------------------------------------------------------------------------

echo
echo "=== 0b. unstage anything the failed attempt left behind ==="
git reset -q

echo "=== 1. P19(docs) — Rule E1   [FIRST: check-bench is whole-repo] ==="
git add docs/cli.md docs/limitations.md
git commit -F - <<'MSG'
P19(docs): close eight Rule E1 violations so check-bench passes -- D-73

PRD sections: §44.3.5. Invariants: I9 (Rule E1), a never-waived quality gate.

scripts/check_bench_markers.py was failing on eight unmarked unit-bearing numbers: six in
docs/cli.md (regression tolerances, test-fixture magnitudes) and two in docs/limitations.md
(mutation-test magnitudes). None was a genuine unmeasured performance claim — all were
configured thresholds or test-fixture values that happened to carry a Rule E1 unit.

Each was reworded to state the same fact without presenting a bare unit-bearing figure,
rather than suppressed: the checker has no suppression mechanism by design (tripwire 7).

docs/cli.md's six were reported-not-fixed by the 2026-08-27 P18/19 session.
docs/limitations.md's two appear in no prior report — that session's own real checker run
predates the file it created.

Gate status: `just check-bench` passes (17 files). It did not before.
ADRs: none.
MSG

echo
echo "=== 2. P19(hygiene) — F-9 and the lint blockers ==="
git add tests/api/test_runs.py gen/make_fixtures.py scripts/print_acceptance_table.py .gitignore
git commit -F - <<'MSG'
P19(hygiene): unblock `just lint` so the rest of `just ci` can run -- F-9

`just ci` runs `lint` first and aborts on failure, so check-imports, check-determinism,
check-ledger, check-bench and check-fixture-evidence had never executed in one invocation.
Three separate causes, all closed here:

- tests/api/test_runs.py: a missing blank line at :87 failed `ruff format --check`. Red
  since d12b763. Already recorded in CONTEXT.md §7's P17 row as failing under this
  project's own pinned ruff, and deferred twice as out of scope. Whitespace only —
  verified token-identical by stripping all whitespace from both versions.
- gen/make_fixtures.py: 10 ruff errors (E501, D205, D209, D103). NOTE: the fix is not
  purely formatting — four explicit write_text calls were collapsed into a
  `for section in (...)` loop. Verified byte-equivalent by simulating both versions, but
  it is a refactor and is recorded as one rather than narrated as a lint fix (tripwire 17).
  OWED: this script regenerates committed frontend fixtures, so the real confirmation is
  re-running it and getting a no-op diff. Not yet done.
- scripts/print_acceptance_table.py: 2 ruff errors. Formatting only, verified
  token-identical.
- .gitignore: git does not support trailing comments on a pattern line — the whole line is
  the pattern. frontend/test-results/ and src/agentdx/api/static/ each carried one, so
  neither was ever actually ignored despite bde03c9 saying so.

Gate status: `just ci` now passes all eight steps — ruff check; ruff format (332 files);
mypy --strict (98 source files); pytest (2205 passed, 10 deselected); lint-imports (10
contracts, 149 files, 730 dependencies); check-determinism (98 files); check-ledger;
check-bench (17 files); check-fixture-evidence (3 files). First end-to-end green run in
the project's history.

CAVEAT on that green: the otel-is-a-projection import contract is vacuously KEPT —
agentdx.otel is a 233-byte docstring-only __init__.py with no imports. "10 kept" is nine
enforced rules plus one placeholder.

ADRs: none.
MSG

echo
echo "=== 3. P19(cli) — D-77 ==="
git add src/agentdx/cli/_target.py src/agentdx/cli/commands/run.py \
        tests/integration/cli/test_target_resolution.py
git commit -F - <<'MSG'
P19(cli): fix fixture-vs-scenario-directory target resolution -- D-77

PRD sections: §37.1 (TARGET grammar), §38.1 (the quickstart command). Invariants: none
directly; this is a correctness fix in the CLI's dispatch, below the invariant layer.

run.py::_is_scenario_path ended in a bare `return path.is_dir()`, claiming EVERY existing
directory as a directory of scenario files. _execute consults it before anything else, so
`agentdx run fixtures/code_pipeline` — PRD §38.1's own literal quickstart, and the command
docker-compose.yml's seed service and `just demo-offline` each issue three times — was
globbed for *.yaml, found none, and exited 7. resolve_target and is_fixture_name, which
have always handled the fixtures/<name> form correctly, were never reached.

This is the confirmed cause of gate G9's exit 7, recorded in CONTEXT.md §6 since
2026-08-27 and misattributed to D-62's deadlock until the Docker seed container
reproduced the identical exit 7 and made it findable.

The first repair was too broad and review caught it: delegating to is_fixture_name reduced
the argument to Path(x).name, so any directory sharing a fixture's basename resolved to
the shipped fixture and ran the wrong graph, silently, exit 0. is_fixture_name now requires
the path to BE the fixture directory and resolves against its own checkout rather than the
caller's cwd.

BEHAVIOUR CHANGE: a directory named after a fixture (my_scenarios/code_pipeline/) is now
read as a directory of scenarios. Write the fixture name bare to mean the fixture. D-77
records this precedence rule; PRD §37.1 is silent on it.

Gate status: G9 and G10 still FAIL — this moves both from "the target never resolved"
(exit 7) to "the run deadlocked" (exit 5, D-62), verified live. Neither passes.
Regression tests: tests/integration/cli/test_target_resolution.py (12 tests), of which
several fail against both the original code and the first repair.

ADRs: none. Rulings: D-77 (CONTEXT.md §9).
MSG

echo
echo "=== 4. P19(packaging) ==="
git add Dockerfile docker-compose.yml bench/harness/docker_cold_start.py \
        bench/results/docker-cold-start.json docs/architecture.md CHANGELOG.md
git commit -F - <<'MSG'
P19(packaging): Dockerfile, compose demo, G10 cold-start harness, docs, CHANGELOG

PRD sections: §38.3 (documentation set), §39.2 (Docker Compose demo), §39.4 (build and
packaging), §39.6 (release process), §44.1 G10.

- Dockerfile: multi-stage (node build -> python runtime), non-root uid 10001. fixtures/
  and scenarios/ are copied because they are NOT package data (D-75).
- docker-compose.yml: seeding split into its own service so a fixture failure is
  distinguishable from a server failure (D-76). Env names corrected to the real
  AGENTDX_<SECTION>_<KEY> contract; PRD §39.2's AGENTDX_MODE / AGENTDX_DATA_DIR match no
  section prefix and are silently ignored by AgentDXConfig.load() (D-74).
- bench/harness/docker_cold_start.py: gate G10 had no harness at all, which is why it
  reported FAIL rather than a number. Measures BOTH halves of the criterion (healthy
  /api/health AND a non-empty run list), records cold_cache so a warm run can never be
  cited as the gate, and exits 3 (CANNOT MEASURE) without writing a result file when
  Docker is absent.
- docs/architecture.md: completes the §38.3 set. C-33 rules the PRD-vs-tree doc-naming
  divergence in favour of the existing filenames.

Invariants touched: I9 — this harness writes the only file a published cold-start number
may cite. I7 unaffected: no API key reaches the container.

Gate status: G10 still FAILS, blocked by D-62 rather than by packaging. The one cold
measurement (181.092s) also exceeds the 180s threshold — a second, independent failure.
Image built and run on Darwin/arm64; never built on Linux, and the sub-500MB §39.4 target
has never been measured.

ADRs: none.
MSG

echo
echo "=== 5. P19(ledger) ==="
git add CONTEXT.md docs/journal/2026-33.md docs/journal/current-position-archive.md \
        verify-p19-repair.sh commit-p19.sh
git commit -F - <<'MSG'
P19(ledger): §5/§6/§7/§9/§13 update, journal rollovers, review scripts

Records the P19 packaging work, the D-77 repair, the F-9 lint unblock, and the OP-2 review
that found them. New deviations D-73 through D-79; new ruling C-33.

Two §13 rollovers: the §7 current-position archive (D-79) and a twenty-sixth session-row
rollover moving the 2026-08-19 P13 row into docs/journal/2026-33.md verbatim, to make
headroom before appending this session's row. CONTEXT.md is at 499 of its 500-line cap.

Gate status unchanged by this commit: 4/10 acceptance gates pass (G2, G3, G5, G8). G1, G4,
G6, G7 fail on missing CLI flags and subcommands. G9 and G10 fail on D-62. D-78 (run_id
collision) blocks G9 independently and is an open product decision, not a defect to repair.

check-ledger: OK — §8 and §9 append-only, length and ADR references all clean.
ADRs: none.
MSG

echo

echo
echo "=== DONE — review before pushing ==="
git log --oneline -6
echo
echo "Verify the tip is green, then push yourself:"
echo "    just ci && git push origin main"
