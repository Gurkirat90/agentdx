"""fixtures/code_pipeline/checks.py — the PRD §21.6 pluggable assertion hook for Fixture 1.

AgentDX judges coordination; these functions judge the task's own semantics, exactly the
split §21.6 draws. Each is `(final_state, run) -> bool | tuple[bool, str]`, run by
`fixtures._harness.run_checks` after the graph produces its final state and before `run_end`
is emitted (see `README.md`).

**The pair below is the fixture's teaching point, not decoration.** `task_success` is the
check a team would actually ship, and it passes — `run_tests` reports green regardless of
which revision survived. `coder_contribution_present` is a stronger, retrospective check that
would only occur to someone who already suspected something was lost; it fails, and it is the
only thing in this file that catches the defect at all. AgentDX's coordination analysis (not
built yet — P12) is supposed to catch it *without* anyone having to write the second check.
"""

from __future__ import annotations

from typing import Any

from fixtures._harness import RunSummary


def task_success(final_state: dict[str, Any], run: RunSummary) -> bool | tuple[bool, str]:
    """Pass iff the module's tests report success — the check a team would actually ship.

    This is deliberately the check that the seeded defect **does not** trip: PRD §45.1's
    point is that the bug is invisible to exactly this kind of test, because `tester`
    certifies whichever draft survived the race, and that draft is syntactically and
    behaviourally fine on its own.
    """
    results = final_state.get("test_results")
    if not isinstance(results, dict):
        return False, "test_results is missing or not an object"
    return bool(results.get("passed")), f"run_tests reported passed={results.get('passed')!r}"


def coder_contribution_present(
    final_state: dict[str, Any], run: RunSummary
) -> bool | tuple[bool, str]:
    """Pass iff the final draft carries both agents' edits.

    It will not: `coder`'s write is silently discarded by the seeded lost update, so only
    `reviewer`'s marker survives. This check exists to demonstrate that catching the defect
    semantically requires *already knowing to look for it* — the entire reason a coordination
    debugger that does not need a human to guess is worth having.
    """
    draft = final_state.get("draft.module_a")
    if not isinstance(draft, str):
        return False, "draft.module_a is missing"
    has_coder = "coder: added a truthiness guard" in draft
    has_reviewer = "reviewer: guard against None explicitly" in draft
    if has_coder and has_reviewer:
        return True, "both contributions present"
    missing = "coder's" if not has_coder else "reviewer's"
    return False, f"{missing} contribution is missing from the final draft — a write was lost"


CHECKS = [task_success, coder_contribution_present]
"""The `checks.py` public surface PRD §21.6 asks for: a list of assertion functions."""

__all__ = ["CHECKS", "coder_contribution_present", "task_success"]
