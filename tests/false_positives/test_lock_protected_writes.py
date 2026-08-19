"""PRD §33.9 row 3: lock-protected concurrent writes - zero conflicts (guard G4).

Three writers all holding the identical `lock_id`, concurrent, with divergent values - only G4
can be suppressing this (G2 does not apply, values differ; G3 does not apply, no reducer).

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


def _three_lock_protected_writes() -> list[Event]:
    log = CausalLog()
    for i in range(3):
        log.add(
            state_write,
            agent_id=f"worker_{i}",
            span_id="s",
            key="shared_counter",
            value=f"v{i}",
            lock_id="lock_shared_counter",
        )
    return log.events


@pytest.mark.false_positives
def test_lock_protected_concurrent_writes_yield_zero_findings() -> None:
    events = _three_lock_protected_writes()
    assert detect_conflicts(events) == ()


@pytest.mark.false_positives
def test_all_raw_pairs_are_suppressed_by_g4_specifically() -> None:
    events = _three_lock_protected_writes()
    conflicts = find_conflicts(events)
    write_write = [c for c in conflicts if c.key == "shared_counter"]
    assert len(write_write) == 3  # C(3, 2)
    assert all(c.divergent for c in write_write)
    assert all(c.suppressed_by == "G4" for c in write_write)
