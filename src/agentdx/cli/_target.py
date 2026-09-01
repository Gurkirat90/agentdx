"""Resolve a `TARGET` argument (PRD §37.1) to something `agentdx run` can execute.

`TARGET` is "a fixture name, an import path (`./app.py:graph`), a scenario file, or a
directory of scenarios" (§37.1's own words). This module tells those four shapes apart and,
for the two that name executable code (fixture / import path), does the one thing neither
`scenario.validate.resolve_graph_identity` (static-only, by design) nor anything else in the
codebase does: actually imports it, so `agentdx run` has an object to hand to `agentdx.run()`.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

__all__ = [
    "GraphTarget",
    "TargetError",
    "find_repo_root",
    "is_fixture_name",
    "list_fixtures",
    "load_fixture_graph",
    "load_import_path_graph",
    "resolve_target",
]


class TargetError(Exception):
    """`TARGET` could not be resolved to something runnable.

    Carries a stable `E-TARGET-0NN` code so `agentdx run`'s usage-error branch (exit 2) can
    name what went wrong without re-deriving a message from a bare exception string.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a human-readable detail."""
        self.code = code
        super().__init__(f"[{code}] {detail}")


@dataclass(frozen=True, slots=True)
class GraphTarget:
    """A resolved, importable target: the graph object, its task, and its identity."""

    graph: object
    task: str
    graph_hash_material: str
    """Source text (or its repr, for a dynamically-instrumented object) hashed into the
    provisional `graph_hash` `run_start` records — see `cli.commands.run`'s own docstring
    for why this is "provisional" and what a real one would need."""
    is_fixture: bool
    fixture_name: str | None = None


def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk upward from `start` (default: cwd) looking for a checkout root.

    A root is a directory containing both `pyproject.toml` and a `fixtures/` directory —
    the same two-marker test `scenario.validate._repo_root` uses, duplicated here rather than
    imported because that helper is module-private and this module has no licence to reach
    into it (AGENTS.md §2's "do not refactor a previous prompt").
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "fixtures").is_dir():
            return candidate
    return None


def _ensure_repo_root_on_path(root: Path) -> None:
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)


def list_fixtures(root: Path | None = None) -> tuple[str, ...]:
    """Return every fixture name shipped under `fixtures/` (has a `graph.py`), sorted."""
    repo_root = root or find_repo_root()
    if repo_root is None:
        return ()
    fixtures_dir = repo_root / "fixtures"
    names = [
        entry.name
        for entry in sorted(fixtures_dir.iterdir())
        if entry.is_dir() and (entry / "graph.py").is_file()
    ]
    return tuple(names)


def is_fixture_name(candidate: str, *, root: Path | None = None) -> str | None:
    """Return the fixture name `candidate` refers to, or `None` if it names no fixture.

    Accepts a bare name (`code_pipeline`) or a path to the fixture directory
    (`fixtures/code_pipeline`, `./fixtures/code_pipeline/`) — the exact two forms PRD §38.1's
    own worked example and §37.1's own examples both use.

    Guarantees, each of which a previous version of this function did not hold (D-77):

    * **A path is a fixture only if it IS the fixture directory**, not if it merely shares a
      basename with one. The earlier `Path(stripped).name` reduction discarded the parent
      path entirely, so `my_scenarios/code_pipeline` — an ordinary directory of scenario
      files — resolved to the shipped `code_pipeline` fixture and ran the wrong graph with
      no warning. §37.1 lists "a directory of scenarios" and "a fixture name" as two of the
      four `TARGET` shapes; collapsing one into the other is not a reading of that sentence.
    * **A path-shaped candidate is resolved against ITS OWN checkout, not the caller's cwd.**
      `find_repo_root()` walks up from the working directory, which answers "which checkout
      am I standing in" — a different question from "which checkout does this target belong
      to". Running `agentdx run /elsewhere/agentdx/fixtures/code_pipeline` from outside any
      checkout previously found no root, declined the fixture, and reproduced D-77 exactly.

    Bare-vs-path is decided on the **raw string**, not on `Path.parts`, because `pathlib`
    normalises `./code_pipeline` to `code_pipeline` and would otherwise route an explicitly
    local path into the bare-name branch.

    Precedence, where PRD §37.1 is silent (see D-77): a bare `code_pipeline` means the
    fixture, even when a directory of that name sits in the working directory; write
    `./code_pipeline` to mean the local directory. A bare name carries no path information,
    so it can only ever be resolved relative to `root` or the cwd.
    """
    stripped = candidate.rstrip("/")
    if not stripped:
        return None
    if "/" not in stripped:
        repo_root = root or find_repo_root()
        if repo_root is None:
            return None
        if (repo_root / "fixtures" / stripped / "graph.py").is_file():
            return stripped
        return None
    resolved = Path(stripped).resolve()
    repo_root = root or find_repo_root(resolved.parent) or find_repo_root()
    if repo_root is None:
        return None
    try:
        relative = resolved.relative_to(repo_root.resolve())
    except ValueError:
        return None
    if len(relative.parts) != 2 or relative.parts[0] != "fixtures":
        return None
    name = relative.parts[1]
    if (repo_root / "fixtures" / name / "graph.py").is_file():
        return name
    return None


def load_fixture_graph(name: str, *, root: Path | None = None) -> GraphTarget:
    """Import `fixtures.<name>.graph`, build the graph, and return it with its task.

    Mirrors `tests/golden/fixtures_runner.py::run_fixture`'s own import shape (the one
    existing precedent for dynamically loading a fixture graph) — `build_graph()` and `TASK`
    are the two names every fixture's `graph.py` exposes.

    Raises:
        TargetError: `E-TARGET-001` no such fixture · `E-TARGET-002` the module does not
            expose `build_graph`/`TASK`.
    """
    repo_root = root or find_repo_root()
    if repo_root is None or not (repo_root / "fixtures" / name / "graph.py").is_file():
        msg = f"no fixture named {name!r} under fixtures/"
        raise TargetError("E-TARGET-001", msg)
    _ensure_repo_root_on_path(repo_root)
    module = importlib.import_module(f"fixtures.{name}.graph")
    build_graph = getattr(module, "build_graph", None)
    task = getattr(module, "TASK", None)
    if not callable(build_graph) or not isinstance(task, str):
        msg = f"fixtures.{name}.graph must define build_graph() and a str TASK"
        raise TargetError("E-TARGET-002", msg)
    source = (repo_root / "fixtures" / name / "graph.py").read_text(encoding="utf-8")
    return GraphTarget(
        graph=build_graph(),
        task=task,
        graph_hash_material=source,
        is_fixture=True,
        fixture_name=name,
    )


def load_import_path_graph(spec: str, *, task: str) -> GraphTarget:
    """Load `./app.py:graph` — a file path, a colon, and the attribute to instrument.

    The attribute may be a graph object to pass straight to `agentdx.run()`, or a zero-
    argument callable returning one (mirrors `load_fixture_graph`'s `build_graph()` shape).

    Raises:
        TargetError: `E-TARGET-003` malformed spec · `E-TARGET-004` file not found ·
            `E-TARGET-005` import failed · `E-TARGET-006` attribute missing.
    """
    if ":" not in spec:
        msg = f"import path target {spec!r} must be FILE.py:attribute"
        raise TargetError("E-TARGET-003", msg)
    file_part, _, attr = spec.rpartition(":")
    path = Path(file_part)
    if not path.is_file():
        msg = f"target file {path} does not exist"
        raise TargetError("E-TARGET-004", msg)
    module_name = f"_agentdx_target_{path.stem}"
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        msg = f"could not build an import spec for {path}"
        raise TargetError("E-TARGET-005", msg)
    module: ModuleType = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        msg = f"importing {path} raised {type(exc).__name__}: {exc}"
        raise TargetError("E-TARGET-005", msg) from exc
    if not hasattr(module, attr):
        msg = f"{path} has no attribute {attr!r}"
        raise TargetError("E-TARGET-006", msg)
    value = getattr(module, attr)
    graph = value() if callable(value) and not hasattr(value, "ainvoke") else value
    return GraphTarget(
        graph=graph,
        task=task,
        graph_hash_material=path.read_text(encoding="utf-8"),
        is_fixture=False,
    )


def resolve_target(target: str, *, task: str | None, root: Path | None = None) -> GraphTarget:
    """Resolve `target` (a fixture name/path, or a `FILE.py:attr` import path) to a graph.

    Scenario-file and scenario-directory targets are handled by the caller before this
    function is reached (`cli.commands.run` inspects the path's suffix/contents first,
    since a scenario file's own `target:` section is what supplies *this* function's
    `target`/`task` in that case) — this function only ever sees something meant to resolve
    directly to code.

    Raises:
        TargetError: see `load_fixture_graph`/`load_import_path_graph`.
    """
    fixture_name = is_fixture_name(target, root=root)
    if fixture_name is not None:
        return load_fixture_graph(fixture_name, root=root)
    if ":" in target:
        return load_import_path_graph(target, task=task or f"Run {target}")
    msg = (
        f"target {target!r} is not a known fixture, an existing scenario file/directory, "
        f"or a FILE.py:attribute import path"
    )
    raise TargetError("E-TARGET-007", msg)
