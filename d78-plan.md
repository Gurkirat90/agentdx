# D-78 resolution plan — `run_id` collision on re-run

**Status (2026-09-03): implemented.** See CONTEXT.md §8 **ADR-020** for the built mechanism,
its one deviation from this plan's own §5.1 wording (deleting an orphan's events needs a
narrow, guarded bypass of `events_no_delete`/`events_no_update` this plan did not spell
out — see ADR-020's own text), and how §6's open question was resolved (additively, not via
a separate ADR of its own). §7's five decisive tests are built:
`tests/unit/store/test_discard_orphan_run.py` (test 4, plus the store half of 3 and 5) and
`tests/integration/cli/test_run_reuse.py` (tests 1, 2, the CLI half of 3 and 5, plus a sixth
covering §6's own resolution). This plan's own text below is left as written — the record of
what was decided before building, not edited to match what shipped.

---

## 1. The defect, restated from the code

`CliRunHost.open_run()` (`cli/host.py:213`) calls `store.create_run(...)` with
`status="running"` **before** the graph executes. `run_id` comes from
`make_run_id(seed, scenario_hash, graph_hash)` (`runtime/scheduler.py:1177`), which PRD §6.1
requires to be content-derived — invariant **I1**. The store rejects a duplicate primary key
with `E-STORE-010` (`store/sqlite.py:577`) — invariant **I2**.

Both are correct in isolation. Together, re-running any fixture at the same seed is
impossible: identical inputs produce an identical `run_id`, and the row is already there.
The demo is once-per-machine until `~/.agentdx/agentdx.db` is deleted.

## 2. The ruling

**A collision means the identical run already exists and its result is already known.**
Same seed, same scenario hash, same graph hash — by I1 the output is byte-identical to what
is on disk. Producing a second copy is not a feature; refusing to answer is not either. The
CLI reports the existing run instead.

This keeps `run_id` purely content-derived (I1 intact), keeps the store append-only (I2
intact), and adds no flag. It is arguably what determinism *means*.

## 3. The case the four options missed — read this before implementing

The run row is written **before** execution, so a collision has two very different causes,
and today the second is the common one:

| Prior run | `sealed_at` | What reuse-and-print should do |
|---|---|---|
| **Completed** — sealed, analysed, has findings | set | **Reuse.** Print its scorecard and findings, exit with the verdict's own code (§4). |
| **Orphaned** — `running`/`analysing`, never sealed | `None` | **Replace.** It holds no analysable log. |
| None | — | Normal path. |

Every machine that has hit **D-62** is in the orphaned state right now: the deadlock leaves a
`status="running"` row with no sealed log. Applying reuse-and-print blindly there would print
nothing and exit 0 — strictly worse than the error it replaces, because it would report
success for a run that never happened.

**Replacing an unsealed run does not violate I2.** I2 protects the event log's
append-only-ness; an unsealed run has no analysable log at all. The project already reasons
this way: PRD §27.3 chooses `synchronous=NORMAL` precisely because "an interrupted run is not
analysable anyway."

*This split was raised as an extension of the owner's ruling rather than part of it, and was
**owner-confirmed 2026-09-01**. It is now ruled as **C-34** (`CONTEXT.md` §10), with the
selection recorded as **D-80** (§9) — appended rather than editing D-78, which §9's
append-only rule forbids.*

## 4. Exit codes — reuse must not flatten them

Exit codes are a **MAJOR contract** (`CHANGELOG.md` version policy; PRD §37.2). Reuse must
replay the prior run's own outcome, not report success for having found a row:

- prior run passed its assertions → exit 0
- prior run failed an assertion → exit **1**, and print the failure as the original did
- prior run was `aborted_guard` → exit **4**

A blanket exit 0 on reuse would turn a previously-red CI run green on re-invocation. That is
the single worst failure this change could introduce, and it is the thing to write a test for
first.

## 5. Where the change goes

1. **`cli/host.py::open_run`** — before `create_run`, call `store.get_run(self._run_id)`.
   - `None` → unchanged.
   - present and `record.sealed` → raise a typed `RunAlreadyExistsError` carrying the record,
     for the CLI layer to render. **Do not print from `host.py`** — it holds no `Output`, and
     `cli/` keeps rendering in the command modules.
   - present and not sealed → delete the orphan row and its events, then proceed. Needs a
     narrowly-scoped `store` method; do not reach into SQL from `cli/`.
2. **`cli/commands/run.py`** — catch `RunAlreadyExistsError`, load the stored analysis, render
   the same scorecard/findings a fresh run would, and map to the exit code in §4.
3. **`store/sqlite.py`** — one new method for (1)'s orphan path. It must refuse to touch a
   sealed run: the guard belongs in `store/`, not in its caller (tripwire 18 — an invariant's
   enforcement must not be skippable from the call site).

## 6. Open — needs an ADR, do not guess

`agentdx run` becoming idempotent is a user-visible semantic change not described in PRD
§37.1 or §22. It deserves an ADR in `CONTEXT.md` §8 naming the sections it refines. The
narrower question the ADR must settle: **is the reuse message on stdout or stderr, and does
`--ci`/JSON output represent a reused run differently from a fresh one?** A CI consumer
diffing two JSON payloads must be able to tell "this re-ran" from "this was already known."

## 7. Tests that prove it

Each must fail before the change:

1. Re-running a **sealed** run prints its findings and exits with the prior verdict's code.
2. Re-running a run whose prior attempt **failed an assertion** exits 1, not 0. *(The §4
   regression — write this one first.)*
3. Re-running an **orphaned** (`running`, unsealed) run proceeds to a real run rather than
   reusing nothing.
4. A **sealed** run is never deleted by the orphan path, asserted at the `store/` boundary.
5. `run_id` is unchanged by all of the above — `make_run_id` keeps its I1 property, asserted
   directly rather than implied.

## 8. What this does and does not unblock

Closes D-78, which blocks **G9** independently of D-62. It does **not** make G9 pass: with
D-78 closed, `just demo-offline` still deadlocks at D-62 (`E-SCHED-003`). The value is that
the demo stops being once-per-machine, so D-62 can be worked on without deleting the database
between every attempt.
