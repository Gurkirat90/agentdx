# OP-3 REPAIR REPORT — second P06 `runtime/` re-audit (`op2-audit-p06-second.md`)

**Repaired same day as the audit, by the orchestrating session (not the audit agent — the
audit agent used only `Read`/`Grep`/`Bash`, no `Edit`/`Write`).** Owner authorized fixing all
three findings in full, applying the audit's own narrow/surgical suggested fix directions
rather than novel designs.

## Finding #1 (CRITICAL) — `yield_point`/`sleep` shared `join`'s pre-ADR-018 ambient-identity collision

**Repair.** `runtime/scheduler.py` gained a new private method,
`_check_ambient_identity_ownership(task_id, *, call)`, reusing `begin_call`'s own existing
comparison (`self._identity_owners.get(task_id) is asyncio.current_task()`) verbatim rather
than re-deriving it. Called from the top of both `yield_point` and `sleep`, immediately after
each confirms `task_id` names a task this scheduler knows, and before either touches
`self._task_futures`. A mismatch — a real `asyncio.Task` other than the identity's recorded
legitimate owner making the call — now raises `SchedulerError` (`E-SCHED-001`) immediately,
naming the actual defect ("two concurrent real tasks... sharing one ambient scheduler identity
with no `begin_call`/`end_call` protection") and pointing at the established fix pattern
(`sdk.langgraph.LangGraphAdapter.run_node_async`), instead of silently overwriting the first
caller's `Future` and surfacing, up to 200 ticks later, as a misleading "genuinely stuck on
real, unmanaged concurrency" error.

This is the audit's own "at minimum" suggested direction, not its "mint a fresh identity
inline" alternative: the latter would need a new begin/end-style retirement lifecycle for an
identity minted mid-`yield_point`/`sleep`, with no natural call-scope to retire it against
(unlike `begin_call`'s own `run_node_async` caller, which retires its minted identity in a
`finally` right after `join()` returns) — a real design question, not a narrow fix, and one
the audit itself left unresolved even in its "full" suggested direction. Detecting the
collision and raising immediately closes the actual defect (a misleading, delayed error
masking a real scheduler bookkeeping bug) without inventing new lifecycle machinery.

**Verified, real `Scheduler`, both repro scripts from the audit, before and after:**
- `repro6b_concurrent_yieldpoint_collision.py` (two concurrent "LLM calls" via
  `asyncio.gather`, no `begin_call`): before the fix, hung 200 ticks then raised the
  misleading `SchedulerError` the audit quoted. After the fix: raises immediately, on the
  very first colliding call — `order so far: ['a-before', 'b-before']` (only two entries,
  not three — caught before `_llm_call("b")`'s own `yield_point` even runs) — with the new,
  correctly-diagnosed message.
- `repro7_concurrent_sleep_collision.py` (two concurrent `sleep()` calls): identical result,
  immediate diagnosis instead of the old misleading path.
- Confirmed the protected path is untouched: `repro1_threeway_fanout.py` (2/3/4/5/8-way
  fan-out), `repro2_nested_fanout.py` (nested fan-out) and `repro3_exception_fanout.py`
  (exception inside a fan-out branch) all still complete cleanly, no leaks — the new check
  never fires for a real task correctly calling under its own identity.
- 2 new decisive unit tests (`tests/unit/runtime/test_scheduler.py`,
  `test_concurrent_yield_point_under_one_shared_identity_raises_immediately`,
  `test_concurrent_sleep_under_one_shared_identity_raises_immediately`), each asserting the
  immediate `SchedulerError` and the truncated interleaving order (`b-after`/`y-after` never
  appearing) — the same "before/after" evidence shape the audit itself used.
- All three reference fixtures (`code_pipeline`, `research_fanout`, `support_triage`) still
  run end-to-end, fresh, exit 0, via the real `agentdx` CLI — confirming the new check never
  false-positives on legitimate sequential or protected-fan-out execution, including
  `code_pipeline`'s own real `planner -> {coder, reviewer}` LangGraph fan-out.

## Finding #2 (HIGH) — ADR-021's root-entry fix did not generalize to a spawned node body's own first dispatch

**Repair.** `sdk/langgraph.py::LangGraphAdapter._run_node_body` now calls
`await run.scheduler.yield_point("sdk_node_entry")` as the very first statement, before
`use_run`/`agent_scope`/`base.ainvoke` are ever reached — reusing ADR-021's own established
pattern (`sdk/generic.py:1879`, `"sdk_run_entry"`) verbatim at this new call site, per the
audit's second suggested direction (the first — a `_drive_coro`-level universal warm-up — would
be new scheduler-core machinery; this reuses an already-proven pattern instead). Since
`_run_node_body` is itself `spawn()`-ed by `run_node_async` and is therefore *this task's own*
first dispatch, this gives every spawned node body the same one guaranteed checkpoint root
already had, so whatever the node's own body does next (including a multi-tick real-async
settle) happens on a resumption (`resume_drain_ticks`, 200 ticks), never on the untouched,
deliberately-strict first dispatch (one tick). `sdk/generic.py`'s own ADR-021 comment block was
updated with a one-paragraph cross-reference noting the scope correction.

**Verified, causally, both directions, through the real shipped adapter (not a scheduler-
primitive stand-in):**
- Built a real single-node LangGraph graph (`tests/integration/sdk/test_node_entry_drain.py`)
  whose node body's first action is the identical "needs another real task to run" settle
  shape `test_root_entry_drain.py` uses for root, instrumented via `agentdx.instrument()` and
  driven by a real `Scheduler` (`scheduler.run(agentdx.run(graph, ...))`, mirroring
  `test_root_entry_drain.py`'s own construction). With the fix: completes cleanly,
  `result.output == {"task": "t", "plan": "settled"}`.
- **Direct causal proof, not just correlation**: temporarily reverted the one new line (a
  scripted, reviewed edit, restored immediately after) and re-ran the identical scenario —
  reproduced `DeadlockError` with `t_..._slow_0` showing an empty `wait_reason` (the exact
  signature the audit's own `repro5_nested_immediate_fanout.py` traced this finding through),
  `t_..._root_0` correctly shown blocked on `join(...)`. Restored the fix; re-ran; passes
  again. This is the same "manually revert the line, confirm the exact regression, restore"
  discipline ADR-021's own repair report used.
- Second, decisive test in the same file: the identical settle shape spawned directly on the
  scheduler (bypassing `_run_node_body` entirely, so its `yield_point` never runs) still
  deadlocks with `E-SCHED-003` — proving the "survives" test above is not vacuously true of
  any multi-tick settle, only of one this specific fix protects.
- **The audit's own literal reproduction shape checked directly, not assumed**: the audit
  flagged, as a NOT-DONE item, "whether Finding #2 is reachable through the shipped LangGraph
  adapter specifically... is a follow-up check for the repair session." Checked:
  `tests/unit/sdk/test_langgraph_adapter.py::test_a_mounted_subgraph_is_a_fatal_gap_rather_than_one_opaque_agent`
  confirms a compiled subgraph mounted as a node is refused outright at bind time
  (`InstrumentationError`, `E-INSTR-002`) — the specific "subgraph node whose first superstep
  fans out" scenario cannot occur through the shipped adapter today. This does not make the
  fix unnecessary: it protects the *general* principle (every spawned task's first dispatch
  gets one guaranteed checkpoint, unconditionally, regardless of what the node's own body
  does) against any other way a node's first action might need multiple real ticks — not only
  the specific subgraph shape the audit could most easily demonstrate — and costs nothing
  (verified below: byte-identical determinism, no test regression, real fixtures unaffected).
- All three reference fixtures still run end-to-end, fresh, exit 0.
- `tests/golden/`, `tests/determinism/` (byte-identical canonical projections, independent of
  this fix's call path) unaffected — these use `fixtures/_harness.py`, a separate, provisional
  harness that never touches `sdk/langgraph.py` at all.

## Finding #3 (MEDIUM-HIGH) — `make_run_id`'s 32-bit hash plus silent-reuse could misattribute a stale verdict

**Repair.** `cli/host.py` gained `RunIdCollisionError(RuntimeError)`, a new exception distinct
from `RunAlreadyExistsError` (deliberately not a flag on it, so no caller can accidentally
catch both the same way). `CliRunHost.open_run`'s collision check now compares
`existing.scenario_hash`/`existing.graph_hash`/`existing.seed` against the current
invocation's own resolved values before treating a sealed collision as a legitimate D-80
rerun: a match still raises `RunAlreadyExistsError` (reuse, unchanged behaviour); a mismatch
raises the new `RunIdCollisionError` instead. `cli/commands/run.py` imports the new exception
and gained an `except RunIdCollisionError` handler (immediately after the existing
`RunAlreadyExistsError` handler, confirmed via `Grep` as the only call site needing it),
returning `INTERNAL_ERROR` (exit 5) with the collision detail — an internal error, never a
scenario pass/fail, so the exit code cannot be mistaken for either (I9: report NOT DONE rather
than fabricate an answer).

`scenario_run.py --repeat` and `_baseline.py::CliBaselineExecutor` were checked and found not
to need the same handling: both use fresh, throwaway, isolated `Store`s per execution, never
reaching the CLI's persistent `~/.agentdx/agentdx.db` path the audit specifically targeted —
this collision path is not reachable through either.

**Verified, forced deterministic collision (real accidental collisions are far too rare to hit
by chance — a 32-bit space):** `tests/integration/cli/test_run_id_collision.py`, new. Runs a
real scenario (seed 42) to completion via the real CLI/`Scheduler` (sealed, real `run_id`),
then monkeypatches `agentdx.cli.commands.run.make_run_id` to force that *same* `run_id` for a
second, different scenario (seed 7) — reproducing the exact ambiguity the audit demonstrated
against the real 32-bit digest, deterministically rather than by chance. Asserts the second
invocation exits `INTERNAL_ERROR`, the detail names the collision, and neither output stream
says "reused" (the wrong-verdict misattribution the finding described). A third invocation —
re-running the *original* scenario, with the monkeypatch still active — confirms the fix
distinguishes a genuine collision from a legitimate rerun correctly regardless of what the
(faked) hash returns: since the original scenario's real resolved inputs match the sealed
record's stored inputs, it is correctly reused (`RunAlreadyExistsError`), proving the check
compares actual inputs, not just "does a sealed row exist at this id."

## What held, no repair needed

Per the audit's own "What was checked and holds up" section: N-way (2/3/4/5/8-way) fan-out via
the protected `begin_call`/`spawn`/`join` path, nested/recursive fan-out, exception propagation
through a fan-out branch, `_backgrounded` staleness (ADR-022), and determinism (I1) across
repeated real-fixture runs — none of these needed any change, and all were re-confirmed still
holding after this pass's fixes (same repro scripts, re-run post-fix, in Finding #1's
verification above).

## Full verification, this pass

- `ruff check`/`ruff format --check` — clean on every touched file
  (`runtime/scheduler.py`, `sdk/langgraph.py`, `sdk/generic.py`, `cli/host.py`,
  `cli/commands/run.py`, and the three new/modified test files).
- `mypy --strict` — clean on every individually-checked touched file (used a scratch
  `--cache-dir` after finding this sandbox's default `.mypy_cache/` contains stale,
  permission-locked entries from a prior process — an environment quirk, not a code issue;
  confirmed by re-running with a fresh cache dir).
- `lint-imports` — 10/10 contracts kept, 154 files, 787 dependencies; no new cross-layer
  import (`sdk/` still never imports `analysis/`, `runtime/` still never imports `sdk/`).
- `check_determinism_hygiene.py` — clean on every touched file; only the same standing,
  pre-existing D-66 `api/models.py` parse gap remains (Python 3.10 sandbox, unrelated).
- `pytest tests/unit/runtime/ tests/integration/runtime/ tests/unit/sdk/ tests/integration/sdk/`
  — 103 passed, 1 deselected, zero regressions (includes all 4 new tests from this repair).
- Full suite (`tests/api` excluded, D-66): **2199 passed, 1 failed, 11 deselected** — the one
  failure is `test_doctor_passes_on_a_healthy_setup`, confirmed via `git diff HEAD` to be
  completely untouched by this pass (neither the test nor `doctor.py` changed), a true,
  correct diagnosis by `doctor`'s own `python-version` check of this sandbox's real
  interpreter (3.10.12) being outside the pinned `>=3.12,<3.13` range — the exact standing
  D-66 baseline every prior ADR row in this ledger (ADR-019 through ADR-022) has carried.
  Zero regressions relative to that baseline.
- `pytest tests/acceptance/test_gates.py -m acceptance -k "test_g1_ or test_g4_ or test_g6_ or test_g7_"`
  — 4/4 still pass, real subprocess.
- `tests/golden/`, `tests/determinism/` — clean, unaffected (separate harness, per Finding
  #2's verification above).
- 6 new/updated tests, all passing: `tests/unit/runtime/test_scheduler.py` (+2),
  `tests/integration/sdk/test_node_entry_drain.py` (new, 2 tests),
  `tests/integration/cli/test_run_id_collision.py` (new, 1 test).

## Standing status

**A third independent re-audit remains owed**, same pattern every other repaired module in
this ledger carries — this repair is self-verified by the orchestrating session, not
independently re-checked by a fresh auditor. Not done, explicitly, matching the prior audit's
own NOT DONE list where it still applies: no re-verification on real Python 3.12 hardware
(this sandbox's standing D-66 gap; nothing about any of these three fixes is
Python-version-specific in mechanism — pure `contextvars`/`asyncio.Future` bookkeeping and a
straightforward field comparison); `clock.py`/`determinism.py` were not independently fuzzed
beyond the existing suite (the audit itself found no defect there and didn't extend the fuzz
budget to them, so nothing to repair); `make_run_id`'s 32-bit `digest_size` itself was left
unwidened — the audit named widening it as a good complementary defense-in-depth change that
"does not by itself close the silent-misattribution risk class" the actual repair above closes
regardless of hash width, so it was correctly scoped out of a narrow, surgical repair pass.
