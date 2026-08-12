"""fixtures/research_fanout/checks.py — the PRD §21.6 assertion hook for Fixture 3.

Both checks are expected to pass, unconditionally, on every run — this is the healthy
control. A check that fails here would mean the fixture itself is broken, not that AgentDX
found something.
"""

from __future__ import annotations

from typing import Any

from fixtures._harness import RunSummary


def task_success(final_state: dict[str, Any], run: RunSummary) -> bool | tuple[bool, str]:
    """Pass iff a report was synthesised."""
    report = final_state.get("report")
    return isinstance(report, str) and len(report) > 0, f"report={report!r}"


def all_subtopics_covered(final_state: dict[str, Any], run: RunSummary) -> bool | tuple[bool, str]:
    """Pass iff all four workers' findings made it into the merged list.

    This is the semantic mirror of `code_pipeline`'s `coder_contribution_present`, which
    fails. Here it must pass **every time**, at any completion order, because `findings`
    merges through `operator.add` rather than overwriting — nothing a worker contributes is
    ever silently dropped. See `README.md`.
    """
    report = final_state.get("report", "")
    ok = isinstance(report, str) and report.startswith("4/4 subtopics covered")
    return ok, f"report={report!r}"


CHECKS = [task_success, all_subtopics_covered]

__all__ = ["CHECKS", "all_subtopics_covered", "task_success"]
