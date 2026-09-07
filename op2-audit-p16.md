# OP-2 INDEPENDENT AUDIT — P16 `frontend/` (Graph, Findings, Chaos, Timeline panels + live stream)

**Scope.** `frontend/src/{panels/{Graph,Findings,Chaos,Timeline}.*, panels/{graphLayout,graphHeat}.ts,
store/{graphSlice,findingsSlice,chaosSlice,eventsSlice,linking,index}.ts, api/scenario.ts,
components/PanelErrorBoundary.*}`, `frontend/tests/frontend/{unit,e2e}/*` (P16-authored specs and
`stubApi.ts`), `frontend/tests/frontend/fixtures/README.md`'s "P16 additions", `gen/make_fixtures.py`.
Backend counterpart checked for real wiring, not re-audited for its own internal correctness (already
covered by P14's own OP-2 for `ws.py`, and by P03/P14 build history for `store/snapshots.py`/
`routes/analysis.py`): `src/agentdx/api/{ws.py, routes/analysis.py, routes/runs.py}`,
`src/agentdx/store/snapshots.py`, `src/agentdx/config.py`'s `ws_*` constants. Explicitly out of
scope: P15's Control Tower shell, `tokens.css`, Waterfall/Scorecard panels, `runSlice`/
`selectionSlice`/`timelineSlice`'s P15-built halves — already independently audited
(`op2-audit-p15.md`, FAIL, repaired same day, `op3-repair-report-p15.md`). Where a P16-scope file
integrates with a P15-owned file (e.g. `Waterfall.tsx` now reading `linking.ts`'s
`highlightedSpanIds`), the integration point is in scope; the P15 file's own remaining correctness is
not.

**Method.** Read `CONTEXT.md` in full (§0, §2, the sequencing-hazards note above §5, row 16, §6 G8,
§7's P15/P16/P17 narratives, §8 C-32, §9 D-59/D-60/D-64/D-68/D-85, §10, §11), `AGENTS.md` in full, and
PRD FR-10 (~line 1238), §14.7 (~2356), §20.1–20.7 (~2951–3010), §26.1–26.3 (~3663–3830), §28.1–28.5
(~3972–4058). Read every P16-scope source file listed above end to end, plus
`frontend/src/store/README.md`, `frontend/src/api/README.md`, `frontend/src/panels/README.md`,
`frontend/tests/frontend/fixtures/README.md`, and the real backend files listed above. Cross-checked
the frontend's WebSocket client against `src/agentdx/api/ws.py`'s real, live protocol implementation
and `config.py`'s literal `ws_*` defaults (not inferred). Verified `crossHighlight.spec.ts`'s two
named span ids against the real, committed `code_pipeline.waterfall.json`/`code_pipeline.findings.json`
fixture data by direct JSON inspection (not just reading the test file).

**Executed for real, in this sandbox** (`frontend/`, Node v22.23.2 / npm 10.9.8, HEAD = `b641f88`,
clean tree): `npx tsc --noEmit` (clean, 0 errors), `npx eslint .` (0 errors / 5 pre-existing-pattern
warnings, matching the narrative), `npm run check:hex-literals` (clean — 18 `.tsx` + 13 `.css` files,
including all five P16-owned `.module.css` files), `npx vitest run` (**77/77 passed**, 10 files,
including `linking.test.ts` 12/12 and `eventsSlice.test.ts` 5/5 — see Finding #6). Re-attempted
`npx playwright install chromium` once: succeeds at the download step but `browserType.launch`
still fails on the same missing shared library this project's prior audits already documented
(`libXdamage.so.1`; no root, `sudo` refused by the sandbox's own "no new privileges" flag, and the
egress proxy 403s a direct `.deb` fetch). This session additionally hit a second, independent
environment fault while probing further: several pre-existing files under `frontend/test-results/`
and `frontend/node_modules/.vite/` cannot be unlinked (`EPERM`) on this FUSE-mounted tree even by
their own owning uid, which also prevented the Vite dev server itself from starting under
`--output=/tmp/...`. **No Playwright e2e spec was executed against a real browser.** Per the
assignment's own fallback instruction, this is not re-litigated further — static reading of
`crossHighlight.spec.ts`, `panels.spec.ts`, `chaos.spec.ts`, `wsReconnect.spec.ts`, `perf.spec.ts`,
and `axe.spec.ts` is the evidence used below, cross-checked against real fixture JSON wherever
possible (see Finding #5 and the cross-highlight positive control). Python-side backend code
(`ws.py`, `snapshots.py`, `routes/analysis.py`) could not be executed live either — this sandbox's
Python is 3.10.12 (D-66, already documented project-wide: `agentdx/__init__.py` chains into
`tomllib`/PEP-695 syntax unimportable under 3.10) — verified by static reading and by cross-checking
literal constants in `config.py` against the WS protocol table, and by parsing the real, committed
fixture JSON with plain Python (no `agentdx` import required).

---

## VERDICT: **FAIL**

One HIGH finding and four MEDIUM findings, all real and demonstrated against the live, current
`HEAD` tree — not inferred from the ledger's prose. The module's four self-reported bugs (missing
`getBezierPath`, the `_nearest_leaf_span` evidence-resolution gap, the `run?.run_id` vs.
`runSlice.runId` mismatch, and D-60's `PanelErrorBoundary`) all **genuinely hold** under
re-verification — three of them checked against live fixture data, not just re-read code. The
WebSocket protocol (§26.2) is a strong, faithful implementation, cross-verified end to end against
the real server. But the same defect class D-60 was built to eliminate — an unguarded
`openapi-fetch` result — survives, unpatched, in two of the six real network call sites P16 itself
added; a named PRD slice explicitly forwarded to this module's own scope was never built at all;
and two named PRD/FR-10 behaviours (scrub-synchronised graph rendering, and Home/End reaching true
run bounds) are either unimplemented or measurably short of spec, neither disclosed anywhere in
`CONTEXT.md`.

1. **(HIGH)** D-60's own fix — treating `{data: undefined, error: undefined}` as failure, not
   success — was applied to 4 of 6 real `api.GET`/`api.POST` call sites P16 added, but missed two:
   `timelineSlice.ts`'s `loadStateAt` and `eventsSlice.ts`'s `startPolling` (the WS-degrade-to-polling
   fallback). Both reproduce the exact, already-documented-as-reachable trigger.
2. **(MEDIUM)** `prefsSlice` (PRD §28.2: reduced motion / density / panel sizes, persisted to
   `localStorage`) was explicitly forwarded to P16 by P15's own `store/README.md` and was never
   built. Zero `localStorage` use anywhere in `frontend/src`. Not declared as a deviation or a scope
   cut anywhere in `CONTEXT.md`.
3. **(MEDIUM)** PRD §20.5 "Graph synchronisation" — nodes coloured by state at the scrub instant,
   edges weighted by messages delivered "so far" — is not implemented. `graphSlice`/`GraphPanel`
   never pass `at_virtual_ts` to `GET /api/runs/{id}/graph`, even though the real backend route
   already supports that query parameter. The Graph panel is scrub-position-invariant.
4. **(MEDIUM)** Timeline's Home/End (keyboard and transport buttons) do not reach the literal PRD
   "run bounds" (virtual ts 0 and the true `virtual_makespan_ms`, FR10-AC3/§20.2) — they jump to the
   first/last rendered *span tick* instead, which measurably differs from true bounds on all three
   real fixtures (up to 42% of the run short of the true end on `support_triage`). Disclosed only in
   a code comment, never logged in `CONTEXT.md` §9.
5. **(MEDIUM)** `axe.spec.ts` (NFR-7's zero-violations gate) never renders Graph, Findings, Chaos or
   Timeline — it exercises only `RunListRoute`, the Waterfall route (twice) and the Scorecard route.
   "NFR-7 passes" is not evidence about any P16 panel's own accessibility. (Already flagged by the
   P15 audit as a carried-forward P16 gap — confirmed still open today, unchanged since.)
6. **(LOW)** `CONTEXT.md`'s own P16 narrative ("no new unit tests written this session for
   `linking.ts`/`graphLayout.ts`/`scenario.ts` — declared gap") is factually wrong for `linking.ts`:
   12 real, discriminating unit tests exist, committed in the same P16 commit (`d12b763`). The claim
   holds only for `graphLayout.ts` and `api/scenario.ts`, which genuinely still have zero direct
   tests today.
7. **(LOW)** `graphHeat.ts`'s quantile (rank-among-this-run's-edges) mapping from handoff latency to
   one of six heat-ramp steps is a reasonable, disclosed interpretation of a PRD-silent point (§28.3
   names no absolute latency scale) but — unlike similar interpretive calls elsewhere in this project
   (C-29, C-30, C-32) — carries no corresponding `CONTEXT.md` §10 ruling.

---

## FINDING #1 (HIGH) — D-60's unguarded-`openapi-fetch`-result defect survives in 2 of 6 P16 call sites: `timelineSlice.loadStateAt` and `eventsSlice.startPolling`

**Where:** `frontend/src/store/timelineSlice.ts:56–68` (`loadStateAt`); `frontend/src/panels/Timeline.tsx:362–397` (`StateTable`, the consumer); `frontend/src/store/eventsSlice.ts:166–181` (`startPolling`).

**What D-60 established (§9, and §7's P16 narrative, bug ④):** `openapi-fetch` can return
`{data: undefined, error: undefined}` on a non-JSON error body — this build's own Vite dev-proxy 500
page when the backend is unreachable is the documented, real trigger. Trusting `!result.error` alone
lets `data` reach downstream code as `undefined` while the caller believes the fetch succeeded. The
fix applied to `graphSlice.loadGraph`, `findingsSlice.loadFindings`, `chaosSlice.loadScenario` and
`chaosSlice.fireStaged` is uniform: `if (result.error || result.data === undefined) { ...error...
return; }`.

**What `timelineSlice.loadStateAt` actually does** (full function, lines 56–68):

```ts
loadStateAt: async (runId, virtualTs) => {
  set({ stateAtStatus: 'loading', stateAtError: null });
  const result = await api.GET('/api/runs/{run_id}/state', {
    params: { path: { run_id: runId }, query: { at_virtual_ts: virtualTs } },
  });
  if (get().virtualTs !== virtualTs) return;
  if (result.error) {
    set({ stateAtStatus: 'error', stateAtError: 'State reconstruction failed.' });
    return;
  }
  set({ stateAt: result.data, stateAtStatus: 'loaded' });
},
```

Only `result.error` is checked — the `result.data === undefined` half of D-60's own fix is absent.
On the documented trigger, this sets `stateAtStatus: 'loaded'` with `stateAt: undefined`.
`Timeline.tsx`'s `StateTable` then guards with `if (status === 'error' || stateAt === null)` (line
381) — `undefined !== null`, so this guard is bypassed, and the very next line dereferences
`stateAt.at_virtual_ts` (line 391) on `undefined`, throwing. This is a render-time crash, so
`PanelErrorBoundary` (D-60's own containment mechanism, confirmed correctly wired for all five
`RunRoute.tsx` panels) does catch it — but the Timeline panel then shows "Timeline scrubber hit an
unexpected error" instead of the intended, honest "State reconstruction failed" message, and per
`PanelErrorBoundary.tsx`'s own documented contract ("does not reset its own `state.error` on prop
changes... `key={runId}` is what actually clears it"), the entire panel stays wedged in that fallback
for the rest of the page view — scrubbing again cannot recover it, only navigating away and back can.

**What `eventsSlice.startPolling` actually does** (lines 166–181):

```ts
function startPolling(runId: string): void {
  if (runtime.torn) return;
  set({ wsStatus: 'polling' });
  clearTimers();
  runtime.pollTimer = setInterval(() => {
    void (async () => {
      if (runtime.torn) return;
      const fromSeq = get().maxSeqSeen + 1;
      const result = await api.GET('/api/runs/{run_id}/events', {
        params: { path: { run_id: runId }, query: { from_seq: fromSeq, limit: 1000 } },
      });
      if (result.error || runtime.torn) return;
      for (const event of result.data.events) applyEvent(event as unknown as LiveEvent);
    })();
  }, POLL_INTERVAL_MS);
}
```

Same gap: only `result.error` is checked before `result.data.events` is dereferenced. Here the
manifestation is different and, in one respect, worse: this runs inside a bare `void (async () =>
{...})()` with no `.catch()`, inside a `setInterval` callback — a thrown error here becomes an
**unhandled promise rejection outside React's render cycle**, which `PanelErrorBoundary` cannot see
at all (it only catches render-time exceptions). The interval keeps firing every
`POLL_INTERVAL_MS` (2s), silently repeating the same unguarded dereference and the same unhandled
rejection on every tick a malformed response is hit, with nothing visible to the user beyond the
already-present "polling (degraded)" badge — no distinct error state, no recovery signal, only a
devtools console error stream.

**Why this matters:** this is not a hypothetical edge case — `CONTEXT.md`'s own P16 narrative
(§7, bug ④) states plainly that the triggering condition ("a non-JSON error body... this build's Vite
dev-proxy 500 page when the backend is unreachable") is real and was actually hit during this exact
build's own development. The fix for it was applied thoroughly to three slices and missed in a
fourth and fifth call site of the same shape, in the same commit.

**Severity:** HIGH. Reachable via a documented, real trigger; degrades gracefully in one case
(contained by `PanelErrorBoundary`, but with no way for the user to recover short of navigating away)
and silently in the other (an infinite, invisible-to-the-UI polling failure loop).

**Suggested fix direction:** apply the identical `result.error || result.data === undefined` guard to
both call sites, matching the four already-fixed sites verbatim; add a regression test for each
(the existing `graphSlice`/`findingsSlice`/`chaosSlice` fixes have no equivalent guard test either,
so a new pattern-wide test — e.g. asserting all `api.GET`/`api.POST` call sites in `store/` share this
guard — would prevent a sixth instance).

---

## FINDING #2 (MEDIUM) — `prefsSlice` (PRD §28.2), explicitly forwarded to P16 scope, was never built

**Where:** `frontend/src/store/README.md:6`; `frontend/src/store/index.ts` (7 slices wired, no
`prefsSlice`); absence of `frontend/src/store/prefsSlice.ts`.

PRD §28.2's Zustand slice table names `prefsSlice`: **"Reduced motion, density, panel sizes |
Persisted to `localStorage`"**. P15's own `store/README.md`, committed at P15 build time and never
edited since, states explicitly: *"`chaosSlice`/`prefsSlice`/the fuller `eventsSlice` are P16
scope."* P16 built `chaosSlice` and extended `eventsSlice` — both named in that same sentence — but
never built `prefsSlice`.

Confirmed by direct search: `grep -rn "localStorage" frontend/src` returns **zero** matches anywhere
in the whole frontend source tree. `store/index.ts`'s `ControlTowerStore` type is exactly
`RunSlice & SelectionSlice & TimelineSlice & FindingsSlice & GraphSlice & ChaosSlice & EventsSlice` —
seven slices, no eighth. "Reduced motion" is partially covered as a side effect — `Graph.module.css`,
`Waterfall.module.css`, `Timeline.module.css` and `Bar.module.css` all contain a
`@media (prefers-reduced-motion: reduce)` rule reading the OS-level setting directly — but that is
CSS reading browser state, not a Zustand slice with an app-level override or persistence, and
"density" and "panel sizes" have no implementation of any kind, persisted or not.

This is not mentioned anywhere in `CONTEXT.md`: not in §5 row 16, not in §7's P16 narrative (which
lists what P16 built module by module), not in §9's deviations, not in §12's scope-cut order. It
meets `CONTEXT.md` §11 tripwire 14's own definition almost exactly ("a PRD requirement inside a
completed prompt's scope, neither implemented nor declared") — the "completed prompt's scope" part
resting on P15's own, already-committed forwarding statement rather than an inference.

**Severity:** MEDIUM. Not a crash or a data-integrity issue; a named, PRD-required, explicitly-forwarded
piece of state management is simply absent, with no code, no test, and no ledger record of the gap.

**Suggested fix direction:** either build `prefsSlice` (reduced-motion override toggle, density,
panel-size persistence via `localStorage`, per §28.2) or add a `CONTEXT.md` §9 deviation declaring it
cut and why — the current state (silently absent, contradicting a specific prior-module forwarding
note) is the one outcome AGENTS.md §2/§3 exist to prevent.

---

## FINDING #3 (MEDIUM) — PRD §20.5 "Graph synchronisation" (scrub-position-aware node/edge rendering) is not implemented

**Where:** `frontend/src/store/graphSlice.ts` (no `at_virtual_ts`/`virtualTs` reference anywhere);
`frontend/src/panels/Graph.tsx:38–39,88` (the only use of `virtualTs`); PRD §20.5 (line ~2992);
`src/agentdx/api/routes/analysis.py:100–123` (`get_graph`, which already accepts `at_virtual_ts`).

PRD §20.5, in full: *"At a given scrub position the graph panel renders: nodes coloured by their
state at that instant (`idle`/`running`/`blocked`/`crashed`), edges weighted by messages delivered
**so far**, a ring on any agent with an active fault, and the critical path highlighted along its
edges."* This is a named, specific behaviour of the Graph panel while the Timeline scrubber is in
use.

`graphSlice.loadGraph(runId)` takes only a `runId` and is called exactly once, on mount
(`Graph.tsx`'s `useLoadGraph`) — it is never re-invoked, and never passes `at_virtual_ts`, when
`timelineSlice.virtualTs` changes. `GraphPanel`'s own `virtualTs` read (line 39) is used for exactly
one purpose: gating whether the live-message pulse animation is enabled (`pulseEnabled = wsStatus ===
'open' && timelineMode === 'live' && virtualTs === null`, line 88) — it never affects which nodes are
shown, their colour, or edge weight. The real backend route already supports the relevant query
parameter (`analysis.py`'s `get_graph(..., at_virtual_ts: int | None = None)`, itself declaring its
own, narrower simplification: "restricts the *reported* nodes/edges to ones whose own activity had
started by that instant" rather than full historical state) — but the frontend never calls it with
one. The Graph panel is, in effect, entirely scrub-position-invariant: it always shows the same,
final-state graph regardless of where the Timeline is scrubbed to.

This is distinct from — and not covered by — cross-panel *selection* linking (`linking.ts`,
`highlightedAgentIds`/`highlightedEdge`), which correctly derives from `selectionSlice` and works as
specified; §20.5 is about the scrubber's *own* effect on the graph, a different, PRD-named
requirement with no implementation at all.

Not mentioned anywhere in `CONTEXT.md` §7's P16 narrative, §9's deviations, or §10/§11.

**Severity:** MEDIUM. FR-10 (the section §20.5 lives under) is itself P1 tier ("replay engine is P0;
the scrubbing UI is P1"), which softens this relative to a P0 gap — but it is a specific, testable,
named requirement with zero implementation and zero disclosure.

**Suggested fix direction:** thread `virtualTs` into `loadGraph` (debounced, matching
`Timeline.tsx`'s existing 80ms debounce pattern for `loadStateAt`) and pass it as `at_virtual_ts`;
alternatively, derive node/edge state client-side from the already-loaded `stateAt`/live-event stream
if a second network round-trip per scrub tick is judged too costly — either way, declare whichever
approach (or the decision to defer this to a later prompt) as a `CONTEXT.md` §9 row.

---

## FINDING #4 (MEDIUM) — Timeline Home/End do not reach the literal PRD "run bounds"; demonstrated with real fixture data, up to 42% short

**Where:** `frontend/src/panels/Timeline.tsx:172–179` (keyboard `Home`/`End`), `:201–218` (transport
buttons), `:249–258` (`eventTicks`); PRD §20.2 (line 2967: *"`home`/`end` jump to run bounds"*);
FR10-AC3 (line 1252: *"`j`/`k` jump between findings; `←`/`→` step events; `shift` ×10"* — the PRD's
FR-10 acceptance text and §20.2's keyboard table together name `home`/`end`).

`Timeline.tsx`'s own code comment (lines 23–27) discloses the mechanism: *"this build has no slice
that loads the full per-run event list... every distinct span boundary (`start_ms`/`end_ms`) already
loaded in the waterfall is used as the stepping resolution instead."* `eventTicks` (lines 249–258)
collects every span's `start_ms`/`end_ms` into a sorted, deduplicated array; Home seeks to
`ticks[0]`, End seeks to `ticks[ticks.length - 1]` (both the keyboard handlers and the transport
buttons). But the Scrubber's own range input is `min={0} max={makespan}` (line 346–347) — the panel
itself supports seeking to the *true* bounds — and `makespan` is `waterfall.virtual_makespan_ms`
(line 48), a materially different number from the first/last tick whenever a run has lead-in before
its first span or trail-off after its last, which `CONTEXT.md`'s own **C-19** ruling establishes is a
real, nonzero, unavoidable property of every run (`run_boundary` weight).

**Verified directly against the real, committed fixture data** (not inferred):

| Fixture | `virtual_makespan_ms` | tick range (`min_start` .. `max_end`) | End-key shortfall |
|---|---|---|---|
| `code_pipeline` | 47 | 8 .. 41 | 6ms (13%) |
| `research_fanout` | 63 | 9 .. 45 | 18ms (29%) |
| `support_triage` | 40 | 9 .. 23 | 17ms (**42%**) |

(Computed with `python3 -c "import json; ..."` directly against
`frontend/tests/frontend/fixtures/{code_pipeline,research_fanout,support_triage}.waterfall.json`.)

On `support_triage`, pressing `End` lands the scrubber at 23ms of a 40ms run — 42% of the run's own
virtual duration is unreachable via the one control PRD §20.2 names specifically for reaching it.
Home has the same shape at the start (8–9ms short of the literal `0`).

This gap is disclosed in the component's own code comment as a "declared simplification," but it is
not logged anywhere in `CONTEXT.md` §9 — no `D-n` row, no mention in §7's P16 narrative (which does
name several other, smaller "declared simplification" items, e.g. the `PLAY_TICK_MS` playback-speed
approximation, but not this one) — despite `AGENTS.md`'s explicit rule that an improvisation "goes
here in the same commit."

**Severity:** MEDIUM. A specific, PRD-named, testable keyboard behaviour (FR10-AC3/§20.2) measurably
fails to do what it says on real, shipped data — not a crash, but a genuine functional shortfall a
user would notice (scrubbing to "the end" of a run and finding it visibly isn't the end).

**Suggested fix direction:** clamp Home/End to `0`/`makespan` directly (the `Scrubber`'s own `seek`
already clamps to that exact range) rather than to the tick array's own extremes — this requires no
new data source, since `makespan` is already loaded. Log the correction (or, if the tick-based
behaviour is intentionally preferred, log the deviation) as a `CONTEXT.md` §9 row either way.

---

## FINDING #5 (MEDIUM) — `axe.spec.ts` (NFR-7) never renders any of the four P16 panels

**Where:** `frontend/tests/frontend/e2e/axe.spec.ts` (full file, 42 lines).

The file's four tests exercise: the run-list route (empty state), the Waterfall route with a pending
ghost baseline, the Waterfall route with a populated ghost baseline, and the Scorecard route. None of
the four calls `stubGraphFindingsState` (the helper every P16 e2e spec uses to stub `/graph`,
`/findings` and `/api/runs/{id}/state` so those panels can actually render past their loading state) —
confirmed by direct search: `stubGraphFindingsState` does not appear anywhere in `axe.spec.ts`. Every
run this file exercises therefore has Graph, Findings, Chaos and Timeline stuck on their own
`'idle'`/`'loading'` guard clauses (e.g. `Graph.tsx:55–61`'s "Loading graph…" branch) for the whole
duration of the axe scan — their real, data-populated DOM (agent nodes, finding rows, the fault
catalogue, the scrub track, the suppressed-findings drawer) is never present when `AxeBuilder().
analyze()` runs.

This means NFR-7's "zero violations" claim, wherever it is cited for this build, is genuine evidence
for `RunListRoute`/`Waterfall`/`Scorecard` (all P15-scope, already independently confirmed by the P15
audit) and **zero evidence** for any of the four P16 panels' own accessibility — despite those panels
containing substantial, good-faith ARIA surface worth verifying (`Graph.tsx`'s screen-reader table
fallback and `aria-label`s on every node; `Findings.tsx`'s `role="group"` filter bars and
`aria-pressed` toggles; `Chaos.tsx`'s `role="group"`/`aria-label="Armed fault, awaiting
confirmation"`; `Timeline.tsx`'s `aria-valuetext` on the scrub range input).

This exact gap was already named in the P15 audit's own "what was checked and holds up" account and
in `CONTEXT.md` §7's "Blocked on" carry-forward list ("`axe.spec.ts` per-panel coverage gap (P16)").
Re-checked directly against the current file content in this audit (not re-derived from the ledger's
claim alone, per this audit's own charter): **confirmed still open, unchanged, as of `HEAD`.**

**Severity:** MEDIUM. Not a defect in the panels themselves (their markup, read directly, looks
genuinely accessibility-conscious — see "positive controls" below) — but a real, uncorrected gap in
what the project's own accessibility gate actually verifies, carried forward through at least two
audit cycles without being closed.

**Suggested fix direction:** add one `axe.spec.ts` case per P16 panel (or one combined case using
`stubGraphFindingsState`, matching `panels.spec.ts`'s own setup) asserting zero violations against
each panel's real, populated state, including the Chaos panel's armed/confirm view and the Findings
panel's expanded suppressed drawer — both distinct DOM states worth their own scan.

---

## FINDING #6 (LOW) — `CONTEXT.md`'s "no new unit tests" claim is false for `linking.ts`/`eventsSlice.ts`; true only for `graphLayout.ts`/`api/scenario.ts`

**Where:** `CONTEXT.md` §7 (P16 narrative): *"45/45 Vitest unit tests (no new unit tests written this
session for `linking.ts`/`graphLayout.ts`/`scenario.ts` — declared gap, see 'Blocked on')."*
`frontend/tests/frontend/unit/linking.test.ts` (208 lines, 12 tests); `frontend/tests/frontend/unit/
eventsSlice.test.ts` (215 lines, 5 tests) — both committed in the same P16 commit (`git show d12b763
--stat` shows both files added at 208/215 lines, alongside the panel/store code the same claim
describes).

Run live in this audit: `npx vitest run` reports `tests/frontend/unit/linking.test.ts (12 tests)` and
`tests/frontend/unit/eventsSlice.test.ts (5 tests)`, both fully passing, as part of the real 77/77
total. `linking.test.ts`'s own header comment even self-identifies as closing exactly this gap:
*"`linking.ts` shipped (P16) with zero unit tests — a declared gap (CONTEXT.md §7 'Blocked on')...
These tests use **two** hand-authored findings with deliberately different agents, spans, seqs and
evidence shapes."* Read in full (see Finding-adjacent "positive controls" below): these are genuinely
discriminating tests, not padding.

The claim is **true** for the other two names it lists: no test file anywhere references
`graphLayout.ts`'s `layoutGraph`, and none references `api/scenario.ts`'s `parseResolvedScenario`/
`blastRadiusIsEmpty` (confirmed by direct grep across `frontend/tests/`) — both remain genuinely
untested today.

This is a minor, disclosed-elsewhere-in-spirit inconsistency (the `linking.test.ts` file's own header
comment is more accurate than the ledger row that supposedly describes the same session's output),
most likely attributable to the same commit-reconstruction provenance issue `CONTEXT.md` itself
already names for this exact era of work (**D-64**: P15/P16/P17 built in one continuous, then-
uncommitted tree, reconstructed into separate commits after the fact by a later session) — i.e. this
is very plausibly the same underlying pattern D-64/D-84 already document, not a new failure mode, but
it is a concrete instance worth recording rather than silently trusting the ledger's prose the way
this audit's own charter warns against.

**Severity:** LOW. No functional impact — if anything, the truth (tests exist) is better than the
claim (no tests) for two of the three named files. Recorded because an auditor or a future session
reading `CONTEXT.md`'s "Blocked on" line at face value would wrongly conclude `linking.ts` — the
single most safety-critical file for gate G8's cross-highlight correctness — has zero test coverage,
when it in fact has strong, real coverage.

**Suggested fix direction:** correct the `CONTEXT.md` §7 P16 paragraph (and its `docs/journal/
2026-33.md` copy is exempt from correction per **C-27**, left as historical record) to name only
`graphLayout.ts`/`api/scenario.ts` as the standing gap.

---

## FINDING #7 (LOW) — `graphHeat.ts`'s latency-ramp bucketing is an undeclared PRD-silent-point interpretation

**Where:** `frontend/src/panels/graphHeat.ts:20–37` (`heatStepsForEdges`).

PRD §28.3 names the Graph panel's edge colour as "latency (the heat ramp)" and §29.1 names six
`--heat-0`..`--heat-5` design tokens on a "fast → slow" ramp, but gives no absolute millisecond
thresholds for which edge gets which step. `heatStepsForEdges` resolves this by ranking edges by
their own mean handoff latency *within the current run* and bucketing by rank into sixths — a
reasonable, deterministic, PRD-consistent choice, and the function's own doc comment explains the
reasoning candidly ("a fixed-ms threshold picked without one would be an invented number this build
cannot back with a PRD citation"). This is functionally fine and was not observed to produce any
incorrect behaviour.

What is missing is process, not substance: this project has an established pattern for exactly this
situation — a PRD-silent point resolved by a documented, testable ruling recorded in `CONTEXT.md` §10
(e.g. **C-29** for the Scorecard payload shape, **C-30** for the waterfall's six-bucket→four-fill
mapping, **C-32** for this very panel's own finding-evidence shape). `graphHeat.ts`'s ramp choice has
no equivalent `C-n` row.

**Severity:** LOW. A documentation-process gap on an already-correct, already-disclosed-in-code
decision — not a functional defect.

**Suggested fix direction:** add a `C-n` row citing `graphHeat.ts`'s own doc comment, same shape as
C-29/C-30/C-32.

---

## Positive controls — checked and genuinely correct, not just absence of finding

- **Bug ① (missing `getBezierPath`) is fixed and holds.** `Graph.tsx`'s `AgentEdgeLabel` (lines
  162–201) genuinely calls `getBezierPath({...})` and renders through `BaseEdge`/`EdgeLabelRenderer`,
  not a label-only stub.
- **Bug ② (`_nearest_leaf_span`) is fixed and holds — verified against real, live data, not just
  code.** `gen/make_fixtures.py:140–162` implements the described containing-range/nearest-by-
  distance/preceding-tiebreak algorithm. Parsed the real, committed
  `code_pipeline.waterfall.json`/`code_pipeline.findings.json` directly: the one real finding's
  evidence (`span_ids: ['1539b0159eb5', '5f55856dac38']`) names two span ids that **do** exist among
  the seven spans `get_waterfall` actually renders for this fixture — `crossHighlight.spec.ts`'s
  target spans are real, not stale.
- **Bug ③ (`run?.run_id` vs. `runSlice.runId`) is fixed and holds in both call sites.**
  `Chaos.tsx:39` and `Timeline.tsx:45` both read `useControlTowerStore((s) => s.runId)` (`runSlice`'s
  own field), not `run?.run_id`.
- **D-60 (`PanelErrorBoundary`) is fully and correctly wired — no bypass found.** All five of
  `RunRoute.tsx`'s panels (Graph, Chaos, Findings, Waterfall, Timeline) are wrapped, each
  `key={runId}`-scoped (confirmed lines 115–138). Grepped every reference to
  `GraphPanel`/`ChaosPanel`/`FindingsPanel`/`TimelinePanel`/`WaterfallPanel` across `frontend/src` —
  `RunRoute.tsx` is the only mount site for any of them; no alternate, unwrapped route exists (unlike
  P15's `ScorecardRoute.tsx` gap, which the P15 audit already found and repaired).
- **D-59 (hand-rolled deterministic layout) genuinely is deterministic.** `graphLayout.ts`'s
  `layoutGraph` contains no `Math.random`, no wall-clock read, and no unordered-`Set`/`Map`-key
  iteration that could vary run to run — node ids are sorted before every loop that matters, layering
  uses a bounded BFS relaxation, and within-layer ordering is `.sort()`. Matches the deviation's own
  claimed algorithm exactly.
- **The WebSocket protocol (§26.2) is a strong, faithful, end-to-end-consistent implementation,**
  cross-checked against the real server (`ws.py`) rather than assumed: backlog batching (`config.py`'s
  `ws_backlog_batch_size = 1_000`, matching PRD's "1000"), flow control (`ws_flow_control_max_unacked
  = 5_000`, matching PRD's "5000"; the client sends a real `ack` with `through_seq: maxSeqSeen` after
  every `event`/`events` frame), heartbeat (`HEARTBEAT_INTERVAL_MS = 15000` client-side ping;
  `ws_heartbeat_timeout_s = 45.0` server-side, matching PRD's "15s"/"45s" exactly), reconnect at
  `from_seq = maxSeqSeen + 1` (real exponential backoff, 500ms→8s cap with jitter, 6 attempts before
  degrading), degrade-to-polling with the visible `wsStatus`/`data-ws-status` badge
  (`RunRoute.tsx`'s `WsStatusBadge`, genuinely rendered, not just modeled in the store), and sampling
  (`wsSamplingN` recorded verbatim from the server's `status` frame, never inferred). The dedup
  guarantee ("no event ever delivered twice") is proven by a real, live-executed unit test
  (`eventsSlice.test.ts`, resends a genuine duplicate seq and asserts it's dropped, not just a mock
  server that never actually resends one — `wsReconnect.spec.ts`'s own comment candidly discloses
  that its e2e-level claim is narrower than it sounds, and points at this exact unit test for the real
  proof).
- **Cross-panel linking (§20.3, C-32, gate G8) genuinely derives from one shared `selectionSlice`,
  with no parallel highlight state anywhere.** `store/index.ts` wires exactly seven slices (four P15,
  three P16); `linking.ts`'s selectors are the only place `highlightedAgentIds`/`highlightedSpanIds`/
  `highlightedEdge`/`highlightedEventSeqs` are computed, and both `Graph.tsx` and the P15-owned
  `Waterfall.tsx` now read from these same selectors (`Waterfall.tsx:44` imports and uses
  `highlightedSpanIds` from `linking.ts`) rather than maintaining independent selection logic.
  `crossHighlight.spec.ts` (read, not executed) asserts **both** halves PRD §20.3 requires — the two
  waterfall spans *and* the two graph nodes highlight together on one finding click — not just one,
  matching the literal G8 gate text.
- **The suppressed-findings drawer (§14.7) is correctly implemented.** `visibleFindings`/
  `suppressedFindings` (`linking.ts:184–198`) correctly partition on `suppressed_by === null` vs.
  `!== null`; `Findings.tsx`'s `SuppressedDrawer` is collapsed by default, shows a real count, and
  `includeSuppressed` correctly gates only the expansion, not the count.
- **FR-10's real backing algorithm (§20.4) is genuinely correct** — this is P03/P14-built code
  (`store/snapshots.py`), not P16's own work, but P16's Timeline panel correctly delegates to it
  rather than reimplementing a shortcut: `last_seq_at_or_before` uses `SELECT MAX(seq) FROM events
  WHERE virtual_ts_ms <= ?` — a real highest-seq tiebreak at the target timestamp, exactly matching
  §20.4's pseudocode — and `nearest_snapshot`/`state_at` implement the snapshot-every-500-events
  acceleration exactly as specified.
- **D-68's correction is honored correctly wherever it matters.** `bench/results/scrub-
  reconstruction.json` carries the disclosed `gate_status` field explaining the p95 figures are real,
  variable wall-clock measurements; `CONTEXT.md` §7's live P16 prose no longer quotes a fixed digit
  (it now embeds the full "Correction (2026-08-29, D-68)" paragraph verbatim); the one place a stale
  "9.1ms/10.8ms" figure still appears verbatim, `docs/journal/2026-33.md:176`, is the intentionally-
  preserved historical archive copy, correctly exempted from re-editing by **C-27**'s own ruling — not
  a live violation.
- **The two-step Chaos arm→confirm→fire flow (Design Constraint 2, I12) genuinely has no code path
  that reaches `fireStaged` without the blast-radius-bearing confirm view rendering first** —
  `Chaos.tsx`'s only route to `ArmedConfirm` is `staged !== null`, itself only settable by `arm()`,
  and `BlastRadiusDisplay` renders unconditionally inside that same branch, before the Fire button.
  `chaos.spec.ts` (read, not executed) asserts the DOM literally contains no `chaos-fire-button`
  element at all prior to arming — not merely hidden/disabled.
- **No I1/determinism concern found in P16's own code.** The one `Math.random()` call
  (`eventsSlice.ts`'s reconnect-backoff jitter, line 162) is UI reconnection-timing only — it never
  touches the event log, the canonical projection, or anything replay-relevant, and this file lives
  entirely outside `src/agentdx/`'s enforced determinism boundary (AGENTS.md §4.1). Nothing in the
  live WebSocket UI path was found to make the same event stream render differently across two page
  loads in a way that would matter to I1 (the store's own state — `liveEvents`, `maxSeqSeen`, finding
  list — is a pure function of the events actually received, dedup'd by `seq`).

---

## What was checked and holds up

- `npx tsc --noEmit`, `npx eslint .`, `npm run check:hex-literals`, `npx vitest run` all genuinely
  clean/passing against the real, current `HEAD` tree (see Method for exact figures) — matching the
  `CONTEXT.md` narrative's own claims for these four commands digit-for-digit.
- The real backend wire shape (`_project_event` in `routes/runs.py`, reused verbatim by `ws.py`'s
  `_event_json`) matches `eventsSlice.ts`'s/the e2e specs' own `LiveEvent` shape field-for-field
  (`schema_version`, `run_id`, `seq`, `sched_step`, `virtual_ts_ms`, `wall_ts_ms`, `vclock`, `type`,
  `causal_parents`, `payload`, `agent_id`, `clock_slot`, `span_id`, `fault_id`).
  `panels.spec.ts`/`chaos.spec.ts` (read, not executed) exercise all three real fixtures
  (`code_pipeline`, `research_fanout`, `support_triage`), not just one synthetic case.
- `findingsSlice.loadFindings`'s deliberate "always fetch `include_suppressed=true` once, filter
  client-side" design (documented inline) is a reasonable, disclosed simplification of PRD §26.1's
  `include_suppressed` query parameter — not a defect.
- The Findings panel's I10 honesty requirement ("bounded search... absence of findings is not proof
  of absence") is rendered as real, distinct copy when `findings.length === 0`, not silently
  presented as an ambiguous empty list (`Findings.tsx:83–90`).

---

## NOT DONE / RISKS

- **No Playwright e2e spec was executed against a real browser** in this sandbox — same
  `libXdamage.so.1`/no-root limitation this project's prior frontend audits already documented, plus
  a second, apparently independent `EPERM` fault on this FUSE-mounted tree that also blocked a bare
  Vite dev-server boot attempt via an alternate output directory. Findings #4 and #5, and the
  cross-highlight/chaos positive controls, were established by static reading of the specs
  cross-checked against real fixture JSON and real source code, per the assignment's own fallback
  instruction — not by watching the specs actually run and pass or fail.
- **No live execution of the Python backend** (`ws.py`, `snapshots.py`, `routes/analysis.py`,
  `routes/runs.py`) — this sandbox's Python is 3.10.12 only (D-66, project-wide, already documented);
  `agentdx.*` cannot be imported at all under it. Verified these files by static reading, by
  cross-checking literal constants (`ws_backlog_batch_size`, `ws_flow_control_max_unacked`,
  `ws_heartbeat_timeout_s`) directly against PRD §26.2's table, and by parsing the real, committed
  fixture JSON with plain Python (no `agentdx` import required) for Finding #4's numeric claims. Did
  not attempt the disclosed stdlib-shim methodology other recent audits in this project's history used
  to run the real Python suite — out of this audit's time budget, and P16's own scope is
  frontend-first per the assignment.
- **Did not re-derive `bench/results/scrub-reconstruction.json`'s own p95 figures** — Python 3.12
  unavailable here; relied on the committed file and its self-documenting `gate_status` field, per
  D-68's own guidance to cite the file directly rather than a quoted digit.
- **No live mutation testing was performed** (e.g. reverting the `getBezierPath` fix or the
  `_nearest_leaf_span` resolution and confirming the relevant tests go red) — time budget went to
  breadth (reading every P16-scope file end to end, plus the real backend counterpart) over that kind
  of depth-per-claim. Where a claim could be checked against real, live data instead (Finding #4's
  fixture measurements, the cross-highlight span-id check, the real 77/77 vitest run), that was done
  in preference to reading alone.
- **P15-owned shared components** (`Bar.tsx`, `Badge.tsx`, `SeverityDot.tsx`, `EvidenceLink.tsx`) were
  read only enough to confirm P16 panels invoke them correctly — not re-audited for their own
  correctness, which is `op2-audit-p15.md`'s territory and already covered there.
- **Severity judgment calls:** Findings #2–#5 are all rated MEDIUM. A reasonable owner could argue
  #3 (Graph sync) and #4 (Home/End) deserve LOW given FR-10's own P1 tier, or that #2 (`prefsSlice`)
  deserves HIGH given it is a complete, zero-code absence of an explicitly-forwarded PRD requirement.
  None of the four crash, corrupt data, or violate a numbered invariant (I1–I13) — that judgment is
  what kept them out of HIGH.
- **Did not investigate** whether `docs/api.md`'s own disclosed `get_graph`/`at_virtual_ts`
  simplification note was written with awareness that the frontend never calls the parameter at all,
  or independently — out of time budget; Finding #3 stands regardless of that authorship question.
