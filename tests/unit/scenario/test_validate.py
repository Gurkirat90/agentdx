"""Tests for `agentdx.scenario.validate` — PRD §21.3's validation rules.

`test_ten_invalid_scenarios` is this prompt's DEFINITION OF DONE item: "Ten deliberately
invalid scenarios each produce an error with the correct line number and a useful
suggestion." Each case names the exact code and line it expects, so a regression that moves
an error to the wrong line fails loudly here rather than in a human's read of pasted output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentdx.scenario import loader, validate

_SOURCE_SCENARIO_LITERAL = """\
scenario: reviewer_crash_midflight
task: fixtures/tasks/refactor_module.md
seed: 42
hypothesis:
  task_success: ">= 0.9"
  p95_virtual_duration_ms: "<= 45000"
faults:
  - type: agent_crash
    agent: reviewer
    at_virtual_ts: 2400
guards:
  max_virtual_duration_ms: 120000
  max_tokens: 200000
assertions:
  - no_state_conflicts
  - speedup_vs_baseline: ">= 1.0"
"""
"""PRD §21.2's `[SOURCE]` block, character-for-character. `fixtures/tasks/refactor_module.md`
is a real file (`fixtures/tasks/`, not a fixture directory — a shared task-description
directory referenced by multiple fixtures), so this text has no `target:` key and no way for
`infer_fixture_from_task_path` to guess which fixture it belongs to. Before the P08 OP-3
repair (2026-08-16, C-13), this silently inferred the bogus `target.fixture: "tasks"` with
zero validation errors — an independent OP-2 audit caught it live against this exact text.
"""

_SOURCE_SCENARIO_WITH_TARGET = _SOURCE_SCENARIO_LITERAL.replace(
    "scenario: reviewer_crash_midflight\n",
    "scenario: reviewer_crash_midflight\ntarget: {fixture: code_pipeline}\n",
)
"""The `[SOURCE]` text plus the explicit `target:` PRD §21.2's own note allows ("... or
supplied by `--fixture`") for exactly this case: a task under the shared `fixtures/tasks/`
directory, whose owning fixture (`code_pipeline`, the same one `kill_reviewer.yaml` and the
`reviewer` agent it targets both live in) cannot be inferred from the path alone."""


def test_prd_21_2_source_scenario_literal_text_requires_explicit_target() -> None:
    """C-13: PRD §21.2's `[SOURCE]` text, unedited, does not validate without an explicit target.

    `fixtures/tasks/` is a shared directory, not a fixture, and inference correctly declines
    to guess rather than silently resolving to `{fixture: "tasks"}` (the bug an OP-2 audit
    found live in this exact text on 2026-08-16). This test exists so it cannot return.
    """
    parsed = loader.parse_scenario_text(
        _SOURCE_SCENARIO_LITERAL, source_name="reviewer_crash_midflight.yaml"
    )
    errors = validate.validate(parsed)
    assert len(errors) == 1
    assert errors[0].code == "E-SCEN-003"
    assert "fixtures/tasks/" in errors[0].message

    resolved = loader.resolve_defaults(parsed.data)
    assert "target" not in resolved  # never silently resolved to a bogus fixture


def test_prd_21_2_source_scenario_validates_with_explicit_target() -> None:
    """The `[SOURCE]` block plus the explicit `target:` its own note allows validates clean.

    The corrected form of the claim CONTEXT.md's P08 DoD makes.
    """
    parsed = loader.parse_scenario_text(
        _SOURCE_SCENARIO_WITH_TARGET, source_name="reviewer_crash_midflight.yaml"
    )
    errors = validate.validate(parsed)
    assert errors == (), "\n".join(str(e) for e in errors)

    resolved = loader.resolve_defaults(parsed.data)
    assert resolved["version"] == 1
    assert resolved["target"] == {"fixture": "code_pipeline"}


# ---------------------------------------------------------------------------------------
# `target.fixture` existence check (C-15, `E-SCEN-012`, second independent OP-2 finding #3
# extension, 2026-08-16) — before this, neither an inferred nor an explicit `target.fixture`
# naming a nonexistent fixture (a typo, or wholly fictional) was ever caught at validation
# time, directly contradicting PRD §21.3's own stated rationale ("failing after a 40-second
# run because of a typo is unacceptable in CI").
# ---------------------------------------------------------------------------------------


def test_explicit_target_fixture_that_does_not_exist_is_rejected() -> None:
    text = "scenario: x\ntask: t.md\ntarget: {fixture: this_fixture_absolutely_does_not_exist}\n"
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(
        e.code == "E-SCEN-012" and "this_fixture_absolutely_does_not_exist" in e.message
        for e in errors
    ), errors


def test_explicit_target_fixture_that_exists_is_accepted() -> None:
    text = "scenario: x\ntask: t.md\ntarget: {fixture: code_pipeline}\n"
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert not any(e.code == "E-SCEN-012" for e in errors), errors


def test_target_graph_is_never_checked_for_fixture_existence() -> None:
    """`E-SCEN-012` is scoped to `target.fixture` only.

    `target.graph` is user-owned and resolved best-effort elsewhere
    (`resolve_graph_identity`); an unresolvable `graph:` path must not raise `E-SCEN-012`,
    which would wrongly treat a legitimate user graph as if it had to be one of this
    project's own shipped fixtures.
    """
    text = 'scenario: x\ntask: t.md\ntarget: {graph: "./nonexistent_app.py:graph"}\n'
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert not any(e.code == "E-SCEN-012" for e in errors), errors


def test_shipped_scenarios_validate(repo_scenarios_dir: Path) -> None:
    for name in ("reviewer_crash_midflight.yaml", "kill_reviewer.yaml"):
        parsed = loader.load_scenario_file(repo_scenarios_dir / name)
        errors = validate.validate(parsed)
        assert errors == (), f"{name}:\n" + "\n".join(str(e) for e in errors)


def test_kill_reviewer_targets_reviewer_at_3000(repo_scenarios_dir: Path) -> None:
    """Gate G4's own precondition: the shipped file actually crashes `reviewer` at t=3000."""
    parsed = loader.load_scenario_file(repo_scenarios_dir / "kill_reviewer.yaml")
    fault = parsed.data["faults"][0]
    assert fault["type"] == "agent_crash"
    assert fault["agent"] == "reviewer"
    assert fault["at_virtual_ts"] == 3000


# ---------------------------------------------------------------------------------------
# DEFINITION OF DONE: ten deliberately invalid scenarios
# ---------------------------------------------------------------------------------------

_INVALID_CASES: dict[str, tuple[str, int, str]] = {
    "unknown_top_level_key": (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\nchaso_opt_in: true\n",
        "E-SCEN-002",
        3,
    ),
    "unsupported_version": (
        "version: 2\nscenario: x\ntask: fixtures/code_pipeline/t.md\n",
        "E-SCEN-001",
        1,
    ),
    "both_target_kinds": (
        "scenario: x\ntask: t.md\ntarget:\n  fixture: code_pipeline\n  graph: './app.py:g'\n",
        "E-SCEN-003",
        3,
    ),
    "fault_target_not_in_graph": (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\nfaults:\n  - type: agent_crash\n"
        "    agent: revieweer\n    at_virtual_ts: 100\n",
        "E-SCEN-005",
        5,
    ),
    "guard_above_ceiling": (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\nguards:\n  max_tokens: 99999999999\n",
        "E-SCEN-008",
        4,
    ),
    "success_check_ref_not_importable": (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\nsuccess_check:\n  type: python\n"
        "  ref: 'fixtures.code_pipeline.checks:does_not_exist'\n",
        "E-SCEN-009",
        5,
    ),
    "missing_required_scenario_key": (
        "task: fixtures/code_pipeline/t.md\n",
        "E-SCEN-010",
        1,
    ),
    "malformed_hypothesis_comparison": (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "hypothesis:\n  task_success: 'not-a-comparison'\n",
        "E-SCEN-011",
        4,
    ),
    "unmeasurable_assertion": (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "assertions:\n  - resilience_score: '>= 70'\n",
        "E-SCEN-007",
        4,
    ),
    "wrong_type_for_seed": (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\nseed: 'forty-two'\n",
        "E-SCEN-011",
        3,
    ),
}


@pytest.mark.parametrize("case_name", sorted(_INVALID_CASES))
def test_ten_invalid_scenarios(case_name: str) -> None:
    text, expected_code, expected_line = _INVALID_CASES[case_name]
    parsed = loader.parse_scenario_text(text, source_name=f"{case_name}.yaml")
    errors = validate.validate(parsed)
    matching = [e for e in errors if e.code == expected_code]
    assert matching, f"{case_name}: expected {expected_code}, got {[e.code for e in errors]}"
    found = matching[0]
    line_msg = f"{case_name}: expected line {expected_line}, got {found.line}"
    assert found.line == expected_line, line_msg
    assert found.suggestion.strip(), f"{case_name}: empty suggestion"


def test_ten_invalid_scenarios_is_actually_ten() -> None:
    assert len(_INVALID_CASES) == 10


# ---------------------------------------------------------------------------------------
# I12 / E-SCEN-004 — the safety gate for chaos
# ---------------------------------------------------------------------------------------


def test_fault_on_user_graph_with_no_blast_radius_fails_validation() -> None:
    text = (
        "scenario: x\ntask: t.md\ntarget:\n  graph: './app.py:g'\nchaos_opt_in: true\n"
        "faults:\n  - type: agent_crash\n    agent: worker\n    at_virtual_ts: 100\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    codes = [e.code for e in errors]
    assert "E-SCEN-004" in codes


def test_fault_on_user_graph_with_no_chaos_opt_in_fails_validation() -> None:
    text = (
        "scenario: x\ntask: t.md\ntarget:\n  graph: './app.py:g'\n"
        "blast_radius:\n  agents: [worker]\nfaults:\n  - type: agent_crash\n"
        "    agent: worker\n    at_virtual_ts: 100\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-004" for e in errors)


def test_fault_on_user_graph_with_chaos_opt_in_and_blast_radius_passes_i12() -> None:
    text = (
        "scenario: x\ntask: t.md\ntarget:\n  graph: './app.py:g'\nchaos_opt_in: true\n"
        "blast_radius:\n  agents: [worker]\nfaults:\n  - type: agent_crash\n"
        "    agent: worker\n    at_virtual_ts: 100\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert not any(e.code == "E-SCEN-004" for e in errors), errors


def test_fault_on_fixture_never_needs_chaos_opt_in() -> None:
    """PRD §13.3: "Faults against a fixture: permitted by default"."""
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: agent_crash\n    agent: reviewer\n    at_virtual_ts: 100\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert not any(e.code == "E-SCEN-004" for e in errors), errors


def test_fault_target_outside_declared_blast_radius_is_rejected() -> None:
    text = (
        "scenario: x\ntask: t.md\ntarget:\n  graph: './app.py:g'\nchaos_opt_in: true\n"
        "blast_radius:\n  agents: [other]\nfaults:\n  - type: agent_crash\n"
        "    agent: worker\n    at_virtual_ts: 100\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-004" and "worker" in e.message for e in errors)


def test_state_key_blast_radius_matches_by_glob() -> None:
    text = (
        "scenario: x\ntask: t.md\ntarget:\n  graph: './app.py:g'\nchaos_opt_in: true\n"
        "blast_radius:\n  state_keys: ['draft.*']\nfaults:\n  - type: state_corrupt\n"
        "    state_key: draft.module_a\n    on_state_write: draft.module_a\n    mutation: drop\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert not any(e.code == "E-SCEN-004" for e in errors), errors


# ---------------------------------------------------------------------------------------
# E-SCEN-005 target-kind coverage (OP-3 repair, 2026-08-16) — AGENT/TOOL were already
# checked; EDGE was not, even though `graph_identity.edges` was already collected and used
# by `critical_path_share`. An independent OP-2 audit proved the gap live against a
# fabricated edge on the real `code_pipeline` fixture; these are the regression tests.
# ---------------------------------------------------------------------------------------


def test_fault_target_edge_not_in_graph_is_rejected() -> None:
    """A `message_drop` fault against a nonexistent edge on a real fixture graph is E-SCEN-005."""
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: message_drop\n    edge: 'nonexistent->alsofake'\n"
        "    always: true\n    probability_permille: 500\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-005" for e in errors), errors


def test_fault_target_edge_present_in_graph_is_accepted() -> None:
    """The positive case: a real edge (`planner->coder`) on `code_pipeline` is not E-SCEN-005."""
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: message_drop\n    edge: 'planner->coder'\n"
        "    always: true\n    probability_permille: 500\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert not any(e.code == "E-SCEN-005" for e in errors), errors


# ---------------------------------------------------------------------------------------
# Fault parameter and trigger *value* validation (OP-3 repair, 2026-08-16) — before this,
# `_check_faults` validated parameter and trigger *names* only (`err.check_unknown_keys`),
# never their values. An OP-2 audit proved `at_virtual_ts: "not-a-timestamp"`,
# `recoverable: "yes-please"`, `window: 999999`, `copies: 999999` and out-of-range
# `probability_permille` all validated with zero errors; these are the regression tests for
# PRD §12.2's Safety-row bounds and the implied parameter types.
# ---------------------------------------------------------------------------------------


def test_message_reorder_window_above_safety_bound_is_rejected() -> None:
    """PRD §12.2 Safety row: `message_reorder`'s `window <= 16`."""
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: message_reorder\n    edge: 'planner->coder'\n"
        "    always: true\n    window: 999999\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-011" and "window" in e.message for e in errors), errors


def test_message_duplicate_copies_above_safety_bound_is_rejected() -> None:
    """PRD §12.2 Safety row: `message_duplicate`'s `copies <= 5`."""
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: message_duplicate\n    edge: 'planner->coder'\n"
        "    always: true\n    copies: 999999\n    probability_permille: 500\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-011" and "copies" in e.message for e in errors), errors


def test_agent_slow_factor_above_safety_bound_is_rejected() -> None:
    """PRD §12.2 Safety row: `agent_slow`'s `factor <= 100`, i.e. `factor_milli <= 100_000`."""
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: agent_slow\n    agent: reviewer\n"
        "    always: true\n    factor_milli: 999999999\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-011" and "factor_milli" in e.message for e in errors), errors


def test_probability_permille_out_of_range_is_rejected() -> None:
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: message_drop\n    edge: 'planner->coder'\n"
        "    always: true\n    probability_permille: 99999999\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-011" and "probability_permille" in e.message for e in errors), (
        errors
    )


# ---------------------------------------------------------------------------------------
# Exact-boundary regression tests (second independent OP-2 finding, 2026-08-16) — the four
# tests above each proved *some* absurdly large value is rejected, but none of them proved
# the *real* PRD ceiling is what's enforced: the auditor demonstrated live that monkeypatching
# `window`'s maximum from 16 down to a wrong 6 still leaves `test_..._is_rejected` passing,
# because 999999 is bigger than either number. These pair every PRD-numeric-bound fault
# parameter with a ceiling-passes / ceiling-plus-one-fails test, which a wrong ceiling cannot
# survive.
# ---------------------------------------------------------------------------------------


def test_message_reorder_window_at_ceiling_passes_one_above_fails() -> None:
    """PRD §12.2 Safety row: `message_reorder`'s `window <= 16`, boundary exact."""

    def errors_for(window: int) -> tuple[str, ...]:
        text = (
            "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
            "faults:\n  - type: message_reorder\n    edge: 'planner->coder'\n"
            f"    always: true\n    window: {window}\n"
        )
        parsed = loader.parse_scenario_text(text, source_name="t.yaml")
        return tuple(e.code for e in validate.validate(parsed) if e.code == "E-SCEN-011")

    assert errors_for(16) == ()
    assert errors_for(17) == ("E-SCEN-011",)


def test_message_duplicate_copies_at_ceiling_passes_one_above_fails() -> None:
    """PRD §12.2 Safety row: `message_duplicate`'s `copies <= 5`, boundary exact."""

    def errors_for(copies: int) -> tuple[str, ...]:
        text = (
            "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
            "faults:\n  - type: message_duplicate\n    edge: 'planner->coder'\n"
            f"    always: true\n    copies: {copies}\n    probability_permille: 500\n"
        )
        parsed = loader.parse_scenario_text(text, source_name="t.yaml")
        return tuple(e.code for e in validate.validate(parsed) if e.code == "E-SCEN-011")

    assert errors_for(5) == ()
    assert errors_for(6) == ("E-SCEN-011",)


def test_agent_slow_factor_milli_at_ceiling_passes_one_above_fails() -> None:
    """PRD §12.2 Safety row: `agent_slow`'s `factor <= 100`, i.e. `factor_milli <= 100_000`."""

    def errors_for(factor_milli: int) -> tuple[str, ...]:
        text = (
            "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
            "faults:\n  - type: agent_slow\n    agent: reviewer\n"
            f"    always: true\n    factor_milli: {factor_milli}\n"
        )
        parsed = loader.parse_scenario_text(text, source_name="t.yaml")
        return tuple(e.code for e in validate.validate(parsed) if e.code == "E-SCEN-011")

    assert errors_for(100_000) == ()
    assert errors_for(100_001) == ("E-SCEN-011",)


def test_probability_permille_at_ceiling_passes_one_above_fails() -> None:
    """PRD §12.2: every `probability_permille` field is `0 <= x <= 1000`, boundary exact."""

    def errors_for(probability_permille: int) -> tuple[str, ...]:
        text = (
            "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
            "faults:\n  - type: message_drop\n    edge: 'planner->coder'\n"
            f"    always: true\n    probability_permille: {probability_permille}\n"
        )
        parsed = loader.parse_scenario_text(text, source_name="t.yaml")
        return tuple(e.code for e in validate.validate(parsed) if e.code == "E-SCEN-011")

    assert errors_for(1000) == ()
    assert errors_for(1001) == ("E-SCEN-011",)


def test_agent_crash_recoverable_wrong_type_is_rejected() -> None:
    """The exact bug an OP-2 audit demonstrated live: `recoverable: "yes-please"`."""
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: agent_crash\n    agent: reviewer\n"
        "    at_virtual_ts: 100\n    recoverable: yes-please\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-011" and "recoverable" in e.message for e in errors), errors


def test_at_virtual_ts_trigger_wrong_type_is_rejected() -> None:
    """The exact bug an OP-2 audit demonstrated live: `at_virtual_ts: "not-a-timestamp"`."""
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: agent_crash\n    agent: reviewer\n"
        "    at_virtual_ts: not-a-timestamp\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-011" and "at_virtual_ts" in e.message for e in errors), errors


def test_tool_failure_mode_choices_enforced() -> None:
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: tool_failure\n    tool: lint\n"
        "    always: true\n    mode: not-a-real-mode\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-011" and "mode" in e.message for e in errors), errors


# ---------------------------------------------------------------------------------------
# `tool_failure` trigger vocabulary fix (C-14, OP-3 repair 2026-08-16) — `AT_VIRTUAL_TS`
# (matching PRD §12.2's `after_virtual_ts`) is now accepted; `AFTER_N_MESSAGES` (which was
# substituted in without ever being cross-checked, and has no clear meaning for a
# tool-targeted fault) is no longer part of `tool_failure`'s trigger vocabulary.
# ---------------------------------------------------------------------------------------


def test_tool_failure_accepts_at_virtual_ts_trigger() -> None:
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: tool_failure\n    tool: lint\n"
        "    at_virtual_ts: 100\n    mode: timeout\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert errors == (), errors


def test_tool_failure_rejects_after_n_messages_trigger() -> None:
    text = (
        "scenario: x\ntask: fixtures/code_pipeline/t.md\n"
        "faults:\n  - type: tool_failure\n    tool: lint\n"
        "    after_n_messages: 3\n    mode: timeout\n"
    )
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-011" for e in errors), errors


# ---------------------------------------------------------------------------------------
# Unknown-key suggestions
# ---------------------------------------------------------------------------------------


def test_unknown_key_suggests_the_nearest_known_key() -> None:
    text = "scenario: x\ntask: fixtures/code_pipeline/t.md\nchaso_opt_in: true\n"
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    error = next(e for e in errors if e.code == "E-SCEN-002")
    assert "chaos_opt_in" in error.suggestion


def test_error_str_includes_file_line_code_and_suggestion() -> None:
    text = "version: 2\nscenario: x\ntask: fixtures/code_pipeline/t.md\n"
    parsed = loader.parse_scenario_text(text, source_name="my_scenario.yaml")
    errors = validate.validate(parsed)
    rendered = str(errors[0])
    assert "my_scenario.yaml:1" in rendered
    assert "E-SCEN-001" in rendered
    assert "suggestion:" in rendered
