"""Load, resolve and validate one scenario file — the chain every scenario-aware command needs.

`scenario/` deliberately re-exports nothing (see its `__init__.py`) so every caller reaches
each phase through its owning submodule; this module is that one, named chain, used by
`agentdx scenario validate/list/expand` and by `agentdx run`'s scenario-target branch, so the
five-call sequence is written once rather than five times slightly differently.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentdx.scenario.loader import (
    ParsedScenario,
    ScenarioLoadError,
    compute_scenario_hash,
    load_scenario_file,
    resolve_defaults,
    resolve_extends,
)
from agentdx.scenario.validate import ScenarioValidationError, validate_or_raise

__all__ = [
    "LoadedScenario",
    "ScenarioLoadError",
    "ScenarioValidationError",
    "discover_scenarios",
    "load_and_validate",
]


@dataclass(frozen=True, slots=True)
class LoadedScenario:
    """One scenario, loaded, `extends`-merged, validated and defaults-resolved."""

    path: Path
    parsed: ParsedScenario
    resolved: dict[str, object]
    scenario_hash: str

    @property
    def scenario_id(self) -> str:
        """Return the scenario's declared `scenario:` name."""
        value = self.resolved.get("scenario")
        return value if isinstance(value, str) else self.path.stem


def load_and_validate(path: Path) -> LoadedScenario:
    """Run the full load -> extends -> validate -> resolve-defaults chain for one file.

    Raises:
        ScenarioLoadError: the YAML itself, or an `extends:` chain, is malformed.
        ScenarioValidationError: the resolved document fails schema/semantic validation
            (`E-SCEN-0NN`) — carries every error found, not just the first.
    """
    parsed = load_scenario_file(path)
    parsed = resolve_extends(parsed)
    validate_or_raise(parsed)
    if parsed.data is None:
        # validate_or_raise already rejects an empty document (E-SCEN-001) — unreachable in
        # practice, but the type checker cannot see that across the function boundary.
        msg = f"{path}: empty scenario document passed validation unexpectedly"
        raise ScenarioLoadError("E-SCEN-000", msg)
    resolved = resolve_defaults(parsed.data)
    scenario_hash = compute_scenario_hash(resolved)
    return LoadedScenario(path=path, parsed=parsed, resolved=resolved, scenario_hash=scenario_hash)


def discover_scenarios(path: Path) -> tuple[Path, ...]:
    """Return every `*.yaml`/`*.yml` scenario file under `path`, in sorted-path order.

    PRD §22.1 item 5: "scenarios execute in sorted-path order" for deterministic `--ci` runs.
    `path` itself is returned as a single-element tuple when it is already a file.
    """
    if path.is_file():
        return (path,)
    if not path.is_dir():
        return ()
    found = [*path.rglob("*.yaml"), *path.rglob("*.yml")]
    return tuple(sorted(set(found)))
