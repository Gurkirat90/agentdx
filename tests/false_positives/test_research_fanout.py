"""PRD §33.9 row 1: the research fan-out fixture - **zero** findings, always.

Gate G2 / invariant I4, in full: "`research_fanout` yields an empty race-findings set across
all 100 determinism replays **and** the k=2 exploration frontier" (`CONTEXT.md` §2). The
100-replay half is proven here, self-contained, so `pytest tests/false_positives/ -q` alone is
sufficient evidence for the gate (not split across two directories a reviewer has to remember
to run together); the k=2-frontier half is `test_k2_frontier.py`, in this same directory.

`research_fanout` is real: four workers concurrently write a genuine LangGraph
`Annotated[list[str], operator.add]` reducer channel (`fixtures/research_fanout/graph.py`,
`README.md`) - every write's `payload.reducer` is `"operator.add"`, not a test fixture faking
the field. Guard G3 (declared reducer) is what must suppress all `C(4,2) = 6` raw concurrent
write pairs; if this test ever fails, per the mission's own STOP CONDITION: that means either a
real bug was seeded into this fixture, or the detector is unsound - stop and ask, do not adjust
the assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentdx.analysis.race import detect_conflicts, find_conflicts
from agentdx.events.canonical import decode_event
from agentdx.events.schema import Event

_GOLDEN_LOG = Path(__file__).resolve().parents[2] / "tests" / "golden" / "research_fanout.jsonl"
_REPLAYS = 100


def _load() -> list[Event]:
    with _GOLDEN_LOG.open() as fh:
        return [decode_event(line) for line in fh]


@pytest.mark.false_positives
def test_research_fanout_has_zero_findings() -> None:
    events = _load()
    findings = detect_conflicts(events)
    assert findings == (), f"gate G2 FAILED: {len(findings)} finding(s) on research_fanout"


@pytest.mark.false_positives
def test_research_fanout_raw_conflicts_are_all_suppressed_by_g3() -> None:
    """Not just "zero reported" - the six raw `write_write` pairs exist, and are all G3.

    A passing `test_research_fanout_has_zero_findings` above is thereby provably G3 doing its
    job, not an accident of an algorithm that never even considered these writes concurrent.
    """
    events = _load()
    conflicts = find_conflicts(events)
    write_write = [c for c in conflicts if c.subtype.value == "write_write" and c.key == "findings"]
    assert len(write_write) == 6, f"expected C(4,2)=6 raw write_write pairs, got {len(write_write)}"
    assert all(c.suppressed_by == "G3" for c in write_write), [
        (c.evidence_seq, c.suppressed_by) for c in write_write
    ]


@pytest.mark.false_positives
def test_research_fanout_is_clean_across_100_replays() -> None:
    """I4's "100 determinism replays" half, self-contained in this gate-G2 directory."""
    events = _load()
    for i in range(_REPLAYS):
        findings = detect_conflicts(events)
        assert findings == (), f"gate G2 FAILED on replay {i}: {len(findings)} finding(s)"
