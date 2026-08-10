# tests/false_positives/

**Gate G2 / invariant I4 — never waived.** Zero findings on the healthy `research_fanout` fixture
across 100 determinism replays *and* the k=2 exploration frontier.

Contains its own minimal, test-only k=2 schedule enumerator (ADR-002). It is P0 and independent of
FR-6, which is P1 and scope-cut #1. **It must never grow a CLI flag, an output format or a
reduction report** — that is scope creep, not progress (CONTEXT.md §11.9b).
