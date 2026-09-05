"""`verdict_rules.toml` round-trips and prints (Definition of Done; CONTEXT.md §3 locked decision).

Two separate loaders read this one file: `verdict.load_verdict_rules()` (the `[verdict.*]`
tables) and `resilience.load_resilience_rules()` (`[resilience]`/`[resilience.
degradation_weights]`) and `baseline`'s two private loaders (`[comparability]`/`[baseline]`).
This file checks all four against the same on-disk bytes, so a future edit to the committed
TOML that any one loader silently stops honouring is caught here, not discovered downstream in
a verdict/resilience/comparability test that happens to use a value the edit didn't touch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agentdx.analysis.verdict as verdict_module
from agentdx.analysis.resilience import DegradationClass, load_resilience_rules
from agentdx.analysis.verdict import DuplicateThresholdKeyError, format_rules, load_verdict_rules

_RULES_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "agentdx" / "analysis" / "verdict_rules.toml"
)


def test_the_committed_file_exists_and_is_readable() -> None:
    assert _RULES_PATH.is_file()
    assert _RULES_PATH.read_text(encoding="utf-8")


def test_load_verdict_rules_raw_is_byte_identical_to_the_committed_file() -> None:
    rules = load_verdict_rules()
    assert rules.raw == _RULES_PATH.read_text(encoding="utf-8")


def test_format_rules_prints_the_verbatim_committed_text() -> None:
    """CONTEXT.md §3's "versioned, printable via `agentdx analyze --explain`," satisfied.

    This is the byte-identical passthrough `--explain` (P17, not yet built) will call.
    """
    rules = load_verdict_rules()
    printed = format_rules(rules)
    assert printed == _RULES_PATH.read_text(encoding="utf-8")
    assert "schema_version = 1" in printed
    assert "[verdict.classes]" in printed
    assert "[resilience]" in printed


def test_load_verdict_rules_values_match_the_committed_toml_verbatim() -> None:
    """A spot-check against the exact numbers committed in the file.

    Catches a loader/file drift a byte-identity check on `.raw` alone would not (the raw text
    always matches itself; only the *parsed* values can silently diverge from what the loader
    actually returns).
    """
    rules = load_verdict_rules()
    assert rules.beneficial_min_speedup == 1.15
    assert rules.neutral_min_speedup == 0.95
    assert rules.coordination_bottleneck_edge_cp_share == 0.40
    assert rules.coordination_bottleneck_agent_cp_share == 0.60
    assert rules.unreliable_topology_max_resilience == 60
    assert rules.insufficient_data_min_agents == 2
    assert rules.insufficient_data_min_spans == 5
    assert rules.insufficient_data_max_residual_fraction == 0.20
    assert rules.speedup_weight == 40
    assert rules.efficiency_weight == 25
    assert rules.reliability_weight == 25
    assert rules.conflict_penalty_per_finding == 10
    assert rules.conflict_penalty_max == 25
    assert rules.high_max_residual_fraction == 0.02
    assert rules.medium_max_residual_fraction == 0.05
    assert rules.medium_max_instrumentation_gaps == 2
    assert rules.merge_edge_cp_share == 0.40
    assert rules.token_cost_multiplier_recommend == 3.0


def test_load_resilience_rules_values_match_the_committed_toml_verbatim() -> None:
    rules = load_resilience_rules()
    assert rules.recovery_budget_multiplier == 2.0
    assert rules.amplification_budget == 4.0
    assert rules.success_ratio_weight == 0.50
    assert rules.recovery_weight == 0.20
    assert rules.amplification_weight == 0.15
    assert rules.degradation_weight == 0.15
    assert rules.silent_failure_cap == 49
    assert rules.degradation_weights[DegradationClass.GRACEFUL] == 1.0
    assert rules.degradation_weights[DegradationClass.DEGRADED_FLAGGED] == 0.6
    assert rules.degradation_weights[DegradationClass.HARD_FAILURE] == 0.4
    assert rules.degradation_weights[DegradationClass.SILENT_FAILURE] == 0.0


def test_the_committed_files_duplicate_key_agrees_and_does_not_raise() -> None:
    """The real, committed file's duplicate key must load cleanly, not raise.

    `coordination_bottleneck_edge_cp_share` is declared in both `[verdict.classes]` and
    `[verdict.severity]` — the real, committed file has both at `0.40` today, so
    `load_verdict_rules()` must load cleanly (see the next test for the disagreeing-values case).
    """
    rules = load_verdict_rules()  # must not raise
    assert rules.coordination_bottleneck_edge_cp_share == 0.40


def test_load_verdict_rules_raises_when_a_duplicate_key_disagrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OP-2 second-pass finding #5 (`op2-audit-p11-second.md`).

    `coordination_bottleneck_edge_cp_share` is declared under both `[verdict.classes]` and
    `[verdict.severity]` — one flat `VerdictRules` field for a key present in two subtables. If
    the two ever disagree, `load_verdict_rules` must fail loudly rather than let whichever
    subtable is merged last silently win.
    """
    bad_toml = tmp_path / "verdict_rules.toml"
    bad_toml.write_text(
        "schema_version = 1\n"
        "[verdict.classes]\n"
        "coordination_bottleneck_edge_cp_share = 0.40\n"
        "[verdict.severity]\n"
        "coordination_bottleneck_edge_cp_share = 0.90\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(verdict_module, "_find_rules_path", lambda: bad_toml)

    with pytest.raises(DuplicateThresholdKeyError) as excinfo:
        verdict_module.load_verdict_rules()
    assert excinfo.value.code == "E-VERD-002"
    assert "coordination_bottleneck_edge_cp_share" in str(excinfo.value)
    assert "0.4" in str(excinfo.value)
    assert "0.9" in str(excinfo.value)


def test_schema_version_is_present_and_an_integer() -> None:
    """`verdict_rules.toml` is itself versioned (Design Constraint 4).

    `schema_version` is the field a future migration would check, so its presence and type
    are asserted directly.
    """
    import tomllib

    data = tomllib.loads(_RULES_PATH.read_text(encoding="utf-8"))
    assert isinstance(data.get("schema_version"), int)
    assert data["schema_version"] == 1
