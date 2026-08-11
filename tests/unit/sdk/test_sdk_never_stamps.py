"""`sdk/` builds drafts and never stamps them — asserted against the AST, not a comment.

PRD §9.6 puts stamping under the scheduler lock, which is P06's. CONTEXT.md §14 states the
consequence: "the SDK builds `DraftEvent`s and **cannot stamp** — `seq`, `vclock` and
`sched_step` are assigned under the scheduler lock at P06, and the type system enforces
that." The type system enforces it only as long as nothing in `sdk/` calls `Event.from_draft`
or constructs a `Stamp`, and a type checker will happily accept either.

This mirrors the fix P02's OP-3 made to the same problem in `events/writer.py`: an assertion
about *absence* has to name the construct, or a refactor slips past it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SDK_ROOT = Path(__file__).resolve().parents[3] / "src" / "agentdx" / "sdk"

FORBIDDEN_CALLS = frozenset({"Stamp", "from_draft", "Event"})
"""Constructing any of these inside `sdk/` would cross the PRD §9.6 stamping boundary."""

STAMPED_FIELDS = frozenset({"seq", "sched_step", "vclock", "causal_parents", "wall_ts_ms"})
"""Keyword names that only appear when something is being stamped."""


def _sdk_sources() -> list[Path]:
    return sorted(SDK_ROOT.rglob("*.py"))


def test_the_sdk_package_has_sources_to_check() -> None:
    # A green suite over an empty glob is the classic way this kind of test rots.
    assert len(_sdk_sources()) >= 5


@pytest.mark.parametrize("path", _sdk_sources(), ids=lambda p: p.name)
def test_no_sdk_module_constructs_a_stamped_event(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offences: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = None
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.Attribute):
            name = target.attr
        if name in FORBIDDEN_CALLS:
            offences.append(f"{path.name}:{node.lineno} calls {name}(...)")
        for keyword in node.keywords:
            if keyword.arg in STAMPED_FIELDS:
                offences.append(f"{path.name}:{node.lineno} passes {keyword.arg}=")
    assert offences == [], (
        "sdk/ must build DraftEvents only; stamping happens under the scheduler lock at P06 "
        f"(PRD §9.6). Offences: {offences}"
    )


@pytest.mark.parametrize("path", _sdk_sources(), ids=lambda p: p.name)
def test_no_sdk_module_imports_the_stamping_types(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.extend(alias.name for alias in node.names if alias.name in ("Stamp", "Event"))
    assert imported == [], f"{path.name} imports {imported}; sdk/ only needs DraftEvent"


@pytest.mark.parametrize("path", _sdk_sources(), ids=lambda p: p.name)
def test_no_sdk_module_imports_analysis_or_store(path: Path) -> None:
    # CONTEXT.md §4: sdk/ may import events and runtime. `analysis` is forbidden by
    # import-linter; `store` is simply not on the "may import" list, and this catches it
    # before the layer table and the config drift apart.
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    forbidden: list[str] = []
    for node in ast.walk(tree):
        module = None
        if isinstance(node, ast.ImportFrom):
            module = node.module
        elif isinstance(node, ast.Import):
            module = node.names[0].name
        if module and module.startswith(("agentdx.analysis", "agentdx.store")):
            forbidden.append(module)
    assert forbidden == [], f"{path.name} imports {forbidden}, outside the sdk/ layer contract"
