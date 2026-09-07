# OP-2 INDEPENDENT AUDIT — P15 `frontend/` (Control Tower shell, tokens, Waterfall, Scorecard)

**Scope.** `frontend/src/{tokens.css, store/{runSlice,selectionSlice,index,types}.ts,
api/{client,scorecard}.ts, api/schema.ts (generated), components/{Badge,Bar,EvidenceLink}.*,
panels/{Waterfall,GhostBaseline,waterfallBuckets,spanWindow,useWaterfallViewport,Scorecard,
scorecardLabels}.*, routes/{router,RunListRoute,RunRoute,ScorecardRoute}.tsx}`,
`frontend/scripts/{check-hex-literals,patch-schema-jsonvalue}.mjs`,
`frontend/tests/frontend/{unit,e2e}/*` (P15-authored specs),
`frontend/tests/frontend/fixtures/README.md`. Explicitly out of scope per the assignment:
Graph/Findings/Chaos/Timeline panels, `graphSlice`/`chaosSlice`/`eventsSlice`/`linking.ts`
(P16). Where a P15-scope file has a hard compile-time dependency on P16 code, that dependency
itself is in scope (see Finding #1) even though the P16 file's own correctness is not.

**Method.** Read `CONTEXT.md` in full (§0, §2, the sequencing-hazards note above §5, row 15,
§6 G8, §7's P15/P16 narratives, §9 D-58/D-60/D-64, §10 Q-P15.1 and C-28…C-32, §11), `AGENTS.md`
in full, and PRD §17.4–17.6, the waterfall/scorecard endpoint specs (§26.1, lines ~3723–3781),
§28.2–28.5, §29.1–29.9, §45.15, and the NFR-3/NFR-7 rows (§32). Read every P15-scope source
file listed above end to end, plus `frontend/package.json`, `tailwind.config.js`,
`postcss.config.js`, `frontend/tests/frontend/fixtures/README.md`, `frontend/src/api/README.md`
and the sibling `README.md` in each touched directory. Cross-checked the frontend's assumed
API contract against the real backend (`src/agentdx/api/models.py`, `routes/scorecard.py`,
`routes/analysis.py::get_waterfall`) and against `docs/openapi.json`.

**Executed for real, in this sandbox** (`frontend/`, Node v22.23.2 / npm 10.9.8):
`npx tsc --noEmit` (clean), `npx eslint .` (0 errors / 5 warnings, 2 of them P15-scope and
pre-existing per the narrative), `npm run check:hex-literals` (clean, 18 files), `npx vitest
run` (66/66 passed across 8 files). Downloaded Playwright's Chromium + headless-shell binaries
successfully (~300 MB, second attempt after the first was cut off by a 110 s timeout) and
copied the whole `frontend/` tree to a local (non-mounted) path to route around an `EPERM` on
the mounted folder's `node_modules/.vite` cache — but `browserType.launch` then failed on a
single missing shared library, `libXdamage.so.1` (confirmed via `ldd` on the real Chrome
binary), and neither `sudo apt-get install libxdamage1` (blocked: no root, container's
"no new privileges" flag) nor a direct `apt-get download` of the `.deb` (blocked: `403` from
the sandbox's own egress proxy for `ports.ubuntu.com`) could close that one gap. **No
Playwright e2e spec was executed against a real browser in this audit** — see NOT DONE. Static
reading of the five P15-scope specs (`waterfall.spec.ts`, `scorecard.spec.ts`, `axe.spec.ts`,
`keyboardNav.spec.ts`, `panelErrorBoundary.spec.ts`) is the best available evidence for what
they would catch, per the assignment's own fallback instruction.

Additionally used a real, live TypeScript compile — not inference — to test the single most
consequential claim in this audit (Finding #1): checked out the actual `e1a1719` commit
(`git worktree add --detach`, since `HEAD` already contains the union of P15+P16+P17) into an
isolated directory, symlinked `node_modules`, and ran `tsc --noEmit` against that commit's tree
alone.

---

## VERDICT: **FAIL**

Two HIGH-severity findings, both real and demonstrated rather than inferred. Everything the
build narrative claims about the actually-shipped, current `HEAD` tree — token contrast, the
hand-rolled router, the C-29/C-30/C-31 payload/fixture rulings, the screen-reader table
alternative, `tsc`/`eslint`/`check:hex-literals`/Vitest — **holds up under live re-execution**,
and the D-58 WCAG contrast fix in particular is a well-built, non-tautological repair. But the
module fails on two grounds this project's own protocol treats as first-class: (1) the
committed artifact labeled "P15" is not what §0's OP-2 charter needs it to be — it does not
build in isolation, which means the ledger's own "tsc clean / 40/40 Vitest / 14/15 e2e"
claims for P15 could not literally have been produced by testing this commit, only some
larger, never-isolated working tree (the same D-64/D-84 failure class, now demonstrated with a
real compiler run rather than asserted); and (2) a real, demonstrable, unfixed instance of the
exact defect class `CONTEXT.md`'s own D-58/D-60-adjacent narrative was supposedly vigilant
about — an unguarded `openapi-fetch` result trusted with `!result.error` alone — sits in
`runSlice.ts`, one of P15's own four named files, silently hanging the Scorecard panel forever
on any non-JSON error response, with zero test coverage of that path.

1. **(HIGH)** The reconstructed P15 commit (`e1a1719`) does not compile standalone. Checked
   out alone, `tsc --noEmit` produces 29 real errors: `RunRoute.tsx` and `store/index.ts`
   (both committed to this commit) import `../panels/{Chaos,Findings,Graph,Timeline}` and
   `./{chaosSlice,eventsSlice,findingsSlice,graphSlice,timelineSlice}` — none of which exist
   until the P16 commit (`d12b763`). Even `Waterfall.tsx` itself — unambiguously P15's own
   panel — imports `highlightedSpanIds` from `../store`, a function defined only in P16's
   `linking.ts`. Even the nominally-P15 e2e specs (`axe.spec.ts`, `keyboardNav.spec.ts`,
   `panelErrorBoundary.spec.ts`, `scorecard.spec.ts`, `waterfall.spec.ts`) all import
   `./stubApi`, a file that ships only in the P16 commit.
2. **(HIGH)** `frontend/src/store/runSlice.ts`'s `loadRun` — one of P15's own four named
   slices — inherits the exact `!result.error`-only success-guard defect `CONTEXT.md`'s D-58/
   D-60 narrative documents finding and fixing in `graphSlice`/`findingsSlice`/`chaosSlice`,
   but was never itself patched. On the documented trigger (a non-JSON error body from the dev
   proxy when the backend is unreachable), the scorecard branch calls
   `parseScorecardPayload(undefined)`, which throws inside a bare `.then()` with no `.catch()`
   — an unhandled promise rejection outside React's render cycle, invisible to
   `PanelErrorBoundary`, leaving `scorecardStatus` stuck at `'loading'` forever with no error
   shown to the user. The waterfall branch has the milder version of the same bug (an
   uncaught render exception, at least caught by `PanelErrorBoundary`). Zero test in
   `runSlice.test.ts` exercises this path.
3. **(MEDIUM)** `ScorecardPanel` (mounted via `ScorecardRoute.tsx`) is the one P15-scope panel
   not wrapped in `PanelErrorBoundary` — every panel in `RunRoute.tsx` (Graph, Chaos, Findings,
   Waterfall, Timeline) is, per D-60's own stated rationale ("a render-time crash in any one
   panel ... unmounted the entire route, including the unrelated, working Waterfall/Scorecard
   panels P15 shipped"), but that rationale was never re-applied to Scorecard's own route.
4. **(MEDIUM)** `parseScorecardPayload` extracts `resilience_score` from the API response, but
   `Scorecard.tsx` never renders it — the PRD's own `GET /runs/{id}/scorecard` spec (§26.1)
   requires `resilience{score, per_fault[]}` with `per_fault` mandatory whenever `score` is
   present (§19.7); `per_fault` is not even parsed. Untested with a non-null value anywhere.
5. **(LOW-MEDIUM)** Tailwind is part of `CONTEXT.md` §3's locked frontend stack and is fully
   configured (`tailwind.config.js`, `postcss.config.js`'s `tailwindcss` plugin, a
   `devDependency`) but is never actually invoked anywhere in `src/` — no `@tailwind`
   directive, no utility class reference. CSS Modules do all real styling instead. This
   substitution is not recorded as a `§9` deviation.
6. **(LOW)** `check-hex-literals.mjs` only walks `.tsx` files; a literal hex value hardcoded
   directly inside a `.module.css` rule (as opposed to a comment) would pass this gate
   silently, contrary to `AGENTS.md` §4's "no literal hex in a component" rule, which does not
   itself exempt CSS. No live violation exists today (verified by direct grep) — this is a
   gate blind spot, not a demonstrated defect.
7. **(LOW)** `frontend/scripts/patch-schema-jsonvalue.mjs`'s header comment cites "CONTEXT.md
   ruling C-016" (twice) — no such ruling exists. The real, ratifying ledger entry is
   **ADR-016**, whose own text explicitly says "the script's own header comment ... point[s]
   back to this ADR." Same citation-drift class `§13` already records for P14's narrative
   miscitng ADR-014/D-58.
8. **(LOW)** `CONTEXT.md` §5 row 15's own `Gate` cell reads "NFR-3 ... is borderline in this
   build's sandbox — see §10 Q-NEW.1." No `Q-NEW.1` exists anywhere in the file; the real
   entry is `Q-P15.1`, and §10's own `Q-P15.1` row records the question **RESOLVED
   2026-08-24** on real hardware — the row-15 gate cell is stale and self-contradicts the
   ledger's own §10 a few hundred lines away.

---

## FINDING #1 (HIGH) — The reconstructed P15 commit does not build in isolation; the tree the ledger's verification claims describe never existed as a standalone artifact

**Where:** `git show e1a1719 --stat` (the reconstructed P15 commit, per D-64);
`frontend/src/routes/RunRoute.tsx:1-12`, `frontend/src/store/index.ts:7-15`,
`frontend/src/store/selectionSlice.ts` (all three committed *inside* `e1a1719`), all import
from P16-only files.

**Demonstrated, not inferred.** A detached worktree was checked out at `e1a1719` and `tsc
--noEmit` run against it directly:

```
$ git worktree add --detach /tmp/p15-only-check e1a1719
$ cd /tmp/p15-only-check/frontend && ln -s <repo>/frontend/node_modules node_modules
$ npx tsc --noEmit
src/panels/Waterfall.tsx(7,10): error TS2305: Module '"../store"' has no exported member 'highlightedSpanIds'.
src/routes/RunRoute.tsx(5,45): error TS2307: Cannot find module '../panels/Chaos' ...
src/routes/RunRoute.tsx(6,48): error TS2307: Cannot find module '../panels/Findings' ...
src/routes/RunRoute.tsx(7,42): error TS2307: Cannot find module '../panels/Graph' ...
src/routes/RunRoute.tsx(8,31): error TS2307: Cannot find module '../panels/Timeline' ...
src/store/index.ts(9,51): error TS2307: Cannot find module './chaosSlice' ...
src/store/index.ts(10,53): error TS2307: Cannot find module './eventsSlice' ...
src/store/index.ts(11,57): error TS2307: Cannot find module './findingsSlice' ...
src/store/index.ts(12,51): error TS2307: Cannot find module './graphSlice' ...
src/store/index.ts(15,57): error TS2307: Cannot find module './timelineSlice' ...
... (29 errors total, including tests/frontend/e2e/*.spec.ts all failing on `./stubApi`)
```

`git cat-file -e e1a1719:frontend/src/panels/Graph.tsx` (and the four sibling panel/slice
files) confirms directly: **MISSING** in the P15 commit. They land only in the P16 commit
(`d12b763`), whose diff never touches `RunRoute.tsx`, `store/index.ts` or
`store/selectionSlice.ts` at all — those three files, in their current, P16-dependent form,
are entirely inside `e1a1719`.

**Why this matters, beyond git-history bookkeeping.** `CONTEXT.md` §9's D-64 already discloses
that P15/P16/P17 were reconstructed post-hoc from 129 uncommitted files and says "treat
provenance accordingly," and D-84 explicitly names P15 as one of the rows "not re-derived
line-by-line" the way P09 was. This finding turns that abstract caveat into a concrete,
reproducible fact: **the artifact currently labeled `e1a1719` "P15(frontend): Control Tower
shell, tokens, Waterfall, Scorecard" is not a snapshot of a working P15-only build.** It is
whatever the reconstruction session's module-boundary heuristic attributed to P15 after
*all three* prompts had already been built, uncommitted, on top of each other in one
continuous working tree. Consequently, `CONTEXT.md` §13/§7's own P15 verification paragraph
("`tsc --noEmit` clean; ... 40/40 Vitest unit tests pass; 14/15 Playwright e2e pass") describes
a run that — on the evidence above — could not have been performed against this commit in
isolation; it must have been run against the full, then-uncommitted P15+P16(+P17) tree,
which is a materially different (and never re-labeled) claim than "P15 passes its own gates."
This also means `git bisect`, a future targeted regression test against "P15 alone," or any
attempt to treat row 15's `Verified on` column as eventually satisfiable by re-testing this
specific commit, will fail at the compile step before reaching anything module-specific.

**Confirmed not already flagged anywhere.** `CONTEXT.md` names this risk in the abstract
(D-64, D-84) but never demonstrates it, and no test or CI check in this repository would catch
it — `tsc`/`vitest`/`eslint` are only ever run against the full working tree (`HEAD`), never
against an isolated historical commit, so this defect is invisible to every existing gate.

**Severity:** HIGH. It does not affect the presently-shipped `HEAD` tree (which does compile,
type-check and pass its unit tests, verified live above) — end users of the current app are
unaffected. It is HIGH rather than CRITICAL because nothing observable breaks today; it is not
MEDIUM because it directly falsifies the specific, mechanical claim ("this commit passed these
checks") the ledger attaches to row 15, in exactly the pattern this project has twice already
paid for (D-64, D-84).

**Suggested fix direction (not mandatory).** Either (a) re-file `CONTEXT.md`'s P15
verification paragraph to say plainly that it was run against the combined, then-uncommitted
P15+P16+P17 tree rather than against commit `e1a1719` specifically, or (b) if a standalone P15
artifact is wanted for future bisection, construct one deliberately (a stub `Chaos`/`Findings`/
`Graph`/`Timeline`/`linking.ts`/`stubApi.ts` sufficient to satisfy the type checker) and commit
it as an explicitly-labeled amendment, not silently rewrite history.

---

## FINDING #2 (HIGH) — `runSlice.ts` (P15's own file) has the same unguarded-`openapi-fetch`-result defect D-58/D-60 fixed elsewhere, unpatched here; the Scorecard panel hangs forever with no error on the documented trigger

**Where:** `frontend/src/store/runSlice.ts:81-106` (`loadRun`'s `waterfallPromise.then`/
`scorecardPromise.then` callbacks); `frontend/src/api/scorecard.ts:98-103`
(`parseScorecardPayload`'s first line, `raw.speedup`).

```ts
// runSlice.ts:81-88 — waterfall branch: `!result.error` is the only success guard.
void waterfallPromise.then((result) => {
  if (get().runId !== runId) return;
  if (result.error) {
    set({ waterfallStatus: 'error', waterfallError: describeError(result.error) });
  } else {
    set({ waterfall: result.data, waterfallStatus: 'loaded' });   // result.data can be undefined
  }
});

// runSlice.ts:90-106 — scorecard branch: same guard, then hands the (possibly undefined)
// payload straight to a parser with no null-check of its own on `raw` itself.
void scorecardPromise.then((result) => {
  ...
  if (result.error) { ...; return; }
  const parsed = parseScorecardPayload(result.data);   // no try/catch; result.data may be undefined
  ...
});
```

```ts
// scorecard.ts:98-99 — the very first line dereferences `raw` unconditionally.
export function parseScorecardPayload(raw: ScorecardResponse): ScorecardPayload | null {
  const speedupRaw = raw.speedup;   // throws TypeError if raw is undefined
```

**Why this is reachable, not contrived.** `CONTEXT.md`'s own D-60 deviation documents that
`openapi-fetch` "can return `{data: undefined, error: undefined}` on a non-JSON error body —
this build's Vite dev-proxy 500 page when the backend is unreachable" — and that this
*already happened* in this exact codebase, taking down `graphSlice`/`findingsSlice`/
`chaosSlice`'s `loadGraph`/`loadFindings`/`loadScenario`. That fix, landed at P16, touched
those three slices only; `runSlice.ts` — one of P15's own four named slices, per
`CONTEXT.md`'s own P15 narrative and `frontend/src/store/README.md` — was never revisited.

**Consequence, traced end to end.**
- *Waterfall branch:* `waterfall` state becomes `undefined` (not `null`). `Waterfall.tsx`'s
  own guard is `if (waterfall === null || waterfall.lanes.length === 0)` — `undefined !==
  null`, so execution falls through to `waterfall.lanes.reduce(...)` on `undefined.lanes`, a
  synchronous render-time `TypeError`. This crash *is* caught by `PanelErrorBoundary` (P16's
  fix generalized that far), so the panel shows a generic crash fallback — but not the honest,
  already-written `waterfallStatus === 'error'` branch that exists specifically for this case
  and never gets reached.
- *Scorecard branch:* `parseScorecardPayload(undefined)` throws **inside a bare `.then()`
  with no `.catch()`** — an unhandled promise rejection that happens entirely outside React's
  render cycle. No error boundary in this codebase can catch it (error boundaries only catch
  synchronous render-phase throws). `scorecardStatus` never leaves `'loading'`. The user sees
  "Loading scorecard…" forever, with no error message, no retry affordance, and (worse than
  the waterfall case) no console-visible crash either unless the developer happens to be
  watching for unhandled-rejection warnings. This directly contradicts PRD §29.8's own
  copy-rules requirement ("state what happened") and is a materially worse failure mode than
  the one D-60 was written to prevent.

**Confirmed not already covered by any existing test.** `runSlice.test.ts`'s three tests cover
(1) full success, (2) a well-formed JSON error on the *run* fetch only, and (3) a superseded
`loadRun` call — none constructs a `{data: undefined, error: undefined}` response for either
the waterfall or scorecard promise, so this path has zero test coverage today, confirmed by
reading the full file (reproduced above in "What was checked").

**Severity:** HIGH. It is a real, demonstrated code path (traced through the actual source,
not hypothesized), it sits in a file explicitly inside P15's own named scope, it reproduces a
bug class this exact project already found and fixed once (proving it is not a theoretical
edge case), and its scorecard-side consequence — an infinite silent spinner — is arguably
worse than the crash D-60 was written to contain, while receiving none of that fix's benefit.

**Suggested fix direction (not mandatory).** Mirror D-60's own fix shape: treat
`result.data === undefined` as an error in both `.then()` callbacks (matching
`graphSlice`/`findingsSlice`/`chaosSlice`'s pattern), and wrap the `parseScorecardPayload` call
in a `try { } catch { set({ scorecardStatus: 'error', ... }) }` so a parser throw can never
become an unhandled rejection regardless of what future payload shapes arrive.

---

## FINDING #3 (MEDIUM) — `ScorecardPanel` is the one P15-scope panel left outside `PanelErrorBoundary`

**Where:** `frontend/src/routes/ScorecardRoute.tsx:28-30` (`<main>{...}<ScorecardPanel
/></main>`, no boundary) vs. `frontend/src/routes/RunRoute.tsx:113-139` (every one of Graph,
Chaos, Findings, Waterfall, Timeline wrapped in `<PanelErrorBoundary key={runId} ...>`).

D-60's own stated rationale for building `PanelErrorBoundary` at all was explicitly about
protecting "the unrelated, working Waterfall/Scorecard panels P15 shipped" from a sibling
panel's crash. That rationale generalizes just as much to `ScorecardPanel`'s own render-time
exceptions (Finding #2's scorecard branch, if it ever became a synchronous throw instead of an
unhandled rejection; or any future bug) — but `ScorecardRoute.tsx` renders `ScorecardPanel`
completely unguarded. A crash there takes the entire route down, including its own header/nav
(`Link` back to the Waterfall route), leaving the user on a blank page with no recovery path
other than the browser's own back button — the exact failure shape D-60 exists to prevent,
just on the one panel route it was never re-applied to.

**Confirmed not already covered by any existing test.** `panelErrorBoundary.test.tsx`/
`panelErrorBoundary.spec.ts` both test `PanelErrorBoundary` itself and its use inside
`RunRoute.tsx`; neither exercises `ScorecardRoute.tsx`.

**Severity:** MEDIUM — no live crash is demonstrated today (`ScorecardPanel`'s own render path
has defensive `status`-based early returns for every state), but the safety net this project
built specifically for this class of panel is absent on this one route.

**Suggested fix direction (not mandatory).** Wrap `<ScorecardPanel />` in
`ScorecardRoute.tsx` with the same `<PanelErrorBoundary key={runId} panelName="Scorecard">` used
in `RunRoute.tsx`.

---

## FINDING #4 (MEDIUM) — `resilience_score` is parsed but never rendered; `per_fault` is never parsed at all

**Where:** `frontend/src/api/scorecard.ts:163-164,194` (`resilience_score` extracted into
`ScorecardPayload`) vs. `frontend/src/panels/Scorecard.tsx:50` (destructures only `{ speedup,
buckets, tokens, comparability }` — `resilience_score` is discarded).

PRD §26.1's own scorecard endpoint spec: *"Returns the §17.4 structure as data: `speedup{}`,
`buckets[]` with `evidence`, `tokens{}`, `comparability{}`, `resilience{score, per_fault[]}` —
with `per_fault` **required** whenever `score` is present (§19.7)."* The frontend's own ruling
(C-29) correctly names `resilience{}` as one of the five top-level keys the persisted payload
uses, and `scorecard.ts` does parse `resilience_score` (defensively, `null` when absent) — but
that value is dead: nothing in `Scorecard.tsx` reads `scorecard.resilience_score`, and
`per_fault` is not represented in `ScorecardPayload` at all, so there is no way for the panel
to ever show it even if the field were read.

**Confirmed not already covered by any existing test.**
`scorecardParser.test.ts`'s only assertion involving this field is `'treats resilience_score/
wall_makespan_ms as optional — absent, not a parse failure'`, which only ever exercises the
`null` case; no test constructs a payload with a real `resilience.score` and checks it renders.

**Severity:** MEDIUM. `demo_chain.scorecard.json` (the one populated fixture P15 ships) has no
chaos run behind it, so `resilience_score` is `null` there too — meaning this gap has likely
never been visually observed even by the build session itself. But it is a real, silent
divergence from a PRD requirement inside P15's own claimed-complete "full §17.4" scope, not
declared anywhere as a gap the way the verdict-recommendation line (also absent, but correctly
covered by C-29's documented payload-shape ruling) is.

**Suggested fix direction (not mandatory).** Add a `resilience` row to `Scorecard.tsx`,
rendered only when `resilience_score !== null` (matching the existing `wall_makespan_ms !==
null` pattern already in the file), and extend `ScorecardPayload`/`parseScorecardPayload` to
carry `per_fault` so a future non-null score is never shown without it (I6/§19.7).

---

## FINDING #5 (LOW-MEDIUM) — Tailwind is locked into the stack and fully configured, but never actually used; not declared as a deviation

**Where:** `CONTEXT.md` §3 ("Frontend | React 18 + Vite + TypeScript + React Flow + Zustand +
Tailwind + visx"); `frontend/tailwind.config.js` (full config, colors mapped to `tokens.css`
custom properties); `frontend/postcss.config.js` (`tailwindcss` + `autoprefixer` plugins);
`frontend/package.json` (`tailwindcss` devDependency).

No file under `frontend/src/` contains an `@tailwind` directive (`grep -rn "@tailwind"
src` → nothing) or references a Tailwind utility class (`grep -rn "tailwind" src` → nothing).
Every actual style rule in the shipped tree goes through hand-written CSS Modules
(`*.module.css`), which is a perfectly reasonable implementation choice — but it silently
diverges from the locked stack's inclusion of Tailwind as a real, load-bearing tool rather
than dead configuration, and no `§9` deviation row records the substitution the way, e.g.,
D-59 records the dagre-vs-deterministic-layout choice for P16's graph panel.

**Severity:** LOW-MEDIUM — purely a documentation/governance gap (an unused devDependency and
an unused PostCSS plugin cost nothing at runtime), but it is exactly the kind of "locked stack
item quietly not used" `AGENTS.md` §2's "ask first, always" dependency discipline exists to
surface, just in the opposite direction (declared but unused, rather than used but
undeclared).

**Suggested fix direction (not mandatory).** Either add a `§9` deviation declaring CSS Modules
as the actual styling mechanism and Tailwind as present-but-unused (with a one-line rationale
for keeping the dependency at all), or remove the unused `tailwindcss`/`autoprefixer`
devDependencies and `tailwind.config.js`/`postcss.config.js` files if there is no forward plan
to use them.

---

## FINDING #6 (LOW) — `check-hex-literals.mjs` never scans `.css`/`.module.css` files

**Where:** `frontend/scripts/check-hex-literals.mjs:22-34` (`walk()` only pushes files ending
in `.tsx` into `tsxFiles`).

`AGENTS.md` §4's rule is "All colour comes from CSS custom properties in `tokens.css` — never
a literal hex in a component," which does not itself carve out CSS files, and `tokens.css`'s
own header says the same ("A literal hex value in a component is a defect"). The CI gate
enforcing this only ever reads `.tsx` files. Today this is harmless — every hex literal found
outside `tokens.css` is inside a `.module.css` file's *comment*, never a property value
(verified directly: `grep -rnE ':\s*#[0-9a-fA-F]{3,6}\b' src --include='*.css'` matches
nothing outside `tokens.css` itself) — but the gate would not catch a real regression if one
were introduced directly in a CSS Module.

**Severity:** LOW — a structural blind spot in an enforcement script, not a live violation.

**Suggested fix direction (not mandatory).** Extend `walk()`'s file filter to also collect
`.css`/`.module.css` files (excluding `tokens.css` itself, the one file allowed to define
hexes), the same scope `tokenContrast.test.ts`'s own `findCssModules()` helper already covers
for a different check.

---

## FINDING #7 (LOW) — `patch-schema-jsonvalue.mjs` cites a nonexistent ruling ("C-016") instead of the real ADR-016

**Where:** `frontend/scripts/patch-schema-jsonvalue.mjs:5,60` ("See CONTEXT.md ruling C-016" /
"patched in by scripts/patch-schema-jsonvalue.mjs, see CONTEXT.md C-016").

`CONTEXT.md` §8's real, ratifying entry for this exact script is **ADR-016** ("`frontend/
scripts/patch-schema-jsonvalue.mjs` runs automatically as the second step of `npm run
generate:api`..."), whose own closing consequence explicitly says "the script's own header
comment ... point[s] back to this ADR" — i.e. the ledger entry itself asserts a correct
citation that the shipped script does not actually contain. `grep -n "^| C-016" CONTEXT.md`
returns nothing; no such ruling exists anywhere in §10.

**Severity:** LOW — cosmetic, but the same citation-integrity class `CONTEXT.md` §13 already
flags for P14's own narrative (misciting ADR-014/D-58), now found in shipped source rather
than only in session prose.

**Suggested fix direction (not mandatory).** `s/C-016/ADR-016/` in both comment locations.

---

## FINDING #8 (LOW) — `CONTEXT.md` §5 row 15's own gate cell is stale and cites a dead cross-reference

**Where:** `CONTEXT.md` §5, row 15's `Gate` column: *"NFR-3 (60fps @ 5 000 spans) is
**borderline in this build's sandbox — see §10 Q-NEW.1**."*

No `Q-NEW.1` exists anywhere in `CONTEXT.md` (`grep -n "Q-NEW" CONTEXT.md` matches only this
one line). The real, corresponding entry is `Q-P15.1`, whose own row states: *"**RESOLVED
2026-08-24** ... the same, unmodified `perf.spec.ts` run via `npm run test:e2e` on the project
owner's own machine ... passed cleanly — 15/15 e2e specs including this one."* Row 15's gate
cell was never updated after Q-P15.1 closed, so a reader trusting the table literally is told
NFR-3 is still an open, borderline concern and is pointed at an ID that does not resolve to
anything.

**Severity:** LOW — an internal ledger inconsistency, not a code defect; the underlying
question is genuinely closed per §10's own account (this audit did not independently
re-measure NFR-3 — see NOT DONE).

**Suggested fix direction (not mandatory).** Update row 15's gate cell to read "NFR-3 ...
resolved on real hardware, see §10 Q-P15.1 (CLOSED 2026-08-24)."

---

## What was checked and holds up

- **`tsc --noEmit`, `eslint .`, `npm run check:hex-literals`, `npx vitest run` all genuinely
  clean/passing against the real, current `HEAD` tree** (66/66 tests, 8 files; 0 eslint errors,
  5 warnings — 2 on `router.tsx`, matching the narrative's own "2 pre-existing-pattern
  warnings on the router file" claim exactly; 18 `.tsx` files scanned for hex, 0 found).
- **D-58's `--crit` WCAG contrast fix is real, not tautological.** Read every claimed usage
  site (`Badge.module.css .crit`, `EvidenceLink.module.css .missing`,
  `Scorecard.module.css .lowComparability`/`.gradeC`, `SeverityDot.module.css .mark`) and
  confirmed each genuinely uses the solid-fill-plus-cream-text/stroke compositing pattern the
  narrative describes, never bare `color: var(--crit)` on a dark canvas.
  `tokenContrast.test.ts` computes real WCAG relative-luminance/contrast-ratio math from the
  actual `tokens.css` hex values and the actual set of `.module.css` files present (26 tests
  today, up from the P15-build-time 21 because more CSS Modules exist post-P16) — this is
  **not** the tautological-test failure mode this project's own P11 audit found; it is a
  real, adversarially-meaningful computation, and one test explicitly locks in that `--crit`
  alone *fails* AA, guarding against a future revert. One minor imprecision noted but not
  filed as a separate finding: the `.ok`/`.warn`/`.fault` test cases are labeled "on
  elevation-well" but actually assert against `--navy-900`, a lighter (harder-to-pass)
  background than the `--elevation-well`/`--navy-950` the components actually composite
  against — since navy-900 is the stricter case, this does not produce a false pass.
- **C-28 (hand-rolled router, no new dependency) holds.** `package.json` has no
  `react-router`/`history`/routing library anywhere in `dependencies` or `devDependencies`;
  `router.tsx` is exactly the ~70-line `history`-API implementation the ruling describes.
- **C-29 (Scorecard payload shape) and C-30 (six-bucket→four-fill waterfall mapping) are
  implemented exactly as documented**, verified by reading `waterfallBuckets.ts` and
  `api/scorecard.ts` against their own ruling text and against the real
  `demo_chain.scorecard.json` fixture, whose fields match `ScorecardPayload` field-for-field.
- **C-31 (fixture provenance) holds** — `fixtures/README.md`'s claims were spot-checked
  against the actual fixture JSON (`demo_chain.scorecard.json`'s numbers are internally
  consistent: `achieved 2.0 = 38ms baseline / 19ms multi`, matching the file's own
  `virtual_makespan_baseline_ms`/`virtual_makespan_multi_ms`) and the synthetic perf fixture
  is clearly isolated from the three real ones.
- **PRD §29.4's ghost baseline, both states, are correctly implemented** in
  `GhostBaseline.tsx`: the pending state renders a `prefers-reduced-motion`-gated spinner with
  honest "not yet computed" copy (never a fabricated line), the populated state renders the
  dashed line, speedup and comparability grade together, matching the PRD's own "never in a
  footnote" requirement.
- **PRD §29.9's screen-reader data-table alternative is real**, not a stub — `WaterfallTable`
  in `Waterfall.tsx` renders every span as a real `<table>` row with a caption, memoized
  independently of scroll-driven viewport updates so it never costs the 60fps scrub budget.
- **NFR-3's binary-search virtualisation (`spanWindow.ts`) is a genuine, correctly-reasoned
  O(log n + k) implementation**, not a claimed-but-hollow optimization — read in full and its
  7 unit tests pass live.
- **`axe.spec.ts`/`keyboardNav.spec.ts` (read, not executed — see NOT DONE) assert against
  real rendered DOM and real computed CSS values** (`getComputedStyle(el).outlineStyle`/
  `outlineWidth`, not just "the CSS rule exists"), consistent with D-58's own account of
  axe-core catching the contrast bug and a missing `<h1>` live during the original build.
- **The backend contract genuinely matches**, spot-checked directly: `WaterfallResponse`/
  `WaterfallLane`/`WaterfallSpan` in `src/agentdx/api/models.py` line up field-for-field with
  `frontend/src/store/types.ts`'s equivalents and with the generated `schema.ts`;
  `ScorecardResponse`'s deliberate `extra="allow"` (`api/routes/scorecard.py`) is correctly
  mirrored by the frontend's own defensive, never-fabricating `parseScorecardPayload`.
- **`prefers-reduced-motion` is genuinely handled** (via real CSS `@media` queries in
  `Waterfall.module.css`, `Graph.module.css`, `Bar.module.css`), even though the PRD-named
  `prefsSlice` (§28.2) was never built as a literal Zustand slice by either P15 or P16 —
  `store/README.md` honestly attributes that gap forward to P16, and P16 didn't close it
  either (out of P15's own scope to fix; noted, not filed as a P15 finding).

---

## NOT DONE / RISKS

- **No Playwright e2e spec was executed against a live browser.** Browsers downloaded
  successfully; `browserType.launch` failed on one missing shared library
  (`libXdamage.so.1`, confirmed via `ldd`) that this sandbox cannot install without root
  (blocked by the container's own `no-new-privileges` flag) and cannot fetch as a loose `.deb`
  either (the sandbox's egress proxy returns `403` for `ports.ubuntu.com`). This blocks
  `waterfall.spec.ts`, `scorecard.spec.ts`, `axe.spec.ts`, `keyboardNav.spec.ts` and
  `panelErrorBoundary.spec.ts` specifically for P15's own scope. Static reading of all five
  (reproduced/quoted above) is the basis for "what was checked and holds up" regarding
  axe-core/keyboard-nav/screen-reader claims — a real gap relative to actually running them,
  disclosed rather than assumed passing.
- **NFR-3 (60fps @ 5,000 spans) was not independently re-measured**, for the same Playwright
  blocker. Relied on `CONTEXT.md` §10's own Q-P15.1 account (measured borderline 58.2–60.3fps
  in a prior session's sandbox, then confirmed passing 15/15 on the project owner's real
  hardware, 2026-08-24) plus a static read of the optimization code (`spanWindow.ts`,
  `LaneRow`'s event-delegation/plain-`<rect>` pattern in `Waterfall.tsx`), which is consistent
  with — but does not independently confirm — that account. Per the assignment's own
  instruction, this is *not* treated as a fresh regression on this session's inability to
  measure it.
- **The backend contract check was static** (Pydantic model definitions and route handlers
  read directly, `docs/openapi.json` read as committed text), not a live HTTP round-trip
  against a running FastAPI server — consistent with this project's own already-disclosed
  D-66 Python-3.10-sandbox limitation for `api/`, not attempted independently here since `api/`
  itself was already OP-2-audited separately (P14).
- **Graph/Findings/Chaos/Timeline panels, `graphSlice`/`chaosSlice`/`eventsSlice`/
  `linking.ts`/`graphLayout.ts` were read only as far as necessary** to establish Finding #1's
  compile-boundary problem and Finding #2/#3's `runSlice`/`PanelErrorBoundary` wiring — their
  own internal correctness is P16's scope and was not otherwise assessed.
- **`package-lock.json` integrity/`npm audit` was not run.**
- **The README screenshot (`frontend/docs/waterfall-ghost-baseline.png`) and any pixel-level
  visual claim were not verified** — no live browser render was available.
- **A full, file-by-file audit of every `.module.css` file's token compliance was not
  performed by hand** beyond the D-58-related files and `tokenContrast.test.ts`'s own
  automated, repo-wide `--sage-dim`-as-text-color scan (which is real code, not this audit,
  doing that check across every `.module.css` file that exists).
- **This audit's own findings have not been independently re-audited** — per this project's
  own standing pattern, a further OP-2 pass on any repair made from this report's findings is
  still owed.
