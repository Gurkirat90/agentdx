"""`agentdx.analysis.race.minimal_repro` (PRD §14.8).

Two things need proving, independently: (1) the emitted scenario document is *actually valid*
against the real `scenario.validate.validate()` - not just YAML-shaped - and (2) the
`no_state_conflicts` assertion it carries genuinely discriminates the unfixed race (fails)
from the fixed one (passes), against `race.py`'s own real `Finding` objects. Neither check
requires executing a scenario (no scheduler exists to do that, see `ReproScenario`'s
docstring) - both are real, runnable checks in place of that, not a weaker substitute pretending
to be the real thing.

Importing `agentdx.scenario` here is fine - only `race.py` itself is bound by I3 (`analysis/`
purity); a test module is not a layer `.importlinter` constrains.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from agentdx.analysis.race import (
    Finding,
    MissingRunSeedError,
    ReproScenario,
    detect_conflicts,
    minimal_repro,
)
from agentdx.events.canonical import decode_event
from agentdx.events.schema import Event
from agentdx.scenario.assertions import AssertionStatus, RunSummary, eval_no_state_conflicts
from agentdx.scenario.loader import parse_scenario_text, resolve_defaults
from agentdx.scenario.validate import validate

GOLDEN_DIR = Path(__file__).parents[3] / "tests" / "golden"


def _load_golden(name: str) -> list[Event]:
    with (GOLDEN_DIR / f"{name}.jsonl").open() as fh:
        return [decode_event(line) for line in fh]


def _repro_for_code_pipeline() -> tuple[list[Event], ReproScenario]:
    events = _load_golden("code_pipeline")
    (finding,) = detect_conflicts(events)
    repro = minimal_repro(
        finding, events, fixture="code_pipeline", task="fixtures/tasks/refactor_module.md"
    )
    return events, repro


# ---------------------------------------------------------------------------------------------
# The emitted document is a complete, real-validator-accepted v1 scenario
# ---------------------------------------------------------------------------------------------


def test_repro_yaml_is_accepted_by_the_real_scenario_validator() -> None:
    _, repro = _repro_for_code_pipeline()
    parsed = parse_scenario_text(repro.yaml_text, source_name=repro.path)
    errors = validate(parsed)
    assert errors == (), f"generated scenario failed real validation: {errors}"


def test_repro_yaml_round_trips_through_defaults_resolution() -> None:
    """`resolve_defaults` succeeds and infers the same fixture back out via `target.fixture`.

    `resolve_defaults` is the RESOLVE phase every scenario document goes through.
    """
    _, repro = _repro_for_code_pipeline()
    parsed = parse_scenario_text(repro.yaml_text, source_name=repro.path)
    assert parsed.data is not None
    resolved = resolve_defaults(parsed.data)
    assert resolved["target"] == {"fixture": "code_pipeline"}
    assert resolved["seed"] == 42
    assert resolved["assertions"] == ["no_state_conflicts"]


def test_repro_path_and_scenario_name_match_prd_convention() -> None:
    """PRD §14.8: "written to `scenarios/repro_<finding_id>.yaml`"."""
    _, repro = _repro_for_code_pipeline()
    assert repro.path == f"scenarios/repro_{repro.finding_id}.yaml"
    assert repro.scenario_name == f"repro_{repro.finding_id}"
    assert repro.finding_id == "f_draft_module_a_13_28"


def test_repro_pins_the_run_start_seed() -> None:
    _, repro = _repro_for_code_pipeline()
    assert repro.seed == 42  # tests/golden/code_pipeline.jsonl's own run_start.payload.seed


# ---------------------------------------------------------------------------------------------
# The `no_state_conflicts` assertion genuinely discriminates unfixed vs. fixed
# ---------------------------------------------------------------------------------------------


class _FakeRunSummary:
    """A minimal `RunSummary` (structural Protocol) carrying real `race.Finding` objects.

    Not a scenario execution (no scheduler exists to produce one, see `ReproScenario`'s
    docstring) - a direct, real evaluation of the exact assertion function
    `scenario.assertions.eval_no_state_conflicts` that the generated YAML's `assertions:` list
    names, against `race.py`'s own real output. This is the honest substitute for "run
    `agentdx run scenarios/repro_<id>.yaml`" that this build can actually perform.
    """

    def __init__(self, findings: Sequence[Finding]) -> None:
        self.run_id = "r_test"
        self.findings = findings
        self.faults_fired = 0
        self.success_check_passed: bool | None = None
        self.deterministic_replay_verified: bool | None = None

    def metric(self, name: str) -> float | str | None:
        return None


def test_no_state_conflicts_assertion_fails_today_against_the_real_finding() -> None:
    """Before the fix: the finding is present, so the assertion the repro ships fails."""
    events = _load_golden("code_pipeline")
    findings = detect_conflicts(events)
    fake_run = _FakeRunSummary(findings)
    # `race.Finding` structurally satisfies `scenario.assertions.Finding` (both Protocols check
    # only `type`/`severity`/`evidence_seq`, both duck-typed) - `_FakeRunSummary` really is a
    # `RunSummary` at runtime; this cast only tells mypy, whose strict Protocol-attribute
    # matching does not thread that structural compatibility through `Sequence[...]` itself.
    result = eval_no_state_conflicts(cast(RunSummary, fake_run))
    assert result.status == AssertionStatus.FAILED
    assert "f_draft_module_a_13_28" not in result.detail  # detail names seqs, not ids
    assert "13" in result.detail and "28" in result.detail


def test_no_state_conflicts_assertion_passes_once_the_race_is_fixed() -> None:
    """After the fix (simulated: an empty finding set), the same assertion passes."""
    result = eval_no_state_conflicts(cast(RunSummary, _FakeRunSummary(findings=())))
    assert result.status == AssertionStatus.PASSED


# ---------------------------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------------------------


def test_missing_run_start_raises() -> None:
    events = _load_golden("code_pipeline")
    (finding,) = detect_conflicts(events)
    events_without_run_start = [e for e in events if e.type.value != "run_start"]
    with pytest.raises(MissingRunSeedError) as excinfo:
        minimal_repro(
            finding,
            events_without_run_start,
            fixture="code_pipeline",
            task="fixtures/tasks/refactor_module.md",
        )
    assert excinfo.value.code == "E-RACE-005"
