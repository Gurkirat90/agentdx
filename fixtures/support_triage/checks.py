"""fixtures/support_triage/checks.py — the PRD §21.6 assertion hook for Fixture 2.

Unlike `code_pipeline`, this fixture's semantic checks are expected to **pass**: PRD §23.2
point 4 is explicit that this fixture has performance defects and no correctness defects, so
its output is fine — only the redundant `vector_search` call and the fake fan-out are wrong,
and neither is something a task-level check can see. That is the point: `no_state_conflicts`
(PRD §21.7) and these two functions all pass; only AgentDX's redundancy and fan-out analysis
(not built yet — P10) is positioned to catch anything here.
"""

from __future__ import annotations

from typing import Any

from fixtures._harness import RunSummary


def task_success(final_state: dict[str, Any], run: RunSummary) -> bool | tuple[bool, str]:
    """Pass iff a response was drafted at all."""
    draft = final_state.get("response_draft")
    return isinstance(draft, str) and len(draft) > 0, f"response_draft={draft!r}"


def response_mentions_refund(
    final_state: dict[str, Any], run: RunSummary
) -> bool | tuple[bool, str]:
    """Pass iff the drafted response addresses the ticket's actual request (a refund)."""
    draft = final_state.get("response_draft", "")
    ok = isinstance(draft, str) and "refund" in draft.lower()
    return ok, f"response_draft={draft!r}"


CHECKS = [task_success, response_mentions_refund]

__all__ = ["CHECKS", "response_mentions_refund", "task_success"]
