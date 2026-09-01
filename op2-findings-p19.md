# OP-2 (partial) — findings against the 2026-09-01 P19 session's own work

**Scope and honesty statement.** This is **not** an independent audit. `CONTEXT.md` §0 requires
an OP-2 be run by a session that did not build the module, and this one wrote every artefact it
examines. It is a self-audit, and self-audit is exactly the thing this ledger's history says
under-finds — `scenario/` passed two consecutive repairs and the second independent audit still
found a defect *inside the first repair*. **A real OP-2 is still owed.**

Read-only per §0: no code changed, no ruling edited. Findings only.

Every line reference below was read, not recalled.

---

## F-12 — C-34's "replace the orphan" branch cannot be implemented

**Severity: high.** C-34 was ruled and committed (`99beb11`) roughly an hour before this audit.

C-34 says a `run_id` collision against an **unsealed** run is resolved by replacing it. It
justifies this with: *"I2 protects a sealed run's event log."*

That is false. `store/migrations/m0001_initial.py:75`:

```sql
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END
```

The trigger is **unconditional**. It does not consult `sealed_at`, `status`, or anything else.
Deleting an orphaned run's events is impossible at the database layer, by design.

`store/sqlite.py`'s own module docstring names this exact failure mode: the guarantee is that
nothing *can* delete, *"from this process, from the API reader, from `sqlite3` on the command
line, or from a later prompt that forgets."* The ruling was written by the later prompt that
forgot, and the evidence was one `grep` away.

Three escapes checked and closed:

- **No trigger on `runs`.** The run row *is* deletable while its events are not. Deleting only
  the row leaves orphan events with a dangling `run_id`, and `findings` carries
  `REFERENCES runs(run_id)`. Strictly worse than either extreme.
- **PRD §27.5 contemplates deletion** via `agentdx prune` — which does not exist. Only
  `cache prune` (`cli/main.py:228`), an exit-2 stub. There is no sanctioned run-deletion path
  anywhere in the codebase.
- **Triggers do come off inside a migration** (`store/migrations/__init__.py:190`) and are
  re-created in the same transaction. That is a precedent for a sanctioned window, not a licence.

## F-13 — D-78 was never an open product decision, and the schema already answers it

**Severity: high. This supersedes the framing of D-78, C-34, and `d78-plan.md`.**

`run_id` is marked `Volatility.IDENTITY` (`events/schema.py:262-271`), and the mark exists
*specifically* for it. The `Volatility` docstring (`:99-120`) says a binary volatile/stable split
*"has no honest home for `run_id`"*, and `IDENTITY` is documented as **"Differs by construction
between two otherwise identical executions."**

The field's own doc is explicit:

> `r_` + 5 hex of a content hash (PRD §6.1). IDENTITY, not stable: **two replays of the same run
> must differ here or they collide on runs.run_id**, so including it in the projection would make
> G3 unpassable by construction. Ruling R1.

Two consequences:

1. **`run_id` is excluded from the canonical projection.** Adding an attempt component to
   `make_run_id` costs I1 **nothing**. The I1 objection this session raised against the
   attempt-counter option — and used to rank it last of four — was wrong. Marked here because the
   owner's decision was made against that advice.
2. **`make_run_id(seed, scenario_hash, graph_hash)` (`runtime/scheduler.py:1177`) contradicts a
   week-1 schema ruling.** Being purely content-derived, it produces the *same* `run_id` for two
   replays — precisely what `events/schema.py` says must not happen. D-78 is not an unspecified
   product decision. It is `runtime/` never implementing what `events/` ruled, and R1 already
   states the resolution.

**Why nobody noticed:** G3's determinism suite drives the `Scheduler` directly and never goes
through `CliRunHost.open_run` → `store.create_run`, so the collision cannot surface there. The
gate that would have caught it is the one path it does not exercise. **Worth its own test.**

## F-14 — the R-series rulings are cited as authority and defined nowhere

**Severity: medium.** `R1` and `R4` are load-bearing:

- `R1` gates `run_id`'s volatility mark — `events/schema.py:120`, `:269`, `:476`;
  `docs/event-schema.md:208`, `:346`.
- `R4` gates float rejection across three modules — `events/canonical.py:63`, `:77`, `:457`;
  `events/validators.py:273`; `runtime/cache/key.py:22`, `:25`.

Neither is defined anywhere. `grep -c "R1" CONTEXT.md` → **0**. No R-series table exists in
`CONTEXT.md` §10, `docs/event-schema.md`, or `docs/journal/`.

The substance is restated inline at each citation, so nothing is *wrong* today — but there is no
authority to check a citation against, and §0.5's precedence order cannot rank a ruling that is
not in the ledger. This is the same class as tripwire 14's topical-mis-citation note and the P14
finding that ADR-014/D-58 were cited for content they do not contain.

---

## What this implies for the pending work

- **`d78-plan.md` is superseded** in its §2–§3. The sealed/orphaned split it specifies rests on a
  deletion the database forbids (F-12), and the whole reuse-and-print framing rests on a
  collision that R1 says should never occur (F-13).
- **C-34 needs an appended amendment**, following the C-11 / C-13 precedent — an amendment note,
  never a silent edit.
- **The attempt-counter resolution is now corroborated by the codebase**, not a compromise: it is
  what `events/schema.py` has required since week 1, and it costs I1 nothing.
- **An ADR may not be needed to override PRD §6.1.** R1 already resolves it. What is needed is
  R1's *definition of record* (F-14), after which `make_run_id` is a straightforward defect
  against it rather than a spec deviation.

## What this audit did NOT cover

Stated so the gap is not mistaken for a clean bill:

- `d62-design.md`'s §3 hypothesis — **unrun**. It needs CPython 3.12; this sandbox has 3.10.
- The D-77 fix and its 12 tests — not re-derived against the *revised* `is_fixture_name` written
  by the OP-3 session. Only the original version was checked.
- `gen/make_fixtures.py`'s refactor — the owed no-op re-run of the fixture generator. Unrun.
- Everything requiring execution: the suite, mypy, import-linter, Docker.
- **This audit is not independent.** See the scope statement.
