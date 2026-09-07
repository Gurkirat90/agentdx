# OP-3 REPAIR REPORT — P15 `frontend/` (Control Tower shell, tokens, Waterfall, Scorecard)

**Date:** 2026-09-07
**Module / prompt:** P15 — Control Tower shell, `tokens.css`, Zustand slices, generated API client,
Waterfall panel, Scorecard panel (Graph/Findings/Chaos/Timeline are P16's separate scope)
**Audit this repairs:** `op2-audit-p15.md` (independent OP-2, fresh agent, 2026-09-07) — VERDICT FAIL
**Repaired by:** the orchestrating session (not the audit agent), per this project's standing OP-2/OP-3 split

---

## 0. Why no fresh `AskUserQuestion` round preceded this repair

Two HIGH findings, no CRITICAL, and neither carries the "an entire repair evaporated" systemic
implication that triggered the P09-second cycle's check-in. Finding #1 is a provenance/
documentation gap (confirms, with a real compiler run, something `CONTEXT.md`'s own D-64
already disclosed in the abstract) with zero effect on the presently-shipped `HEAD` tree; Finding
#2 is a real but contained UX bug (a silent infinite spinner on one panel, not data loss or a
security gap). This falls squarely inside the standing "propose a full priority order and just
go... checking in only if something serious surfaces" authorization, the same pattern already
used for P06-second (3 findings, no CRITICAL), P07-second (4 findings, no CRITICAL), and P11-
second/P14 (which had a CRITICAL each but no systemic-trust implication).

---

## 1. Methodology note

Real, live execution throughout — Node v22.23.2 / npm 10.9.8, `frontend/`'s own `node_modules/`
already installed. Every verification claim below is a genuine command run in this sandbox: real
`tsc --noEmit`, real `eslint .`, real `vitest run`, real `check:hex-literals`. Four findings were
additionally confirmed decisive via live mutation (temporary in-place edit → run the specific new
test(s) → confirm red → restore → confirm green; `git diff --stat` clean after every restore).
Playwright e2e could not run in this sandbox — same environment limitation the audit itself hit
and disclosed (a missing `libXdamage.so.1` shared library, no root to install it, the sandbox's
egress proxy blocking a direct `.deb` fetch) — not attempted again here, not silently assumed
passing.

---

## 2. Findings and repairs

### Finding #1 (HIGH) — the reconstructed P15 commit does not build in isolation

**Repair.** Documentation-only; no source file changed. `CONTEXT.md`'s P15 verification
paragraph (§7) now states plainly that its `tsc`/Vitest/e2e claims describe the combined,
then-uncommitted P15+P16+P17 working tree, not commit `e1a1719` in isolation — and cites the
audit's own live demonstration (checking `e1a1719` out into an isolated worktree and running
`tsc --noEmit` against it alone produces 29 real errors, since `RunRoute.tsx`/`store/index.ts`/
even `Waterfall.tsx` itself, all committed inside that commit, import P16-only files). New
deviation **D-85** (§9) records this as a further, now compiler-confirmed instance of the D-64
failure class. The audit's own suggested fix direction offered a second option — construct a
deliberately standalone-buildable P15 artifact (stub `Chaos`/`Findings`/`Graph`/`Timeline`/
`linking.ts`/`stubApi.ts` sufficient to satisfy the type checker) — explicitly marked "not
mandatory." That option was not taken: no artifact today needs `e1a1719` to build standalone
(nothing bisects against it, nothing re-tests it), and manufacturing one whose only purpose is to
retroactively make a historical commit type-check would itself be a form of the "silently rewrite
history" the audit's finding warns against, not a fix to anything a real consumer depends on.

**Verified.** `wc -l CONTEXT.md` → 499 (under the 500-line cap). `python3 scripts/check_ledger.py`
→ "check-ledger: OK — §8 and §9 append-only, length and ADR references all clean" — confirms the
new D-85 row and the P15 §13 row landed without breaking append-only discipline or the cap.

### Finding #2 (HIGH) — `runSlice.ts` never got D-60's unguarded-`openapi-fetch`-result fix

**Repair.** `frontend/src/store/runSlice.ts`'s `loadRun`: both the `waterfallPromise.then()` and
`scorecardPromise.then()` callbacks now treat `result.data === undefined` as an error, mirroring
`graphSlice.ts`/`findingsSlice.ts`/`chaosSlice.ts`'s existing D-60 pattern verbatim (`if
(result.error || result.data === undefined)`). The scorecard branch's call to
`parseScorecardPayload(result.data)` is now wrapped in `try { } catch { }`, so a parser throw —
reachable for any payload shape narrower than "completely absent" (e.g. a literal JSON `null`,
which passes the `=== undefined` guard but still fails `raw.speedup` on the parser's first line)
— sets `scorecardStatus: 'error'` with a real message instead of becoming an unhandled promise
rejection outside React's render cycle.

**Verified.** Three new tests in `tests/frontend/unit/runSlice.test.ts`: a waterfall result with
`data === undefined`/no error now reports `waterfallStatus: 'error'`, not a silent `undefined`; a
scorecard result with the same shape reports `scorecardStatus: 'error'`; a scorecard result with
`data: null` (passes the `=== undefined` guard, still throws inside the parser) is caught, with
`scorecardError` containing "could not be parsed." Live mutation: reverting both `|| result.data
=== undefined` guards back to `result.error` alone turns the waterfall test red immediately; the
scorecard-branch test for that same mutation stayed green only because the `try/catch` layer
independently backstopped it (confirmed by inspection — a real, if less surgically isolated,
defense-in-depth result, not a test bug) — the `data: null` test (which exercises the `try/catch`
specifically, independent of the guard) remains the decisive coverage for that layer on its own.

### Finding #3 (MEDIUM) — `ScorecardRoute` was the one P15-scope panel route outside `PanelErrorBoundary`

**Repair.** `frontend/src/routes/ScorecardRoute.tsx`: `<ScorecardPanel />` is now wrapped in
`<PanelErrorBoundary key={runId} panelName="Scorecard">`, matching `RunRoute.tsx`'s existing
per-panel pattern (including keying by `runId`, so a crash on one run doesn't poison the boundary
for the next).

**Verified.** New `tests/frontend/unit/scorecardRoute.test.tsx` mocks `ScorecardPanel` itself to
throw (the real panel has a defensive status-based early return for every known state, so it
cannot be made to throw organically through store state alone — the audit's own "no live crash is
demonstrated today" note) and asserts the boundary's fallback renders while the route's own
header/nav (the "Waterfall" back-link) survives. Live mutation: removing the `PanelErrorBoundary`
wrap turns this test red (`Error: boom from ScorecardPanel` propagates uncaught, `getByRole
('alert')` never finds anything) — restored and reconfirmed green.

### Finding #4 (MEDIUM) — `resilience_score` parsed but never rendered; `per_fault` never parsed

**Repair.** `frontend/src/api/scorecard.ts`: new `ScorecardPerFault` interface (fields drawn from
`analysis/resilience.py`'s real `FaultScore` dataclass — the same ground-truth-not-invented
method this file's own header already describes for every other field, and the same gap it
already discloses: no prompt has shipped a real persisted example of this JSON yet).
`ScorecardPayload` gained `per_fault: ScorecardPerFault[] | null`. `parseScorecardPayload` now
enforces PRD §19.7 rule 1 ("the aggregate never appears without the per-fault breakdown... the UI:
the score component will not render without its table") **at parse time**: if `resilience.score`
is present, `resilience.per_fault` must parse as a well-formed array or the *entire* payload is
rejected (`null`), never a naked score with no evidence. `frontend/src/panels/Scorecard.tsx`
renders a new "Resilience" section — the score plus a per-fault table (label, status, score-or-
"n/a", degradation class) — gated on both fields being non-null (defense-in-depth; the parser
already guarantees they travel together).

**A live self-caught defect in this fix's own first draft.** The first version of the new
`.resilienceLow` CSS class (score < 60, PRD's own `UNRELIABLE_TOPOLOGY` threshold) used `color:
var(--crit)` as bare text on the section's background — but `--elevation-base` **is**
`--navy-900` (`tokens.css` line 65), the exact same 2.50:1-against-navy-900 WCAG AA failure D-58
already fixed elsewhere in this file. Caught by `tokenContrast.test.ts`'s own real, repo-wide
scan (not by manual review) before this reached a commit; fixed via the same solid-fill-plus-
cream-text compositing pattern D-58/`.lowComparability` already establish.

**Verified.** 5 new tests in `tests/frontend/unit/scorecardParser.test.ts` (a real score+per_fault
payload parses correctly, including a `not_fired` fault's `score`/`degradation_class` staying
`null` rather than being coerced to 0; a score with no `per_fault` at all is rejected outright; a
non-array `per_fault` is rejected; a per-fault entry missing `fault_id`/`status` is rejected;
`fault_label` falls back to `fault_id` when absent) plus 2 new tests in new file
`tests/frontend/unit/scorecardPanelResilience.test.tsx` (nothing resilience-related renders when
`resilience_score` is `null`; the score and full per-fault table render together, including the
"n/a" honest-absence text for an unscored fault). Live mutation, both layers: removing the
parse-time §19.7-rule-1 guard turns 2 parser tests red (score-without-per_fault, malformed
per_fault-as-string) — restored, reconfirmed green. Forcing the render guard to `false` turns the
render test red (`getByText('Resilience')` finds nothing) — restored, reconfirmed green.
`tokenContrast.test.ts` re-run clean (26/26) after the WCAG fix, confirming the corrected CSS is
genuinely AA-compliant, not just no-longer-flagged-by-coincidence.

### Finding #5 (LOW-MEDIUM) — Tailwind configured but never invoked, undeclared

**Repair.** Declared, not removed. New deviation **D-85** (§9, same entry as Finding #1's
provenance confirmation — both are documentation/governance-class findings from the same audit)
records that `tailwindcss`/`autoprefixer` are real `devDependencies` with full config
(`tailwind.config.js`, `postcss.config.js`) but zero live usage anywhere in `src/` — CSS Modules
do all real styling instead. Left as an open decision for whoever next touches `frontend/`'s
styling (keep Tailwind as a real, invoked tool and migrate, or drop it from `package.json`/§3's
locked stack) rather than resolved unilaterally: `AGENTS.md` §2's "ask first, always" dependency
discipline treats removing a locked dependency as needing the same scrutiny as adding one, and
this audit's own scope was P15's correctness, not the project's dependency list.

**Verified.** `grep -rn "@tailwind" src` / `grep -rn "tailwind" src` both still return nothing —
confirms the finding's own evidence, unchanged (nothing to verify against a code fix, since none
was made).

### Finding #6 (LOW) — `check-hex-literals.mjs` never scanned `.css`/`.module.css` files

**Repair.** `frontend/scripts/check-hex-literals.mjs` now also walks `.css` files (any extension
ending `.css`, which covers `*.module.css`), excluding `tokens.css` itself (the one file allowed
to define hex values). CSS block comments (`/* ... */`) are stripped before scanning — replacing
comment interior characters with spaces while preserving newlines, so line numbers in any
reported violation still point at the real source line — because three `.module.css` files
(`Badge`, `EvidenceLink`, `SeverityDot`) legitimately *cite* a hex value in a comment documenting
the D-58 WCAG fix; citing one in prose is not the defect this gate exists to catch, using one as a
live property value is.

**Verified.** `npm run check:hex-literals` → "clean (18 .tsx files + 13 .css files scanned, 0
literal hex colours)" — the three legitimate comment-citations correctly produce zero false
positives. Live mutation: appending a real hex-valued CSS rule to `Scorecard.module.css`
(`.mutationTest { color: #ff00ff; }`) is caught immediately (`found 1 literal hex colour(s)`,
correct file/line) — restored, reconfirmed clean.

### Finding #7 (LOW) — `patch-schema-jsonvalue.mjs` cites nonexistent "C-016" instead of ADR-016

**Repair.** `s/C-016/ADR-016/` in both citation sites in `frontend/scripts/patch-schema-
jsonvalue.mjs` (the module docstring and the generated-output comment template), and in
`frontend/src/api/schema.ts`'s own already-generated header comment (which had baked in the same
wrong citation from a prior run of the script) — the real, ratifying `CONTEXT.md` §8 entry is
ADR-016, whose own text explicitly says "the script's own header comment... point[s] back to this
ADR."

**Verified.** `grep -n "C-016" frontend/scripts/patch-schema-jsonvalue.mjs
frontend/src/api/schema.ts` → no matches (both instances fixed); `grep -n "ADR-016"` on the same
files confirms the corrected citation is present.

### Finding #8 (LOW) — `CONTEXT.md` row 15's `Gate` cell cited a dead `Q-NEW.1`

**Repair.** Row 15's `Gate` column corrected to read "NFR-3... resolved on real hardware,
`Q-P15.1` (§10) CLOSED 2026-08-24" — the real, already-closed entry, replacing the dead
`Q-NEW.1` reference (which never existed anywhere in the file).

**Verified.** `grep -n "Q-NEW" CONTEXT.md` → no matches (the dead reference is gone); the row's
own text now correctly cross-references the real `Q-P15.1` row, itself unchanged and still
reading `RESOLVED 2026-08-24` / `CLOSED 2026-08-24`.

---

## 3. Full verification, this cycle

- `frontend/`, real execution, Node v22.23.2 / npm 10.9.8:
  - `npx tsc --noEmit` — clean.
  - `npx eslint .` — 0 errors, 5 warnings (the exact pre-existing pattern: 2 on `router.tsx`, 3
    on P16-authored `Chaos.tsx`/`Findings.tsx`/`Graph.tsx`, none new, none in P15 scope).
  - `npm run check:hex-literals` — clean, 18 `.tsx` files + 13 `.css` files scanned (up from 18
    `.tsx`-only pre-repair — the widened scope itself, per Finding #6).
  - `npx vitest run` — **77/77 passed**, 10 files (66 baseline + 11 new: 3 in `runSlice.test.ts`,
    1 new `scorecardRoute.test.tsx`, 5 in `scorecardParser.test.ts`, 2 new
    `scorecardPanelResilience.test.tsx`), including `tokenContrast.test.ts`'s real WCAG scan
    (26/26) — the same check that caught this repair's own CSS mistake before it shipped.
- Four live mutation probes (Finding #2's guard, Finding #3's boundary wrap, Finding #4's
  parse-time §19.7 guard, Finding #4's render guard) each independently confirmed to turn the
  relevant new test(s) red, then fully restored — `git diff --stat` clean after each restore, no
  residue left in the tree.
- `CONTEXT.md`: `wc -l` → 499 (under the 500-line cap, one line of headroom); `python3
  scripts/check_ledger.py` → OK, both before and after this cycle's edits.
- **Not executed, disclosed rather than assumed passing:** Playwright e2e (`waterfall.spec.ts`,
  `scorecard.spec.ts`, `axe.spec.ts`, `keyboardNav.spec.ts`, `panelErrorBoundary.spec.ts`) — the
  same `libXdamage.so.1`/no-root/egress-proxy-403 environment limitation the audit itself hit and
  disclosed. Nothing in this repair touched those spec files or the code paths they exercise
  beyond what the new Vitest-level tests already re-verify statically.

---

## 4. Standing status

**Not `VERIFIED`.** A second independent re-audit is owed, same standing pattern as every module
in this project since P02. This repair, like the P09-second cycle immediately before it, is
genuinely live-verified throughout — including four decisive mutation probes and a real,
self-caught WCAG regression fixed before commit — but remains self-verified by the orchestrating
session, not independently confirmed by a fresh, memory-free agent.

**Carried forward, not addressed this cycle:**
- D-85's own open question: whether Tailwind stays as a real, invoked tool or is dropped from the
  locked stack — deliberately left as a decision for whoever next touches `frontend/` styling.
- Playwright e2e remains unexecutable in this sandbox specifically (real hardware, per the
  established Q-P15.1 precedent, is where this project's e2e/perf gates get their trustworthy
  signal) — not a P15-specific gap, the same standing limitation every frontend cycle this
  session has carried.
- P16's own scope (Graph/Findings/Chaos/Timeline, the WebSocket live stream) was read only as far
  as necessary to establish Finding #1's compile-boundary problem and Finding #2/#3's
  `runSlice`/`PanelErrorBoundary` cross-references — its own internal correctness is the next
  item in the standing audit queue (task #33), not reassessed here.
