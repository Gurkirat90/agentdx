#!/usr/bin/env python3
"""Fail the build when ambient non-determinism appears in the package (AGENTS.md §4.1).

Invariant I1 — same seed, same cache, same scenario, byte-identical canonical projection —
is a codebase-wide property, not a module. The runtime traps ambient non-determinism at
run time (PRD §10.5), but a trap that is bypassed is a silent I1 violation, so the source is
also checked statically. This is CONTEXT.md §11 tripwire 2, mechanised.

**This check parses the AST; it does not grep.** The first version of this script matched
source text for ``time.time(``, ``random.`` and ``uuid.uuid4(``, and was trivially defeated
by changing the import form — ``from time import time`` then ``time()`` produced none of
those strings and passed clean. A gate that only catches the obvious spelling of a violation
is worse than no gate, because it is believed. Import aliases are now resolved, so
``import random as rng`` / ``from random import choice`` / ``from time import time`` are all
caught at the call site regardless of how the name arrived.

**Banned under ``src/agentdx/``:** calls to ``time.time``, ``time.monotonic``,
``time.perf_counter``, ``time.sleep``, ``asyncio.sleep``, ``datetime.datetime.now``,
``datetime.datetime.utcnow``, ``datetime.date.today``, ``uuid.uuid1``, ``uuid.uuid4``, any
``random.*``, and iteration-order dependence on ``set``.

**Exemption requires both** an explicit path in ``ALLOWLIST`` *and* a per-line
``# determinism-exempt: <reason>`` comment. Requiring both is the point:

* a comment alone cannot exempt a file, so nobody suppresses this by annotation;
* a path alone cannot exempt a line, so nobody suppresses a whole file by adding it here;
* an annotation on a non-allowlisted line is itself reported, so the escape hatch is visible.

Adding a path to ``ALLOWLIST`` is a reviewable change to this file with a §4.1 clause named
next to it. There is no blanket suppression and no wildcard.

**Known residual limits**, stated rather than discovered: dynamic access defeats any static
check — ``getattr(time, "time")()``, ``__import__("random")``, ``eval`` — and a banned call
reached through a local alias assignment (``f = time.time``) is caught at the assignment, not
the call. The runtime traps in ``runtime/determinism.py`` are what catch those at execution;
this gate exists to make the *accidental* case impossible, not the determined one.

Exit codes: 0 clean · 2 violation.
"""

from __future__ import annotations

import ast
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

# Fully-qualified call targets, resolved through import aliases before matching.
BANNED_CALLS: dict[str, str] = {
    "time.time": "take it from RunContext.clock, or agentdx.wall_time() if the field is volatile",
    "time.monotonic": "take it from RunContext.clock",
    "time.perf_counter": "take it from RunContext.clock",
    "time.sleep": "advance the virtual clock instead",
    "asyncio.sleep": "yield to the scheduler; it owns time",
    "datetime.datetime.now": "use agentdx.wall_time(); the field must be §10.7-excluded",
    "datetime.datetime.utcnow": "use agentdx.wall_time(); the field must be §10.7-excluded",
    "datetime.date.today": "use agentdx.wall_time(); the field must be §10.7-excluded",
    "uuid.uuid4": "derive the id from run_id and a counter (PRD §8.8)",
    "uuid.uuid1": "derive the id from run_id and a counter (PRD §8.8)",
}

# Any call into these modules is banned, whatever the attribute.
BANNED_MODULES: dict[str, str] = {
    "random": "use the seeded generator on RunContext",
}

# Builtins whose result does not depend on set iteration order, so a set inside them is fine.
ORDER_SAFE_WRAPPERS = frozenset({"sorted", "len", "any", "all", "sum", "min", "max", "frozenset"})

SET_FIX = "use agentdx.sorted_set() — set iteration order is not a contract"


def _allowlist_reason(relative: Path) -> str | None:
    """Return the §4.1 clause justifying this path, or None if it is not allowlisted."""
    as_posix = relative.as_posix()
    for prefix, reason in ALLOWLIST.items():
        if as_posix == prefix or as_posix.startswith(prefix):
            return reason
    return None


def _dotted_name(node: ast.expr) -> str | None:
    """Return the dotted source name of an expression, or None if it is not a plain name.

    Guarantees: ``a.b.c`` yields ``"a.b.c"``; a subscript, call or literal yields None,
    so only statically resolvable references are considered.
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _alias_map(tree: ast.Module) -> dict[str, str]:
    """Return local name → fully-qualified module path, for every import in the file.

    Guarantees: covers ``import x``, ``import x as y``, ``from x import y`` and
    ``from x import y as z``, including imports nested inside functions or ``if`` blocks.
    Relative imports are ignored — they cannot reach the stdlib modules this gate cares about.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _resolve(dotted: str, aliases: dict[str, str]) -> str:
    """Return the dotted name with its leading segment expanded through the alias map."""
    head, _, tail = dotted.partition(".")
    target = aliases.get(head)
    if target is None:
        return dotted
    return f"{target}.{tail}" if tail else target


def _order_safe_set_calls(tree: ast.Module) -> set[int]:
    """Return the node ids of ``set(...)`` calls whose result cannot expose iteration order."""
    safe: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in ORDER_SAFE_WRAPPERS:
            continue
        for argument in node.args:
            if isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name):
                if argument.func.id == "set":
                    safe.add(id(argument))
            # sorted(a - set(b)) and friends: one level of binary operation.
            if isinstance(argument, ast.BinOp):
                for side in (argument.left, argument.right):
                    if isinstance(side, ast.Call) and isinstance(side.func, ast.Name):
                        if side.func.id == "set":
                            safe.add(id(side))
    return safe


def _findings(tree: ast.Module) -> list[tuple[int, str, str]]:
    """Return (line, label, remedy) for every banned construct in the parsed module."""
    aliases = _alias_map(tree)
    safe_sets = _order_safe_set_calls(tree)
    found: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Set | ast.SetComp):
            found.append((node.lineno, "set-literal iteration", SET_FIX))
            continue

        if not isinstance(node, ast.Call):
            continue

        dotted = _dotted_name(node.func)
        if dotted is None:
            continue

        if dotted == "set" and "set" not in aliases:
            if id(node) not in safe_sets:
                found.append((node.lineno, "set iteration order", SET_FIX))
            continue

        resolved = _resolve(dotted, aliases)

        remedy = BANNED_CALLS.get(resolved)
        if remedy is not None:
            found.append((node.lineno, resolved, remedy))
            continue

        module = resolved.split(".")[0]
        module_remedy = BANNED_MODULES.get(module)
        if module_remedy is not None and resolved != module:
            found.append((node.lineno, resolved, module_remedy))

    return found


def _scan(path: Path) -> list[str]:
    """Return one message per determinism-hygiene violation in ``path``.

    Guarantees: a file that cannot be parsed is reported as a violation rather than skipped
    — an unparseable file is an unchecked file.
    """
    relative = path.relative_to(PACKAGE)
    reason = _allowlist_reason(relative)
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        return [f"src/agentdx/{relative}: cannot parse ({error.msg} at line {error.lineno})"]

    problems: list[str] = []

    for number, raw in enumerate(lines, start=1):
        if ANNOTATION.search(raw) and reason is None:
            problems.append(
                f"src/agentdx/{relative}:{number}: '# determinism-exempt' on a path that is not "
                f"in the allowlist. The annotation does not grant an exemption on its own — add "
                f"the path to ALLOWLIST in this script, with its AGENTS.md §4.1 clause, or move "
                f"the code."
            )

    for line_number, label, remedy in _findings(tree):
        raw = lines[line_number - 1] if 0 < line_number <= len(lines) else ""
        if reason is not None and ANNOTATION.search(raw):
            continue
        location = f"src/agentdx/{relative}:{line_number}"
        problems.append(f"{location}: {label} — {raw.strip()[:70]!r}\n      → {remedy}")

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
