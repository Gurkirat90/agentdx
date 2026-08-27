"""Output conventions shared by every command (PRD §37.3).

**The rule, stated once:** human output goes to stdout when `--json` is not set; when
`--json` *is* set, the machine-readable object is the only thing that reaches stdout and
every human-oriented line (progress, warnings, the coverage statement's human rendering)
moves to stderr instead. This is what "every command is scriptable" (§37.3) means in
practice — `agentdx run fixtures/code_pipeline --json | jq .verdict.class` must never see a
progress line mixed into its stdout.

Colour is applied only when stdout is a real TTY and `--no-color` was not passed (`FORCE_COLOR`
in the environment overrides both, matching `--ci` mode's own stated posture, PRD §22.1 item 2).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Final, TextIO

from agentdx.explore.report import COVERAGE_STATEMENT

__all__ = [
    "COVERAGE_STATEMENT",
    "Output",
]

_RESET: Final = "\x1b[0m"
_DIM: Final = "\x1b[2m"
_BOLD: Final = "\x1b[1m"
_COLORS: Final = {
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
}


@dataclass
class Output:
    """The one object every command uses to write anything.

    `json_mode` suppresses all human prose to stderr and reserves stdout for exactly one
    `emit_json` call. `quiet` suppresses progress and informational lines but never errors
    or the final result. `verbose` adds diagnostic detail human commands may print.
    """

    json_mode: bool = False
    quiet: bool = False
    verbose: bool = False
    no_color: bool = False
    # `default_factory`, not a bare `= sys.stdout` default: a dataclass field default is
    # evaluated once, at class-definition (module-import) time — binding it directly to
    # `sys.stdout` would freeze in whatever stream object existed at import, not the one
    # live when `Output(...)` is actually constructed. That breaks any caller that swaps
    # `sys.stdout` after import — `typer.testing.CliRunner.invoke`, in particular, redirects
    # it for the duration of one call, and `main.py`'s callback builds one `Output` per
    # invocation. A factory re-reads `sys.stdout`/`sys.stderr` at construction time instead.
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)

    def _color_enabled(self) -> bool:
        if os.environ.get("FORCE_COLOR"):
            return True
        if self.no_color or os.environ.get("NO_COLOR"):
            return False
        try:
            return self.stdout.isatty()
        except (AttributeError, ValueError):
            return False

    def style(self, text: str, *, color: str | None = None, bold: bool = False) -> str:
        """Wrap `text` in ANSI codes when colour is enabled; otherwise return it unchanged."""
        if not self._color_enabled():
            return text
        prefix = ""
        if bold:
            prefix += _BOLD
        if color is not None:
            prefix += _COLORS.get(color, "")
        if not prefix:
            return text
        return f"{prefix}{text}{_RESET}"

    def dim(self, text: str) -> str:
        """Return `text` dimmed, when colour is enabled."""
        if not self._color_enabled():
            return text
        return f"{_DIM}{text}{_RESET}"

    def _human_stream(self) -> TextIO:
        return self.stderr if self.json_mode else self.stdout

    def line(self, message: str = "") -> None:
        """Print one line of human-oriented output, respecting `--json`/`--quiet`."""
        if self.quiet and not self.json_mode:
            return
        print(message, file=self._human_stream())

    def info(self, message: str) -> None:
        """Print one informational line, suppressed by `--quiet` (never by `--json` alone)."""
        if self.quiet:
            return
        print(message, file=self._human_stream())

    def verbose_line(self, message: str) -> None:
        """Print one line only when `--verbose` was passed."""
        if not self.verbose:
            return
        print(message, file=self._human_stream())

    def warn(self, message: str) -> None:
        """Print a warning. Always shown — warnings are never suppressed by `--quiet`."""
        print(self.style(f"⚠ {message}", color="yellow"), file=self._human_stream())

    def error(self, message: str) -> None:
        """Print an error to stderr, unconditionally."""
        print(self.style(f"✗ {message}", color="red"), file=self.stderr)

    def progress(self, message: str) -> None:
        """Print a single-line, overwritten progress status (PRD §37.3).

        A no-op under `--json`, `--quiet`, or when stdout is not a TTY — a non-interactive
        consumer (a pipe, a CI log) should never see a carriage-return-driven line.
        """
        if self.json_mode or self.quiet:
            return
        try:
            interactive = self.stdout.isatty()
        except (AttributeError, ValueError):
            interactive = False
        if not interactive:
            return
        print(f"\r\x1b[K{message}", end="", file=self.stdout, flush=True)

    def end_progress(self) -> None:
        """Clear the current progress line, if one was printed."""
        if self.json_mode or self.quiet:
            return
        try:
            interactive = self.stdout.isatty()
        except (AttributeError, ValueError):
            interactive = False
        if interactive:
            print("\r\x1b[K", end="", file=self.stdout, flush=True)

    def emit_json(self, obj: object) -> None:
        """Write one JSON object to stdout, and only stdout (PRD §37.3 `--json` contract)."""
        json.dump(obj, self.stdout, indent=2, sort_keys=False, default=str)
        self.stdout.write("\n")
        self.stdout.flush()

    def coverage_statement(self) -> None:
        """Print the I10 bounded-exploration coverage statement, verbatim, to human output."""
        self.info(self.dim(COVERAGE_STATEMENT))
