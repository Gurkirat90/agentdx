"""Gate G5 (CONTEXT.md §5 row 10, §6): Σ(six buckets) + residual = virtual makespan.

Residual must be < 2% (`agentdx.toml`'s `[analysis] residual_tolerance`), on all three real
golden fixtures — not synthetic data, the actual `tests/golden/*.jsonl` logs this build ships.
This is also the "decomposition table prints in the terminal" week-5 demo milestone: run with
`-s` to see `format_decomposition_table`'s output for each fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentdx.analysis.overhead import decompose_critical_path, format_decomposition_table
from agentdx.analysis.redundancy import detect_redundancy
from agentdx.analysis.timing import build_timing_dag, critical_path
from agentdx.events.canonical import decode_event
from agentdx.events.schema import Event

_GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"
_FIXTURES = ("code_pipeline", "research_fanout", "support_triage")


def _load(name: str) -> list[Event]:
    path = _GOLDEN_DIR / f"{name}.jsonl"
    with path.open(encoding="utf-8") as f:
        return [decode_event(line) for line in f]


@pytest.mark.parametrize("fixture_name", _FIXTURES)
def test_decomposition_invariant_holds(fixture_name: str) -> None:
    """PRD §16.2.3, verbatim: buckets + residual sum to makespan within 1ms.

    And the residual fraction is below the configured tolerance — this *is* gate G5.
    """
    events = _load(fixture_name)
    dag = build_timing_dag(events)
    cp = critical_path(dag)
    groups = detect_redundancy(dag)
    dec = decompose_critical_path(dag, cp, events, redundancy_groups=groups)

    # The hard invariant (Design Constraint 2's runtime assertion already raised inside
    # decompose_critical_path if this were violated — re-asserted here as the test's own
    # independent check, not just trusting the analyser didn't raise).
    total = sum(dec.bucket_ms.values()) + dec.residual_ms
    assert abs(total - dec.virtual_makespan_ms) <= 1

    # Gate G5's tolerance.
    assert dec.residual_fraction < dec.residual_tolerance, (
        f"{fixture_name}: residual {dec.residual_ms}ms / {dec.virtual_makespan_ms}ms = "
        f"{dec.residual_fraction:.3%}, exceeds tolerance {dec.residual_tolerance:.1%}"
    )
    assert not dec.residual_flagged

    print(f"\n{fixture_name}:")  # noqa: T201 — the week-5 demo milestone is this table printing
    print(format_decomposition_table(dec))  # noqa: T201


def test_every_bucket_traces_to_evidence_seq() -> None:
    """I6: any non-zero bucket carries the event seqs that justify it."""
    events = _load("support_triage")
    dag = build_timing_dag(events)
    cp = critical_path(dag)
    dec = decompose_critical_path(dag, cp, events)

    for name, ms in dec.bucket_ms.items():
        if ms > 0:
            assert dec.bucket_evidence_seq[name], f"bucket {name!r} has {ms}ms but no evidence"
            assert all(isinstance(s, int) for s in dec.bucket_evidence_seq[name])
