"""Position-preserving YAML loading, `extends` composition, and defaults resolution.

The scenario system's headline usability promise (PRD §21.3: "Failing after a 40-second run
because of a typo is unacceptable") is only deliverable if every value in the parsed document
still knows which source line it came from. That is a decision this module makes once, at
parse time — a validator that only sees plain `dict`/`list`/`str` after the fact has already
lost the information a good error message needs, and no amount of cleverness in `validate.py`
recovers it. This is Design Constraint 1 of the P08 prompt, made concrete.

**How position tracking works.** `yaml.safe_load` alone throws the source positions away —
by the time it returns, a mapping key and a hand-typed literal are both just Python objects.
This module parses each document *twice*, deliberately: once with `yaml.compose` (returns a
`Node` tree — every node carries a `start_mark.line`, nothing is constructed into Python
values) and once with `yaml.safe_load` (returns the Python values, no position information).
Both traversals visit a document's mappings and sequences in identical order (`PyYAML`
preserves document order both ways, and both calls share one `SafeLoader` resolver), so
`_walk` zips them positionally to build a `SourceMap`: a `path -> line` table addressed by
the same `ScenarioPath` tuples (`("faults", 0, "agent")`-shaped) that `validate.py` reports
errors against. Reimplementing a custom `yaml.Loader` subclass would do this in one pass
instead of two; it was not the chosen design because the zip-based approach is auditable in
about a third of the code and this module is not on any hot path — a scenario file is at most
a few hundred lines, parsed once per `agentdx scenario run` invocation, not once per event.

PRD §21 (scenario system) · §21.3 (validation rules cite `loader.py`'s positions) ·
§21.5 (`extends` composition, matrix expansion) · §12.4 (LOAD / RESOLVE lifecycle phases).
"""

from __future__ import annotations

import copy
import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import yaml

from agentdx.scenario.schema import (
    DEFAULT_BASELINE_GENERATE,
    DEFAULT_CHAOS_OPT_IN,
    DEFAULT_GUARDS,
    DEFAULT_MODE,
    DEFAULT_REPEATS,
    DEFAULT_SEED,
    DEFAULT_SUCCESS_CHECK_TYPE,
    SCHEMA_VERSION,
)

_DOCS: Final = "docs/scenario-reference.md"

ScenarioPathKey = str | int
ScenarioPath = tuple[ScenarioPathKey, ...]
"""Addresses one value inside a parsed scenario document, root-relative.

`("faults", 0, "agent")` is the `agent:` key of the first entry of `faults:`. The empty tuple
`()` addresses the whole document.
"""


class ScenarioLoadError(Exception):
    """Raised for a failure that happens before there is a document to validate at all.

    Guarantees: always carries an `E-SCEN-NNN` code (`.code`) and, where the failure has a
    source location, a 1-indexed `.line`. Malformed YAML syntax has no `ScenarioPath` to
    anchor to — PyYAML's own `problem_mark` is the only position available — so this is a
    distinct exception from `ScenarioValidationError` (`validate.py`), which always carries
    at least one located, coded finding.
    """

    def __init__(self, code: str, message: str, *, line: int | None = None) -> None:
        """Build the error. `code` is namespaced `E-SCEN-*`, matching every other layer."""
        self.code = code
        self.message = message
        self.line = line
        super().__init__(str(self))

    def __str__(self) -> str:
        """Render `[CODE] file:line: message (docs url)`, matching every other error type here."""
        where = f":{self.line}" if self.line is not None else ""
        return f"[{self.code}]{where}: {self.message} ({_DOCS}#{self.code.lower()})"


@dataclass(frozen=True, slots=True)
class SourceMap:
    """Maps a `ScenarioPath` to the source line of its key and of its value.

    Guarantees: `value_line(())` is always present for a non-empty document (the document's
    first line). A path with no entry in `origin_file` was defined in the document this map
    was built for; one that does exist there was inherited, unmodified, from an `extends`
    ancestor — `resolve()` merges maps across the `extends` chain so an error about an
    inherited field still names the file the value actually came from.
    """

    key_lines: dict[ScenarioPath, int] = field(default_factory=dict)
    value_lines: dict[ScenarioPath, int] = field(default_factory=dict)
    origin_file: dict[ScenarioPath, Path] = field(default_factory=dict)

    def line_for(self, path: ScenarioPath, *, prefer: str = "value") -> int | None:
        """Return the best available line for `path`, walking up to the nearest known ancestor.

        A required key that is entirely absent has no position of its own — there is no
        `foo:` token anywhere in the file to point at — so the sensible fallback is the line
        of the *parent* mapping that should have contained it. `prefer` picks `key_lines` or
        `value_lines` first at the exact path; the ancestor walk always prefers `value_lines`,
        since an ancestor's own key line would be misleading once we are one level removed
        from what was actually being reported.
        """
        primary, secondary = (
            (self.key_lines, self.value_lines)
            if prefer == "key"
            else (self.value_lines, self.key_lines)
        )
        if path in primary:
            return primary[path]
        if path in secondary:
            return secondary[path]
        for depth in range(len(path) - 1, -1, -1):
            ancestor = path[:depth]
            if ancestor in self.value_lines:
                return self.value_lines[ancestor]
            if ancestor in self.key_lines:
                return self.key_lines[ancestor]
        return None

    def file_for(self, path: ScenarioPath, *, default: Path) -> Path:
        """Return the file a path's value actually came from (its own file, or an ancestor's)."""
        for depth in range(len(path), -1, -1):
            candidate = path[:depth]
            if candidate in self.origin_file:
                return self.origin_file[candidate]
        return default

    def merged_with(self, other: SourceMap, *, other_file: Path) -> SourceMap:
        """Return a map covering both sources: `self` (the child) wins on any shared path.

        Used by `extends` composition: `other` is the parent's map, `other_file` the parent's
        path. Every path `other` locates gets `origin_file` set to `other_file` unless `self`
        already places it (the child overrode that key, so the child's own line is correct).
        """
        key_lines = dict(other.key_lines)
        key_lines.update(self.key_lines)
        value_lines = dict(other.value_lines)
        value_lines.update(self.value_lines)
        origin_file = dict.fromkeys(other.value_lines, other_file)
        origin_file.update(other.origin_file)
        origin_file.update(self.origin_file)
        for p in self.value_lines:
            origin_file.pop(p, None)
        return SourceMap(key_lines=key_lines, value_lines=value_lines, origin_file=origin_file)


@dataclass(frozen=True, slots=True)
class ParsedScenario:
    """The raw result of parsing one scenario file: untyped, unmerged, undefaulted.

    Guarantees: `data` is `None` only for a genuinely empty document (zero bytes or all
    comments) — an error state `validate.py` reports as a missing document, not a crash.
    `source_map` covers exactly the keys physically present in `path`; it has not yet been
    merged with any `extends` ancestor (`load_scenario_file` performs that merge separately).
    """

    data: dict[str, object] | None
    source_map: SourceMap
    path: Path
    text: str


def _walk(
    node: yaml.Node,
    value: object,
    path: ScenarioPath,
    key_lines: dict[ScenarioPath, int],
    value_lines: dict[ScenarioPath, int],
) -> None:
    """Recursively record source lines for every path reachable from `node`/`value`.

    `node` and `value` are the two parallel representations of the same document position —
    see the module docstring for why two representations exist at all. Mismatched shapes
    (possible with a YAML merge key, `<<:`, which PyYAML resolves specially) are tolerated by
    simply not descending further; the path still gets *a* line from its parent, just not a
    fully precise one two or more levels down. This is a documented limitation, not a crash.
    """
    if isinstance(node, yaml.MappingNode) and isinstance(value, dict):
        pairs = list(node.value)
        if len(pairs) != len(value):
            return
        for (key_node, val_node), (py_key, py_val) in zip(pairs, value.items(), strict=True):
            child_path = (*path, py_key)
            key_lines[child_path] = key_node.start_mark.line + 1
            value_lines[child_path] = val_node.start_mark.line + 1
            _walk(val_node, py_val, child_path, key_lines, value_lines)
    elif isinstance(node, yaml.SequenceNode) and isinstance(value, list):
        items = list(node.value)
        if len(items) != len(value):
            return
        for i, (item_node, item_val) in enumerate(zip(items, value, strict=True)):
            child_path = (*path, i)
            value_lines[child_path] = item_node.start_mark.line + 1
            _walk(item_node, item_val, child_path, key_lines, value_lines)
    # ScalarNode: nothing further to record; the caller already recorded `path`'s own line.


def parse_scenario_text(text: str, *, source_name: str = "<scenario>") -> ParsedScenario:
    """Parse YAML text into a `ParsedScenario`, source positions included.

    Raises:
        ScenarioLoadError: `E-SCEN-000` — malformed YAML syntax, or the document's root is
            not a mapping (a scenario file must be `key: value` at the top level, not a bare
            list or scalar). `E-SCEN-000` is not in PRD §21.3's table (that table starts from
            "the document parsed"); it is declared here for the one failure mode that occurs
            *before* parsing succeeds at all, following the project's standing precedent of
            minting a new code rather than overloading an unrelated one (CONTEXT.md D-13,
            D-25, D-40).
    """
    try:
        root_node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        line = _mark_line(exc)
        raise ScenarioLoadError(
            "E-SCEN-000", f"malformed YAML in {source_name}: {exc}", line=line
        ) from exc

    if root_node is None:
        return ParsedScenario(data=None, source_map=SourceMap(), path=Path(source_name), text=text)

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover — compose already validated syntax
        line = _mark_line(exc)
        raise ScenarioLoadError(
            "E-SCEN-000", f"malformed YAML in {source_name}: {exc}", line=line
        ) from exc

    if not isinstance(data, dict):
        raise ScenarioLoadError(
            "E-SCEN-000",
            f"{source_name} must be a YAML mapping at the top level, got "
            f"{type(data).__name__}. A scenario file starts with `version: 1`, not a list.",
            line=root_node.start_mark.line + 1,
        )

    key_lines: dict[ScenarioPath, int] = {}
    value_lines: dict[ScenarioPath, int] = {(): root_node.start_mark.line + 1}
    _walk(root_node, data, (), key_lines, value_lines)
    return ParsedScenario(
        data=data,
        source_map=SourceMap(key_lines=key_lines, value_lines=value_lines),
        path=Path(source_name),
        text=text,
    )


def _mark_line(exc: yaml.YAMLError) -> int | None:
    """Best-effort extraction of a 1-indexed line number from a PyYAML syntax error."""
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return None
    line = getattr(mark, "line", None)
    return None if line is None else line + 1


def load_scenario_file(path: Path) -> ParsedScenario:
    """Read and parse a scenario file from disk.

    Raises:
        ScenarioLoadError: `E-SCEN-000` (malformed YAML) or the file cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioLoadError("E-SCEN-000", f"cannot read {path}: {exc}") from exc
    return parse_scenario_text(text, source_name=str(path))


# ---------------------------------------------------------------------------------------
# `extends` composition (PRD §21.5) — deep merge, lists replaced not concatenated
# ---------------------------------------------------------------------------------------


def _deep_merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """Merge `override` onto `base`. A dict merges recursively; anything else replaces.

    "Anything else" includes lists: PRD §21.5 is explicit that a list is *replaced*, not
    concatenated — "the classic 'inherited fault I did not intend' hazard" is exactly what
    concatenation would reintroduce, since a child scenario would have no way to say "no
    faults" against a parent that declared some.
    """
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if key in merged and isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def resolve_extends(parsed: ParsedScenario, *, _chain: tuple[Path, ...] = ()) -> ParsedScenario:
    """Resolve one `extends:` chain into a single merged `ParsedScenario`.

    Raises:
        ScenarioLoadError: `E-SCEN-006` — the `extends` target does not exist, is not
            readable, or the chain cycles back on itself (a scenario may not, transitively,
            extend itself).
    """
    if parsed.data is None:
        return parsed
    extends_value = parsed.data.get("extends")
    if extends_value is None:
        return parsed
    if not isinstance(extends_value, str):
        line = parsed.source_map.line_for(("extends",))
        raise ScenarioLoadError(
            "E-SCEN-006", "`extends` must be a path to another scenario file (a string)", line=line
        )

    resolved_path = (
        parsed.path.parent / extends_value
        if not Path(extends_value).is_absolute()
        else Path(extends_value)
    )
    resolved_path = resolved_path.resolve() if resolved_path.exists() else resolved_path
    if resolved_path in _chain or resolved_path == parsed.path.resolve():
        raise ScenarioLoadError(
            "E-SCEN-006",
            f"`extends` cycle detected: {' -> '.join(str(p) for p in (*_chain, resolved_path))}",
            line=parsed.source_map.line_for(("extends",)),
        )
    if not resolved_path.exists():
        raise ScenarioLoadError(
            "E-SCEN-006",
            f"`extends: {extends_value}` resolved to {resolved_path}, which does not exist",
            line=parsed.source_map.line_for(("extends",)),
        )

    parent_parsed = load_scenario_file(resolved_path)
    parent_resolved = resolve_extends(parent_parsed, _chain=(*_chain, parsed.path.resolve()))
    if parent_resolved.data is None:
        raise ScenarioLoadError(
            "E-SCEN-006", f"`extends: {extends_value}` ({resolved_path}) is an empty document"
        )

    child_data = dict(parsed.data)
    child_data.pop("extends", None)
    merged_data = _deep_merge(parent_resolved.data, child_data)
    merged_map = parsed.source_map.merged_with(
        parent_resolved.source_map, other_file=parent_resolved.path
    )
    return ParsedScenario(
        data=merged_data, source_map=merged_map, path=parsed.path, text=parsed.text
    )


# ---------------------------------------------------------------------------------------
# RESOLVE phase (PRD §12.4): defaults, task-path target inference, scenario_hash
# ---------------------------------------------------------------------------------------


def _repo_root() -> Path | None:
    """Return the directory containing `agentdx.toml`, walking up from this file.

    Duplicated from `validate.py`'s identical helper rather than imported: `validate.py`
    imports from this module (`loader.py`), so the reverse import would be circular.
    `None` when this module is running somewhere `agentdx.toml` is not a filesystem sibling.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "agentdx.toml").is_file():
            return parent
    return None


def _load_scenario_toml_section() -> dict[str, object]:
    """Return `agentdx.toml`'s `[scenario]` table, or `{}` if it can't be found/read/parsed.

    Shared by `load_guard_defaults` and `load_success_check_timeout_s` — both read-at-call-
    time from the same section, never a module-level cached constant, so a process that
    reloads `agentdx.toml` never serves a stale value (same reasoning as
    `validate.load_guard_ceilings`).
    """
    root = _repo_root()
    if root is None:
        return {}
    toml_path = root / "agentdx.toml"
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("scenario", {})
    return section if isinstance(section, dict) else {}


def load_guard_defaults() -> dict[str, int]:
    """Return the default guard value for each key, from `agentdx.toml`'s `[scenario]` section.

    OP-3 repair (2026-08-16, D-41 follow-up): `agentdx.toml` has carried `guard_default_*`
    keys since P08 shipped, but nothing ever read them — `schema.DEFAULT_GUARDS` (a second,
    hardcoded copy of the same values) was used instead, exactly the duplicated-source-of-
    truth pattern D-41's own rationale says this section exists to avoid. Mirrors
    `validate.load_guard_ceilings`'s read-at-call-time, `guard_ceiling_`-prefix pattern for
    consistency; falls back to `schema.DEFAULT_GUARDS` when `agentdx.toml` cannot be found or
    has no `[scenario]` defaults, same as the ceiling function's fallback.
    """
    section = _load_scenario_toml_section()
    defaults = {
        key[len("guard_default_") :]: value
        for key, value in section.items()
        if key.startswith("guard_default_")
        and isinstance(value, int)
        and not isinstance(value, bool)
    }
    return defaults if defaults else dict(DEFAULT_GUARDS)


_DEFAULT_SUCCESS_CHECK_TIMEOUT_S: Final = 5
"""PRD §21.6: "run in the sandbox, with a time limit (5 s wall)" — the fallback when
`agentdx.toml` has no `success_check_timeout_s` override, not a second source of truth for
the number itself (`load_success_check_timeout_s` is)."""


def load_success_check_timeout_s() -> int:
    """Return the `success_check` wall-clock timeout (seconds), from `agentdx.toml`.

    Reads `agentdx.toml`'s `[scenario]` section. OP-3 repair (2026-08-16, second independent
    OP-2 finding): `assertions.py` previously hardcoded `_SHELL_TIMEOUT_S = 5` as a bare
    literal — a magic number, and one this same module's own
    `guard_default_*`/`guard_ceiling_*` keys had already established the convention against
    (AGENTS.md §4). Falls back to `_DEFAULT_SUCCESS_CHECK_TIMEOUT_S` (the PRD's own value)
    when `agentdx.toml` has no override.
    """
    section = _load_scenario_toml_section()
    value = section.get("success_check_timeout_s")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return _DEFAULT_SUCCESS_CHECK_TIMEOUT_S


DEFAULTS: Final[dict[str, object]] = {
    "version": SCHEMA_VERSION,
    "mode": DEFAULT_MODE,
    "seed": DEFAULT_SEED,
    "repeats": DEFAULT_REPEATS,
    "chaos_opt_in": DEFAULT_CHAOS_OPT_IN,
    "guards": dict(DEFAULT_GUARDS),
    "baseline": {
        "generate": DEFAULT_BASELINE_GENERATE,
        "prompt": None,
        "allow_low_comparability": False,
    },
    "exploration": {"enabled": False, "k": 2, "max_schedules": 200},
    "success_check": {"type": DEFAULT_SUCCESS_CHECK_TYPE, "ref": None},
    "blast_radius": {"agents": [], "tools": [], "edges": [], "state_keys": [], "providers": []},
    "faults": [],
    "assertions": [],
    "hypothesis": {},
}
"""PRD §21.4's default table, plus the shape defaults for optional nested sections that the
table names only in prose ("Works without user code", "Cost control"). Applying every one of
these from a single table, in a single function (`resolve_defaults`), is Design Constraint 4:
the alternative — defaults sprinkled across `validate.py`'s field-by-field checks — is exactly
how a default silently drifts from this table without anyone noticing.
"""


def _is_real_fixture(name: str) -> bool | None:
    """Whether `fixtures/<name>/graph.py` exists on disk. `None` if the repo root can't be found.

    A syntactic `fixtures/<name>/...` shape is not evidence `<name>` is a fixture — two real,
    shipped directories under `fixtures/` are not fixtures at all (`fixtures/tasks/`, task
    descriptions shared across fixtures; `fixtures/perturbations/`, no `graph.py`). OP-3
    repair (2026-08-16, second independent OP-2, correcting the first OP-3's own C-13 fix):
    the original fix denylisted the single known name `"tasks"`, which still silently
    mis-inferred `fixtures/perturbations/...` the exact same way — a second real path
    reproducing the original bug class. A positive check (does `graph.py` exist?) generalises
    correctly to any future non-fixture directory without needing its name enumerated by hand.
    """
    root = _repo_root()
    if root is None:
        return None
    return (root / "fixtures" / name / "graph.py").is_file()


def infer_fixture_from_task_path(task: str) -> str | None:
    """Infer a fixture name from a `fixtures/<name>/...` task path (PRD §21.2's `[SOURCE]` note).

    Returns `None` when `task` is not shaped like a fixture-relative path (an inline task
    string, or a path outside `fixtures/`), when `<name>` is not a real fixture (no
    `fixtures/<name>/graph.py` on disk — see `_is_real_fixture`), or when the repo root
    itself cannot be resolved (e.g. installed as a wheel with no source checkout nearby) —
    `resolve()` treats all three as "could not infer", not as an error in this function. A
    scenario whose `task:` points outside a real fixture directory must set `target:`
    explicitly (C-13); guessing a fixture name that cannot be verified, or that is verified
    wrong, would be worse than declining to infer at all.
    """
    parts = Path(task).parts
    if len(parts) < 2 or parts[0] != "fixtures":
        return None
    candidate = parts[1]
    return candidate if _is_real_fixture(candidate) else None


def non_fixture_task_dir(task: str) -> str | None:
    """Return the `fixtures/<name>/` directory `task` falls under when it isn't a fixture.

    `None` if `task` isn't `fixtures/`-shaped, or if `<name>` is a real fixture. Companion to
    `infer_fixture_from_task_path`: that function returns `None` both when
    `task` is not `fixtures/`-shaped at all *and* when `<name>` is not a real fixture —
    callers that want to name the specific non-fixture directory for a clearer error message
    (`validate.py`'s `E-SCEN-003`) use this instead. Also returns `None` when the repo root
    can't be resolved, since this function cannot then tell "not a fixture" apart from
    "cannot verify" and a false "shared directory" claim would be worse than the generic
    fallback message `_check_target` uses in that case.
    """
    parts = Path(task).parts
    if len(parts) < 2 or parts[0] != "fixtures":
        return None
    candidate = parts[1]
    is_real = _is_real_fixture(candidate)
    return None if is_real is None or is_real else candidate


def resolve_defaults(data: dict[str, object]) -> dict[str, object]:
    """Return `data` with every PRD §21.4 default merged in (Design Constraint 4's one function).

    Does not validate; call `validate.validate()` first. Also performs the PRD §21.2
    `[SOURCE]`-noted target inference (`target.fixture` from `task`'s path) when `target` is
    absent entirely, since that is a default in every sense that matters here even though
    §21.4's table does not list it by name.

    Guard defaults are read from `agentdx.toml` at call time via `load_guard_defaults()`
    (OP-3 repair, 2026-08-16) rather than taken from the module-level `DEFAULTS["guards"]`
    constant — same "read `agentdx.toml` at call time, not cached at import" reasoning
    `validate.load_guard_ceilings` already documents for the ceiling half of this config.
    """
    defaults = dict(DEFAULTS)
    defaults["guards"] = load_guard_defaults()
    resolved = _deep_merge(defaults, data)
    task_value = resolved.get("task")
    if "target" not in resolved and isinstance(task_value, str):
        inferred = infer_fixture_from_task_path(task_value)
        if inferred is not None:
            resolved["target"] = {"fixture": inferred}
    faults_value = resolved.get("faults")
    if isinstance(faults_value, list):
        for entry in faults_value:
            if isinstance(entry, dict) and entry.get("type") == "agent_crash":
                entry.setdefault("recoverable", True)
                entry.setdefault("allow_total_failure", False)
    return resolved


def canonical_json(data: dict[str, object]) -> str:
    """Return a stable, sorted-key JSON rendering used for hashing and for round-trip tests."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_scenario_hash(resolved_data: dict[str, object]) -> str:
    """Return the `scenario_hash` PRD §12.4's RESOLVE phase computes.

    A sha256 over the fully-resolved, defaulted document. Two scenario files that differ only
    by which optional key was spelled out explicitly hash identically — that is the point of
    hashing the *resolved* form rather than the source text (Design Constraint 4:
    reproducible from the recorded scenario alone, not from incidental formatting of the file
    that produced it).
    """
    return hashlib.sha256(canonical_json(resolved_data).encode("utf-8")).hexdigest()


def dump_resolved_yaml(resolved_data: dict[str, object]) -> str:
    """Render a fully-resolved scenario back to YAML — what `run.agentdx`'s `scenario.yaml` is.

    `sort_keys=False`: preserves `resolved_data`'s own key order, so callers building the
    dict with `schema.py`'s canonical field order get readable, stable output rather than
    alphabetised noise.
    """
    return yaml.safe_dump(resolved_data, sort_keys=False, default_flow_style=False)


__all__ = [
    "DEFAULTS",
    "ParsedScenario",
    "ScenarioLoadError",
    "ScenarioPath",
    "SourceMap",
    "canonical_json",
    "compute_scenario_hash",
    "dump_resolved_yaml",
    "infer_fixture_from_task_path",
    "load_guard_defaults",
    "load_scenario_file",
    "load_success_check_timeout_s",
    "non_fixture_task_dir",
    "parse_scenario_text",
    "resolve_defaults",
    "resolve_extends",
]
