"""PRD §33.9 row 2: a reducer channel with 4 concurrent writers - zero conflicts (guard G3).

Synthetic, not `research_fanout` again (that fixture already gets its own row,
`test_research_fanout.py`) - PRD §33.9 lists this as an independent case, and a hand-authored
log pins the exact shape (4 writers, all concurrent, all declaring the same reducer) without
depending on a fixture that could change shape for unrelated reasons.

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


def _four_concurrent_reducer_writes() -> list[Event]:
    log = CausalLog()
    for i in range(4):
        log.add(
            state_write,
            agent_id=f"worker_{i}",
            span_id="s",
            key="findings",
            value=f"v{i}",  # distinct values - divergence (G2) must not be why this suppresses
            reducer="operator.add",
        )
    return log.events


@pytest.mark.false_positives
def test_four_concurrent_reducer_writers_yield_zero_findings() -> None:
    events = _four_concurrent_reducer_writes()
    assert detect_conflicts(events) == ()


@pytest.mark.false_positives
def test_all_six_raw_pairs_are_suppressed_by_g3_specifically() -> None:
    """Zero findings via G3 specifically, not an accident of G1/G2.

    Every write's value differs, and every pair is genuinely concurrent (independent agents,
    no causal_parents) - so G1 and G2 both let every pair through; only G3 explains the zero.
    """
    events = _four_concurrent_reducer_writes()
    conflicts = find_conflicts(events)
    write_write = [c for c in conflicts if c.key == "findings"]
    assert len(write_write) == 6  # C(4, 2)
    assert all(c.divergent for c in write_write)  # values really do differ
    assert all(c.suppressed_by == "G3" for c in write_write)
