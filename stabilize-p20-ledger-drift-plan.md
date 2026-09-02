# Stabilization plan — P20's ledger drift and unmarked red test

**SITUATION (inferred, not specified — confirm before I act):** this message's template left
`SITUATION` blank. I'm applying the procedure to the two concrete problems the P20 audit
(`op2-audit-p20.md`) just surfaced: (S1) `CONTEXT.md` §9 and the `Dockerfile` header still assert
"LangGraph fan-out causes D-62" as fact, contradicted by measured evidence committed the same
session (`d62-design.md` §3, `tests/integration/runtime/test_d62_suspension_contract.py`); (S2)
that evidence took the form of an unmarked test that fails by design, so it silently reddens the
default `pytest`/`just test`/`just ci` run — a `CONTEXT.md` §11 tripwire-15 condition. If you meant
a different gate/contradiction, say so; nothing below has been executed.

---

## 1. Ground truth

- `CONTEXT.md` §0/§7 and `AGENTS.md` §1: `CONTEXT.md` is the sole state of record, read end-to-end
  at the start of every session; it must reflect "where reality already differs from the PRD" and
  be trustworthy on its own, without requiring `git log`.
- `AGENTS.md` §7: every response ends with a `CONTEXT LEDGER PATCH` — §5/§7/§8/§9/§13 updates —
  copy-paste-ready. Non-discretionary.
- `AGENTS.md` §10 / `check_ledger.py`: §8 and §9 are append-only; a correction is a **new** row
  that names the one it supersedes, never an edit. `CONTEXT.md` is capped at 500 lines, CI-enforced.
- `pyproject.toml`'s own `acceptance` marker (and its comment) plus `CONTEXT.md` §11 item 15: a
  test that is known-red for a structural reason must be excluded from the default `-m` filter,
  not left to redden `main` — this exact mechanism already exists in this repo for this exact
  failure mode.
- PRD §3480 names the scheduler's API as `Scheduler.run()`/`Scheduler.register(task)` — the PRD
  has no opinion on *why* a deadlock happens; that fact lives only in code + the ledger.

## 2. Actual state (as found, verified directly)

- `CONTEXT.md:313` (§9, D-62 row, dated 2026-08-25): still states the fan-out cause as fact.
  Untouched by commit `5a17eb5`.
- `CONTEXT.md` §7 "Current position": frozen at 2026-08-27 (P18/19); no mention of the
  2026-09-01 verification session's own framing correction or of P20 at all. `grep -c P20
  CONTEXT.md` → 0.
- `Dockerfile:30-36`: a "CORRECTION" note says "One test settles it; it has not been run" — false
  as of 2026-09-02; the test ran and returned a confirmed result.
- `pyproject.toml` `addopts = "... -m 'not acceptance'"`; the new test file carries no marker.
  Ran it directly: `1 failed, 3 passed` — the failure is by design
  (`test_a_sequential_langgraph_graph_deadlocks_with_no_fanout_at_all`, raises via `pytest.fail`
  with "HYPOTHESIS CONFIRMED").
- `commit-step1.sh` (committed in the same diff) explicitly expects and accepts this non-zero
  exit, and its own trailing echo names `CONTEXT.md` §7/§9 and the Dockerfile as still stale — an
  acknowledgment in a shell script's stdout, not a fix, and not in `CONTEXT.md` itself.

## 3. The delta

| Expected | Actual | Entered at | Severity |
|---|---|---|---|
| `CONTEXT.md` §9 D-62 row reflects current best understanding | States the superseded fan-out claim as fact | Claim: P17, 2026-08-25. Superseded by: P20, `5a17eb5`, 2026-09-02. Never corrected | Medium-high |
| Dockerfile "CORRECTION" note stays current | Says the settling experiment "has not been run" | Note added ~2026-08-29/09-01; stale as of 2026-09-02 | Low-medium |
| `just ci`/`pytest` green by default, or red tests marked (established `acceptance` precedent) | New test collected by default, fails by design, unmarked | `5a17eb5`, 2026-09-02 | High — fires §11 tripwire 15; reverses the 2026-09-01 "first green `just ci`" milestone |
| `AGENTS.md` §7 ledger patch accompanies every commit | No `CONTEXT.md` change in `5a17eb5` | `5a17eb5`, 2026-09-02 | Medium — mechanical cause of the two rows above |

## 4. Root cause

None of this is a spec-vs-implementation defect, so the (a)–(e) menu doesn't quite fit — nothing
here is a wrong or misread PRD requirement. Closest mappings:

- **Rows 1–2 (ledger/Dockerfile staleness):** nearest to **(d)**, except it isn't a *reasonable,
  undeclared* deviation — the authoring session *knew* the ledger was now wrong (says so in its
  own commit message and in `commit-step1.sh`'s trailing note) and didn't log the fix. This is a
  sixth case the taxonomy doesn't name: **a known correction that was never propagated to the
  state of record.**
- **Row 3 (unmarked red test):** not a spec ambiguity either — it's a **process-precedent miss**.
  The correct mechanism (a marker + `addopts` exclusion) already exists in this same file for the
  same reason; it just wasn't reused.
- **Row 4** is the mechanical cause of rows 1–2.

No human decision is required to fix any of these three — the corrected facts already exist in
the repo (`d62-design.md`, the test itself). The one judgment call worth a nod before I touch
`pyproject.toml`: whether the decisive test should stay red-and-marked-experiment indefinitely, or
be deleted/inverted the moment someone picks an Option — I'm assuming "mark it, per the
`acceptance` precedent, and let the eventual fix delete/invert it" since that's what the test's
own docstring already says should happen.

## 5. Repair plan (ordered; none touches `src/agentdx/` product code or golden fixtures)

1. **Un-redden CI.** Add an `experiment` marker to `pyproject.toml` (parallel to `acceptance`);
   apply it to `test_a_sequential_langgraph_graph_deadlocks_with_no_fanout_at_all` only (the three
   controls should stay in the default run); update `addopts` to `-m 'not acceptance and not
   experiment'`.
   *Risk:* none to product code. *Proof:* `pytest -q` exits 0; `pytest -m experiment -q` still
   shows the 1-fail/3-pass result.
2. **Append a `CONTEXT.md` §9 row** (new ID, e.g. `D-81` — never edit D-62's row) recording that
   D-62's fan-out cause is superseded by `5a17eb5`'s measurement, citing `d62-design.md` §3/§3a.
   *Risk:* `CONTEXT.md` is at 497/500 lines — likely needs a rollover first (mechanical, done 27×
   already). *Proof:* `python scripts/check_ledger.py` passes; `wc -l` ≤ 500.
3. **Update `CONTEXT.md` §7 and add a §13 row** for 2026-09-02 so the mandatory read-through
   actually surfaces this. *Risk:* same rollover dependency as #2 — do both together. *Proof:*
   `grep P20 CONTEXT.md` returns a hit.
4. **Fix the Dockerfile's stale sentence** to record the actual result. *Risk:* none — comment
   only. *Proof:* `grep "has not been run" Dockerfile` returns nothing.
5. **Fix `d62-design.md` §6**, whose step 1 still says "run the §3 experiment" after §3 already
   reports it done. *Risk:* none — design-doc only.

Explicitly **not** in this plan: choosing and building Option A/B/D to actually close D-62 — that
needs its own ADR (per `d62-design.md` §1) and is a separate decision, not a stabilization repair.

## 6. Prevention

- **New `CONTEXT.md` §11 tripwire, proposed:** *"A new test is committed whose own code/docstring
  predicts or accepts a non-zero exit under the default `pytest` invocation, with no marker
  excluding it from `addopts`'s `-m` filter."* Mechanizable: grep new/changed test files for
  `pytest.fail(`/hypothesis-confirmation language and require a matching marker.
- **New ledger-sync check:** extend `check_ledger.py` (or a pre-commit rule) to warn when a commit
  touches an investigation doc like `d62-design.md` but not `CONTEXT.md` — a lint-level nudge for
  the `AGENTS.md` §7 obligation that today has no code-level enforcement.
- **Name this class of prompt:** add an `OP-4: diagnostic experiment` category to `CONTEXT.md` §0
  alongside `OP-1/2/3` — read-only on product code, but required to append a §9/§10 row and a §13
  entry before the session ends. Closes the taxonomy gap the P20 audit flagged (this work had no
  formal category and therefore no checklist forcing the ledger patch).

**Waiting for approval before touching anything**, per the stop conditions.
