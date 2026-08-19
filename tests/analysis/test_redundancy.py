"""Unit coverage for `agentdx.analysis.redundancy` over the real golden fixtures.

PRD §16.3's exact-hash grouping, the qualifying-pair rule, and `wasted_virtual_ms`'s formula
— exercised against `tests/golden/*.jsonl` rather than more hand-authored logs, since the
fixtures already contain a genuine cross-agent duplicate-retrieval case (`support_triage`'s
`vector_search`) and a genuine same-tool-different-args case (`code_pipeline`'s `read_file`
calls, which are *not* grouped together because their `args_hash`es differ) — real data this
build ships, not invented scenarios.
"""

from __future__ import annotations

from pathlib import Path

from agentdx.analysis.redundancy import detect_redundancy, duplicate_node_ids
from agentdx.analysis.timing import build_timing_dag
from agentdx.events.canonical import decode_event
from agentdx.events.schema import Event

_GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"


def _load(name: str) -> list[Event]:
    path = _GOLDEN_DIR / f"{name}.jsonl"
    with path.open(encoding="utf-8") as f:
        return [decode_event(line) for line in f]


def test_support_triage_flags_the_duplicate_vector_search() -> None:
    dag = build_timing_dag(_load("support_triage"))
    groups = detect_redundancy(dag)

    assert len(groups) == 1
    group = groups[0]
    assert group.tool_name == "vector_search"
    assert len(group.member_node_ids) == 2
    assert group.representative_node_id in group.member_node_ids
    # PRD §16.3: wasted_virtual_ms = Σ durations - max(duration).
    members = [dag.nodes[n] for n in group.member_node_ids]
    durations = [n.duration_ms for n in members]
    assert group.wasted_virtual_ms == sum(durations) - max(durations)
    assert group.wasted_tokens == 0


def test_code_pipeline_same_tool_different_args_is_not_grouped() -> None:
    """`code_pipeline` has three `write_draft` calls, each with a distinct `args_hash`.

    (Different draft content.) Exact-hash grouping (§43.3.2) must never treat same-tool,
    different-argument calls as redundant, so `write_draft` must produce zero groups.
    """
    dag = build_timing_dag(_load("code_pipeline"))
    write_draft_calls = [n for n in dag.nodes.values() if n.tool_name == "write_draft"]
    assert len(write_draft_calls) == 3
    assert len({n.args_hash for n in write_draft_calls}) == 3, (
        "fixture assumption: all three write_draft calls have distinct args_hash"
    )

    groups = detect_redundancy(dag)
    assert not [g for g in groups if g.tool_name == "write_draft"]

    # Every group that *is* reported still has all members sharing one args_hash — the
    # exact-hash contract, checked directly rather than assumed.
    for group in groups:
        member_hashes = {dag.nodes[n].args_hash for n in group.member_node_ids}
        assert member_hashes == {group.args_hash}


def test_duplicate_node_ids_excludes_every_representative() -> None:
    dag = build_timing_dag(_load("support_triage"))
    groups = detect_redundancy(dag)
    duplicates = duplicate_node_ids(groups)

    for group in groups:
        assert group.representative_node_id not in duplicates
        for member in group.member_node_ids:
            if member != group.representative_node_id:
                assert member in duplicates


def test_no_group_ever_links_a_retry() -> None:
    """Condition 2 (PRD §16.3): a retry is never redundancy, on any golden fixture."""
    for name in ("code_pipeline", "research_fanout", "support_triage"):
        dag = build_timing_dag(_load(name))
        groups = detect_redundancy(dag)
        for group in groups:
            retry_ofs = {dag.nodes[n].retry_of for n in group.member_node_ids}
            assert retry_ofs == {None}, f"{name}: group {group.group_key} includes a retry"


def test_research_fanout_has_no_redundancy() -> None:
    """A concurrent-fanout fixture with no duplicated tool calls reports zero groups.

    `detect_redundancy` must not manufacture findings where none exist (I9).
    """
    dag = build_timing_dag(_load("research_fanout"))
    assert detect_redundancy(dag) == ()
