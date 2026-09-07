# OP-3 repair report — Control Tower P16 (Graph, Findings, Chaos, Timeline + live stream)

Repairs `op2-audit-p16.md` (independent OP-2, fresh agent, cold read; VERDICT FAIL — 1 HIGH,
4 MEDIUM, 2 LOW). Performed by the orchestrating session directly (`Edit`/`Write`/`Bash`),
same day as the audit, under the standing "propose a full priority order and just go"
authorization (no CRITICAL finding, no systemic-trust implication, so no fresh
`AskUserQuestion` round was needed). Ninth stop in the "clearing verification debt" queue,
directly following the P15 repair cycle (`op3-repair-report-p15.md`).

## Scope decision

Findings #2 (`prefsSlice`) and #3 (Graph synchronization, PRD §20.5) are **disclosed as
deviations (D-86, D-87) rather than built**. Both are genuine gaps, but both are also
under-specified PRD points where the safe, honest move is to name the gap precisely rather
than fabricate an interaction design under time pressure — the same "disclose rather than
build something that only partially satisfies scope" precedent this project has applied
before (D-58, Q-P15.1). See D-86/D-87 (CONTEXT.md §9) for the full reasoning on each.

Findings #1, #4, #5, #6, #7 were fixed in full, each independently verified.

## Finding #1 (HIGH) — `timelineSlice.ts`/`eventsSlice.ts` missing D-60's unguarded-result fix

**What the audit found:** `timelineSlice.ts`'s `loadStateAt` and `eventsSlice.ts`'s
`startPolling` both trusted `!result.error` alone as their success guard — the same
`openapi-fetch` failure mode (D-60) already fixed in `graphSlice`/`findingsSlice`/
`chaosSlice`/`runSlice` (P15's own second HIGH finding) was missed at these two call sites.
`openapi-fetch` can resolve `{data: undefined, error: undefined}` on a non-JSON error body
(e.g. this build's Vite dev-proxy 500 page when the backend is unreachable).

**`timelineSlice.ts`'s `loadStateAt`:** without the guard, `stateAt` becomes `undefined`
while `stateAtStatus` is set to `'loaded'` — `Timeline.tsx`'s own `stateAt === null` guard
(`undefined !== null`) does not catch this, so it crashes on `stateAt.at_virtual_ts` instead
of showing the intended error state.

**`eventsSlice.ts`'s `startPolling`:** without the guard, `result.data.events` throws inside
a bare `void (async () => {...})()` with no `.catch()`, inside a `setInterval` callback — an
unhandled promise rejection outside React's render cycle, invisible to `PanelErrorBoundary`
(which only catches render-time exceptions), repeating silently every `POLL_INTERVAL_MS` for
as long as the malformed response recurs.

**Fix:** both call sites now check `if (result.error || result.data === undefined)`,
identical to D-60's established pattern.

**Tests:**
- `frontend/tests/frontend/unit/timelineSlice.test.ts` (new, 4 tests): well-formed load,
  the documented `data: undefined, error: undefined` shape treated as an error (not
  "loaded" with undefined data), the ordinary `{error}` shape, and stale-in-flight-response
  non-clobbering.
- `frontend/tests/frontend/unit/eventsSlice.test.ts` (extended, +2 tests, new describe
  block): a `driveToPolling()` helper pushes the store through PRD §26.2's degrade-to-polling
  path (7 simulated socket closes — `scheduleReconnect` reads `wsReconnectAttempt` *before*
  incrementing it, so it takes `MAX_RECONNECT_ATTEMPTS + 1` closes to reach the ceiling, not
  6; this was diagnosed and fixed mid-repair, see "Test-infrastructure bug" below). One test
  asserts a malformed poll response produces zero unhandled rejections (via a
  `process.on('unhandledRejection', ...)` listener registered inside the test); the other
  confirms a well-formed response still applies events normally once polling.

**Test-infrastructure bug found and fixed while writing this test:** the first draft of
`driveToPolling()` looped 6 times (matching `MAX_RECONNECT_ATTEMPTS`), which left
`wsReconnectAttempt` at 6 and a 7th socket already opened and sitting in `'connecting'` —
not yet `'polling'`. Root cause: `scheduleReconnect` reads `attempt = get().wsReconnectAttempt`
*before* checking `attempt >= MAX_RECONNECT_ATTEMPTS`, so the ceiling is only reached on the
*next* close after the 6th reconnect has already been scheduled — i.e., the 7th close. Fixed
by looping 6 times (each scheduling a reconnect, advancing fake timers) then closing once
more without a timer advance (`startPolling` runs synchronously inside that final close).

**Mutation-verified (both halves, live, in this session):** reverting `timelineSlice.ts`'s
guard back to `if (result.error)` turned exactly the expected test red
(`expected 'loaded' to be 'error'`); reverting `eventsSlice.ts`'s guard similarly turned its
new test red (`expected [...] to have a length of +0 but got 1`). Both restored, `git diff
--stat` clean after each, full green re-confirmed.

## Finding #4 (MEDIUM) — Timeline Home/End jump to first/last event tick, not true run bounds

**What the audit found, measured against real fixture data:** `Timeline.tsx`'s keyboard
`Home`/`End` handlers and its "⏮ home"/"end ⏭" transport buttons all used
`ticks[0]`/`ticks[ticks.length - 1]` (the first/last *span boundary* already loaded in the
waterfall) instead of the true `0`/`virtual_makespan_ms` run bounds. Measured shortfall on
real fixtures: `code_pipeline` 6ms/13% short, `research_fanout` 18ms/29% short,
`support_triage` 17ms/**42%** short.

**Fix:** all four call sites (`Home` key, `End` key, "⏮ home" button, "end ⏭" button) now
call `seek(0)`/`seek(makespan)` directly — `seek` itself already clamps to `[0, makespan]`
and no-ops while `makespan` is `null` (before the waterfall loads), so this is safe at every
render state.

**Test:** `frontend/tests/frontend/unit/timelinePanel.test.tsx` (new, 4 tests) — a fixture
waterfall with one span from 20ms to 80ms inside a 100ms makespan; asserts keyboard `End`
reaches `virtualTs === 100` (not 80), keyboard `Home` reaches `0` (not 20), and both
transport buttons independently reach the same true bounds.

**Mutation-verified, both the keyboard pair and the button pair separately:** reverting the
keyboard handlers alone turned exactly the 2 keyboard tests red (button tests still passed,
confirming test isolation); reverting the button handlers alone turned exactly the 2 button
tests red. Both restored, `git diff --stat` clean, `tsc`/`eslint` clean.

## Finding #5 (MEDIUM) — `axe.spec.ts` never renders Graph/Findings/Chaos/Timeline

**What the audit found (carried forward from the P15 audit's own note, still open):**
every `axe.spec.ts` test only ever exercises Waterfall/Scorecard routes — none renders the
four P16 panels, so a real accessibility violation in any of them could ship undetected.

**Fix:** a new test, `run detail route, Graph/Findings/Chaos/Timeline panels`, added to
`axe.spec.ts` — mirrors `panels.spec.ts`'s own proven stub setup (`stubRun` +
`stubGraphFindingsState` against the real `code_pipeline` fixture, scenario left honestly
unstubbed/404 since that fixture has no `.scenario.json`), asserts all four panel regions are
visible, then runs `AxeBuilder` and asserts zero violations.

**Verification:** `tsc --noEmit` and `eslint` both clean on the new test. **Could not be
executed** — this sandbox's standing Playwright limitation (previously confirmed twice via
a missing `libXdamage.so.1` blocking Chromium launch) manifested a third way this attempt:
the dev server itself failed to start (`EPERM: operation not permitted, unlink
.../node_modules/.vite/deps/@visx_axis.js`), the same class of sandbox filesystem
restriction (cannot delete previously-written files) that has blocked several file cleanups
this session. Disclosed rather than worked around further, consistent with this session's
established practice for this specific, already-twice-confirmed environment gap. The new
test follows `panels.spec.ts`'s exact, already-proven-passing pattern and references real,
existing fixture files (`code_pipeline.{waterfall,graph,findings,state}.json`, confirmed
present on disk) — high confidence it is correct, but genuinely unexecuted in this sandbox.

## Finding #6 (LOW) — stale CONTEXT.md claim about `linking.ts` test coverage

**What the audit found:** CONTEXT.md §7's P16 paragraph claims "no new unit tests written
this session for `linking.ts`/`graphLayout.ts`/`scenario.ts`" — false for `linking.ts`:
`tests/frontend/unit/linking.test.ts` has 12 real, passing tests, committed the same session
(`d12b763`). Confirmed live: `npx vitest run tests/frontend/unit/linking.test.ts` → 12/12
passed; `git log --oneline -- frontend/tests/frontend/unit/linking.test.ts` → `d12b763`.

**Fix:** CONTEXT.md §7's P16 paragraph corrected with an inline note narrowing the claim to
what's actually true — the gap is real only for `graphLayout.ts` and `api/scenario.ts`.

## Finding #7 (LOW) — `graphHeat.ts`'s heat-ramp bucketing has no `C-n` ruling

**What the audit found:** `graphHeat.ts`'s quantile-bucketing approach (rank among a run's
own edges, not fixed ms thresholds) is a real PRD-silent-point interpretive call — the same
shape as C-29/C-30/C-32 for sibling panels — but was never logged as its own numbered ruling
in CONTEXT.md's "Known PRD-internal conflicts and their rulings" table.

**Fix:** new **C-35** added to that table, and `graphHeat.ts`'s own module header updated to
cite it (matching `waterfallBuckets.ts`/C-30's precedent of a ruling cited both in the ledger
and inline in the file). Also added `frontend/tests/frontend/unit/graphHeat.test.ts` (new, 9
tests) — the citation named a test file that did not yet exist; rather than cite a
nonexistent file (the exact class of fabricated-citation defect this project's own D-64/D-84
precedent exists to catch), a real test file was written: `meanHandoffMs`, quantile-ranking
behaviour (same relative ordering under very different absolute latencies produces identical
step assignments), single-edge/empty-edge-set edge cases, `heatToken`, `widthForMessages`.

**Mutation-verified:** temporarily changing the bucketing formula's multiplier (`* 6` →
`* 3`) turned 2 of the 9 tests red as expected; restored, `git diff --stat` clean.

## Verification (real, live execution)

- `npx tsc --noEmit` — clean.
- `npx eslint .` — 0 errors, 5 pre-existing warnings (unchanged pattern, none new).
- `npm run check:hex-literals` — clean (18 `.tsx` + 13 `.css` files).
- `npx vitest run` — **96/96 passed across 13 files** (77 baseline carried from the P15 cycle
  + 19 new: 4 `timelineSlice.test.ts`, 2 `eventsSlice.test.ts`, 4 `timelinePanel.test.tsx`, 9
  `graphHeat.test.ts`).
- Five independent live mutation probes (Finding #1's two guard fixes, Finding #4's keyboard
  pair and button pair separately, Finding #7's bucketing formula) each confirmed to turn the
  relevant new test(s) red, then fully restored — `git diff --stat` clean after every one.
- Playwright e2e: **not executable in this sandbox**, confirmed a third time via a new
  failure mode (dev-server startup EPERM, not just the previously-confirmed Chromium-launch
  libXdamage gap) — disclosed, not silently assumed passing, matching this project's I9 norm
  and this session's own established practice for this specific, recurring limitation.

## Deviations recorded

- **D-86** (§9): `prefsSlice` (density, panel sizes, `localStorage`-persisted — PRD §28.2,
  explicitly forwarded by P15's own `store/README.md`) never built. Open — needs a design
  amendment or an owner ruling on a minimal default before it's buildable without guessing.
- **D-87** (§9): PRD §20.5 Graph synchronization (`at_virtual_ts`-driven node/edge
  recoloring) not implemented. The backend's `get_graph(at_virtual_ts=...)` already exists
  but is a declared simplification (filters included nodes/edges only, not full state-based
  coloring) — wiring only that half would be a silently partial implementation; wiring the
  coloring half needs a real design pass (node/edge visual-state mapping, re-layout/debounce
  behaviour) this repair's time budget does not cover. Open.

## Not VERIFIED

A second independent re-audit is owed, same standing pattern as every module in this project
since P02.

See `op2-audit-p16.md` for the full audit account and CONTEXT.md §5 row 16 / §9 D-86,D-87 /
§13 for the ledger account.
