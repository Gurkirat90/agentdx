"""`agentdx.analysis.causality` — vector clock rules (PRD §14.2), `happens_before`/`concurrent`.

**Rewritten 2026-08-19 to close an OP-2 finding.** The prior version of this file hand-set
`vclock={}` on every event and relied on the (then-buggy) `build_causality` to derive it by
walking `causal_parents` - which meant every test paired that placeholder with
`causal_parents=()` for a plain local event, a shape the real scheduler never produces (see
`_causal_log.py`'s module docstring for the full story). Every event in this file is now
built through `CausalLog`, which computes `vclock` and `causal_parents` independently and
correctly, the way `runtime.scheduler.Scheduler` does - so every test here exercises the real,
fallback-shaped `causal_parents` field by construction, not as a special case bolted on
afterward.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from agentdx.analysis.causality import (
    RUN_SLOT,
    NonMonotonicSeqError,
    build_causality,
    clock_slot_of,
    concurrent,
    concurrent_events,
    happens_before,
    happens_before_events,
)
from agentdx.events.canonical import decode_event
from agentdx.events.schema import Event, EventType
from tests.analysis._events import ev, run_start
from tests.analysis.race._causal_log import CausalLog
from tests.analysis.race._events import message_recv, message_send, state_write

GOLDEN_DIR = Path(__file__).parents[3] / "tests" / "golden"


def _load_golden(name: str) -> list[Event]:
    with (GOLDEN_DIR / f"{name}.jsonl").open() as fh:
        return [decode_event(line) for line in fh]


# ---------------------------------------------------------------------------------------------
# happens_before / concurrent — the comparison rules, in isolation (raw VClock dicts)
# ---------------------------------------------------------------------------------------------


def test_happens_before_is_strict_and_reflexive_false() -> None:
    """A clock never happens-before itself (no slot is strictly less than itself).

    Two *equal* clocks are, by the literal PRD §14.2 definition, "concurrent" (neither
    happens-before the other) - a degenerate case that cannot arise between two distinct real
    events (every event strictly bumps its own slot), included here only to pin the formula's
    literal behaviour at the boundary rather than leave it untested.
    """
    vc = {"a": 3, "b": 1}
    assert happens_before(vc, vc) is False
    assert concurrent(vc, vc) is True


def test_happens_before_dominates_on_every_slot() -> None:
    a = {"a": 1, "b": 2}
    b = {"a": 2, "b": 2}
    assert happens_before(a, b) is True
    assert happens_before(b, a) is False
    assert concurrent(a, b) is False


def test_disjoint_slots_are_concurrent() -> None:
    """Two clocks touching entirely different slots are concurrent (absent slot reads as 0)."""
    a = {"worker_1": 1}
    b = {"worker_2": 1}
    assert happens_before(a, b) is False
    assert happens_before(b, a) is False
    assert concurrent(a, b) is True


def test_partial_dominance_is_concurrent_not_ordered() -> None:
    """Neither clock dominates every slot of the other: concurrent, per PRD §14.2."""
    a = {"x": 2, "y": 1}
    b = {"x": 1, "y": 2}
    assert concurrent(a, b) is True
    assert happens_before(a, b) is False
    assert happens_before(b, a) is False


def test_empty_clock_happens_before_any_nonempty_clock_on_that_slot() -> None:
    assert happens_before({}, {"a": 1}) is True
    assert happens_before({"a": 1}, {}) is False


# ---------------------------------------------------------------------------------------------
# build_causality — local rule
# ---------------------------------------------------------------------------------------------


def test_local_events_bump_only_their_own_slot() -> None:
    """Three consecutive local writes on one slot: 1, 2, 3 - no other slot ever appears."""
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k1")
    log.add(state_write, agent_id="a", span_id="s", key="k2")
    log.add(state_write, agent_id="a", span_id="s", key="k3")

    graph = build_causality(log.events)
    assert graph.vclock_of(0) == {"a": 1}
    assert graph.vclock_of(1) == {"a": 2}
    assert graph.vclock_of(2) == {"a": 3}


def test_two_independent_slots_never_see_each_others_counter() -> None:
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k")
    log.add(state_write, agent_id="b", span_id="s", key="k")
    log.add(state_write, agent_id="a", span_id="s", key="k2")

    graph = build_causality(log.events)
    assert graph.vclock_of(0) == {"a": 1}
    assert graph.vclock_of(1) == {"b": 1}
    assert graph.vclock_of(2) == {"a": 2}  # unaffected by b's event in between


def test_run_scope_event_with_no_agent_uses_the_run_slot() -> None:
    """PRD §9.2: agent_id/clock_slot are null for run-scope events - RUN_SLOT is the fallback."""
    events = [run_start(seq=0, virtual_ts_ms=0)]
    graph = build_causality(events)
    assert graph.vclock_of(0) == {RUN_SLOT: 1}
    assert clock_slot_of(events[0]) == RUN_SLOT


# ---------------------------------------------------------------------------------------------
# build_causality - send / receive
# ---------------------------------------------------------------------------------------------


def test_receive_merges_the_senders_snapshot_then_bumps_own_slot() -> None:
    """PRD §14.2: receive merges the message's vclock (the sender's snapshot), then bumps."""
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k")  # seq0, a: 1
    send = log.add(message_send, agent_id="a", span_id="s", message_id="m1", to="b")  # seq1, a: 2
    log.add(state_write, agent_id="b", span_id="s", key="k2")  # seq2, b: 1, unrelated to the msg
    recv = log.add(
        message_recv,
        causes=[send.seq],
        agent_id="b",
        span_id="s",
        message_id="m1",
        from_="a",
        delivered_virtual_ts_ms=3,
    )  # seq3

    graph = build_causality(log.events)
    assert graph.vclock_of(1) == {"a": 2}
    assert graph.vclock_of(2) == {"b": 1}
    # receive merges send's {a: 2} into b's own last {b: 1}, then bumps b -> 2
    assert graph.vclock_of(3) == {"a": 2, "b": 2}
    assert happens_before(graph.vclock_of(1), graph.vclock_of(3))
    assert not concurrent(graph.vclock_of(1), graph.vclock_of(recv.seq))


# ---------------------------------------------------------------------------------------------
# build_causality - lock acquire / release
# ---------------------------------------------------------------------------------------------


def _lock_release(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    lock_id: str,
) -> Event:
    return ev(
        EventType.LOCK_RELEASE,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=span_id,
        payload={"lock_id": lock_id, "held_virtual_ms": 1},
    )


def _lock_acquire(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    span_id: str,
    lock_id: str,
) -> Event:
    return ev(
        EventType.LOCK_ACQUIRE,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id=span_id,
        payload={"lock_id": lock_id, "wait_virtual_ms": 0},
    )


def test_lock_acquire_merges_the_matching_release_then_bumps() -> None:
    """PRD §14.2: acquire merges the lock's release_vclock, then bumps; release just bumps."""
    log = CausalLog()
    log.add(_lock_acquire, agent_id="a", span_id="s", lock_id="L")  # seq0, a: 1
    w1 = log.add(state_write, agent_id="a", span_id="s", key="k")  # seq1, a: 2
    rel = log.add(_lock_release, agent_id="a", span_id="s", lock_id="L")  # seq2, a: 3
    log.add(
        _lock_acquire, causes=[rel.seq], agent_id="b", span_id="s", lock_id="L"
    )  # seq3, merges a's release {a:3}, then b: 1
    w2 = log.add(state_write, agent_id="b", span_id="s", key="k")  # seq4

    graph = build_causality(log.events)
    assert graph.vclock_of(2) == {"a": 3}
    assert graph.vclock_of(3) == {"a": 3, "b": 1}
    # the writes under the lock are correctly ordered - the second happens-after the first
    assert happens_before(graph.vclock_of(w1.seq), graph.vclock_of(w2.seq))
    assert not concurrent(graph.vclock_of(w1.seq), graph.vclock_of(w2.seq))


# ---------------------------------------------------------------------------------------------
# build_causality - barrier (all-to-all merge)
# ---------------------------------------------------------------------------------------------


def _barrier(
    *,
    seq: int,
    virtual_ts_ms: int,
    vclock: Mapping[str, int],
    causal_parents: Sequence[int],
    agent_id: str,
    phase: str,
) -> Event:
    return ev(
        EventType.BARRIER,
        seq=seq,
        virtual_ts_ms=virtual_ts_ms,
        vclock=vclock,
        causal_parents=causal_parents,
        agent_id=agent_id,
        clock_slot=agent_id,
        span_id="s",
        payload={
            "barrier_id": "B",
            "participants": ["a", "b", "c"],
            "phase": phase,
            "wait_virtual_ms": 0,
        },
    )


def test_barrier_all_to_all_merge_orders_every_participant_after_every_other() -> None:
    """PRD §14.2: "barrier: all-to-all merge, then each participant increments".

    Modelled below as each participant's post-barrier event naming every *other* participant's
    own pre-barrier event as a causal parent - `causal_parents` is where the SDK is documented
    to place exactly this information (module docstring, "the SDK supplies the causality").

    A barrier is a rendezvous, not a mutual-exclusion lock: it guarantees every pre-barrier
    event (any participant) happens-before every post-barrier event (any participant), which
    this test asserts directly below. It does **not** make the participants' own post-barrier
    releases happen-before *each other* — each release's clock has only its own slot
    incremented past the merge, so `release_a`, `release_b` and `release_c` remain pairwise
    concurrent, exactly as three independent participants resuming independent work after one
    shared synchronisation point should be.
    """
    log = CausalLog()
    enter_a = log.add(_barrier, agent_id="a", phase="enter")
    enter_b = log.add(_barrier, agent_id="b", phase="enter")
    enter_c = log.add(_barrier, agent_id="c", phase="enter")
    # release: each names the *other two* enters as causal parents (all-to-all)
    rel_a = log.add(_barrier, causes=[enter_b.seq, enter_c.seq], agent_id="a", phase="release")
    rel_b = log.add(_barrier, causes=[enter_a.seq, enter_c.seq], agent_id="b", phase="release")
    rel_c = log.add(_barrier, causes=[enter_a.seq, enter_b.seq], agent_id="c", phase="release")

    graph = build_causality(log.events)
    # each release: the merge of all three enters, then only its own slot increments
    assert graph.vclock_of(rel_a.seq) == {"a": 2, "b": 1, "c": 1}
    assert graph.vclock_of(rel_b.seq) == {"a": 1, "b": 2, "c": 1}
    assert graph.vclock_of(rel_c.seq) == {"a": 1, "b": 1, "c": 2}
    # every pre-barrier enter happens-before every post-barrier release, for every pair
    for enter in (enter_a, enter_b, enter_c):
        for release in (rel_a, rel_b, rel_c):
            assert happens_before(graph.vclock_of(enter.seq), graph.vclock_of(release.seq))
    # the three releases remain pairwise concurrent with each other - see the docstring
    assert concurrent(graph.vclock_of(rel_a.seq), graph.vclock_of(rel_b.seq))
    assert concurrent(graph.vclock_of(rel_b.seq), graph.vclock_of(rel_c.seq))
    assert concurrent(graph.vclock_of(rel_a.seq), graph.vclock_of(rel_c.seq))


# ---------------------------------------------------------------------------------------------
# happens_before_events / concurrent_events convenience wrappers
# ---------------------------------------------------------------------------------------------


def test_happens_before_events_and_concurrent_events_wrap_the_graph() -> None:
    log = CausalLog()
    e0 = log.add(state_write, agent_id="a", span_id="s", key="k")
    e1 = log.add(state_write, agent_id="b", span_id="s", key="k")

    graph = build_causality(log.events)
    assert concurrent_events(e0, e1, graph) is True
    assert happens_before_events(e0, e1, graph) is False


# ---------------------------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------------------------


def test_non_monotonic_seq_raises() -> None:
    events = [
        state_write(
            seq=1, virtual_ts_ms=0, vclock={}, causal_parents=(), agent_id="a", span_id="s", key="k"
        ),
        state_write(
            seq=0, virtual_ts_ms=1, vclock={}, causal_parents=(), agent_id="a", span_id="s", key="k"
        ),
    ]
    with pytest.raises(NonMonotonicSeqError) as excinfo:
        build_causality(events)
    assert excinfo.value.code == "E-CAUS-001"


def test_repeated_seq_raises_non_monotonic() -> None:
    events = [
        state_write(
            seq=0, virtual_ts_ms=0, vclock={}, causal_parents=(), agent_id="a", span_id="s", key="k"
        ),
        state_write(
            seq=0, virtual_ts_ms=1, vclock={}, causal_parents=(), agent_id="a", span_id="s", key="k"
        ),
    ]
    with pytest.raises(NonMonotonicSeqError):
        build_causality(events)


# ---------------------------------------------------------------------------------------------
# OP-2 regression (2026-08-19) — the exact bug, reproduced and pinned
# ---------------------------------------------------------------------------------------------


def test_op2_20260819_unrelated_writes_stay_concurrent_with_fallback_causal_parents() -> None:
    """Direct regression test for the OP-2 finding recorded in `CONTEXT.md` §13.

    Two unrelated agents' concurrent writes to the same key, with no lock/message/reducer -
    built through `CausalLog`, so `causal_parents` gets the *real*, fallback-shaped value
    (`[0]`, not `()`) the scheduler actually stamps on a `state_write` with no declared
    `causes`. The prior, buggy `build_causality` walked that field and computed
    `happens_before(e1, e2) is True` - silently defeating gate G1's flagship scenario. The
    fixed version trusts `Event.vclock` (each write only ever bumps its own slot, since
    neither declared a `causes`), so the two remain correctly concurrent.
    """
    log = CausalLog()
    e1 = log.add(state_write, agent_id="coder", span_id="s1", key="draft.module_a", value="A")
    e2 = log.add(state_write, agent_id="reviewer", span_id="s2", key="draft.module_a", value="B")

    # Confirm this test actually exercises the bug's precondition, not the old PRD-ideal shape.
    assert list(e2.causal_parents) == [0], (
        "test setup check: e2 must carry the real scheduler's fallback-shaped causal_parents "
        "([seq - 1]), not an empty tuple, or this test does not reproduce the OP-2 finding"
    )
    assert e1.vclock == {"coder": 1}
    assert e2.vclock == {"reviewer": 1}

    graph = build_causality(log.events)
    assert happens_before_events(e1, e2, graph) is False
    assert concurrent_events(e1, e2, graph) is True


# ---------------------------------------------------------------------------------------------
# Cross-check against every real, committed golden log
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", ["code_pipeline", "research_fanout", "support_triage"])
def test_build_causality_reads_the_stamped_vclock_verbatim_on_every_real_golden_log(
    fixture_name: str,
) -> None:
    """`build_causality` trusts and correctly prunes `Event.vclock` - checked on every event.

    This is close to tautological by construction (`build_causality` now *is* "read
    `event.vclock`, prune zero-valued slots") - its value is confirming the sparse-map
    pruning never disagrees with a real, non-degenerate stamped log, on all three real
    committed golden fixtures, not just hand-authored ones.
    """
    events = _load_golden(fixture_name)
    graph = build_causality(events)
    for event in events:
        assert graph.vclock_of(event.seq) == dict(event.vclock), (
            f"seq {event.seq} ({event.type.value}): computed {graph.vclock_of(event.seq)} "
            f"!= stamped {dict(event.vclock)}"
        )
