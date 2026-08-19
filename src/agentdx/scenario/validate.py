"""Semantic validation of a parsed scenario document (PRD §21.3), before any execution.

Every failure is a `ScenarioError`: a stable `E-SCEN-NNN` code, the exact source line
(`loader.SourceMap` makes this possible — see that module's docstring), and a suggested fix.
"Invalid scenario" is a failure of this module (P08 prompt mission statement) — a validator
that says only *that* something is wrong, not *where* or *what to do about it*, has not done
its job.

**Graph-identity resolution (E-SCEN-005) is static, not an import.** Checking that a fault's
`agent: reviewer` genuinely names a node in `fixtures/code_pipeline/graph.py` needs *some*
knowledge of that graph's shape, but this module must not execute it to get it: `scenario/`
imports nothing else in the package (its own `__init__.py` docstring, upheld here), a fixture
importing `agentdx` pulls in the full SDK/runtime/langgraph chain, and — more fundamentally —
validation is defined to happen "before any execution" (PRD §21.3's own words). The chosen
mechanism is an `ast`-based static scan of the target's `graph.py` source: `add_node("x", ...)`
calls become agent names, `add_edge("a", "b")` calls become edges, `@agentdx.tool("y")`
decorators become tool names. Nothing is executed. When identity cannot be resolved this way
(a user graph built through some other pattern, or a bare dotted-module `target.graph` with no
`.py` file to read) `E-SCEN-005` is silently *not checked* for that scenario rather than either
blocking validation or guessing — declared here and in `docs/scenario-reference.md`, not a
silent gap (AGENTS.md §8).

**Loading `success_check.ref` (E-SCEN-009) *is* an import, deliberately** — PRD §21.6 requires
it ("`ref` not importable, at load time"), and unlike graph identity there is no static
alternative that proves a function exists and is callable. This is the same trust boundary
`assertions.py` documents for evaluating the loaded function: fixture-local Python is trusted
(§13.3's fixture sandbox), but see Design Constraint 5 — never resolved from a path reached
through an imported bundle.
"""

from __future__ import annotations

import ast
import fnmatch
import importlib
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agentdx.scenario.loader import (
    ParsedScenario,
    ScenarioPath,
    infer_fixture_from_task_path,
    non_fixture_task_dir,
)
from agentdx.scenario.schema import (
    BASELINE_KEYS,
    BLAST_RADIUS_KEYS,
    BUILT_IN_ASSERTION_NAMES,
    CRITICAL_PATH_SHARE_KEYS,
    DEFAULT_CHAOS_OPT_IN,
    DEFAULT_GUARDS,
    DEFAULT_SUCCESS_CHECK_TYPE,
    EXPLORATION_KEYS,
    FAULT_CATALOGUE,
    FAULT_COMMON_KEYS,
    GUARD_KEYS,
    HYPOTHESIS_KEYS,
    MAX_FINDINGS_KEYS,
    SCHEMA_VERSION,
    SUCCESS_CHECK_KEYS,
    TARGET_FIELD,
    TARGET_KEYS,
    TOP_LEVEL_KEYS,
    TRIGGER_FIELD,
    TRIGGER_FIELD_CONSTRAINTS,
    Mode,
    ParamConstraint,
    Severity,
    SuccessCheckType,
    TargetKind,
    parse_comparison,
)

_DOCS: Final = "docs/scenario-reference.md"
REQUIRED_TOP_LEVEL: Final = ("scenario", "task")

_BLAST_RADIUS_FIELD: Final[dict[TargetKind, str]] = {
    TargetKind.AGENT: "agents",
    TargetKind.TOOL: "tools",
    TargetKind.EDGE: "edges",
    TargetKind.STATE_KEY: "state_keys",
    TargetKind.PROVIDER: "providers",
}

# `E-SCEN-005` ("fault target not present in the graph") target kinds `GraphIdentity` can
# actually answer for. OP-3 repair (2026-08-16): EDGE was the gap — `graph_identity.edges` was
# already collected and used for `critical_path_share` but never checked here, even though 2
# of the 4 P0 fault types target an edge. STATE_KEY and PROVIDER stay excluded: state keys are
# not part of a graph's static shape (`GraphIdentity`'s own docstring: a static catalogue of
# them would have a real false-negative rate) and providers are not graph elements at all.
_E_SCEN_005_KINDS: Final = (TargetKind.AGENT, TargetKind.TOOL, TargetKind.EDGE)


@dataclass(frozen=True, slots=True)
class ScenarioError:
    """One validation failure: a stable code, an exact location, and a suggested fix.

    Guarantees: `line` is 1-indexed and, whenever the offending value or key is physically
    present in the document, exact — not "somewhere in this file". `suggestion` is always a
    concrete, actionable edit, never "check the docs".
    """

    code: str
    path: str
    message: str
    suggestion: str
    file: str | None = None
    line: int | None = None

    @property
    def docs_url(self) -> str:
        """Return the anchor in the scenario-reference contract that explains this code."""
        return f"{_DOCS}#{self.code.lower()}"

    def __str__(self) -> str:
        """Render `file:line [CODE] (path): message` plus the suggestion, on two lines."""
        where = (
            f"{self.file or '<scenario>'}:{self.line}"
            if self.line is not None
            else (self.file or "<scenario>")
        )
        return (
            f"{where} [{self.code}] ({self.path}): {self.message}\n  suggestion: {self.suggestion}"
        )


class ScenarioValidationError(Exception):
    """Raised when a scenario document fails one or more PRD §21.3 validation rules.

    Guarantees: `errors` is non-empty, and sorted by source line so the first entry is the
    earliest problem in the file — mirroring `events.validators.EventValidationError`'s
    ordering guarantee (a compiler-error-list convention, not a P08 invention).
    """

    def __init__(self, errors: Sequence[ScenarioError]) -> None:
        """Build the exception from one or more collected errors."""
        self.errors: tuple[ScenarioError, ...] = tuple(errors)
        super().__init__("\n".join(str(e) for e in self.errors))


def _path_str(path: ScenarioPath) -> str:
    """Render a `ScenarioPath` as `blast_radius.agents[0]`-style text for messages."""
    if not path:
        return "<root>"
    out = str(path[0])
    for part in path[1:]:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def _suggest_close(key: str, known: frozenset[str]) -> str:
    """Return a "did you mean" suggestion for an unknown key, or a generic one."""
    import difflib

    matches = difflib.get_close_matches(key, sorted(known), n=1)
    if matches:
        return f"did you mean '{matches[0]}'? Known keys: {', '.join(sorted(known))}"
    return f"remove it, or check for a typo. Known keys: {', '.join(sorted(known))}"


class _Errors:
    """Accumulates `ScenarioError`s against one `ParsedScenario`, resolving lines/files itself."""

    def __init__(self, parsed: ParsedScenario) -> None:
        """Bind the accumulator to the document whose `SourceMap` locates every error."""
        self._parsed = parsed
        self.items: list[ScenarioError] = []

    def add(
        self, code: str, path: ScenarioPath, message: str, suggestion: str, *, prefer: str = "key"
    ) -> None:
        """Record one error, resolving its line and origin file from `path`."""
        line = self._parsed.source_map.line_for(path, prefer=prefer)
        file = str(self._parsed.source_map.file_for(path, default=self._parsed.path))
        self.items.append(
            ScenarioError(
                code=code,
                path=_path_str(path),
                message=message,
                suggestion=suggestion,
                file=file,
                line=line,
            )
        )

    def check_unknown_keys(
        self, mapping: object, path: ScenarioPath, known: frozenset[str]
    ) -> None:
        """Report `E-SCEN-002` for every key of `mapping` not in `known`."""
        if not isinstance(mapping, dict):
            return
        for key in mapping:
            if not isinstance(key, str) or key not in known:
                self.add(
                    "E-SCEN-002",
                    (*path, key),
                    f"unknown key '{key}' under '{_path_str(path) or '<root>'}'",
                    _suggest_close(str(key), known),
                    prefer="key",
                )


# ---------------------------------------------------------------------------------------
# Static graph-identity resolution (E-SCEN-005) — ast-based, never imports/executes
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphIdentity:
    """The statically-discoverable shape of a graph: its agents, tools and edges.

    Guarantees: never constructed by importing or executing the graph it describes — see the
    module docstring. `state_keys` is deliberately absent: `agentdx.state().write(...)` calls
    are free-form enough (conditional keys, f-strings) that a static catalogue of them would
    have a real false-negative rate, and a validator that sometimes wrongly rejects a correct
    scenario is worse than one that does not check a lower-value class of typo at all —
    `docs/scenario-reference.md` states this limitation explicitly.
    """

    agents: frozenset[str]
    tools: frozenset[str]
    edges: frozenset[str]
    source: Path


class _GraphVisitor(ast.NodeVisitor):
    """Walks a `graph.py`-shaped AST collecting `add_node`/`add_edge`/`@tool(...)` literals."""

    def __init__(self) -> None:
        """Start with empty collections; `visit` populates them.

        Lists, not sets: membership here is never tested and the only reader (`visit`'s
        caller, below) wraps the result in `frozenset(...)` once traversal is complete, so a
        bare `set` would buy nothing but would trip `scripts/check_determinism_hygiene.py`'s
        set-iteration-order gate for no reason — a list preserves the (already-deterministic,
        single-pass AST-walk) insertion order and needs no such exemption.
        """
        self.agents: list[str] = []
        self.tools: list[str] = []
        self.edges: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        """Collect `<builder>.add_node("x", ...)` and `<builder>.add_edge("a", "b")` literals."""
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "add_node" and node.args:
                name = self._str_const(node.args[0])
                if name is not None:
                    self.agents.append(name)
            elif node.func.attr == "add_edge" and len(node.args) >= 2:
                a = self._str_const(node.args[0])
                b = self._str_const(node.args[1])
                if a is not None and b is not None:
                    self.edges.append(f"{a}->{b}")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Collect `@agentdx.tool("name")`-shaped decorators on a sync function."""
        self._collect_tool_decorators(node.decorator_list)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Collect `@agentdx.tool("name")`-shaped decorators on an async function."""
        self._collect_tool_decorators(node.decorator_list)
        self.generic_visit(node)

    def _collect_tool_decorators(self, decorators: list[ast.expr]) -> None:
        for dec in decorators:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "tool"
                and dec.args
            ):
                name = self._str_const(dec.args[0])
                if name is not None:
                    self.tools.append(name)

    @staticmethod
    def _str_const(node: ast.expr) -> str | None:
        return (
            node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None
        )


def _extract_graph_identity(source: str, path: Path) -> GraphIdentity:
    """Parse `source` (never executed) into a `GraphIdentity`.

    Raises:
        SyntaxError: `source` is not valid Python. Callers treat this as "cannot resolve",
            not as a scenario error — a target's Python file being unparsable is that file's
            problem, out of scope for a scenario validator to diagnose.
    """
    visitor = _GraphVisitor()
    visitor.visit(ast.parse(source))
    return GraphIdentity(
        agents=frozenset(visitor.agents),
        tools=frozenset(visitor.tools),
        edges=frozenset(visitor.edges),
        source=path,
    )


def _repo_root() -> Path | None:
    """Return the directory containing `agentdx.toml`, walking up from this file.

    `None` when this module is running somewhere `agentdx.toml` is not a filesystem sibling
    (e.g. installed as a wheel with no source checkout nearby) — graph-identity resolution
    for shipped fixtures degrades gracefully to "unresolved" in that case, same as any other
    unresolvable target.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "agentdx.toml").is_file():
            return parent
    return None


def resolve_graph_identity(
    target: dict[str, object] | None, *, scenario_file: Path
) -> GraphIdentity | None:
    """Best-effort static resolution of a target's `GraphIdentity`. `None` if it cannot be done."""
    if not target:
        return None
    graph_file: Path | None = None
    fixture_value = target.get("fixture")
    graph_value = target.get("graph")
    if isinstance(fixture_value, str):
        root = _repo_root()
        if root is not None:
            candidate = root / "fixtures" / fixture_value / "graph.py"
            if candidate.is_file():
                graph_file = candidate
    elif isinstance(graph_value, str):
        file_part = graph_value.rsplit(":", 1)[0] if ":" in graph_value else graph_value
        if file_part.endswith(".py"):
            candidate = Path(file_part)
            candidate = candidate if candidate.is_absolute() else (scenario_file.parent / candidate)
            if candidate.is_file():
                graph_file = candidate
    if graph_file is None:
        return None
    try:
        source = graph_file.read_text(encoding="utf-8")
        return _extract_graph_identity(source, graph_file)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------------------
# Guard ceilings (E-SCEN-008) — read from agentdx.toml `[scenario]`, never hardcoded
# ---------------------------------------------------------------------------------------


def load_guard_ceilings() -> dict[str, int]:
    """Return the hard ceiling for each guard key, from `agentdx.toml`'s `[scenario]` section.

    Falls back to 10x the PRD §13.6 default per key when `agentdx.toml` cannot be found or
    has no `[scenario]` ceilings — a declared, conservative default (`schema.py`'s
    `DEFAULT_GUARDS` docstring), not a silent one. AGENTS.md §4 forbids an inline magic
    number for a threshold; reading it from `agentdx.toml` at call time (rather than caching
    a module-level constant computed at import time) keeps `agentdx.toml` the single source
    of truth even across a process that reloads it.
    """
    root = _repo_root()
    if root is not None:
        toml_path = root / "agentdx.toml"
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        section = data.get("scenario", {})
        if isinstance(section, dict):
            ceilings = {
                key[len("guard_ceiling_") :]: value
                for key, value in section.items()
                if key.startswith("guard_ceiling_")
                and isinstance(value, int)
                and not isinstance(value, bool)
            }
            if ceilings:
                return ceilings
    return {key: value * 10 for key, value in DEFAULT_GUARDS.items()}


# ---------------------------------------------------------------------------------------
# Per-section checks
# ---------------------------------------------------------------------------------------


def _check_version(data: dict[str, object], err: _Errors) -> None:
    if "version" not in data:
        return  # CONTEXT.md ruling C-11: absent -> defaults to SCHEMA_VERSION, not an error
    version = data["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        err.add(
            "E-SCEN-001",
            ("version",),
            f"unsupported schema version {version!r}; "
            f"this build supports version {SCHEMA_VERSION} only",
            f"set `version: {SCHEMA_VERSION}`, or omit the key entirely",
        )


def _check_required(data: dict[str, object], err: _Errors) -> None:
    for key in REQUIRED_TOP_LEVEL:
        if key not in data:
            err.add(
                "E-SCEN-010",
                (key,),
                f"missing required key '{key}'",
                f"add `{key}: <value>` at the top level",
            )
            continue
        value = data[key]
        if key == "scenario" and (not isinstance(value, str) or not value.strip()):
            err.add(
                "E-SCEN-011",
                (key,),
                "`scenario` must be a non-empty string slug",
                "e.g. `scenario: reviewer_crash_midflight`",
            )
        if key == "task" and not isinstance(data[key], str):
            err.add(
                "E-SCEN-011",
                (key,),
                "`task` must be a string (a path or an inline task description)",
                "e.g. `task: fixtures/code_pipeline/tasks/refactor_module.md`",
            )


def _check_target(data: dict[str, object], err: _Errors) -> dict[str, object] | None:
    target = data.get("target")
    if target is None:
        task = data.get("task")
        if isinstance(task, str):
            inferred = infer_fixture_from_task_path(task)
            if inferred is not None:
                return {"fixture": inferred}
            shared_dir = non_fixture_task_dir(task)
            if shared_dir is not None:
                err.add(
                    "E-SCEN-003",
                    ("target",),
                    f"no `target` given, and `task`'s path is under `fixtures/{shared_dir}/`, "
                    "a shared directory (not itself a fixture) — a fixture cannot be inferred "
                    "from it (C-13, CONTEXT.md §10)",
                    "add `target: {fixture: <name>}` naming the fixture this task actually "
                    'belongs to, or `target: {graph: "./app.py:graph"}`',
                )
                return None
        err.add(
            "E-SCEN-003",
            ("target",),
            "no `target` given, and `task`'s path does not look like `fixtures/<name>/...` "
            "so a fixture could not be inferred (PRD §21.2)",
            'add `target: {fixture: <name>}` or `target: {graph: "./app.py:graph"}`',
        )
        return None
    if not isinstance(target, dict):
        err.add(
            "E-SCEN-011",
            ("target",),
            "`target` must be a mapping",
            "e.g. `target: {fixture: code_pipeline}`",
        )
        return None
    err.check_unknown_keys(target, ("target",), TARGET_KEYS)
    has_fixture = isinstance(target.get("fixture"), str) and target.get("fixture")
    has_graph = isinstance(target.get("graph"), str) and target.get("graph")
    if bool(has_fixture) == bool(has_graph):
        both_or_neither = (
            "both `fixture` and `graph` are set"
            if has_fixture
            else "neither `fixture` nor `graph` is set"
        )
        err.add(
            "E-SCEN-003",
            ("target",),
            f"`target` must set exactly one of `fixture`/`graph` ({both_or_neither})",
            "keep only one of `fixture:` / `graph:`",
        )
        return None
    return target


def _check_target_fixture_exists(target: dict[str, object] | None, err: _Errors) -> None:
    """`E-SCEN-012`: a `target.fixture` name must correspond to a real, shipped fixture.

    OP-3 repair (2026-08-16, second independent OP-2 finding #3, ruled **C-15**): PRD §21.3's
    validation-rules table does not enumerate this check, but the same section's own closing
    rationale — "failing after a 40-second run because of a typo is unacceptable in CI" — does
    not hold for `target.fixture` without it. Before this, neither an inferred nor an
    explicit `target.fixture` naming a nonexistent fixture (a typo, or wholly fictional) was
    ever caught at validation time.

    Deliberately scoped to `fixture` only, never `graph`: a `target.graph` reference is
    user-owned and resolved best-effort elsewhere (`resolve_graph_identity` — `None` there
    covers both "not on this filesystem" and "not statically resolvable", and either is a
    legitimate outcome for a real user graph, not an error). A `target.fixture`, by contrast,
    must name one of this project's own shipped fixtures — a closed, enumerable set — so
    "not found" is unambiguous. Skips silently when the repo root cannot be resolved (e.g.
    installed as a wheel with no source checkout nearby), matching every other `_repo_root()`
    caller's graceful-degradation philosophy in this module.
    """
    if not target:
        return
    fixture_value = target.get("fixture")
    if not isinstance(fixture_value, str) or not fixture_value:
        return
    root = _repo_root()
    if root is None:
        return
    if not (root / "fixtures" / fixture_value / "graph.py").is_file():
        err.add(
            "E-SCEN-012",
            ("target", "fixture"),
            f"`target.fixture: {fixture_value!r}` does not name a real fixture "
            f"(no `fixtures/{fixture_value}/graph.py` on disk)",
            "check for a typo, or add the fixture under `fixtures/` if it's meant to be new",
        )


def _check_hypothesis(data: dict[str, object], err: _Errors) -> None:
    hypothesis = data.get("hypothesis", {})
    if not isinstance(hypothesis, dict):
        err.add(
            "E-SCEN-011",
            ("hypothesis",),
            "`hypothesis` must be a mapping",
            "e.g. `hypothesis: {task_success: '>= 0.9'}`",
        )
        return
    err.check_unknown_keys(hypothesis, ("hypothesis",), HYPOTHESIS_KEYS)
    for key in HYPOTHESIS_KEYS & hypothesis.keys():
        value = hypothesis[key]
        if not isinstance(value, str) or parse_comparison(value) is None:
            err.add(
                "E-SCEN-011",
                ("hypothesis", key),
                f"hypothesis.{key} must be a comparison string like '>= 0.9', got {value!r}",
                "use one of the operators >=, <=, ==, >, < followed by a number",
            )


def _check_blast_radius(data: dict[str, object], err: _Errors) -> dict[str, list[object]]:
    blast_radius = data.get("blast_radius", {})
    if not isinstance(blast_radius, dict):
        err.add(
            "E-SCEN-011",
            ("blast_radius",),
            "`blast_radius` must be a mapping",
            "e.g. `blast_radius: {agents: [reviewer]}`",
        )
        return {}
    err.check_unknown_keys(blast_radius, ("blast_radius",), BLAST_RADIUS_KEYS)
    for key in BLAST_RADIUS_KEYS & blast_radius.keys():
        value = blast_radius[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            err.add(
                "E-SCEN-011",
                ("blast_radius", key),
                f"blast_radius.{key} must be a list of strings",
                f"e.g. `{key}: [reviewer]`",
            )
    return {k: v for k, v in blast_radius.items() if isinstance(v, list)}


def _check_param_constraint(
    value: object,
    constraint: ParamConstraint,
    fpath: ScenarioPath,
    field_name: str,
    err: _Errors,
) -> None:
    """Report `E-SCEN-011` if `value` does not satisfy `constraint`.

    OP-3 repair (2026-08-16, following the P08 OP-2 FAIL): the check `_check_faults` was
    missing entirely — a fault or trigger parameter's *name* was validated, never its value.
    Checks type first (`bool` before `int`, since `isinstance(True, int)` is `True` in
    Python and a `py_type=int` constraint must reject a bare boolean), then bounds/choices.
    """
    path = (*fpath, field_name)
    if constraint.py_type is bool:
        if not isinstance(value, bool):
            err.add(
                "E-SCEN-011",
                path,
                f"`{field_name}` must be `true` or `false` (found {value!r})",
                f"e.g. `{field_name}: true`",
            )
        return
    if constraint.py_type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            err.add(
                "E-SCEN-011",
                path,
                f"`{field_name}` must be an integer (found {value!r})",
                f"e.g. `{field_name}: 0`",
            )
            return
        if constraint.minimum is not None and value < constraint.minimum:
            err.add(
                "E-SCEN-011",
                path,
                f"`{field_name}` must be >= {constraint.minimum} (found {value})",
                f"raise {field_name} to at least {constraint.minimum}",
            )
            return
        if constraint.maximum is not None and value > constraint.maximum:
            err.add(
                "E-SCEN-011",
                path,
                f"`{field_name}` must be <= {constraint.maximum} (found {value}, "
                "PRD §12.2 safety bound)",
                f"lower {field_name} to at most {constraint.maximum}",
            )
        return
    if constraint.py_type is str:
        if not isinstance(value, str) or not value:
            err.add(
                "E-SCEN-011",
                path,
                f"`{field_name}` must be a non-empty string (found {value!r})",
                f"e.g. `{field_name}: <value>`",
            )
            return
        if constraint.choices is not None and value not in constraint.choices:
            err.add(
                "E-SCEN-011",
                path,
                f"`{field_name}` must be one of {sorted(constraint.choices)} (found {value!r})",
                _suggest_close(value, constraint.choices),
            )


def _check_faults(
    data: dict[str, object],
    target: dict[str, object] | None,
    blast_radius: dict[str, list[object]],
    graph_identity: GraphIdentity | None,
    err: _Errors,
) -> bool:
    """Validate `faults:`. Returns whether any well-formed fault entry was found."""
    faults = data.get("faults", [])
    if not isinstance(faults, list):
        err.add(
            "E-SCEN-011",
            ("faults",),
            "`faults` must be a list",
            "e.g. `faults: [{type: agent_crash, agent: reviewer, at_virtual_ts: 2400}]`",
        )
        return False

    is_user_graph = target is not None and isinstance(target.get("graph"), str)
    chaos_opt_in = bool(data.get("chaos_opt_in", DEFAULT_CHAOS_OPT_IN))
    blast_radius_nonempty = any(blast_radius.get(k) for k in BLAST_RADIUS_KEYS)

    if faults and is_user_graph and (not chaos_opt_in or not blast_radius_nonempty):
        missing = []
        if not chaos_opt_in:
            missing.append("`chaos_opt_in: true`")
        if not blast_radius_nonempty:
            missing.append("a non-empty `blast_radius`")
        err.add(
            "E-SCEN-004",
            ("faults",),
            "faults declared against a user graph (`target.graph`) require both chaos_opt_in "
            f"and a non-empty blast_radius; missing: {' and '.join(missing)} "
            "(PRD §13.3, invariant I12)",
            f"add {' and '.join(missing)} to the scenario file",
        )

    any_well_formed = False
    for i, fault in enumerate(faults):
        fpath: ScenarioPath = ("faults", i)
        if not isinstance(fault, dict):
            err.add(
                "E-SCEN-011",
                fpath,
                "each `faults` entry must be a mapping",
                "e.g. `{type: agent_crash, agent: reviewer, at_virtual_ts: 2400}`",
            )
            continue
        fault_type = fault.get("type")
        if fault_type not in FAULT_CATALOGUE:
            err.add(
                "E-SCEN-011",
                (*fpath, "type"),
                f"unknown fault type {fault_type!r}",
                f"one of: {', '.join(sorted(FAULT_CATALOGUE))}",
            )
            continue
        spec = FAULT_CATALOGUE[fault_type]
        err.check_unknown_keys(fault, fpath, FAULT_COMMON_KEYS | spec.params)

        target_fields = [TARGET_FIELD[k] for k in spec.target_kinds]
        present_target_fields = [f for f in target_fields if f in fault]
        if len(present_target_fields) != 1:
            err.add(
                "E-SCEN-011",
                fpath,
                f"fault type {fault_type!r} needs exactly one of: {', '.join(target_fields)} "
                f"(found {len(present_target_fields)})",
                f"add exactly one, e.g. `{target_fields[0]}: <name>`",
            )
            continue
        target_field = present_target_fields[0]
        target_kind = next(k for k in spec.target_kinds if TARGET_FIELD[k] == target_field)
        target_value = fault[target_field]

        trigger_fields = [TRIGGER_FIELD[k] for k in spec.trigger_kinds]
        present_trigger_fields = [f for f in trigger_fields if f in fault]
        if len(present_trigger_fields) != 1:
            err.add(
                "E-SCEN-011",
                fpath,
                f"fault type {fault_type!r} needs exactly one trigger: {', '.join(trigger_fields)} "
                f"(found {len(present_trigger_fields)})",
                f"add exactly one, e.g. `{trigger_fields[0]}: 2400`",
            )
        else:
            trigger_field = present_trigger_fields[0]
            trigger_kind = next(k for k in spec.trigger_kinds if TRIGGER_FIELD[k] == trigger_field)
            trigger_constraint = TRIGGER_FIELD_CONSTRAINTS.get(trigger_kind)
            if trigger_constraint is not None:
                _check_param_constraint(
                    fault[trigger_field], trigger_constraint, fpath, trigger_field, err
                )

        # OP-3 repair (2026-08-16): fault-specific parameter *values*, not just their names
        # (`err.check_unknown_keys` above only checks names) — PRD §12.2's Safety-row bounds
        # and implied types (`window <= 16`, `copies <= 5`, `recoverable: bool`, ...).
        for param_name, param_constraint in spec.param_constraints.items():
            if param_name in fault:
                _check_param_constraint(fault[param_name], param_constraint, fpath, param_name, err)

        if not isinstance(target_value, str) or not target_value:
            err.add(
                "E-SCEN-011",
                (*fpath, target_field),
                f"`{target_field}` must be a non-empty string",
                "name the target explicitly",
            )
        else:
            any_well_formed = True
            if graph_identity is not None and target_kind in _E_SCEN_005_KINDS:
                catalogue = {
                    TargetKind.AGENT: graph_identity.agents,
                    TargetKind.TOOL: graph_identity.tools,
                    TargetKind.EDGE: graph_identity.edges,
                }[target_kind]
                if target_value not in catalogue:
                    known = ", ".join(sorted(catalogue)) or "(none found)"
                    err.add(
                        "E-SCEN-005",
                        (*fpath, target_field),
                        f"fault target {target_value!r} not present in the graph "
                        f"({graph_identity.source}); valid {target_kind.value}s: {known}",
                        _suggest_close(target_value, catalogue)
                        if catalogue
                        else f"add {target_value!r} to the graph",
                    )
            if is_user_graph:
                field_name = _BLAST_RADIUS_FIELD[target_kind]
                allowed = blast_radius.get(field_name, [])
                if target_kind is TargetKind.STATE_KEY:
                    in_radius = any(
                        fnmatch.fnmatchcase(target_value, pattern)
                        for pattern in allowed
                        if isinstance(pattern, str)
                    )
                else:
                    in_radius = target_value in allowed
                if not in_radius:
                    err.add(
                        "E-SCEN-004",
                        (*fpath, target_field),
                        f"fault target {target_value!r} is outside the declared "
                        f"blast_radius.{field_name} (invariant I12)",
                        f"add {target_value!r} to blast_radius.{field_name}, "
                        "or change the fault's target",
                    )
    return any_well_formed


def _check_guards(data: dict[str, object], err: _Errors) -> None:
    guards = data.get("guards", {})
    if not isinstance(guards, dict):
        err.add(
            "E-SCEN-011",
            ("guards",),
            "`guards` must be a mapping",
            "e.g. `guards: {max_tokens: 100000}`",
        )
        return
    err.check_unknown_keys(guards, ("guards",), GUARD_KEYS)
    ceilings = load_guard_ceilings()
    for key in GUARD_KEYS & guards.keys():
        value = guards[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            err.add(
                "E-SCEN-011",
                ("guards", key),
                f"guards.{key} must be a positive integer, got {value!r}",
                "use a positive integer",
            )
            continue
        ceiling = ceilings.get(key)
        if ceiling is not None and value > ceiling:
            err.add(
                "E-SCEN-008",
                ("guards", key),
                f"guards.{key}={value} exceeds the hard ceiling {ceiling} "
                "(agentdx.toml [scenario])",
                f"lower guards.{key} to <= {ceiling}, "
                "or raise the ceiling deliberately in agentdx.toml",
            )


def _check_baseline(data: dict[str, object], err: _Errors) -> bool:
    """Validate `baseline:`. Returns the resolved `generate` flag (default True)."""
    baseline = data.get("baseline", {})
    if not isinstance(baseline, dict):
        err.add(
            "E-SCEN-011",
            ("baseline",),
            "`baseline` must be a mapping",
            "e.g. `baseline: {generate: true}`",
        )
        return True
    err.check_unknown_keys(baseline, ("baseline",), BASELINE_KEYS)
    if "generate" in baseline and not isinstance(baseline["generate"], bool):
        err.add(
            "E-SCEN-011",
            ("baseline", "generate"),
            "baseline.generate must be a boolean",
            "use `true` or `false`",
        )
    if "allow_low_comparability" in baseline and not isinstance(
        baseline["allow_low_comparability"], bool
    ):
        err.add(
            "E-SCEN-011",
            ("baseline", "allow_low_comparability"),
            "baseline.allow_low_comparability must be a boolean",
            "use `true` or `false`",
        )
    if (
        "prompt" in baseline
        and baseline["prompt"] is not None
        and not isinstance(baseline["prompt"], str)
    ):
        err.add(
            "E-SCEN-011",
            ("baseline", "prompt"),
            "baseline.prompt must be a string path or null",
            "give a file path, or omit the key",
        )
    return bool(baseline.get("generate", True))


def _check_exploration(data: dict[str, object], err: _Errors) -> None:
    exploration = data.get("exploration", {})
    if not isinstance(exploration, dict):
        err.add(
            "E-SCEN-011",
            ("exploration",),
            "`exploration` must be a mapping",
            "e.g. `exploration: {enabled: false}`",
        )
        return
    err.check_unknown_keys(exploration, ("exploration",), EXPLORATION_KEYS)
    if "enabled" in exploration and not isinstance(exploration["enabled"], bool):
        err.add(
            "E-SCEN-011",
            ("exploration", "enabled"),
            "exploration.enabled must be a boolean",
            "use `true` or `false`",
        )
    for key in ("k", "max_schedules"):
        if key in exploration and (
            not isinstance(exploration[key], int)
            or isinstance(exploration[key], bool)
            or exploration[key] < 1
        ):
            err.add(
                "E-SCEN-011",
                ("exploration", key),
                f"exploration.{key} must be a positive integer",
                "use a positive integer",
            )


def _check_success_check(data: dict[str, object], err: _Errors) -> str:
    """Validate `success_check:`, importing `ref` when `type: python` (PRD §21.6/§21.3 E-SCEN-009).

    Returns the resolved `type` (defaulted to `none` on any structural failure).
    """
    success_check = data.get("success_check", {})
    if not isinstance(success_check, dict):
        err.add(
            "E-SCEN-011",
            ("success_check",),
            "`success_check` must be a mapping",
            "e.g. `success_check: {type: none}`",
        )
        return DEFAULT_SUCCESS_CHECK_TYPE
    err.check_unknown_keys(success_check, ("success_check",), SUCCESS_CHECK_KEYS)
    sc_type = success_check.get("type", DEFAULT_SUCCESS_CHECK_TYPE)
    valid_types = {t.value for t in SuccessCheckType}
    if sc_type not in valid_types:
        err.add(
            "E-SCEN-011",
            ("success_check", "type"),
            f"success_check.type must be one of {sorted(valid_types)}, got {sc_type!r}",
            "use `python`, `shell`, or `none`",
        )
        return DEFAULT_SUCCESS_CHECK_TYPE

    ref = success_check.get("ref")
    if sc_type == SuccessCheckType.PYTHON.value:
        if not isinstance(ref, str) or ":" not in ref:
            err.add(
                "E-SCEN-011",
                ("success_check", "ref"),
                "a `python` success_check needs `ref: '<module.path>:<function_name>'`",
                "e.g. `ref: fixtures.code_pipeline.checks:module_a_compiles`",
            )
        else:
            module_path, _, func_name = ref.rpartition(":")
            try:
                module = importlib.import_module(module_path)
                fn = getattr(module, func_name)
            except (ImportError, AttributeError, ValueError) as exc:
                err.add(
                    "E-SCEN-009",
                    ("success_check", "ref"),
                    f"success_check.ref {ref!r} is not importable: {exc}",
                    "fix the module path or function name, or switch to `type: shell`/`type: none`",
                )
            else:
                if not callable(fn):
                    err.add(
                        "E-SCEN-009",
                        ("success_check", "ref"),
                        f"success_check.ref {ref!r} resolved to a non-callable",
                        "point `ref` at a function",
                    )
    elif sc_type == SuccessCheckType.SHELL.value and (not isinstance(ref, str) or not ref.strip()):
        err.add(
            "E-SCEN-011",
            ("success_check", "ref"),
            "a `shell` success_check needs a non-empty `ref` command string",
            "e.g. `ref: 'pytest -q fixtures/code_pipeline/test_output.py'`",
        )
    return str(sc_type)


def _check_assertions(
    data: dict[str, object],
    *,
    success_check_type: str,
    faults_present: bool,
    baseline_generate: bool,
    graph_identity: GraphIdentity | None,
    err: _Errors,
) -> None:
    assertions = data.get("assertions", [])
    if not isinstance(assertions, list):
        err.add(
            "E-SCEN-011",
            ("assertions",),
            "`assertions` must be a list",
            "e.g. `assertions: [no_state_conflicts]`",
        )
        return

    for i, item in enumerate(assertions):
        apath: ScenarioPath = ("assertions", i)
        name_raw: object
        params: object
        if isinstance(item, str):
            name_raw, params = item, None
        elif isinstance(item, dict) and len(item) == 1:
            name_raw, params = next(iter(item.items()))
        else:
            err.add(
                "E-SCEN-011",
                apath,
                "each assertion must be a bare name or a single-key mapping {name: value}",
                "e.g. `no_state_conflicts` or `speedup_vs_baseline: '>= 1.0'`",
            )
            continue
        if not isinstance(name_raw, str) or name_raw not in BUILT_IN_ASSERTION_NAMES:
            err.add(
                "E-SCEN-011",
                apath,
                f"unknown assertion {name_raw!r}",
                _suggest_close(str(name_raw), BUILT_IN_ASSERTION_NAMES),
            )
            continue
        name = name_raw

        if name in {
            "no_state_conflicts",
            "no_silent_failures",
            "task_success",
            "deterministic_replay",
        }:
            if params is not None:
                err.add(
                    "E-SCEN-011",
                    (*apath, name),
                    f"`{name}` takes no parameters",
                    f"write it as a bare `- {name}`",
                )
            if name == "task_success" and success_check_type == SuccessCheckType.NONE.value:
                err.add(
                    "E-SCEN-007",
                    apath,
                    "`task_success` is not measurable: no `success_check` is configured",
                    "set `success_check: {type: python, ref: ...}` or remove this assertion",
                )
            if name == "no_silent_failures" and not faults_present:
                err.add(
                    "E-SCEN-007",
                    apath,
                    "`no_silent_failures` is not measurable: the scenario declares no faults",
                    "add a `faults:` entry, or remove this assertion",
                )
        elif name in {"speedup_vs_baseline", "resilience_score", "token_cost_multiplier"}:
            if not isinstance(params, str) or parse_comparison(params) is None:
                err.add(
                    "E-SCEN-011",
                    (*apath, name),
                    f"`{name}` needs a comparison string like '>= 1.0', got {params!r}",
                    "use an operator (>=, <=, ==, >, <) followed by a number",
                )
            if name == "resilience_score" and not faults_present:
                err.add(
                    "E-SCEN-007",
                    apath,
                    "`resilience_score` is not measurable: the scenario declares no faults",
                    "add a `faults:` entry, or remove this assertion",
                )
            if name == "speedup_vs_baseline" and not baseline_generate:
                err.add(
                    "E-SCEN-007",
                    apath,
                    "`speedup_vs_baseline` is not measurable: `baseline.generate` is false",
                    "remove `baseline: {generate: false}`, or drop this assertion",
                )
        elif name == "max_findings":
            if not isinstance(params, dict):
                err.add(
                    "E-SCEN-011",
                    (*apath, name),
                    "`max_findings` needs {severity: <level>, count: <n>}",
                    "e.g. `max_findings: {severity: high, count: 0}`",
                )
            else:
                err.check_unknown_keys(params, (*apath, name), MAX_FINDINGS_KEYS)
                valid_severities = {s.value for s in Severity}
                if params.get("severity") not in valid_severities:
                    err.add(
                        "E-SCEN-011",
                        (*apath, name, "severity"),
                        f"max_findings.severity must be one of {sorted(valid_severities)}",
                        "use one of the listed severities",
                    )
                count = params.get("count")
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    err.add(
                        "E-SCEN-011",
                        (*apath, name, "count"),
                        "max_findings.count must be a non-negative integer",
                        "use 0 or a positive integer",
                    )
        elif name == "critical_path_share":
            if not isinstance(params, dict):
                err.add(
                    "E-SCEN-011",
                    (*apath, name),
                    "`critical_path_share` needs {edge: <name>, cmp: '<op> <v>'}",
                    "e.g. `critical_path_share: {edge: \"coder->reviewer\", cmp: '<= 0.4'}`",
                )
            else:
                err.check_unknown_keys(params, (*apath, name), CRITICAL_PATH_SHARE_KEYS)
                edge = params.get("edge")
                cmp_expr = params.get("cmp")
                if not isinstance(edge, str) or not edge:
                    err.add(
                        "E-SCEN-011",
                        (*apath, name, "edge"),
                        "critical_path_share.edge must be a non-empty string",
                        'e.g. `edge: "coder->reviewer"`',
                    )
                elif graph_identity is not None and edge not in graph_identity.edges:
                    known = ", ".join(sorted(graph_identity.edges)) or "(none found)"
                    err.add(
                        "E-SCEN-007",
                        (*apath, name, "edge"),
                        f"critical_path_share.edge {edge!r} not present in the graph; "
                        f"known edges: {known}",
                        "fix the edge name",
                    )
                if not isinstance(cmp_expr, str) or parse_comparison(cmp_expr) is None:
                    err.add(
                        "E-SCEN-011",
                        (*apath, name, "cmp"),
                        "critical_path_share.cmp must be a comparison string like "
                        f"'<= 0.4', got {cmp_expr!r}",
                        "use an operator followed by a number",
                    )


# ---------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------


def validate(parsed: ParsedScenario) -> tuple[ScenarioError, ...]:
    """Validate a parsed scenario document. Returns every error found, sorted by source line.

    An empty return means the document is valid. Does not raise for validation failures —
    use `validate_or_raise` when a single exception is more convenient.
    """
    if parsed.data is None:
        return (
            ScenarioError(
                code="E-SCEN-000",
                path="<root>",
                message="the scenario document is empty",
                suggestion="add at least `scenario: <slug>`, `task: <path>`",
                file=str(parsed.path),
                line=1,
            ),
        )

    err = _Errors(parsed)
    data = parsed.data

    err.check_unknown_keys(data, (), TOP_LEVEL_KEYS)
    _check_version(data, err)
    _check_required(data, err)
    target = _check_target(data, err)
    _check_target_fixture_exists(target, err)

    if "seed" in data and (not isinstance(data["seed"], int) or isinstance(data["seed"], bool)):
        err.add(
            "E-SCEN-011",
            ("seed",),
            f"`seed` must be an integer, got {data['seed']!r}",
            "use an integer, e.g. `seed: 42`",
        )
    if "repeats" in data and (
        not isinstance(data["repeats"], int)
        or isinstance(data["repeats"], bool)
        or data["repeats"] < 1
    ):
        err.add(
            "E-SCEN-011",
            ("repeats",),
            f"`repeats` must be a positive integer, got {data['repeats']!r}",
            "use a positive integer, e.g. `repeats: 1`",
        )
    if "mode" in data and data["mode"] not in {m.value for m in Mode}:
        err.add(
            "E-SCEN-011",
            ("mode",),
            f"`mode` must be one of {sorted(m.value for m in Mode)}, got {data['mode']!r}",
            "use `replay` (the default) unless you specifically need another mode",
        )
    if "chaos_opt_in" in data and not isinstance(data["chaos_opt_in"], bool):
        err.add(
            "E-SCEN-011",
            ("chaos_opt_in",),
            "`chaos_opt_in` must be a boolean",
            "use `true` or `false`",
        )
    if (
        "description" in data
        and data["description"] is not None
        and not isinstance(data["description"], str)
    ):
        err.add(
            "E-SCEN-011",
            ("description",),
            "`description` must be a string",
            "use a short string, or omit the key",
        )

    _check_hypothesis(data, err)
    blast_radius = _check_blast_radius(data, err)
    graph_identity = resolve_graph_identity(target, scenario_file=parsed.path) if target else None
    faults_present = _check_faults(data, target, blast_radius, graph_identity, err)
    _check_guards(data, err)
    baseline_generate = _check_baseline(data, err)
    _check_exploration(data, err)
    success_check_type = _check_success_check(data, err)
    _check_assertions(
        data,
        success_check_type=success_check_type,
        faults_present=faults_present,
        baseline_generate=baseline_generate,
        graph_identity=graph_identity,
        err=err,
    )

    return tuple(sorted(err.items, key=lambda e: (e.line if e.line is not None else 0, e.path)))


def validate_or_raise(parsed: ParsedScenario) -> None:
    """Validate `parsed`, raising `ScenarioValidationError` if any error was found."""
    errors = validate(parsed)
    if errors:
        raise ScenarioValidationError(errors)


__all__ = [
    "GraphIdentity",
    "ScenarioError",
    "ScenarioValidationError",
    "load_guard_ceilings",
    "resolve_graph_identity",
    "validate",
    "validate_or_raise",
]
