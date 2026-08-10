#!/usr/bin/env python3
"""Fail the build when ambient non-determinism appears in the package (AGENTS.md §4.1).

Invariant I1 — same seed, same cache, same scenario, byte-identical canonical projection —
is a codebase-wide property, not a module. The runtime traps ambient non-determinism at
run time (PRD §10.5), but a trap that is bypassed is a silent I1 violation, so the source is
also checked statically. This is CONTEXT.md §11 tripwire 2 as a grep.

**Banned under ``src/agentdx/``:** ``time.time``, ``time.monotonic``, ``time.perf_counter``,
``time.sleep``, ``datetime.now``, ``datetime.utcnow``, ``random.*``, ``uuid.uuid4``,
``asyncio.sleep``, and iteration-order dependence on ``set``.

**Exemption requires both** an explicit path in ``ALLOWLIST`` *and* a per-line
``# determinism-exempt: <reason>`` comment. Requiring both is the point:

* a comment alone cannot exempt a file, so nobody suppresses this by annotation;
* a path alone cannot exempt a line, so nobody suppresses a whole file by adding it here;
* an annotation on a non-allowlisted line is itself reported, so the escape hatch is visible.

Adding a path to ``ALLOWLIST`` is a reviewable change to this file with a §4.1 clause named
next to it. There is no blanket suppression and no wildcard.

Exit codes: 0 clean · 2 violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "src" / "agentdx"
EXIT_VIOLATION = 2

# The four sanctioned exceptions of AGENTS.md §4.1 — these and no others. Each entry is a
# path prefix relative to src/agentdx/, annotated with the clause that justifies it. A line
# in one of these files is still a violation unless it also carries the annotation.
ALLOWLIST: dict[str, str] = {
    # Clause 1 — installs and removes the patches.
    "runtime/determinism.py": "§4.1(1) installs/removes the determinism patches",
    # Clause 2 — owns virtual time.
    "runtime/clock.py": "§4.1(2) owns virtual time",
    # Clause 3 — the volatile-field writers. wall_ts_ms, payload.duration_wall_ms and the
    # run_start provenance fields must read the real clock (PRD §9.2, §10.7) and are all on
    # the §10.7 canonical-projection exclusion list.
    "events/writer.py": "§4.1(3) volatile-field writer, via agentdx.wall_time()",
    # Clause 4 — code that executes outside a run context, where no run is in progress.
    "api/": "§4.1(4) long-lived server, never inside a run",
    "cli/": "§4.1(4) argument handling and progress output",
    "store/": "§4.1(4) file naming",
}

ANNOTATION = re.compile(r"#\s*determinism-exempt:\s*\S")

BANNED: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "wall clock",
        re.compile(r"\btime\.(?:time|monotonic|perf_counter)\s*\("),
        "take it from RunContext.clock, or agentdx.wall_time() if the field is volatile",
    ),
    (
        "blocking sleep",
        re.compile(r"\btime\.sleep\s*\("),
        "advance the virtual clock instead",
    ),
    (
        "event-loop sleep",
        re.compile(r"\basyncio\.sleep\s*\("),
        "yield to the scheduler; it owns time",
    ),
    (
        "wall date",
        re.compile(r"\bdatetime\.(?:now|utcnow)\s*\("),
        "use agentdx.wall_time(); the field must be on the §10.7 exclusion list",
    ),
    (
        "unseeded random",
        re.compile(r"(?<![\w.])random\.\w+\s*\("),
        "use the seeded generator on RunContext",
    ),
    (
        "random identity",
        re.compile(r"\buuid\.uuid4\s*\("),
        "derive the id from run_id and a counter (PRD §8.8)",
    ),
    (
        "set iteration order",
        re.compile(r"(?<!\w)(?<!frozen)(?<!sorted_)set\s*\("),
        "use agentdx.sorted_set() — set iteration order is not a contract",
    ),
    (
        "set-literal iteration",
        re.compile(r"\bfor\s+\w+\s+in\s*\{(?![^}]*:)"),
        "iterate a sorted sequence, not a set literal",
    ),
)

# `sorted(set(...))` and `len(set(...))` are order-independent, so a line that wraps the set
# in one of these is not an iteration-order dependence.
SET_SAFE = re.compile(r"\b(?:sorted|len|any|all|sum|min|max)\s*\(\s*(?:\w+\s*[-&|^]\s*)?set\s*\(")


def _allowlist_reason(relative: Path) -> str | None:
    """Return the §4.1 clause justifying this path, or None if it is not allowlisted."""
    as_posix = relative.as_posix()
    for prefix, reason in ALLOWLIST.items():
        if as_posix == prefix or as_posix.startswith(prefix):
            return reason
    return None


def _strip_string_literals(line: str) -> str:
    """Return the line with quoted spans blanked, so prose in a docstring is not scanned.

    Guarantees: preserves line length and column positions; does not attempt to parse
    multi-line strings — a banned call spelled inside a triple-quoted block is reported,
    which is the safe direction to be wrong in.
    """
    return re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", lambda m: m.group(0)[0] * len(m.group(0)), line)


def _scan(path: Path) -> list[str]:
    """Return one message per determinism-hygiene violation in ``path``."""
    relative = path.relative_to(PACKAGE)
    reason = _allowlist_reason(relative)
    problems: list[str] = []

    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        code = _strip_string_literals(raw)
        stripped = code.strip()
        if stripped.startswith("#"):
            continue
        annotated = bool(ANNOTATION.search(raw))

        if annotated and reason is None:
            problems.append(
                f"src/agentdx/{relative}:{number}: '# determinism-exempt' on a path that is not "
                f"in the allowlist. The annotation does not grant an exemption on its own — add "
                f"the path to ALLOWLIST in this script, with its AGENTS.md §4.1 clause, or move "
                f"the code."
            )
            continue

        for label, pattern, fix in BANNED:
            if not pattern.search(code):
                continue
            if label.startswith("set ") and SET_SAFE.search(code):
                continue
            if annotated:
                continue  # allowlisted path + per-line reason
            problems.append(
                f"src/agentdx/{relative}:{number}: {label} — {stripped[:70]!r}\n      → {fix}"
            )
    return problems


def main() -> int:
    """Scan the package for ambient non-determinism and report every violation.

    Guarantees: exits 2 on any violation; scans every ``.py`` file under src/agentdx/ with
    no directory skipped, so a new package cannot silently escape the gate.
    """
    if not PACKAGE.is_dir():
        print("check-determinism: src/agentdx/ not found", file=sys.stderr)
        return EXIT_VIOLATION

    files = sorted(PACKAGE.rglob("*.py"))
    problems: list[str] = []
    for path in files:
        problems += _scan(path)

    if problems:
        print("check-determinism: FAILED (I1, AGENTS.md §4.1, tripwire 2)", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        print(
            "\nAnything that needs a clock, an id or a random number takes it from the injected "
            "RunContext — seeded, virtual and reproducible.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    print(f"check-determinism: OK — {len(files)} file(s) scanned, no ambient non-determinism")
    return 0


if __name__ == "__main__":
    sys.exit(main())
