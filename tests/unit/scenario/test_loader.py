"""Tests for `agentdx.scenario.loader`: position tracking, `extends`, defaults, hashing."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentdx.scenario import loader


def test_position_tracking_locates_a_top_level_key() -> None:
    text = "scenario: x\ntask: t.md\nseed: 42\n"
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    assert parsed.source_map.key_lines[("seed",)] == 3
    assert parsed.source_map.value_lines[("seed",)] == 3


def test_position_tracking_locates_a_nested_key() -> None:
    text = "scenario: x\ntask: t.md\nfaults:\n  - type: agent_crash\n    agent: reviewer\n"
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    assert parsed.source_map.key_lines[("faults", 0, "agent")] == 5
    assert parsed.source_map.value_lines[("faults", 0, "agent")] == 5
    assert parsed.source_map.value_lines[("faults", 0)] == 4


def test_line_for_falls_back_to_the_nearest_known_ancestor() -> None:
    text = "scenario: x\ntask: t.md\nblast_radius:\n  agents: [a]\n"
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    # `blast_radius.tools` was never written — its line falls back to `blast_radius`'s own
    # mapping-value line (line 4, where its first real child `agents:` sits — a YAML block
    # mapping's node position is its first key, not its own `key:` line one line above).
    assert parsed.source_map.line_for(("blast_radius", "tools")) == 4


def test_empty_document_parses_to_none_data() -> None:
    parsed = loader.parse_scenario_text("# just a comment\n", source_name="t.yaml")
    assert parsed.data is None


def test_malformed_yaml_raises_e_scen_000_with_a_line() -> None:
    with pytest.raises(loader.ScenarioLoadError) as exc_info:
        loader.parse_scenario_text("scenario: x\n  bad: [unterminated\n", source_name="t.yaml")
    assert exc_info.value.code == "E-SCEN-000"


def test_non_mapping_root_raises_e_scen_000() -> None:
    with pytest.raises(loader.ScenarioLoadError) as exc_info:
        loader.parse_scenario_text("- a\n- b\n", source_name="t.yaml")
    assert exc_info.value.code == "E-SCEN-000"
    assert exc_info.value.line == 1


def test_infer_fixture_from_task_path() -> None:
    inferred = loader.infer_fixture_from_task_path("fixtures/code_pipeline/refactor_module.md")
    assert inferred == "code_pipeline"
    assert loader.infer_fixture_from_task_path("Fix the bug in normalise()") is None
    assert loader.infer_fixture_from_task_path("tasks/x.md") is None


@pytest.mark.parametrize("non_fixture_dir", ["tasks", "perturbations", "totally_made_up"])
def test_infer_fixture_from_task_path_declines_every_non_fixture_directory(
    non_fixture_dir: str,
) -> None:
    """C-13/C-15 regression coverage — a positive check, not a per-name denylist.

    The original C-13 fix (2026-08-16) denylisted only the single known name `"tasks"`; a
    second independent OP-2 proved this class of bug was still live against
    `fixtures/perturbations/` (a second real, shipped, non-fixture directory — confirmed on
    disk, no `graph.py`) and against any wholly fictional name, since neither was in the
    denylist. `infer_fixture_from_task_path` now checks `fixtures/<name>/graph.py` actually
    exists rather than maintaining a name list by hand, so this parametrisation is a proof of
    the mechanism, not just a record of the two names known at repair time.
    """
    task = f"fixtures/{non_fixture_dir}/some_task.md"
    assert loader.infer_fixture_from_task_path(task) is None
    assert loader.non_fixture_task_dir(task) == non_fixture_dir


@pytest.mark.parametrize("real_fixture", ["code_pipeline", "research_fanout", "support_triage"])
def test_infer_fixture_from_task_path_accepts_every_real_fixture(real_fixture: str) -> None:
    """The positive-check rewrite must not regress the happy path for any real fixture."""
    task = f"fixtures/{real_fixture}/some_task.md"
    assert loader.infer_fixture_from_task_path(task) == real_fixture
    assert loader.non_fixture_task_dir(task) is None


def test_load_success_check_timeout_s_reads_agentdx_toml() -> None:
    """Second independent OP-2 finding #6 regression coverage.

    `assertions.py` previously hardcoded `_SHELL_TIMEOUT_S = 5` as a bare literal — this
    proves the timeout is now sourced from `agentdx.toml`'s `[scenario]` section, matching
    PRD §21.6's own value (`success_check_timeout_s = 5`).
    """
    assert loader.load_success_check_timeout_s() == 5


def test_load_success_check_timeout_s_is_not_hardcoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the timeout genuinely tracks `agentdx.toml`, not a coincidental hardcoded match.

    Mirrors the independent OP-2's own verification method for `load_guard_defaults`
    (mutate the config, observe the returned value change) rather than trusting that
    `success_check_timeout_s = 5` in the real file and `_DEFAULT_SUCCESS_CHECK_TIMEOUT_S = 5`
    in code just happen to agree.
    """
    (tmp_path / "agentdx.toml").write_text("[scenario]\nsuccess_check_timeout_s = 47\n")
    monkeypatch.setattr(loader, "_repo_root", lambda: tmp_path)
    assert loader.load_success_check_timeout_s() == 47


def test_load_guard_defaults_reads_agentdx_toml() -> None:
    """D-41 follow-up regression coverage.

    `agentdx.toml`'s `guard_default_*` keys were written but never read —
    `schema.DEFAULT_GUARDS`, a second hardcoded copy, was used instead. This proves the
    config is now the actual source, not just present in the file.
    """
    defaults = loader.load_guard_defaults()
    assert defaults["max_tokens"] == 200_000  # agentdx.toml's guard_default_max_tokens
    assert defaults["max_virtual_duration_ms"] == 120_000


def test_resolve_defaults_guards_come_from_load_guard_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove `guards` is no longer sourced from the frozen `DEFAULTS` constant alone.

    Changing what `load_guard_defaults()` returns changes `resolve_defaults`'s guards.
    """
    monkeypatch.setattr(loader, "load_guard_defaults", lambda: {"max_tokens": 12345})
    resolved = loader.resolve_defaults({"scenario": "x", "task": "t.md"})
    assert resolved["guards"]["max_tokens"] == 12345


def test_resolve_defaults_fills_missing_sections_without_clobbering_explicit_values() -> None:
    resolved = loader.resolve_defaults({"scenario": "x", "task": "t.md", "seed": 7})
    assert resolved["seed"] == 7  # explicit value preserved
    assert resolved["mode"] == "replay"  # defaulted
    assert resolved["guards"]["max_tokens"] == 200_000  # defaulted, nested
    assert resolved["blast_radius"] == {
        "agents": [],
        "tools": [],
        "edges": [],
        "state_keys": [],
        "providers": [],
    }


def test_resolve_defaults_infers_target_from_task_path() -> None:
    resolved = loader.resolve_defaults({"scenario": "x", "task": "fixtures/code_pipeline/t.md"})
    assert resolved["target"] == {"fixture": "code_pipeline"}


def test_resolve_defaults_does_not_override_an_explicit_target() -> None:
    data = {
        "scenario": "x",
        "task": "fixtures/code_pipeline/t.md",
        "target": {"fixture": "support_triage"},
    }
    resolved = loader.resolve_defaults(data)
    assert resolved["target"] == {"fixture": "support_triage"}


def test_agent_crash_fault_gets_its_own_defaults() -> None:
    data = {
        "scenario": "x",
        "task": "t.md",
        "faults": [{"type": "agent_crash", "agent": "r", "at_virtual_ts": 1}],
    }
    resolved = loader.resolve_defaults(data)
    fault = resolved["faults"][0]
    assert fault["recoverable"] is True
    assert fault["allow_total_failure"] is False


def test_extends_deep_merges_and_replaces_lists_not_concatenates(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text(
        "scenario: base\ntask: fixtures/code_pipeline/t.md\nguards:\n  max_tokens: 100000\n"
        "  max_retries: 5\nfaults:\n  - type: latency\n    edge: 'a->b'\n"
        "    delay_ms: 10\n    always: true\n",
        encoding="utf-8",
    )
    child = tmp_path / "child.yaml"
    child.write_text(
        f"extends: {base.name}\nscenario: child\nguards:\n  max_tokens: 50000\n"
        "faults:\n  - type: agent_crash\n    agent: reviewer\n    at_virtual_ts: 1\n",
        encoding="utf-8",
    )
    parsed = loader.load_scenario_file(child)
    merged = loader.resolve_extends(parsed)
    assert merged.data is not None
    assert merged.data["guards"]["max_tokens"] == 50000  # child overrides
    assert merged.data["guards"]["max_retries"] == 5  # inherited from base
    assert merged.data["scenario"] == "child"
    # lists are replaced wholesale, not concatenated (PRD §21.5) — only the child's fault remains
    assert len(merged.data["faults"]) == 1
    assert merged.data["faults"][0]["type"] == "agent_crash"


def test_extends_cycle_is_rejected(tmp_path: Path) -> None:
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text(f"extends: {b.name}\nscenario: a\ntask: t.md\n", encoding="utf-8")
    b.write_text(f"extends: {a.name}\nscenario: b\ntask: t.md\n", encoding="utf-8")
    with pytest.raises(loader.ScenarioLoadError) as exc_info:
        loader.resolve_extends(loader.load_scenario_file(a))
    assert exc_info.value.code == "E-SCEN-006"


def test_extends_missing_file_is_e_scen_006(tmp_path: Path) -> None:
    child = tmp_path / "child.yaml"
    child.write_text("extends: does_not_exist.yaml\nscenario: c\ntask: t.md\n", encoding="utf-8")
    with pytest.raises(loader.ScenarioLoadError) as exc_info:
        loader.resolve_extends(loader.load_scenario_file(child))
    assert exc_info.value.code == "E-SCEN-006"


def test_scenario_hash_is_stable_regardless_of_source_key_order() -> None:
    a = loader.compute_scenario_hash({"scenario": "x", "seed": 1, "mode": "replay"})
    b = loader.compute_scenario_hash({"mode": "replay", "scenario": "x", "seed": 1})
    assert a == b


def test_scenario_hash_changes_when_content_changes() -> None:
    a = loader.compute_scenario_hash({"scenario": "x", "seed": 1})
    b = loader.compute_scenario_hash({"scenario": "x", "seed": 2})
    assert a != b


def test_dump_resolved_yaml_round_trips_through_the_loader() -> None:
    data = {"scenario": "x", "task": "fixtures/code_pipeline/t.md", "seed": 5}
    resolved = loader.resolve_defaults(data)
    text = loader.dump_resolved_yaml(resolved)
    reparsed = loader.parse_scenario_text(text, source_name="roundtrip.yaml")
    assert reparsed.data == resolved
