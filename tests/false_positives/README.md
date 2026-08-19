# tests/false_positives/

**Gate G2 / invariant I4 — never waived.** Zero findings on the healthy `research_fanout` fixture
across 100 determinism replays *and* the k=2 exploration frontier. Run with
`pytest tests/false_positives/ -q` — that command alone is the gate's full evidence.

Contains its own minimal, test-only k=2 schedule enumerator (ADR-002). It is P0 and independent of
FR-6, which is P1 and scope-cut #1. **It must never grow a CLI flag, an output format or a
reduction report** — that is scope creep, not progress (CONTEXT.md §11.9b).

## Contents (P12)

- `test_research_fanout.py` — the real `research_fanout` golden log: zero findings, the six raw
  `write_write` pairs all suppressed by G3 specifically (not an accident of G1/G2), and the
  100-replay half of I4, self-contained in this directory.
- `test_reducer_channel_four_writers.py`, `test_lock_protected_writes.py`,
  `test_sequential_pipeline.py`, `test_identical_concurrent_writes.py` — the four synthetic PRD
  §33.9 rows (guards G3, G4, G1, G2 respectively), each hand-authored rather than reusing
  `research_fanout` again.
- `_k2_frontier.py` / `test_k2_frontier.py` — the k=2 harness itself (see its own docstring for
  exactly what "k=2" means in a build with no real scheduler yet) and the k=2 half of I4, run
  against a `research_fanout`-shaped synthetic log reordered every way the frontier allows.

**Scope note.** PRD §33.9's table has three rows this directory does not cover: retry-of-a-tool-
-call-is-not-redundancy, ≥3-average-parallelism-is-not-fake-fan-out, and single-agent-run-has-no-
-coordination-findings. Those are `analysis.redundancy` and `analysis.verdict`'s own gates,
already `BUILT` with their own dedicated suites (`tests/analysis/test_redundancy.py`,
`tests/analysis/test_verdict.py`) — see `docs/race-detection.md`'s coverage statement for the
full reasoning. This directory covers exactly the rows that are `race.py`'s own responsibility.
