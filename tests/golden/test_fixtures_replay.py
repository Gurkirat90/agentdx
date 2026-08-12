"""tests/golden/test_fixtures_replay.py — the comparison harness, as a pytest suite.

What `just fixtures-check` runs (`tests/golden/fixtures_runner.py`) is a superset of what CI
runs here: the runner is a standalone script (so a human can run it with no test framework),
and this file is its pytest-collected form plus the structural assertions this prompt's
fixture READMEs promise (gate G1's evidence, gate G2's empty set, and the race-freedom
argument's mechanical check).

No network: every fixture is served from its own committed `cache/responses.json` (I7).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from agentdx.events.schema import Event
from tests.golden.fixtures_runner import check_fixture, load_golden, run_fixture

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


def _writes(events: list[Event], key: str) -> list[Event]:
    """Return every `state_write` event to `key`, in seq order."""
    return [e for e in events if e.type.value == "state_write" and e.payload["key"] == key]


def _reads(events: list[Event], key: str) -> list[Event]:
    """Return every `state_read` event of `key`, in seq order."""
    return [e for e in events if e.type.value == "state_read" and e.payload["key"] == key]


def _assertion_results(events: list[Event]) -> dict[str, bool]:
    """Return `{assertion_id: passed}` for every `assertion_result` event."""
    return {
        e.payload["assertion_id"]: e.payload["passed"]
        for e in events
        if e.type.value == "assertion_result"
    }


# ---------------------------------------------------------------------------------------
# Golden-log equality — one test per fixture, so a failure names the fixture
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_pipeline_matches_golden() -> None:
    """A fresh run's canonical log hash equals the committed golden log's."""
    ok, detail = await check_fixture("code_pipeline")
    assert ok, detail


@pytest.mark.asyncio
async def test_support_triage_matches_golden() -> None:
    """A fresh run's canonical log hash equals the committed golden log's."""
    ok, detail = await check_fixture("support_triage")
    assert ok, detail


@pytest.mark.asyncio
async def test_research_fanout_matches_golden() -> None:
    """A fresh run's canonical log hash equals the committed golden log's."""
    ok, detail = await check_fixture("research_fanout")
    assert ok, detail


# ---------------------------------------------------------------------------------------
# Gate G1 — code_pipeline's seeded lost update, deterministic and reachable
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_pipeline_lost_update_is_deterministic_10_of_10() -> None:
    """Ten fresh runs all have reviewer's write survive and coder's be discarded.

    Design constraint 2 (this prompt): the seeded race "fires under the seeded schedule, not
    only under a rare interleaving." This is the reproduction-scenario check PRD §23.1's test
    expectations ask for ("re-triggers 10/10"), run against ten independent executions rather
    than the same one replayed.
    """
    for _ in range(10):
        run = await run_fixture("code_pipeline")
        writes = _writes(run.events, "draft.module_a")
        assert len(writes) == 2, f"expected exactly 2 writes, found {len(writes)}"
        assert [e.agent_id for e in writes] == ["coder", "reviewer"], (
            "coder must write before reviewer for reviewer's write to be the one that survives"
        )
        reads = _reads(run.events, "draft.module_a")
        assert reads, "tester must read draft.module_a"
        assert reads[-1].payload["value_hash"] == writes[-1].payload["value_hash"], (
            "the surviving value must be reviewer's, every time"
        )
        assert writes[0].payload["value_hash"] != writes[1].payload["value_hash"], (
            "coder's and reviewer's writes must actually differ in content — otherwise this "
            "isn't a lost update, it's two agents coincidentally agreeing, and the finding "
            "would have nothing to be about (OP-2 finding 5, added under OP-3)"
        )


@pytest.mark.asyncio
async def test_code_pipeline_lost_update_has_no_reducer_or_lock() -> None:
    """Both writes to draft.module_a carry `reducer: null` and `lock_id: null`.

    This is the unambiguous, mechanical signal PRD §23.1 describes ("the channel has no
    reducer... last writer wins") — see `fixtures/code_pipeline/README.md` "Why explicit
    state, not a LangGraph channel" for why this fixture does not use a declared-but-trivial
    reducer, which would leave this signal ambiguous against `research_fanout`'s real one.
    """
    events = load_golden("code_pipeline")
    writes = _writes(events, "draft.module_a")
    assert len(writes) == 2
    for write in writes:
        assert write.payload["reducer"] is None
        assert write.payload["lock_id"] is None
    assert not set(writes[1].causal_parents) & {writes[0].seq}, (
        "reviewer's write must not be causally linked to coder's write — two independent "
        "writers is what makes this a race rather than an ordinary ordered overwrite"
    )


# ---------------------------------------------------------------------------------------
# Gate G2 / invariant I4 — research_fanout's empty finding set and its structural argument
# ---------------------------------------------------------------------------------------


def test_research_fanout_golden_findings_is_empty_above_info() -> None:
    """The most important assertion in the repository (PRD §23.3)."""
    golden = json.loads((FIXTURES_DIR / "research_fanout" / "golden_findings.json").read_text())
    assert golden["findings"] == [], "research_fanout must yield zero findings above info"
    assert golden["must_be_empty_above_info"] is True


def test_research_fanout_has_no_shared_unreduced_writes() -> None:
    """Every state key with more than one writer in the golden log has a real reducer.

    The mechanical half of the structural race-freedom argument in
    `fixtures/research_fanout/README.md`: grep the golden log itself, rather than trust the
    prose.
    """
    events = load_golden("research_fanout")
    writers_by_key: dict[str, set[str | None]] = defaultdict(set)
    reducers_by_key: dict[str, set[str | None]] = defaultdict(set)
    for event in events:
        if event.type.value != "state_write":
            continue
        key = event.payload["key"]
        writers_by_key[key].add(event.agent_id)
        reducers_by_key[key].add(event.payload["reducer"])

    for key, writers in writers_by_key.items():
        if len(writers) <= 1:
            continue
        reducers = reducers_by_key[key]
        assert None not in reducers, (
            f"key {key!r} has {len(writers)} writers ({sorted(writers)}) and at least one "
            f"write with no reducer — that is exactly the code_pipeline shape, not the "
            f"healthy-control shape"
        )


def test_research_fanout_findings_reducer_is_operator_add() -> None:
    """The one shared key, `findings`, reduces through `operator.add` on every write."""
    events = load_golden("research_fanout")
    writes = [e for e in events if e.type.value == "state_write" and e.payload["key"] == "findings"]
    assert len(writes) == 4, "all four workers must contribute a finding"
    assert {w.agent_id for w in writes} == {"worker_1", "worker_2", "worker_3", "worker_4"}
    for write in writes:
        assert write.payload["reducer"] == "operator.add"


# ---------------------------------------------------------------------------------------
# Design constraint 3 — support_triage's redundancy is exact-hash, not fabricated
# ---------------------------------------------------------------------------------------


def test_support_triage_redundant_vector_search_is_exact_hash() -> None:
    """retriever_a's and retriever_b's vector_search calls share one args_hash."""
    events = load_golden("support_triage")
    calls = [
        e for e in events if e.type.value == "tool_call" and e.payload["tool"] == "vector_search"
    ]
    assert len(calls) == 2
    assert {c.agent_id for c in calls} == {"retriever_a", "retriever_b"}
    assert calls[0].payload["args_hash"] == calls[1].payload["args_hash"]


def test_support_triage_has_zero_state_conflicts() -> None:
    """No state key in the golden log has more than one writer."""
    events = load_golden("support_triage")
    writers_by_key: dict[str, set[str | None]] = defaultdict(set)
    for event in events:
        if event.type.value == "state_write":
            writers_by_key[event.payload["key"]].add(event.agent_id)
    for key, writers in writers_by_key.items():
        assert len(writers) == 1, f"key {key!r} has more than one writer: {sorted(writers)}"


# ---------------------------------------------------------------------------------------
# PRD §21.6 — assertion_result events, one per fixture's checks.py, added under OP-3.
#
# OP-2 finding 4: the §21.6 pluggable-assertion mechanism was built (fixtures/_harness.py
# runs checks.py's CHECKS list and emits assertion_result before run_end) but nothing
# verified its output matched what each fixture's checks.py was written to prove. In
# particular code_pipeline's checks.py has one check that is EXPECTED to fail
# (`coder_contribution_present` — the lost update means it can't pass); a suite that only
# ever asserted `passed is True` would never have caught a regression that made it pass by
# accident, or a regression that silently dropped the check event entirely.
# ---------------------------------------------------------------------------------------


def test_code_pipeline_assertion_results_match_expected() -> None:
    """`task_success` passes; `coder_contribution_present` fails — the lost update's own proof."""
    events = load_golden("code_pipeline")
    results = _assertion_results(events)
    assert results == {
        "task_success": True,
        "coder_contribution_present": False,
    }, "code_pipeline's checks.py exists specifically to prove the lost update via a failing check"


def test_support_triage_assertion_results_match_expected() -> None:
    """Both checks pass — this fixture's defects are performance-only, not correctness."""
    events = load_golden("support_triage")
    results = _assertion_results(events)
    assert results == {
        "task_success": True,
        "response_mentions_refund": True,
    }


def test_research_fanout_assertion_results_match_expected() -> None:
    """Both checks pass — the healthy control must not fail its own task-level assertions."""
    events = load_golden("research_fanout")
    results = _assertion_results(events)
    assert results == {
        "task_success": True,
        "all_subtopics_covered": True,
    }
