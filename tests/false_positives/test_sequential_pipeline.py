"""PRD §33.9 row 4: sequential pipeline, no concurrency - zero conflicts.

Four agents write the same key one after another, each explicitly causally downstream of the
last (a real pipeline handoff, not an accident of seq ordering) - G1 (concurrency, the
definition itself) is what suppresses every pair here: there is no code path that reaches a
guard at all, because `causality.concurrent` is False for every pair (module docstring).

**Rewritten 2026-08-19 (OP-2 finding, `CONTEXT.md` §13) to build the log through `CausalLog`**
rather than hand-setting `vclock={}` alongside an explicit `causal_parents` tuple - see
`tests/analysis/race/_causal_log.py`'s module docstring for why that combination was never a
shape the real scheduler produces (this file's `causal_parents` shape happened to be right,
since the handoff really was a declared cause, but `vclock={}` never was).
"""

from __future__ import annotations

import pytest

from agentdx.analysis.race import detect_conflicts, find_conflicts
from agentdx.events.schema import Event
from tests.analysis.race._causal_log import CausalLog
from tests.analysis.race._events import state_write


def _four_stage_pipeline() -> list[Event]:
    log = CausalLog()
    causes: list[int] = []
    for i in range(4):
        log.add(
            state_write,
            causes=causes,
            agent_id=f"stage_{i}",
            span_id="s",
            key="pipeline_output",
            value=f"v{i}",
        )
        causes = [i]  # each stage explicitly hands off to the next
    return log.events


@pytest.mark.false_positives
def test_sequential_pipeline_yields_zero_findings() -> None:
    events = _four_stage_pipeline()
    assert detect_conflicts(events) == ()


@pytest.mark.false_positives
def test_no_raw_conflicts_are_even_constructed() -> None:
    """Stronger than "suppressed": PRD §14.7's G1 means no `Conflict` object exists at all.

    See `race.py`'s own module docstring for why a happens-before-ordered pair never reaches
    guard evaluation in the first place.
    """
    events = _four_stage_pipeline()
    assert find_conflicts(events) == ()
