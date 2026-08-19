"""PRD §33.9 row 5: identical concurrent writes - zero conflicts (guard G2).

Three agents, genuinely concurrent, all writing the exact same value to the same key - nothing
was lost (PRD §14.6: "identical concurrent writes are harmless"). No reducer, no lock - only G2
can be suppressing this.

**Rewritten 2026-08-19 (OP-2 finding, `CONTEXT.md` §13) to build the log through `CausalLog`**
rather than hand-setting `vclock={}` alongside `causal_parents=()` - see
`tests/analysis/race/_causal_log.py`'s module docstring for why that combination was never a
shape the real scheduler produces.
"""

from __future__ import annotations

import pytest

from agentdx.analysis.race import detect_conflicts, find_conflicts
from agentdx.events.schema import Event
from tests.analysis.race._causal_log import CausalLog
from tests.analysis.race._events import state_write


def _three_identical_concurrent_writes() -> list[Event]:
    log = CausalLog()
    for i in range(3):
        log.add(
            state_write,
            agent_id=f"worker_{i}",
            span_id="s",
            key="status",
            value="ready",  # every writer agrees
        )
    return log.events


@pytest.mark.false_positives
def test_identical_concurrent_writes_yield_zero_findings() -> None:
    events = _three_identical_concurrent_writes()
    assert detect_conflicts(events) == ()


@pytest.mark.false_positives
def test_all_raw_pairs_are_suppressed_by_g2_specifically() -> None:
    events = _three_identical_concurrent_writes()
    conflicts = find_conflicts(events)
    write_write = [c for c in conflicts if c.key == "status"]
    assert len(write_write) == 3  # C(3, 2)
    assert all(c.divergent is False for c in write_write)
    assert all(c.suppressed_by == "G2" for c in write_write)
