"""Tests for `agentdx.scenario.matrix` — Design Constraint 3: deterministic expansion."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentdx.scenario.matrix import MatrixError, _parse_matrix_key, expand_matrix

_DOC = {
    "scenario": "kill_reviewer",
    "task": "fixtures/code_pipeline/t.md",
    "faults": [{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 3000}],
    "matrix": {"seed": [3, 1, 2], "faults[0].type": ["agent_crash", "tool_failure"]},
}


def test_no_matrix_key_expands_to_nothing() -> None:
    assert expand_matrix({"scenario": "x", "task": "t.md"}) == ()


def test_expansion_is_the_full_cross_product() -> None:
    expansions = expand_matrix(_DOC)
    assert len(expansions) == 6  # 3 seeds x 2 fault types
    ids = {e.scenario_id for e in expansions}
    assert len(ids) == 6, "every derived id must be unique"


def test_expansion_overrides_the_targeted_field_and_nothing_else() -> None:
    expansions = expand_matrix(_DOC)
    for e in expansions:
        assert e.document["faults"][0]["agent"] == "reviewer"  # untouched
        assert e.document["faults"][0]["at_virtual_ts"] == 3000  # untouched
        assert "matrix" not in e.document  # consumed, not carried forward


def test_bare_alias_keys_map_to_top_level_scalars() -> None:
    assert _parse_matrix_key("seed") == ("seed",)
    assert _parse_matrix_key("mode") == ("mode",)
    assert _parse_matrix_key("repeats") == ("repeats",)


def test_dotted_path_keys() -> None:
    assert _parse_matrix_key("guards.max_tokens") == ("guards", "max_tokens")
    assert _parse_matrix_key("faults[0].type") == ("faults", 0, "type")
    assert _parse_matrix_key("faults[2].agent") == ("faults", 2, "agent")


@pytest.mark.parametrize("bad_key", ["faults[abc]", "a..b", "guards.", "..seed"])
def test_malformed_matrix_keys_raise(bad_key: str) -> None:
    with pytest.raises(MatrixError):
        _parse_matrix_key(bad_key)


def test_prds_own_bare_fault_type_example_is_rejected_with_guidance() -> None:
    """PRD §21.5's own example key, `fault_type`, is not one of the three bare aliases.

    (module docstring explains why); it must still fail loudly with the dotted-path fix,
    either here or — since `fault_type` alone is syntactically a valid *bare* top-level
    path — one layer up, in `validate.validate()`'s unknown-top-level-key check. Both are
    exercised: this test proves the document-level failure mode.
    """
    doc = {"scenario": "x", "task": "t.md", "matrix": {"fault_type": ["agent_crash"]}}
    expansions = expand_matrix(doc)
    assert len(expansions) == 1
    # `fault_type` was written straight onto the document as an unknown top-level key —
    # validate.py's own unknown-key check (E-SCEN-002) is what actually rejects it.
    from agentdx.scenario import loader, validate

    resolved_doc = expansions[0].document
    text = loader.dump_resolved_yaml(resolved_doc)
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert any(e.code == "E-SCEN-002" and "fault_type" in e.path for e in errors)


def test_expansion_output_must_be_revalidated_by_the_caller() -> None:
    """Prove the caller-revalidation obligation an OP-2 audit found undocumented.

    `expand_matrix` does not call `validate.validate()` on its own output (confirmed by this
    module's docstring: expansion "runs independently of `validate.validate()`"). This test
    proves the contract both ways: a matrix substitution CAN produce an invalid document
    (switching `faults[0].type` from `agent_crash` to `tool_failure` leaves
    `agent`/`at_virtual_ts` on the entry, which `tool_failure` does not accept, and drops the
    required `tool` field), and re-running `validate.validate()` on the expansion's
    `.document` — exactly as `docs/scenario-reference.md`'s "Matrix expansion" section now
    instructs callers to do — catches it.
    """
    from agentdx.scenario import loader, validate

    expansions = expand_matrix(_DOC)
    tool_failure_expansions = [
        e for e in expansions if e.document["faults"][0]["type"] == "tool_failure"
    ]
    assert tool_failure_expansions, "the matrix substitution to tool_failure did not occur"

    text = loader.dump_resolved_yaml(tool_failure_expansions[0].document)
    parsed = loader.parse_scenario_text(text, source_name="t.yaml")
    errors = validate.validate(parsed)
    assert errors, "a matrix expansion that produces an invalid document must fail validation"


def test_expansion_is_deterministic_across_20_in_process_calls() -> None:
    def _dump() -> str:
        return json.dumps(
            [(e.scenario_id, e.document) for e in expand_matrix(_DOC)], sort_keys=True
        )

    first = _dump()
    for _ in range(20):
        assert _dump() == first


def test_expansion_is_byte_identical_across_20_fresh_subprocesses() -> None:
    """DEFINITION OF DONE says matrix expansion is byte-identical across 20 runs.

    Each run here is a genuinely fresh OS process (no shared caches, no import-order
    coincidences), not 20 in-process loop iterations — the stronger claim the prompt
    actually asks for.
    """
    src_root = str(Path(__file__).resolve().parents[3] / "src")
    script = (
        "import json\n"
        "from agentdx.scenario.matrix import expand_matrix\n"
        f"doc = {_DOC!r}\n"
        "exps = expand_matrix(doc)\n"
        "print(json.dumps([[e.scenario_id, e.document] for e in exps], sort_keys=True))\n"
    )
    outputs = set()
    for _ in range(20):
        # `sys.executable` is the trusted interpreter running this test suite, and `script`
        # is a fixed literal built above (not attacker- or environment-controlled input) —
        # this is a fixed, fully-trusted argv list, not untrusted-input execution.
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            env={"PYTHONPATH": src_root, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.add(completed.stdout)
    assert len(outputs) == 1, f"expansion differed across fresh processes: {outputs}"
