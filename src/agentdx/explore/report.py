"""explore/report.py — reporting and THE HONESTY REQUIREMENT (PRD §15.6).

**This module is a deliverable of equal weight to the search itself (mission statement,
Design Constraint 1).** Its purpose is to make it impossible to read an exploration result as
"no races" rather than what it actually is: races were not found among a bounded number of
explored interleavings.

I10, mechanically: `COVERAGE_STATEMENT` is the exact sentence PRD §15.6 mandates, defined once
here and reused everywhere a result is rendered — the CLI-style text (`format_report`), the API
payload (`to_api_payload`, where it is a *required* field, not an optional caption a UI could
omit), and `Report.coverage_statement` itself, which every constructor of a `Report` in this
codebase goes through (`build_report`) rather than building the dataclass by hand, so the
statement is never absent by omission.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from agentdx.analysis.race import Finding, detect_conflicts
from agentdx.explore.generate import ExplorationResult

COVERAGE_STATEMENT: Final[str] = "Bounded search: absence of findings is not proof of absence."
"""PRD §15.6, verbatim, `[SOURCE]`. I10: this exact string, unmodified, in the CLI output, the
API response and the UI. Removing or paraphrasing it is a release blocker."""


@dataclass(frozen=True, slots=True)
class FindingFirstSeen:
    """One finding discovered anywhere in the exploration, and where it was first seen.

    `first_seen_at_k` is `len(delay_schedule)` of the *first-executed* schedule (BFS order —
    fewest delays first, `generate.py`'s own guarantee) whose log produced this finding, per
    PRD §15.3's rationale: "it finds the simplest reproducing schedule first."
    """

    finding: Finding
    first_seen_at_k: int
    first_seen_delay_schedule: Sequence[tuple[int, int]]


@dataclass(frozen=True, slots=True)
class Report:
    """PRD §15.6's exploration report. Every field named in the PRD's own example block.

    Guarantees: `coverage_statement` is always `COVERAGE_STATEMENT` — there is no constructor
    path that leaves it unset or lets a caller substitute different text (the field exists
    precisely so a consumer *cannot* render this result without it, PRD §15.6's own API-field
    requirement, satisfied at the type level for every in-process consumer and re-asserted by
    `tests/unit/explore/test_report.py`'s literal-string test for every serialised one).
    """

    delay_bound_k: int
    schedules_executed: int
    schedule_cap_n: int
    unique_schedules: int
    reduced_away: int
    duplicate_children: int
    new_findings: tuple[FindingFirstSeen, ...]
    capped: bool
    budget_exceeded: bool
    coverage_statement: str = COVERAGE_STATEMENT

    def __post_init__(self) -> None:
        """Refuse to exist with the wrong coverage statement.

        Raises:
            ValueError: `coverage_statement` was constructed with anything other than the
                verbatim PRD §15.6 sentence — I10 enforced at the type's own boundary, not
                merely by convention at the one call site that is supposed to set it.
        """
        if self.coverage_statement != COVERAGE_STATEMENT:
            msg = (
                "Report.coverage_statement must be exactly COVERAGE_STATEMENT (I10) — got "
                f"{self.coverage_statement!r}"
            )
            raise ValueError(msg)


def build_report(
    exploration: ExplorationResult, *, crdt_keys: frozenset[str] = frozenset()
) -> Report:
    """Run race detection over every executed schedule and assemble the PRD §15.6 `Report`.

    No new detector: this calls `analysis.race.detect_conflicts` — the same P12 detector every
    other caller uses — once per executed schedule, in the schedule's own execution order
    (BFS, fewest delays first), and records each *new* finding id the first time it appears.

    Args:
        exploration: The result of `generate.explore(...)`.
        crdt_keys: Passed straight through to `detect_conflicts` — see its own docstring.

    Returns:
        A `Report` whose `coverage_statement` is always the verbatim I10 sentence.
    """
    # A list, not a set() — this accumulator is only ever membership-tested (`in`) and
    # appended to, never iterated on its own; a bare `set()` here would trip
    # check_determinism_hygiene.py for no actual determinism benefit (AGENTS.md §4.1).
    seen_finding_ids: list[str] = []
    new_findings: list[FindingFirstSeen] = []

    for executed in exploration.results:
        findings = detect_conflicts(executed.events, crdt_keys=crdt_keys)
        for finding in findings:
            if finding.finding_id in seen_finding_ids:
                continue
            seen_finding_ids.append(finding.finding_id)
            new_findings.append(
                FindingFirstSeen(
                    finding=finding,
                    first_seen_at_k=len(executed.delay_schedule),
                    first_seen_delay_schedule=tuple(sorted(executed.delay_schedule.items())),
                )
            )

    return Report(
        delay_bound_k=exploration.delay_bound_k,
        schedules_executed=len(exploration.results),
        schedule_cap_n=exploration.schedule_cap_n,
        unique_schedules=exploration.unique_signatures,
        reduced_away=exploration.pruned_by_reduction,
        duplicate_children=exploration.duplicate_children,
        new_findings=tuple(new_findings),
        capped=exploration.capped,
        budget_exceeded=exploration.budget_exceeded,
    )


def format_report(report: Report) -> str:
    """Return PRD §15.6's CLI-style text block, field for field, coverage line last.

    Guarantees: the returned text always ends with a line built from `COVERAGE_STATEMENT` —
    not a paraphrase, not a summary, the literal sentence (I10). `tests/unit/explore/
    test_report.py` asserts this string appears byte-for-byte in the output.
    """
    lines = [
        "Bounded schedule exploration",
        f"  delay bound (k)        {report.delay_bound_k}",
        f"  schedules executed     {report.schedules_executed}  (cap {report.schedule_cap_n})",
        f"  unique schedules       {report.unique_schedules}",
        f"  reduced away           {report.reduced_away}   "
        f"(provably equivalent under independence)",
        f"  duplicate schedules    {report.duplicate_children}   (already explored, never re-run)",
        f"  new findings           {len(report.new_findings)}"
        + (
            "     ("
            + "; ".join(
                f"{f.finding.subtype} {f.finding.key}, first seen at k={f.first_seen_at_k}"
                for f in report.new_findings
            )
            + ")"
            if report.new_findings
            else ""
        ),
        f"  coverage               bounded — {report.coverage_statement}",
    ]
    if report.budget_exceeded:
        lines.append(
            "  NOTE: the time budget was exhausted before the frontier was empty — this "
            "result is a partial exploration, reported honestly rather than presented as "
            "complete."
        )
    if report.capped and not report.budget_exceeded:
        lines.append(
            f"  NOTE: the schedule cap (N={report.schedule_cap_n}) was reached before the "
            f"frontier was empty — this result is a partial exploration, reported honestly "
            f"rather than presented as complete."
        )
    return "\n".join(lines)


def to_api_payload(report: Report) -> dict[str, object]:
    """Return the PRD §15.6 API representation of a `Report`.

    Guarantees: `coverage_statement` is a top-level, required key — PRD §15.6: "downstream
    consumers cannot render the result without it." There is no schema variant of this payload
    that omits the key; a consumer that does not read it is choosing to ignore data that was
    handed to it, which is a UI defect to fix there, not a gap this function should paper over
    by making the field harder to miss than it already is.
    """
    return {
        "delay_bound_k": report.delay_bound_k,
        "schedules_executed": report.schedules_executed,
        "schedule_cap_n": report.schedule_cap_n,
        "unique_schedules": report.unique_schedules,
        "reduced_away": report.reduced_away,
        "duplicate_children": report.duplicate_children,
        "new_findings": [
            {
                "finding_id": f.finding.finding_id,
                "type": f.finding.type,
                "subtype": f.finding.subtype,
                "severity": f.finding.severity,
                "key": f.finding.key,
                "first_seen_at_k": f.first_seen_at_k,
                "first_seen_delay_schedule": [list(pair) for pair in f.first_seen_delay_schedule],
            }
            for f in report.new_findings
        ],
        "capped": report.capped,
        "budget_exceeded": report.budget_exceeded,
        "coverage_statement": report.coverage_statement,
    }


__all__ = [
    "COVERAGE_STATEMENT",
    "FindingFirstSeen",
    "Report",
    "build_report",
    "format_report",
    "to_api_payload",
]
