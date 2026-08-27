"""The PRD §37.2 exit-code table — authoritative, stable, and defined exactly once.

Every command module imports these names rather than a bare integer literal, so the table
in `docs/cli.md` and the table here can never drift silently (AGENTS.md §4 "no magic
numbers"). PRD §22.2 restates the same seven codes for `--ci` mode and says explicitly:
"§37.2 is the authoritative table" — this module is that table, not a second copy of it.

Changing a value here is a breaking change to a public contract (CONTEXT.md §3, "CLI:
Typer; exit codes 0-7 are authoritative and stable"): scripts depend on these.
"""

from __future__ import annotations

from typing import Final

OK: Final = 0
"""Success; all assertions passed."""

ASSERTION_FAILURE: Final = 1
"""Assertion failure / regression detected."""

USAGE_ERROR: Final = 2
"""Usage, configuration or validation error."""

CACHE_MISS: Final = 3
"""LLM cache miss in replay mode (`E-CACHE-001`)."""

GUARD_ABORTED: Final = 4
"""A safety guard aborted the run (`E-GUARD-001`)."""

INTERNAL_ERROR: Final = 5
"""Internal error — an AgentDX defect, never a user error."""

DETERMINISM_FAILURE: Final = 6
"""Determinism verification failed (`E-REPLAY-001`)."""

NOT_FOUND: Final = 7
"""No scenarios or runs found at the given path."""

__all__ = [
    "ASSERTION_FAILURE",
    "CACHE_MISS",
    "DETERMINISM_FAILURE",
    "GUARD_ABORTED",
    "INTERNAL_ERROR",
    "NOT_FOUND",
    "OK",
    "USAGE_ERROR",
]
