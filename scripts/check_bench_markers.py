#!/usr/bin/env python3
"""Enforce Rule E1: no published statistic without a reproducible measurement.

Invariant I9 says every number this project publishes traces to a committed benchmark
result. AGENTS.md §6 mechanises it as a marker convention, and this script is the grep that
makes it real. Two checks:

1. **Every number carries a marker.** A numeric literal bearing a ``%``, ``×``, ``ms``, ``s``
   or ``fps`` unit, appearing in ``README.md`` or under ``docs/``, must have a
   ``[bench:<file>]`` marker in the same sentence.
2. **Every marker resolves.** The file a marker names must exist in ``bench/results/``.

``docs/AgentDX-PRD-v2.md`` is exempt: it is the read-only spec of record (ADR-000), a
document of design targets and thresholds, not a claim about measured behaviour. Nothing
else is exempt and there is deliberately no suppression comment — an unmeasured number in
published prose is the failure mode this exists to prevent (CONTEXT.md §11 tripwire 7).

Exit codes: 0 clean · 2 violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH_RESULTS = REPO_ROOT / "bench" / "results"
EXIT_VIOLATION = 2

# Scanned surfaces. The UI and release notes are the other two surfaces named by Rule E1;
# they are added here when they exist (P15, P19).
SCANNED_FILES = (REPO_ROOT / "README.md",)
SCANNED_DIRS = (REPO_ROOT / "docs",)
SCANNED_SUFFIXES = (".md",)

# ADR-000: the spec of record is read-only and is not a published claim.
EXEMPT = frozenset({REPO_ROOT / "docs" / "AgentDX-PRD-v2.md"})

# A number followed by one of Rule E1's units. The trailing boundary stops `4 spans` from
# reading as `4 s`, and the leading boundary stops a version like 0.4.0 from matching.
UNIT_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*\s*(?:%|×|ms|fps|s)(?![\w])")
MARKER = re.compile(r"\[bench:([^\]\s]+)\]")

# `[bench:<file>]` is how the convention is *described* — a metavariable, not a filename. A
# placeholder neither resolves nor satisfies a number, so documenting the rule is possible
# and citing `<file>` as though it were evidence is not.
PLACEHOLDER = re.compile(r"^<.*>$")

# A "sentence" is a line, further split on sentence-terminating punctuation. Splitting on
# lines first is what makes table rows and list items work: each is its own claim and needs
# its own marker.
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _iter_target_files() -> list[Path]:
    """Return every file Rule E1 applies to, exemptions removed, in a stable order."""
    targets: list[Path] = [path for path in SCANNED_FILES if path.is_file()]
    for directory in SCANNED_DIRS:
        if not directory.is_dir():
            continue
        targets += [
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix in SCANNED_SUFFIXES
        ]
    return sorted({path for path in targets if path not in EXEMPT})


def _unmarked_numbers(path: Path) -> list[str]:
    """Return a message per unit-bearing number in ``path`` with no marker in its sentence."""
    problems: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for sentence in SENTENCE_SPLIT.split(line):
            if any(not PLACEHOLDER.match(m.group(1)) for m in MARKER.finditer(sentence)):
                continue
            for match in UNIT_NUMBER.finditer(sentence):
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_number}: "
                    f"published number '{match.group(0).strip()}' has no [bench:<file>] marker "
                    f"in its sentence — {sentence.strip()[:80]!r}"
                )
    return problems


def _dangling_markers(path: Path) -> list[str]:
    """Return a message per marker in ``path`` naming a file absent from bench/results/."""
    problems: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in MARKER.finditer(line):
            target = match.group(1)
            if PLACEHOLDER.match(target):
                continue
            if not (BENCH_RESULTS / target).is_file():
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_number}: "
                    f"[bench:{target}] does not resolve — bench/results/{target} is not committed"
                )
    return problems


def main() -> int:
    """Run both Rule E1 checks over every published surface and report all violations.

    Guarantees: exits 2 on any violation; a clean repository with no published numbers
    passes vacuously, which is the expected state until the first benchmark lands.
    """
    problems: list[str] = []
    files = _iter_target_files()
    for path in files:
        problems += _unmarked_numbers(path)
        problems += _dangling_markers(path)

    if problems:
        print("check-bench: FAILED (Rule E1, invariant I9)", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        print(
            "\nEither delete the number, or measure it: run the benchmark, commit its output "
            "to bench/results/, and cite it inline as [bench:<filename>] in the same sentence.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    print(f"check-bench: OK — {len(files)} published file(s) scanned, every number measured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
