"""`agentdx.analysis.race` — the four PRD §14.7 false-positive guards, tested in both directions.

Design Constraint 3 (mission brief): a guard tested in only one direction is an untested guard.
Every guard below gets exactly two tests - one proving it suppresses the false-positive class it
exists for, one proving it does *not* suppress a genuine conflict that merely shares a surface
feature with that class (concurrent-but-ordered-looking, lock-shaped-but-different-lock, and so
on). Eight tests total, one guard's docstring away from `race.py`'s own guard section.

Every event pair here is otherwise "clean" for the other three guards (diverging values, no
reducer, no lock, genuinely concurrent) except for the one dimension the test under it is
actually varying - so a guard test failing here can only be that guard's own logic, never a
neighbour's.

**Rewritten 2026-08-19 (OP-2 finding, `CONTEXT.md` §13) to build every log through `CausalLog`**
rather than hand-setting `vclock={}` alongside `causal_parents=()`/`(0,)` - see
`_causal_log.py`'s module docstring for why that combination was never a shape the real
scheduler produces.
"""

from __future__ import annotations

from agentdx.analysis.race import detect_conflicts, find_conflicts
from tests.analysis.race._causal_log import CausalLog
from tests.analysis.race._events import state_write

# ---------------------------------------------------------------------------------------------
# G1 - concurrency (the definition itself): a happens-before-ordered pair never reaches a guard
# ---------------------------------------------------------------------------------------------


def test_g1_concurrency_suppresses_a_causally_ordered_pair() -> None:
    """Two writes on different agents, but the second names the first as a causal parent.

    `build_causality` merges the parent's clock in before bumping - the second write's clock
    then strictly dominates the first's, so `causality.concurrent` is False and `_raw_conflicts`
    never even constructs a `Conflict` for this pair (module docstring: "there is no code path
    that reaches guard evaluation for a happens-before-ordered pair"). Different values, no
    reducer, no lock - every *other* guard would let this through, which is exactly why this
    pair proves G1, and only G1, is what is suppressing it.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, causes=[0], agent_id="b", span_id="s", key="k", value="v2")

    assert find_conflicts(log.events) == ()
    assert detect_conflicts(log.events) == ()


def test_g1_concurrency_does_not_suppress_a_genuinely_concurrent_pair() -> None:
    """The identical pair, minus the causal link: now concurrent, and reported."""
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")

    (conflict,) = find_conflicts(log.events)
    assert conflict.suppressed_by is None
    assert len(detect_conflicts(log.events)) == 1


# ---------------------------------------------------------------------------------------------
# G2 - value divergence: identical concurrent writes are harmless
# ---------------------------------------------------------------------------------------------


def test_g2_divergence_suppresses_identical_concurrent_writes() -> None:
    """Same key, same value, concurrent, no reducer, no lock - nothing was lost."""
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="same")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="same")

    (conflict,) = find_conflicts(log.events)
    assert conflict.suppressed_by == "G2"
    assert detect_conflicts(log.events) == ()


def test_g2_divergence_does_not_suppress_diverging_concurrent_writes() -> None:
    """The identical pair, minus the shared value - now a real, reported lost update."""
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")

    (conflict,) = find_conflicts(log.events)
    assert conflict.suppressed_by is None
    assert len(detect_conflicts(log.events)) == 1


# ---------------------------------------------------------------------------------------------
# G3 - declared reducer/CRDT key: a real merge function already resolves the concurrency
# ---------------------------------------------------------------------------------------------


def test_g3_declared_reducer_suppresses_a_reducer_channel() -> None:
    """One of the two concurrent writers names a `reducer` - this is `research_fanout`'s shape.

    PRD §14.7 only requires *a* declared reducer on the channel, not on every individual write
    (a LangGraph `Annotated[list, operator.add]` channel's SDK adapter stamps `reducer` on every
    write to it, but the guard's own logic - `_has_declared_reducer(write_a, ...) or
    _has_declared_reducer(write_b, ...)` - only needs to see it once to trust the channel).
    """
    log = CausalLog()
    log.add(
        state_write, agent_id="a", span_id="s", key="findings", value="v1", reducer="operator.add"
    )
    log.add(state_write, agent_id="b", span_id="s", key="findings", value="v2")

    (conflict,) = find_conflicts(log.events)
    assert conflict.suppressed_by == "G3"
    assert detect_conflicts(log.events) == ()


def test_g3_declared_reducer_does_not_suppress_an_unrelated_crdt_key() -> None:
    """`crdt_keys` naming a *different* key is a red herring, not a match - still reported.

    Same surface feature as the suppressing case (the caller passed a non-empty `crdt_keys`
    set, proving this isn't "no G3 configuration exists at all"), but it does not name *this*
    conflict's key, so G3 must not fire on it.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2")

    crdt_keys = frozenset({"some_other_key"})
    (conflict,) = find_conflicts(log.events, crdt_keys=crdt_keys)
    assert conflict.suppressed_by is None
    assert len(detect_conflicts(log.events, crdt_keys=crdt_keys)) == 1


# ---------------------------------------------------------------------------------------------
# G4 - explicit lock (write_write only): both writers held the same lock, never concurrently
# ---------------------------------------------------------------------------------------------


def test_g4_explicit_lock_suppresses_writes_under_the_same_lock() -> None:
    """Both writes carry the identical, non-null `lock_id` - the lock already serialised them."""
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1", lock_id="L1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2", lock_id="L1")

    (conflict,) = find_conflicts(log.events)
    assert conflict.suppressed_by == "G4"
    assert detect_conflicts(log.events) == ()


def test_g4_explicit_lock_does_not_suppress_writes_under_different_locks() -> None:
    """Both writes carry *a* lock (the surface feature) but not the *same* one - still a race.

    Two different lock ids protecting the same key is itself a bug (the "lock" isn't mutually
    exclusive across both writers) - exactly the genuine conflict G4 must not paper over.
    """
    log = CausalLog()
    log.add(state_write, agent_id="a", span_id="s", key="k", value="v1", lock_id="L1")
    log.add(state_write, agent_id="b", span_id="s", key="k", value="v2", lock_id="L2")

    (conflict,) = find_conflicts(log.events)
    assert conflict.suppressed_by is None
    assert len(detect_conflicts(log.events)) == 1
