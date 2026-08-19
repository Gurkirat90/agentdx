# tests/analysis/race/

Tests for `agentdx.analysis.causality` (vector clocks, happens-before) and
`agentdx.analysis.race` (conflict detection, the four PRD §14.7 guards, minimal repro,
determinism). Gate **G1** — `test_gate_g1.py` runs `detect_conflicts` directly against the
real, committed `tests/golden/code_pipeline.jsonl` and asserts the required `lost_update` on
`draft.module_a`.

The mandatory false-positive suite (gate **G2**, invariant I4) lives in `tests/false_positives/`
instead, per that directory's own README and `CONTEXT.md` §5 row 12b — it is a separate,
never-skippable suite, not folded into this directory's ordinary test files.

- `_events.py` — a local, explicit `Event` builder extending `tests/analysis/_events.py`'s
  `ev()`/`RUN_ID` with the `reducer`/`lock_id`/`txn_id`/`value_hash` parameters this module's
  tests need and the shared builder deliberately omits (it hardcodes one fixed `value_hash`
  and `reducer=None` for every `state_write`, which is correct for the timing/aggregates
  tests it was built for and wrong for a module whose entire subject is value divergence and
  guard fields). Not a fork of the shared builder — it imports and reuses `ev`/`RUN_ID`.
- `test_causality.py` — vector clock rules (local/send/receive/lock/barrier), `happens_before`/
  `concurrent`, cross-checked against the real golden logs' own stamped `vclock` field.
- `test_conflicts.py` — classification (§14.5), value divergence (§14.6), dedup (§14.3).
- `test_guards.py` — the four guards, each independently tested in both directions (eight
  tests total): one proving the guard suppresses the false-positive class it exists for, one
  proving it does not suppress a genuine conflict sharing a surface feature with that class.
- `test_minimal_repro.py` — §14.8.
- `test_determinism.py` — NFR-14: 100 analyses of one log, byte-identical findings, in order,
  plus 8 fresh-interpreter replays at varied `PYTHONHASHSEED` (`_subprocess_runner.py`).
- `test_gate_g1.py` — gate G1 itself, against the real golden log.
- `test_true_positive_matrix.py` — PRD §33.8's literal 12-case matrix as one auditable
  checklist, cross-referencing the fuller tests above and adding the few shapes nothing else
  here covers (three-agent conflict, same-agent-two-subtasks, message-ordered write_write).
