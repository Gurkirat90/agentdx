"""Delay-bounded schedule exploration, independence-based reduction and dedup (FR-6).

The only module that runs many runs. P1 and scope-cut #1 — the `tests/false_positives/`
k=2 harness is a separate, P0, test-only enumerator and is never cut with it (ADR-002).
May import `runtime` and `analysis.race`. Lands at P13.
"""
