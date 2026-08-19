"""The committed 40-event fixture: it parses, it validates, and its hash is pinned.

`tests/golden/event_log_40.jsonl` is the shared input for every downstream analyser prompt
(P10 timing, P11 baseline, P12 race). Those prompts will assert findings against it, so a
silent change here would move every one of their expectations at once. The pinned hash is
the tripwire: regenerating the fixture changes it, and AGENTS.md §5 requires an explicit
written instruction plus a stated reason for that to happen.

The log is authored in `tests/golden/build_event_log_40.py`; this test reads the committed
file, never the builder, so a builder that drifts from the committed bytes is caught.
"""

from __future__ import annotations

import pathlib

import pytest

from agentdx.events.canonical import build_chain, canonical_log_hash, decode_event, verify_chain
from agentdx.events.schema import Event, EventType
from agentdx.events.validators import validate_log

GOLDEN = pathlib.Path(__file__).parents[2] / "golden" / "event_log_40.jsonl"

GOLDEN_HASH = "blake2b:badcb6b2d3af20e91a7571bf6703836f4bb5fb5e1cea6a751ae1062611cae295"
"""Pinned canonical hash. Changing this line requires the instruction that changed the log.

**This is that kind of change, and it is not a golden-file regeneration (AGENTS.md §5).**
`event_log_40.jsonl`'s committed bytes are untouched — every event in it is still written
at its original `schema_version=1`, byte for byte. What changed is `decode_event`'s
*interpretation* of those bytes: P09 OP-3 repair (D-45) wired migrate-on-read in, so a v1
record now decodes to an in-memory `Event` with `schema_version=2` (`events/migrations`'s
v1->v2 step). `schema_version` is a `Volatility.STABLE`, canonically-hashed field
(`events/schema.py`), so that one-field change necessarily moves the hash of every event
in every fixture — this is the fixture text agreeing with itself under the new, still-
correct interpretation, not new content. Recomputed directly from the committed file via
`canonical_log_hash([decode_event(line) for line in GOLDEN.read_text().splitlines() if
line])` after the migration landed; no call to `fixtures_runner.regenerate_all` or any
other write to `event_log_40.jsonl` was made to produce this value.
"""


@pytest.fixture(scope="module")
def log() -> list[Event]:
    """Return the committed golden log, decoded."""
    text = GOLDEN.read_text(encoding="utf-8")
    return [decode_event(line) for line in text.splitlines() if line]


def test_the_fixture_exists_and_has_forty_events(log: list[Event]) -> None:
    assert len(log) == 40


def test_the_fixture_passes_every_validation_layer(log: list[Event]) -> None:
    """If the shared fixture is invalid, every analyser built on it inherits the defect."""
    assert validate_log(log) is None


def test_the_canonical_hash_is_pinned(log: list[Event]) -> None:
    assert canonical_log_hash(log) == GOLDEN_HASH


def test_the_hash_chain_verifies(log: list[Event]) -> None:
    assert verify_chain(log, build_chain(log)) is None


def test_it_opens_with_run_start_and_closes_with_run_end(log: list[Event]) -> None:
    assert log[0].type is EventType.RUN_START
    assert log[-1].type is EventType.RUN_END


def test_it_contains_the_lost_update_the_race_detector_must_find(log: list[Event]) -> None:
    """Two concurrent blind writes to `draft.module_a` — the gate G1 finding."""
    writes = [
        e for e in log if e.type is EventType.STATE_WRITE and e.payload["key"] == "draft.module_a"
    ]
    assert len(writes) == 2
    assert {e.agent_id for e in writes} == {"coder", "reviewer"}
    assert all(e.payload["prev_value_hash"] is None for e in writes), "both writes are blind"
    assert writes[0].vclock.get("reviewer", 0) == 0, "the coder write does not see the reviewer"
    assert writes[1].vclock.get("coder", 0) == 0, "the reviewer write does not see the coder"


def test_fault_taint_descends_but_does_not_precede_the_fault(log: list[Event]) -> None:
    """The cascade tree is causal, not temporal (PRD §9.4)."""
    injected = next(e for e in log if e.type is EventType.FAULT_INJECTED)
    assert injected.fault_id == "f_01"
    assert all(e.fault_id is None for e in log[: injected.seq])
    coder_writes = [e for e in log if e.agent_id == "coder" and e.type is EventType.STATE_WRITE]
    assert all(e.fault_id is None for e in coder_writes), "the coder is not downstream of it"
    reviewer_events = [e for e in log if e.agent_id == "reviewer"]
    assert all(e.fault_id == "f_01" for e in reviewer_events[1:]), "the reviewer is"


def test_virtual_and_wall_time_are_not_conflated(log: list[Event]) -> None:
    """I11: the run's virtual makespan is not its wall makespan, and only one is compared."""
    run_end = log[-1]
    assert run_end.payload["virtual_makespan_ms"] != run_end.payload["wall_makespan_ms"]
    assert run_end.payload["virtual_makespan_ms"] == log[-1].virtual_ts_ms


def test_every_event_type_used_is_in_the_closed_enum(log: list[Event]) -> None:
    assert {e.type for e in log} <= set(EventType)


def test_the_fixture_is_deterministic_under_reserialisation(log: list[Event]) -> None:
    """Reading and rewriting the fixture must not move its hash."""
    from agentdx.events.canonical import encode_event

    revived = [decode_event(encode_event(e)) for e in log]
    assert canonical_log_hash(revived) == GOLDEN_HASH
