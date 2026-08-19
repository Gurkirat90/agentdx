# Addendum — D-45 closed (migration-based resolution)

Follow-up to `op3-repair-report-p09.md`'s NOT DONE item 1. You selected **"Write a migration (Recommended)"** via the clarifying question; this is done. Follows `AGENTS.md` §7's four-block contract, scoped to just this piece.

---

## SELF-AUDIT

**Done, mutation-verified:**

- `SCHEMA_VERSION` bumped 1 → 2 (`src/agentdx/events/schema.py`); `run_end.payload.status`'s enum gained `"aborted_guard"`.
- `events/migrations/__init__.py`'s previously-empty `MIGRATIONS` registry gained its first real entry: `_migrate_v1_to_v2`, purely additive (`{**record, "schema_version": 2}` — a v1 record predates the new enum member, so there is nothing to translate, only the version marker to advance).
- `events/canonical.py::decode_event` now calls `migrations.migrate(raw, to_version=SCHEMA_VERSION)` on the raw parsed record, immediately after confirming it's a `Mapping` and before constructing the `Event` — the boundary *before* `check_structural`'s strict `E-EVENT-008` check, which has no migration tolerance of its own.
- **No golden fixture was touched.** `event_log_40.jsonl`, `code_pipeline.jsonl`, `support_triage.jsonl`, `research_fanout.jsonl` are byte-for-byte unchanged — confirmed by never calling `fixtures_runner.regenerate_all()`, and by the fixture-comparison harness recomputing both sides' canonical hash fresh on every call rather than pinning either.
- One test constant *did* need a considered update: `test_golden_log.py`'s pinned `GOLDEN_HASH`. `schema_version` is a `Volatility.STABLE`, canonically-hashed field, so migrating it 1→2 in memory necessarily moves every event's canonical hash even though the underlying bytes never move. The new value was computed directly from the committed file through the now-migrating `decode_event` — not by regenerating anything. The test file's own comment states in full why this is not a golden-file regeneration under `AGENTS.md` §5, given how close this sits to the exact rule that blocked the first D-45 attempt.
- Two more tests updated for the same root cause, both now dynamic rather than hardcoded: `test_writer.py`'s registry-emptiness assertion (now asserts the real `{1: _migrate_v1_to_v2}` contents), and `test_bundle_safety.py`'s manifest string-replace (was hardcoded to `schema_version:1`, now built from the live `SCHEMA_VERSION` constant so it keeps working after future bumps too).
- A `mypy --strict` complication was resolved without an escape hatch: `migrate()`'s precise return type was too narrow for `decode_event`'s existing loose field coercions. Rather than an explicit `cast(..., Any)` (banned by this project's `disallow_any_explicit = true`), the migrated result round-trips through an `object`-typed local and a second `isinstance` narrowing — the same pattern the function already used on its own input.

**Mutation-verified:** temporarily bypassing the `migrate()` call inside `decode_event` reproduces exactly 3 of the original 6 failures (the fixture-validation, pinned-hash, and reserialisation-determinism tests); restored and re-verified green, confirmed byte-identical to the pre-mutation file via `diff`.

## VERIFY THIS YOURSELF

```
cd agentdx && source .venv/bin/activate
PYTHONHASHSEED=0 python -m pytest      # 1872 passed (same count — 2 tests rewritten, none added/removed net)
ruff check src tests                    # All checks passed!
ruff format --check src tests           # 161 files already formatted
mypy --strict                           # Success: no issues found in 58 source files
lint-imports                            # Contracts: 10 kept, 0 broken
python scripts/check_determinism_hygiene.py   # same 3 pre-existing scenario/validate.py findings, unrelated
```

## CONTEXT LEDGER PATCH

Applied to `CONTEXT.md` directly:
- §9: new row **D-49**, explicitly closing D-45 (append-only — D-45 itself is untouched).
- §5 row 9, §7 Current Position, top metadata: all updated to say D-45 is closed, nothing is blocked.
- §13: new session-log row; the oldest existing row (`store/` OP-3, 2026-08-11) rolled into `docs/journal/2026-33.md` (seventh rollover) to keep the table at 15.

## NOT DONE / RISKS

1. **Production wiring is still not done.** Nothing in this build sets a real run's `run_end.payload.status` to `"aborted_guard"` — that needs `sdk/generic.py`'s `run()` (or wherever `RunHost` eventually seals a run) to branch on `AbortGuardTripped`, and no such call site exists yet. This was out of scope for "resolve D-45's schema gap" specifically; same "declared capability, no call site yet" shape as D-37/D-47. Flagging so it isn't silently assumed done.
2. The re-audit `runtime/faults/` was already owed before `VERIFIED` (per `op3-repair-report-p09.md`) is unchanged and still owed — it now additionally covers this addendum's schema/migration work, since it lands in the same module family before that audit runs.
3. Everything above is verified in the sandbox and now synced to your device (`/Users/gurkiratsingh/Desktop/SUNDAY`) along with the original `op3-repair-report-p09.md`, which hadn't made it across in the prior session due to the bridge being disconnected at the time.
