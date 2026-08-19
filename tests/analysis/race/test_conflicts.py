"""`agentdx.analysis.race` — classification (PRD §14.5), value divergence (§14.6), dedup (§14.3).

Guard suppression itself (G2/G3/G4, in both directions) is `test_guards.py`'s job — this file
holds every event pair unguarded (no reducer, no lock, always divergent) except where the
divergence/torn-read behaviour under test *is* the point, so a classification test failing
here can never be secretly explained by a guard firing underneath it.

**Rewritten 2026-08-19 (OP-2 finding, `CONTEXT.md` §13) to build every log through `CausalLog`**
rather than hand-setting `vclock={}` alongside `causal_parents=()` - see `_causal_log.py`'s
module docstring for why that combination was never a shape the real scheduler produces.
"""

from __future__ import annotations

from agentdx.analysis.race import ConflictSubtype, Severity, detect_conflicts, find_conflicts
from tests.analysis.race._causal_log import CausalLog
from tests.analysis.race._events import state_read, state_write

# ---------------------------------------------------------------------------------------------
# write_write -> "lost update" (PRD §14.5)
# ---------------------------------------------------------------------------------------------


def test_two_concurrent_diverging_writes_are_a_lost_update() -> None:
    """Two different agents write the same key, concurrently, to different values."""
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")
    events = log.events

    conflicts = find_conflicts(events)
    assert len(conflicts) == 1
    (conflict,) = conflicts
    assert conflict.subtype is ConflictSubtype.WRITE_WRITE
    assert conflict.divergent is True
    assert conflict.suppressed_by is None
    assert conflict.evidence_seq == (0, 1)

    findings = detect_conflicts(events)
    assert len(findings) == 1
    (finding,) = findings
    assert finding.type == "state_conflict"
    assert finding.subtype == "write_write"
    assert finding.severity == Severity.CRITICAL.value
    assert finding.key == "k"
    assert (finding.agent_a, finding.agent_b) == ("a", "b")
    assert (finding.seq_a, finding.seq_b) == (0, 1)
    assert finding.evidence_seq == (0, 1)
    # the later write (seq 1) survives; the earlier one (seq 0) is silently discarded
    assert finding.surviving_seq == 1
    assert finding.discarded_seq == 0
    assert finding.torn is False
    assert finding.fault_id is None


# ---------------------------------------------------------------------------------------------
# read_write -> "stale read": a write lands, then a concurrent read of the same key
# ---------------------------------------------------------------------------------------------


def test_read_concurrent_with_an_earlier_write_is_a_stale_read() -> None:
    """Agent a writes k; agent b's later-in-seq but *concurrent* read may not observe it."""
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_read, agent_id="b", span_id="s", key="k", value="v2")
    events = log.events

    (conflict,) = find_conflicts(events)
    assert conflict.subtype is ConflictSubtype.READ_WRITE
    assert conflict.suppressed_by is None

    (finding,) = detect_conflicts(events)
    assert finding.subtype == "read_write"
    assert finding.severity == Severity.HIGH.value
    # the read (b, seq 1) is access_a; the write it raced with (a, seq 0) is access_b
    assert (finding.agent_a, finding.agent_b) == ("b", "a")
    assert (finding.seq_a, finding.seq_b) == (1, 0)
    assert finding.evidence_seq == (0, 1)
    assert finding.surviving_seq is None  # only write_write ever discards a value
    assert finding.discarded_seq is None


# ---------------------------------------------------------------------------------------------
# write_read -> "dirty read": a read lands, then a concurrent write of the same key
# ---------------------------------------------------------------------------------------------


def test_write_concurrent_with_an_earlier_read_is_a_dirty_read() -> None:
    """Agent a reads k; agent b's later-in-seq but *concurrent* write invalidates that read."""
    log = CausalLog()
    log.add(state_read, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")
    events = log.events

    (conflict,) = find_conflicts(events)
    assert conflict.subtype is ConflictSubtype.WRITE_READ
    assert conflict.suppressed_by is None

    (finding,) = detect_conflicts(events)
    assert finding.subtype == "write_read"
    assert finding.severity == Severity.HIGH.value
    assert finding.torn is False
    assert finding.surviving_seq is None
    assert finding.discarded_seq is None


def test_torn_write_read_is_elevated_to_critical() -> None:
    """A `write_read` whose write carries a `txn_id` is a torn read: severity HIGH -> CRITICAL."""
    log = CausalLog()
    log.add(state_read, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2", txn_id="txn-1")
    events = log.events

    (conflict,) = find_conflicts(events)
    assert conflict.torn is True

    (finding,) = detect_conflicts(events)
    assert finding.torn is True
    assert finding.severity == Severity.CRITICAL.value  # elevated from the write_read base HIGH


def test_non_torn_write_read_stays_at_base_severity() -> None:
    """The same shape, minus `txn_id`, stays HIGH - torn elevation is `txn_id`-gated, not free."""
    log = CausalLog()
    log.add(state_read, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")
    events = log.events

    (finding,) = detect_conflicts(events)
    assert finding.torn is False
    assert finding.severity == Severity.HIGH.value


# ---------------------------------------------------------------------------------------------
# value divergence (PRD §14.6) - the `Conflict.divergent` field itself
# ---------------------------------------------------------------------------------------------


def test_identical_value_hash_is_not_divergent() -> None:
    """Two concurrent writes hashing to the same value: `divergent` is False, not a guess."""
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="same")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="same")
    events = log.events

    (conflict,) = find_conflicts(events)
    assert conflict.divergent is False
    assert conflict.suppressed_by == "G2"


def test_different_value_hash_is_divergent() -> None:
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")
    events = log.events

    (conflict,) = find_conflicts(events)
    assert conflict.divergent is True


# ---------------------------------------------------------------------------------------------
# dedup (PRD §14.3): one representative conflict per (key, subtype, {slot_a, slot_b})
# ---------------------------------------------------------------------------------------------


def test_repeated_conflicts_between_the_same_two_slots_collapse_to_the_latest() -> None:
    """Three writes (a, b, a again) on one key produce two raw write_write pairs; one survives.

    a@0 and b@1 race (pair 1); b@1 and a@2 also race (pair 2, since a@2 still knows nothing of
    b@1 - no causal_parents links them). Both pairs share the same `{a, b}` slot set, same key,
    same subtype, same `suppressed_by` (None) - `_dedupe` keeps only the chronologically later
    one, matching what a user re-running the log right now would still observe: the seq-0 write
    is stale evidence once seq 2 has also raced with seq 1.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v3")
    events = log.events

    conflicts = find_conflicts(events)
    assert len(conflicts) == 1
    (conflict,) = conflicts
    assert conflict.evidence_seq == (1, 2)


def test_different_keys_never_collapse_into_each_other() -> None:
    """Dedup groups by key too - two independent races on different keys both survive."""
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k1", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k1", value="v2")
    log.add(state_write, agent_id="a", span_id="s", key="k2", value="w1")
    log.add(state_write, agent_id="b", span_id="s", key="k2", value="w2")
    events = log.events

    conflicts = find_conflicts(events)
    assert {c.key for c in conflicts} == {"k1", "k2"}
    assert len(conflicts) == 2


# ---------------------------------------------------------------------------------------------
# fault taint (PRD §9.4) - race.py reads `Event.fault_id`, never recomputes it
# ---------------------------------------------------------------------------------------------


def test_fault_id_is_the_earlier_accesss_fault_id() -> None:
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1", fault_id="flt_1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")
    events = log.events

    (finding,) = detect_conflicts(events)
    assert finding.fault_id == "flt_1"


def test_fault_id_is_none_when_neither_access_is_tainted() -> None:
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")
    events = log.events

    (finding,) = detect_conflicts(events)
    assert finding.fault_id is None


def test_fault_tainted_conflict_is_still_reported_not_suppressed() -> None:
    """PRD §14.7: fault taint is not a fifth guard - a fault-caused race is still a real race."""
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1", fault_id="flt_1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")
    events = log.events

    findings = detect_conflicts(events)
    assert len(findings) == 1
