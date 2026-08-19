"""Delay-bounded schedule exploration, independence-based reduction and dedup (FR-6, P13).

The only module that runs many runs. P1 and scope-cut #1 — the `tests/false_positives/`
k=2 harness (P12, ADR-002) is a separate, P0, test-only enumerator and is never cut with it.

Public surface: `schedule` (delay-schedule representation, signatures, turn reconstruction),
`reduce` (independence-based reduction, PRD §15.4), `dedup` (duplicate elimination, PRD §15.5),
`generate` (the BFS explorer and termination bounds, PRD §15.3/§15.5), `report` (the honesty
requirement, PRD §15.6 — `COVERAGE_STATEMENT` and the `Report` type it is bound to, I10).
"""

from agentdx.explore.dedup import SeenSchedules
from agentdx.explore.generate import (
    Budget,
    ExplorationResult,
    ExploredSchedule,
    ScheduleExecutor,
    explore,
)
from agentdx.explore.reduce import (
    Op,
    ReductionStats,
    independent,
    interesting_steps,
    op_from_event,
    reduction_stats,
)
from agentdx.explore.report import (
    COVERAGE_STATEMENT,
    FindingFirstSeen,
    Report,
    build_report,
    format_report,
    to_api_payload,
)
from agentdx.explore.schedule import (
    DelaySchedule,
    ExploreError,
    MalformedRunError,
    Turn,
    signature,
    turns_from_events,
)

__all__ = [
    "COVERAGE_STATEMENT",
    "Budget",
    "DelaySchedule",
    "ExplorationResult",
    "ExploreError",
    "ExploredSchedule",
    "FindingFirstSeen",
    "MalformedRunError",
    "Op",
    "ReductionStats",
    "Report",
    "ScheduleExecutor",
    "SeenSchedules",
    "Turn",
    "build_report",
    "explore",
    "format_report",
    "independent",
    "interesting_steps",
    "op_from_event",
    "reduction_stats",
    "signature",
    "to_api_payload",
    "turns_from_events",
]
