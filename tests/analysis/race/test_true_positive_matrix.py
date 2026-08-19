"""PRD §33.8's literal 12-case true-positive matrix, as one auditable checklist.

"Minimum 12 hand-authored logs" — most already exist, spread across `test_conflicts.py` and
`test_guards.py` (which is where their fuller assertions and docstrings live); this file is not
a duplicate of that coverage but the single place a reviewer can see all twelve PRD rows named
one-to-one against the test that proves each, plus the three rows nothing else in this
directory covers yet (three-agent conflict; same-agent, two-subtasks conflict; write-write
ordered by an actual `message_send`/`message_recv` pair rather than a bare `causal_parents`
link). Row 11 (unavailable value hashes) is the one row this build does not implement — see
that test's own docstring and `docs/race-detection.md`'s coverage statement for why.

**Rewritten 2026-08-19 (OP-2 finding, `CONTEXT.md` §13) to build every log through `CausalLog`**
rather than hand-setting `vclock={}` alongside `causal_parents=()` - see `_causal_log.py`'s
module docstring for why that combination was never a shape the real scheduler produces.
"""

from __future__ import annotations

from agentdx.analysis.race import _diverges, detect_conflicts, find_conflicts
from tests.analysis.race._causal_log import CausalLog
from tests.analysis.race._events import message_recv, message_send, state_read, state_write

# ---------------------------------------------------------------------------------------------
# Row 1: write-write concurrent divergent — must report
# ---------------------------------------------------------------------------------------------


def test_row_01_write_write_concurrent_divergent_must_report() -> None:
    """Covered fully elsewhere.

    See `test_conflicts.py::test_two_concurrent_diverging_writes_are_a_lost_update`.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")
    assert len(detect_conflicts(log.events)) == 1


# ---------------------------------------------------------------------------------------------
# Row 2: write-write ordered by an actual message send/recv — must not report
# ---------------------------------------------------------------------------------------------


def test_row_02_write_write_ordered_by_a_real_message_must_not_report() -> None:
    """Proves the send/receive vector-clock rule itself, not just G1's suppression logic.

    Unlike `test_guards.py`'s G1 test (a bare `causal_parents` link standing in for *any*
    synchronisation primitive), this uses a genuine `message_send`/`message_recv` pair - PRD
    §33.8's literal row.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(message_send, agent_id="a", span_id="s", message_id="m1", to="b")
    log.add(
        message_recv,
        causes=[1],
        agent_id="b",
        span_id="s",
        message_id="m1",
        from_="a",
        delivered_virtual_ts_ms=2,
    )
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")
    assert detect_conflicts(log.events) == ()


# ---------------------------------------------------------------------------------------------
# Row 3: write-write concurrent, identical values — suppress as idempotent
# ---------------------------------------------------------------------------------------------


def test_row_03_write_write_concurrent_identical_values_suppressed_as_idempotent() -> None:
    """Covered fully elsewhere.

    See `test_guards.py::test_g2_divergence_suppresses_identical_concurrent_writes`.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="same")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="same")
    events = log.events

    (conflict,) = find_conflicts(events)
    assert conflict.suppressed_by == "G2"
    assert detect_conflicts(events) == ()


# ---------------------------------------------------------------------------------------------
# Row 4: read-write concurrent — report as stale read
# ---------------------------------------------------------------------------------------------


def test_row_04_read_write_concurrent_reported_as_stale_read() -> None:
    """Covered fully elsewhere.

    See `test_conflicts.py::test_read_concurrent_with_an_earlier_write_is_a_stale_read`.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_read, agent_id="b", span_id="s", key="k", value="v2")

    (finding,) = detect_conflicts(log.events)
    assert finding.subtype == "read_write"


# ---------------------------------------------------------------------------------------------
# Row 5: write-read concurrent — report as dirty read
# ---------------------------------------------------------------------------------------------


def test_row_05_write_read_concurrent_reported_as_dirty_read() -> None:
    """Covered fully elsewhere.

    See `test_conflicts.py::test_write_concurrent_with_an_earlier_read_is_a_dirty_read`.
    """
    log = CausalLog()
    log.add(state_read, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")

    (finding,) = detect_conflicts(log.events)
    assert finding.subtype == "write_read"


# ---------------------------------------------------------------------------------------------
# Row 6: three-agent conflict
# ---------------------------------------------------------------------------------------------


def test_row_06_three_agent_conflict() -> None:
    """Three agents write the same key; only the genuinely concurrent pair races.

    `a` and `b` write concurrently (races). `c` writes last, but only after receiving a message
    from *both* `a` and `b` first - `c`'s write vclock therefore dominates both `a`'s and `b`'s,
    so `c` races with neither. `a` and `b` never communicate with each other at all, so they
    stay concurrent regardless of what `c` later does - happens-before does not transitively
    order `a` before `c`'s peers unless `a` itself is in the chain. Exactly one finding: `a`/`b`.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")  # seq 0
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")  # seq 1
    log.add(message_send, agent_id="a", span_id="s", message_id="m_a", to="c")  # seq 2
    log.add(message_send, agent_id="b", span_id="s", message_id="m_b", to="c")  # seq 3
    log.add(
        message_recv,
        causes=[2],
        agent_id="c",
        span_id="s",
        message_id="m_a",
        from_="a",
        delivered_virtual_ts_ms=4,
    )  # seq 4
    log.add(
        message_recv,
        causes=[3],
        agent_id="c",
        span_id="s",
        message_id="m_b",
        from_="b",
        delivered_virtual_ts_ms=5,
    )  # seq 5
    # No `causes=` here: real `state_write`s never pass one (see `_causal_log.py`'s module
    # docstring); c's write naturally inherits both merges already folded into its own slot
    # by the two `message_recv`s above, which is exactly how `Scheduler._compute_vclock`
    # would carry it forward for a plain event with no declared causes.
    log.add(state_write, agent_id="c", span_id="s", key="k", value="v3")  # seq 6

    findings = detect_conflicts(log.events)
    assert len(findings) == 1
    assert {findings[0].agent_a, findings[0].agent_b} == {"a", "b"}


# ---------------------------------------------------------------------------------------------
# Row 7: same-agent, two-subtasks conflict
# ---------------------------------------------------------------------------------------------


def test_row_07_same_agent_two_subtasks_conflict() -> None:
    """One `agent_id` running two concurrent subtasks (distinct `clock_slot`s) still races.

    `Access.slot` is `clock_slot_of(event)` (`clock_slot` if set, else `agent_id`) - two
    `state_write`s sharing an `agent_id` but declaring different `clock_slot`s are exactly the
    "same agent, two subtasks" shape PRD §33.8 names (e.g. an agent fanning out async
    sub-coroutines that both touch shared state without coordinating between themselves).
    """
    log = CausalLog()
    log.add(
        state_write,
        agent_id="planner",
        span_id="s1",
        key="k",
        value="v1",
        clock_slot="planner:subtask_1",
    )
    log.add(
        state_write,
        agent_id="planner",
        span_id="s2",
        key="k",
        value="v2",
        clock_slot="planner:subtask_2",
    )

    findings = detect_conflicts(log.events)
    assert len(findings) == 1
    assert findings[0].agent_a == findings[0].agent_b == "planner"


# ---------------------------------------------------------------------------------------------
# Row 8: conflict across a lock — must suppress
# ---------------------------------------------------------------------------------------------


def test_row_08_conflict_across_a_lock_suppressed() -> None:
    """Covered fully elsewhere.

    See `test_guards.py::test_g4_explicit_lock_suppresses_writes_under_the_same_lock`.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1", lock_id="L1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2", lock_id="L1")
    assert detect_conflicts(log.events) == ()


# ---------------------------------------------------------------------------------------------
# Row 9: conflict on a reducer channel — must suppress
# ---------------------------------------------------------------------------------------------


def test_row_09_conflict_on_a_reducer_channel_suppressed() -> None:
    """Covered fully elsewhere.

    See `test_guards.py::test_g3_declared_reducer_suppresses_a_reducer_channel`.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1", reducer="operator.add")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")
    assert detect_conflicts(log.events) == ()


# ---------------------------------------------------------------------------------------------
# Row 10: torn read across a transaction — report, elevated
# ---------------------------------------------------------------------------------------------


def test_row_10_torn_read_across_a_transaction_reported_elevated() -> None:
    """Covered fully in `test_conflicts.py::test_torn_write_read_is_elevated_to_critical`."""
    log = CausalLog()
    log.add(state_read, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2", txn_id="txn-1")

    (finding,) = detect_conflicts(log.events)
    assert finding.torn is True
    assert finding.severity == "critical"  # elevated from write_read's HIGH base


# ---------------------------------------------------------------------------------------------
# Row 11: conflict with unavailable value hashes — NOT IMPLEMENTED (documented gap)
# ---------------------------------------------------------------------------------------------


def test_row_11_unavailable_value_hash_is_a_documented_non_goal() -> None:
    """This build does not implement PRD §14.6's fourth row - a documented, not silent, gap.

    That row ("value_hash unavailable ... report at reduced confidence, severity capped") is
    not implemented — `race._diverges`'s own docstring states why: `value_hash` is a required,
    non-nullable field on both payload schemas (PRD §9.5), so no live code path in this build
    ever produces a `state_read`/`state_write` with one absent. This test exists to make that
    gap auditable (a row this suite does not silently skip) - see `docs/race-detection.md`'s
    coverage statement for the same point stated for a human reader.
    """
    assert _diverges.__doc__ is not None
    assert "unavailable" in _diverges.__doc__


# ---------------------------------------------------------------------------------------------
# Row 12: conflict after an agent crash — fault taint (PRD §9.4)
# ---------------------------------------------------------------------------------------------


def test_row_12_conflict_after_an_agent_crash_is_reported_and_fault_tainted() -> None:
    """Covered fully elsewhere.

    See `test_conflicts.py::test_fault_tainted_conflict_is_still_reported_not_suppressed`.
    `race.py` does not know what an `agent_crash` fault *is* (I3 purity - no `runtime` import);
    it reads `Event.fault_id`, already resolved upstream by `runtime.faults.taint` (PRD §9.4).
    This test's `fault_id="flt_crash_1"` stands in for that resolved value, exactly as it would
    arrive from a real `agent_crash`-perturbed run.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1", fault_id="flt_crash_1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")

    (finding,) = detect_conflicts(log.events)
    assert finding.fault_id == "flt_crash_1"
