# AgentDX — remaining work

Snapshot as of 2026-09-08. Cross-references `CONTEXT.md` (the authoritative ledger — this file
is a working punch list derived from it, not a replacement for it).

**2026-09-08, later same day:** all three P19 audit findings (Finding #1, both LOW findings)
now repaired; G10/D-86/D-87 owner decisions made (all: leave as recommended); D-88 built
(`CliRunHost` bypass); D-92 row 7 re-scoped (bigger than first thought, correctly flagged rather
than built). None of this is live-verified in this sandbox — see each item's own note.

**10/10 gates PASS happened once, real, in one `just acceptance` run — a real milestone, not
G10's steady state.** G1-G9 are solid, real subprocess passes. G10 is genuinely intermittent:
10 attempts today, 2 clean, 8 failures (see §1b for the full account) — most of the failures are
registry/network-fetch variance on a genuinely cold build, not a defect this harness can fix;
two were a real Docker daemon race that's now been root-caused and fixed (unverified). Gate-green
is not the same as module-`VERIFIED` (§0's own bar) either way. Section 2 below (the re-audit
backlog) and section 3 (release mechanics) are still real, independent of this.

## 1. G3 / G5 — independently audited 2026-09-08 (`op2-audit-g3-g5.md`)

Both came back **PASS WITH NOTES** — no false-PASS mechanism found in either, but real gaps
surfaced (`CONTEXT.md` D-94):

- [x] ~~G3 never independently audited~~ — done, PASS WITH NOTES.
  - [x] ~~**G3-1** fixed~~ — `tests/acceptance/test_gates.py`'s G3 gate now requires
        `min_pytest_passed=3` (was `1`): all three of the file's own DoD tests, not just one.
  - [ ] **G3-2, still open:** the literal gate test only proves determinism on a synthetic
        scenario, not the real fixtures. A stronger real-fixture result exists
        (`bench/results/replay-determinism.json`, 100/100 on all 3) but isn't cross-referenced
        from the G3 row and was itself measured on Python 3.10.12, not the required 3.12.
  - [x] ~~Live-verify the fix~~ — done, 2026-09-08: owner ran `just acceptance` on real
        Python 3.12. **G3 PASS for real**, first genuine execution this gate has ever had.
  - [ ] Still not confirmed in CI (this was a local run)
- [x] ~~G5 never independently audited~~ — done, PASS WITH NOTES.
  - [x] ~~G5 had no `min_pytest_passed` floor at all~~ — fixed alongside G3 (same pattern, found
        while making that fix): now `min_pytest_passed=4`, matching the file's real test count.
  - [ ] **G5-3, still open, NOT a quick fix:** the golden fixtures this gate tests are stamped by
        `ImmediateScheduler()` ("makes no determinism claim"), not the real scheduler `agentdx
        run` actually uses — D-89 already found a structurally different real-scheduler log. The
        <2% margin is measured against a provisional corpus. The real fix is D-89's own
        already-recommended, deliberately-deferred real-fixture regeneration — a breaking change
        to the whole golden corpus (event counts, `seq` numbers, every downstream consumer of
        `evidence.seq`), not something to do as a drive-by patch.
  - [ ] Minor, not fixed: the top-level summing assertion is tautological by construction
        (disclosed, not hidden) — the real guarantee lives in production code, not the test.
  - [x] ~~Live-verify the fix~~ — done, 2026-09-08, same `just acceptance` run: **G5 PASS**.

`just acceptance` full run, 2026-09-08 (owner's machine, real Python 3.12): **8/10 gates PASS**
(G1, G2, G3, G4, G5, G6, G7, G9). Two real failures, both newly surfaced by this exact run —
see §1a and §1b below.

## 1a. G8 — nested-interactive violation, repaired 2026-09-08 (not live-verified)

`axe-core` failed the `run detail route, Graph/Findings/Chaos/Timeline panels` check:
`Findings.tsx`'s row is a real `<button data-testid="finding-row">` whose children include
`EvidenceLink.tsx`'s real `<button>` seq/span chips — buttons nested inside a button, invalid
per WCAG (`nested-interactive`, serious impact), only masked from assistive tech by a
`stopPropagation()` click handler, which doesn't satisfy the underlying rule. This bug is
pre-existing (P15/P16-era), not something today's changes caused — it was only caught now
because an earlier repair (task #59, "P16 Finding #5 — axe.spec.ts coverage gap") extended
`axe.spec.ts` to this exact route/state for the first time, and this is the first time that
extended test has actually run.

- [x] ~~Needs a decision~~ — on reflection this didn't need one: the interaction was already
      fully specified (row click = select finding, chip click = jump to event), only its DOM
      shape was WCAG-invalid. Fixed by making the row-select button and the evidence chips
      siblings instead of ancestor/descendant, matching a pattern (`ReproCommand`) this same
      file already used. `Findings.tsx` + `Findings.module.css` updated.
- [x] ~~Live-verify~~ — done, 2026-09-08: `npm run test:e2e` → **26/26 passed** (was 25/26,
      the one failure being exactly this violation). Confirmed fixed for real.

## 1b. G10 timeout — re-run in isolation, 2026-09-08: PASS, 27.097s

Re-ran `just bench-docker-cold` alone (nothing else running): clean PASS, **27.097s** —
actually faster than the first isolated pass (67.308s), not slower, which is itself consistent
with "nothing else was competing for CPU/disk this time." Four attempts today, in order: outright
failure (exit 1, unexplained) → clean pass (67.3s, isolated) → timeout (200s+, run immediately
after G8's Playwright/npm suite) → clean pass (27.1s, isolated). Both isolated runs passed
clean; the one failure with a plausible cause (the timeout) directly followed a heavy concurrent
process. This is reasonably good evidence the harness/product are sound and G10 is sensitive to
running alongside other heavy test suites, not that it's inherently flaky — but it's evidence,
not proof (2 clean runs, one unexplained early failure never fully root-caused per D-93).

- [x] ~~Re-run in isolation to check for contention~~ — done, passed clean.
- [ ] Optional: one more back-to-back isolated run for a third clean data point, if you want
      stronger confidence before treating this as settled.
- [x] Practical takeaway either way: don't run G10 concurrently with `npm run test:e2e`/other
      heavy processes as a matter of practice, given the one real failure this session
      correlates with exactly that.

**Root cause found and fixed, 2026-09-08.** A later `just acceptance` run (9/10 PASS —
G1-G9 all real) hit G10 with a *concrete* error, not a timeout: `Network agentdx-g10_default
... already exists` / `Container agentdx-g10-seed-1 ... already in use`. This confirms the
200s timeout's real mechanism: it left an orphaned, still-running container behind (compose
`-d` detaches, so killing the outer process doesn't kill the container), and the harness's own
teardown (`docker compose down`, exit code silently discarded) didn't catch that on the next
run either — exactly `op2-audit-docker-cold-start.md` Finding #1, now confirmed live instead
of theoretical.

- [x] ~~Fix Finding #1~~ — done. `bench/harness/docker_cold_start.py` now verifies its own
      teardown (`_teardown_ok()`) and escalates to a forceful fallback (`_force_teardown()`)
      if containers/network survive `docker compose down`, raising `CannotMeasure` with an
      actionable message if even that fails. Same pattern applied to the harness's own final
      exit-cleanup so it doesn't leave the *next* run in this same state.
- [ ] **Not live-verified** — no Docker daemon available to this session. Needs a real re-run.
- [x] ~~Immediate unblock~~ — done; the stuck container/network were already gone by the time
      the cleanup commands ran (self-resolved between attempts).

**Sixth attempt, same day, a fourth distinct outcome:** `build + up: 215.512s`, over the 180s
threshold — a genuinely slow build, not a hang or a conflict. This also exposed a real, separate
harness bug: the poll loop's `deadline` was computed from *before* the build started, so when
the build alone already exceeded the threshold, the loop never ran even once — `healthy`/
`populated` were reported `False` by an unreached branch, not by an actual failed check, and the
`detail` text ("/api/health never returned 200") was misleading about why. **Fixed alongside**:
the loop now guarantees at least one real check regardless of the deadline, and the failure
message now says plainly when the build alone consumed the whole budget rather than implying
health was checked and failed.

Full tally today: 6 attempts, 2 clean passes (67.3s, 27.1s, both isolated), 4 failures (1
unexplained early exit-1; 1 outer-timeout with a since-diagnosed-and-fixed orphaned-container
cause; 1 direct consequence of that same cause — a naming conflict, also fixed; 1 genuinely slow
build, 215.5s). **Recommendation: pause further attempts for now rather than keep re-running.**
Six consecutive full cold builds (each wiping the *entire* daemon-wide builder cache via
`docker builder prune -af`) in one sitting is a heavy, unusual load — plausible cumulative
strain on Docker Desktop's VM (disk I/O, memory), not necessarily a product or harness defect.
The two clean isolated passes already prove the product itself can do this fast; if it's still
inconsistent after a Docker Desktop restart or a break, that's worth investigating for real,
but chasing it attempt-by-attempt today has hit diminishing returns.

**Update: a second real bug found.** `just acceptance` hit the same silent 200s timeout again
— and both times this exact timeout happened, it was specifically through `just acceptance`'s
own wrapper, never on a standalone `just bench-docker-cold` run (which has always either passed
or failed with a real, diagnosable error). Cause: the wrapper's `timeout_s=200`
(`tests/acceptance/test_gates.py`) was tighter than the *inner* harness's own accepted worst
case — its `docker compose up -d --build` subprocess already treats 540s as legitimate, and
this session measured a real 215.512s build. Killing it mid-`up` is exactly what orphans a
detached container and causes the *next* run's naming-conflict failure (Finding #1 again).

- [x] ~~Fix the outer timeout~~ — done: raised to `timeout_s=800`, well past every inner
      sub-budget (540s build ceiling + up to 180s poll + teardown/process margin).
- [x] ~~Live-verify~~ — done, 2026-09-08: `just acceptance` → **10/10 gates PASS**, real, in
      one run. G10 clean. This item is closed.

**Seventh attempt, same day: another slow build, worse than the sixth (494.43s vs 215.5s).**
7 attempts today total: 2 clean (67.3s, 27.1s), 5 failures, the last two both "slow build, not
hung/conflicted" and getting *worse* each time — consistent with cumulative Docker Desktop VM
strain from repeated full daemon-wide cache wipes (`docker builder prune -af` each time), not a
code regression (nothing changed between attempts 6 and 7). **Recommendation: stop running G10
today.** Restart Docker Desktop or take a break before trusting the next measurement — the two
genuinely clean early results (67.3s, 27.1s) are the more representative numbers right now.

**This "VM fatigue" theory was then tested directly and retracted.** The owner did a full
quit-and-reopen of Docker Desktop and re-ran cold: **225.003s, still over threshold** (attempt
9). A real restart not fixing it rules out simple fatigue. Two more attempts followed (10 total
today): attempt 10 hit `compose_up` failing with `container is marked for removal and cannot be
started`, and an immediate re-run hit the classic naming-conflict symptom again — **despite**
`_teardown_ok()`/`_force_teardown()` already being in place. Root-caused for real this time:
Docker removes containers/networks *asynchronously*, and a single point-in-time check can report
"clean" before the daemon has actually finished. **Fixed:** `_teardown_settled()` (poll with
retry/backoff instead of checking once), wired into both `_go_cold()` and the exit-cleanup path.
Not live-verified.

**Honest final tally, 10 attempts: 2 clean (67.3s, 27.1s), 8 failures** — 1 unexplained exit-1,
1 outer-timeout/orphaned-container (fixed), 2 async-removal-race naming conflicts (fixed just
now, unverified), 4 slow-builds-over-threshold (215.5s, 494.4s, 201.4s, 225.0s — most likely
registry/network-fetch variance on a genuinely cold build, a variance class this harness's own
docstring already documented *before* today; not fixable by this harness, since re-fetching ~79
Python packages + npm deps fresh is what "cold" means here). **Owner's call: stop chasing a
green run today, commit the honest record.** G10 is not in `release.yml`'s blocking gate set
(already decided, D-93), so none of this blocks the actual release.

## 2. Standing re-audit backlog

Every module below had a first independent OP-2 audit, real findings, and a same-day repair —
but a *second* re-audit of that repair, this project's own bar for `VERIFIED`, hasn't happened:

- [ ] `runtime/` (P06)
- [ ] `runtime/cache/` (P07)
- [ ] `runtime/faults/` (P09)
- [ ] `analysis/baseline` + `analysis/verdict` (P11)
- [ ] `explore/` (P13)
- [ ] `api/` (P14)
- [ ] Control Tower shell / Waterfall / Scorecard (P15)
- [ ] Graph / Findings / Chaos / Timeline panels (P16)
- [ ] `cli/` (P17)
- [ ] P19 packaging/release cycle overall — the G10 harness itself got a code-review OP-2
      today (`op2-audit-docker-cold-start.md`, PASS WITH NOTES); the rest of P19 has not

None of these are known-broken. They're provisionally trusted, not verified.

## 3. P19 (packaging & release) — what's left in the current phase

- [ ] No git tag has ever been pushed — `release.yml` (wheel/sdist/PyPI/GHCR/GitHub release)
      has never actually run, only been YAML-validated
- [ ] PyPI trusted publishing needs one-time setup on PyPI's own side before the workflow's
      publish step can succeed even once a tag is pushed
- [x] ~~**Decision needed:** fold G10 into `release.yml`'s blocking set, or keep informational?~~
      — owner confirmed 2026-09-08: **keep informational.** No `release.yml` change needed (it
      already excludes G10). G10 stays a manual pre-release step (`just bench-docker-cold` on
      an arm64 Mac) rather than a CI gate.
- [x] ~~Repair audit Finding #1 (MEDIUM) — cold teardown discarding exit codes~~ — done, see §1b
      above (`_teardown_ok()`/`_force_teardown()`). Not live-verified against a real daemon.
- [x] ~~Two LOW findings — uncaught `TimeoutExpired`; version-sensitive NDJSON parsing~~ — both
      done, 2026-09-08 (CONTEXT.md D-93 addendum). `TimeoutExpired` was fixed earlier this same
      session (during the G10 debugging sequence in §1b, not a separate item — this list had gone
      stale); NDJSON parsing fixed just now — `_seed_exit_code()` falls back to whole-stdout
      `json.loads()` (object or array) when the per-line NDJSON read finds nothing. Neither is
      live-verified against a real Docker daemon — same standing constraint as the rest of P19.
- [ ] Linux bind-mount ownership (`./.agentdx-data:/data`, uid 10001, `chown` shadowed by the
      mount at runtime) — genuinely untestable right now; needs a real Linux machine with
      Docker, which neither this session's sandbox nor the owner's (Darwin) machine has
- [ ] **Image size: measured 618MB, root-caused, fixed, NOT yet re-measured.** Root cause:
      `Dockerfile`'s two `uv sync` calls had no `--no-cache` and this build uses no BuildKit
      cache mount, so uv's own download/build cache for the 79-package lock (`duckdb`/
      `langgraph`'s tree the biggest contributors) was baked into the image on top of the real
      dependency closure — both `uv sync` calls now pass `--no-cache`. `UV_COMPILE_BYTECODE=1`
      deliberately left alone (trades size for keeping Python's first-import cost off G10's own
      `/api/health` critical path — a real tradeoff, not free to remove). **Needs a rebuild +
      re-measure** (`docker compose -p agentdx-g10 up -d --build` then `docker images
      agentdx:local --format '{{.Size}}'`) to confirm it actually lands under 500MB now.
- [x] ~~SPA-fallback route (`/`) container-verified~~ — done, 2026-09-08. `curl -i` against a
      live container → `200 OK`, real `index.html` (title + built asset tags), not a 404. Closed.
- [ ] README rewrite + ghost-baseline GIF — no longer blocked ("the demo doesn't run" stopped
      being true 2026-09-03), just not done yet

## 4. Real open design decisions — owner ruled on these 2026-09-08

- [x] ~~**D-86**~~ — owner confirmed: leave deferred. Frontend density/panel-resize
      (`prefsSlice`) was forwarded from P15 to P16 and never built; no PRD spec exists for
      density levels or a resize affordance, so this stays undone rather than guessed.
- [x] ~~**D-87**~~ — owner confirmed: leave deferred, including the filtering-only partial
      build. The Graph panel still doesn't sync to the Timeline scrubber's virtual-time position
      (§20.5) — the backend's filtering support exists, the visual-state color mapping and
      re-layout/debounce behavior don't, and shipping the filtering half alone would be silently
      incomplete rather than a real improvement.
- [x] ~~**D-88**~~ — owner picked the `CliRunHost`-level bypass (candidate b), built 2026-09-08.
      `cli/host.py` gained `bypass_run_id_reuse` + a new `RunIdReuseDisabledError` distinct from
      `RunAlreadyExistsError`; every existing caller (`agentdx run`) is unaffected (flag defaults
      `False`). Closes the *hazard* a future `explore()`-to-fixtures wiring would hit, not the
      feature itself — no CLI command wires `explore()` to `CliRunHost` yet. Regression test:
      `tests/integration/cli/test_run_id_reuse_bypass.py` — compiled, **not live-run** (this
      session's sandbox still can't import `agentdx`, same standing Python-3.12 constraint as
      everything else in this doc). Needs a real pytest run to actually confirm.
- [ ] **D-92** — PRD §33.9's false-positive suite rows 7 (fake-fan-out threshold) and 8
      (single-agent verdict) are documented gaps, not built. Row 8 needs a `VerdictClass` design
      decision. **Row 7 re-scoped 2026-09-08** — it is *not* "just a test" as first written: no
      analysis module computes a branch/fan-out-degree count today (`fake_fanout_min_branches`
      has no data source at all), and `verdict()`'s `parallelism` parameter is accepted but read
      by no rule. Needs new production code first (a branch-count computation + a recommendation
      rule using the already-declared TOML thresholds), then the test. Smaller than D-86/87/88 —
      the PRD gives exact numbers and the semantics are unambiguous — but still a real build, not
      a drive-by. (Also: `explore/`/schedule-enumeration is *not* needed, despite the TOML
      comment that used to say so — corrected alongside this.)

## 5. Deliberately out of scope — not blockers, listed for completeness

- OTel export (PRD §30/FR-13, scope-cut #5)
- CLI stubs: `replay`, `export`, `import`, `scenario new`, `cache *`, `baseline update`, `bench`,
  and `run`'s `--baseline`/`--jobs`/`--fail-on`/`--format github`
- D-90 (virtual makespan always 0 for real fixtures — no calibration profile, tied to the still
  -open Q-43.2.3) and D-91 (resilience scoring can't score real fixtures — no persisted
  `success_check` event) — both flagged, both deferred to a future prompt by design
