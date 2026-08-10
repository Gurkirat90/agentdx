#!/usr/bin/env python3
"""Enforce that CONTEXT.md §8 and §9 are append-only, bounded and internally consistent.

AGENTS.md §10 makes the decision log and the deviations log append-only. This script is
what turns that from a hope into a check. It asserts three things:

1. **Append-only.** Every table row present in §8 and §9 at the diff base is still present,
   byte-for-byte, in the working tree, in the same order. Additions pass; deletions,
   edits and reorderings fail. Whitespace counts — a whitespace-only change to a decision
   row is exactly the kind of quiet tidying the rule exists to prevent.
2. **Bounded.** CONTEXT.md is at most 500 lines (CONTEXT.md §0.1). A ledger nobody finishes
   reading is a ledger nobody reads.
3. **Referentially sound.** Every ``ADR-NNN`` mentioned anywhere in the file has a row in §8.

Exit codes: 0 clean · 2 violation (or the file cannot be read).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER = REPO_ROOT / "CONTEXT.md"
MAX_LINES = 500

# TODO(remote): AGENTS.md §10 specifies `origin/main` as the diff base. There is no remote
# yet, so the base is HEAD — which checks the working tree against the last commit. Switch
# DIFF_BASE to "origin/main" the moment a remote exists, otherwise a PR can delete a row in
# one commit and this check will happily compare against that commit.
DIFF_BASE = "HEAD"

# The two append-only sections, by their CONTEXT.md heading prefix.
GUARDED_SECTIONS = ("## 8.", "## 9.")

ADR_PATTERN = re.compile(r"\bADR-(\d{3})\b")
EXIT_VIOLATION = 2


def _read_at_base(path: str) -> str | None:
    """Return the contents of ``path`` at the diff base, or None if it does not exist there.

    Guarantees: never raises on a missing file or an unborn branch; those are reported as
    None so a first commit is not treated as a deletion of everything.
    """
    result = subprocess.run(
        ["git", "show", f"{DIFF_BASE}:{path}"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _section_rows(markdown: str, heading_prefix: str) -> list[str]:
    """Return the table body rows of the section whose heading starts with ``heading_prefix``.

    A body row is any line beginning with a pipe that is not the header row or the
    ``|---|`` separator. Returns an empty list when the section is absent, which the
    caller distinguishes from "section present but empty".
    """
    rows: list[str] = []
    in_section = False
    header_seen = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            if line.startswith(heading_prefix):
                in_section = True
                header_seen = False
                continue
            if in_section:
                break
            continue
        if not in_section:
            continue
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if not header_seen:
            header_seen = True  # the column header row
            continue
        if set(stripped) <= set("|-: "):
            continue  # the |---|---| separator
        rows.append(line)
    return rows


def _check_append_only(base_text: str, head_text: str) -> list[str]:
    """Return one message per append-only violation across the guarded sections.

    Guarantees: reports the first missing or altered row per section with its position,
    rather than a diff the reader has to interpret.
    """
    problems: list[str] = []
    for prefix in GUARDED_SECTIONS:
        base_rows = _section_rows(base_text, prefix)
        head_rows = _section_rows(head_text, prefix)
        cursor = 0
        for base_row in base_rows:
            try:
                cursor = head_rows.index(base_row, cursor) + 1
            except ValueError:
                still_present = base_row in head_rows
                reason = (
                    "present but reordered — append-only means append at the end"
                    if still_present
                    else "absent or altered (whitespace counts)"
                )
                problems.append(
                    f"CONTEXT.md {prefix.strip()} row from {DIFF_BASE} is {reason}:\n"
                    f"    {base_row.strip()}"
                )
                break
    return problems


def _check_length(head_text: str) -> list[str]:
    """Return a message if CONTEXT.md exceeds its line cap."""
    count = len(head_text.splitlines())
    if count > MAX_LINES:
        return [
            f"CONTEXT.md is {count} lines; the cap is {MAX_LINES} (CONTEXT.md §0.1). "
            f"Roll the oldest §13 session rows into docs/journal/ rather than raising the cap."
        ]
    return []


def _check_adr_references(head_text: str) -> list[str]:
    """Return a message per ADR-NNN referenced in the file that has no row in §8."""
    defined: set[str] = set()
    for row in _section_rows(head_text, "## 8."):
        match = ADR_PATTERN.search(row)
        if match is not None:
            defined.add(match.group(1))
    referenced = set(ADR_PATTERN.findall(head_text))
    dangling = sorted(referenced - defined)
    return [
        f"ADR-{number} is referenced in CONTEXT.md but has no row in §8 (decision log)."
        for number in dangling
    ]


def main() -> int:
    """Run every ledger-integrity check and report all violations at once.

    Guarantees: exits 2 on any violation and prints every problem found, so one run
    surfaces the whole list instead of one item per iteration.
    """
    if not LEDGER.is_file():
        print("check-ledger: CONTEXT.md not found", file=sys.stderr)
        return EXIT_VIOLATION

    head_text = LEDGER.read_text(encoding="utf-8")
    problems: list[str] = []

    base_text = _read_at_base("CONTEXT.md")
    if base_text is None:
        print(
            f"check-ledger: CONTEXT.md does not exist at {DIFF_BASE}; "
            f"skipping the append-only comparison (first commit)."
        )
    else:
        problems += _check_append_only(base_text, head_text)

    problems += _check_length(head_text)
    problems += _check_adr_references(head_text)

    if problems:
        print("check-ledger: FAILED", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        print(
            "\nTo reverse a decision, append a higher-numbered ADR that names the one it "
            "supersedes. To correct a deviation, append a corrected row and mark the original "
            "'superseded by D-nn'. Never edit in place (AGENTS.md §10).",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    print("check-ledger: OK — §8 and §9 append-only, length and ADR references all clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
