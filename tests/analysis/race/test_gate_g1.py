"""Gate G1: `agentdx run fixtures/code_pipeline --assert findings.race >= 1` (`CONTEXT.md` §6).

No `agentdx run` CLI exists yet (P17, `NOT STARTED` — out of this prompt's scope), so this test
is the gate's real, direct equivalent: `detect_conflicts` against the actual, committed
`tests/golden/code_pipeline.jsonl` (the real stamped replay of `fixtures/code_pipeline`, not a
hand-authored stand-in), asserting exactly what the gate requires — at least one `lost_update`
on `draft.module_a`, severity `critical`, naming both writers and both seqs — and printing the
finding in PRD §14.9's reporting format so `pytest -s`'s captured output *is* the "paste
verbatim" evidence the gate's definition of done asks for.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentdx.analysis.race import detect_conflicts, format_finding
from agentdx.events.canonical import decode_event

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GOLDEN_LOG = _REPO_ROOT / "tests" / "golden" / "code_pipeline.jsonl"
_GOLDEN_FINDINGS = _REPO_ROOT / "fixtures" / "code_pipeline" / "golden_findings.json"


def test_gate_g1_code_pipeline_has_at_least_one_lost_update_on_draft_module_a() -> None:
    """Gate G1. Run with `pytest tests/analysis/race/test_gate_g1.py -q -s` to see the report."""
    with _GOLDEN_LOG.open() as fh:
        events = [decode_event(line) for line in fh]
    golden = json.loads(_GOLDEN_FINDINGS.read_text())["findings"][0]

    findings = detect_conflicts(events)

    # the gate's own literal condition: findings.race >= 1
    assert len(findings) >= 1, "gate G1 FAILED: detect_conflicts found zero races"

    matching = [f for f in findings if f.subtype == "write_write" and f.key == "draft.module_a"]
    assert matching, (
        "gate G1 FAILED: no lost_update (write_write) finding on draft.module_a; got "
        f"{[(f.subtype, f.key) for f in findings]}"
    )
    finding = matching[0]

    # Cross-checked against the fixture's own committed golden_findings.json, not just
    # hand-restated numbers - a second, independent source for every value asserted below.
    assert finding.severity == golden["severity"] == "critical"
    assert (
        {finding.agent_a, finding.agent_b}
        == set(golden["evidence"]["writers"])
        == {
            "coder",
            "reviewer",
        }
    )
    assert finding.surviving_seq == golden["evidence"]["surviving_write_seq"] == 28
    assert finding.discarded_seq == golden["evidence"]["discarded_write_seq"] == 13
    assert finding.evidence_seq == (13, 28)  # both racing writes (I6); see note below
    # golden_findings.json's own `evidence.seq` additionally names seq 36 (tester's read that
    # only observed the surviving value) as narrative context for the human reader — that is
    # not part of *this* write_write Conflict's own evidence (`Conflict.evidence_seq` is
    # exactly the two racing accesses, PRD §14.5), so it is deliberately not asserted here.
    assert finding.seq_a != finding.seq_b
    assert finding.evidence_seq  # I6: non-empty evidence
    assert finding.torn is False
    assert finding.fault_id is None

    print()  # noqa: T201 - separates from pytest's own -s banner
    print("GATE G1 — agentdx run fixtures/code_pipeline --assert findings.race >= 1")  # noqa: T201
    print(format_finding(finding))  # noqa: T201
