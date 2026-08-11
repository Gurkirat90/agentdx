"""`lock`, `transaction`, `barrier` — the primitives that declare intent (PRD §8.2 item 4).

These exist so a user can tell the race detector what they *meant*, and their entire value
is in the causality they record. The detector lands at P12 and consumes what is written
here; it cannot repair a wrong signal, so the semantics are stated precisely:

* **`lock(key)`** emits `lock_acquire` naming the previous holder's `lock_release` as a
  causal parent. That edge is the happens-before between two critical sections. Without it a
  correctly locked pair of writes looks concurrent and is reported as a race — a false
  positive, which invariant I5 forbids outright.
* **`transaction(name)`** buffers its writes and emits them together at commit, all carrying
  the same `txn_id`. A rolled-back transaction emits nothing. A write that never took effect
  must not appear as one.
* **`barrier(id, participants)`** is a real N-party rendezvous, not a label. It emits
  `barrier(phase=enter)` on arrival and `barrier(phase=release)` when the last participant
  arrives, so PRD §16.2's coordination bucket can attribute the wait.

None of the three creates a happens-before edge through *shared state*. PRD §14.3 is explicit
that shared-state access is not synchronisation; treating it as such is the classic way to
build a race detector that finds nothing.

PRD §8.1 (`sync.py` names all three) · §8.2 item 4 · §14.3, §14.5 · §16.2.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from agentdx.events.schema import EventType
from agentdx.sdk.generic import (
    AgentContext,
    AgentContextError,
    BarrierState,
    StateHandle,
    current_agent,
    current_run,
    emit,
)


@asynccontextmanager
async def lock(key: str) -> AsyncIterator[str]:
    """Acquire a named lock for the block, emitting `lock_acquire` and `lock_release`.

    This is a real mutual exclusion, not an annotation: concurrent tasks contending for the
    same `key` are serialised through an `asyncio.Lock`. A primitive that recorded the
    intent without enforcing it would let the log claim an ordering the run did not have.

    Guarantees:

    * `lock_acquire` names the previous holder's `lock_release` in `causal_parents`. That is
      the happens-before edge PRD §14.3 needs, and it is the whole reason this primitive
      reduces false positives.
    * `wait_virtual_ms` is the virtual time spent waiting. Under the P02 schema, contention
      is exactly `wait_virtual_ms > 0` — the separate `contended` flag was removed at
      Q-P02.1 amendment 1 precisely so the two could not disagree.
    * `lock_release` is emitted on every exit path, including an exception, and the
      underlying lock is always released.
    * While the lock is held, a `state_write` to a key of the same name **made by this
      holder** records `lock_id`, which is what tells the detector the write was
      declared-protected. A concurrent writer that never acquired the lock records
      `lock_id=null`: the flag is a claim about the writer, not about the world.

    Raises:
        RunContextError: no run is active (`E-INSTR-003`).
        AgentContextError: no ambient agent or no open span (`E-INSTR-004`).
    """
    run = current_run()
    agent = current_agent()
    registry = run.registry
    primitive = registry.locks.get(key)
    if primitive is None:
        primitive = asyncio.Lock()
        registry.locks[key] = primitive

    requested_at = run.clock.virtual_ms()
    await primitive.acquire()
    try:
        previous_release = registry.lock_release_seq.get(key)
        acquire_seq = emit(
            run,
            EventType.LOCK_ACQUIRE,
            {
                "lock_id": key,
                "wait_virtual_ms": run.clock.virtual_ms() - requested_at,
            },
            agent_id=agent.agent_id,
            clock_slot=agent.clock_slot,
            span_id=_span_of(agent, "lock_acquire"),
            causes=() if previous_release is None else (previous_release,),
        )
        registry.lock_held_since[key] = run.clock.virtual_ms()
        # The holder's identity, not just "held": `_held_lock` stamps `lock_id` on a
        # `state_write` only when the writer is this scope, so a concurrent writer that
        # never acquired the lock is never recorded as declared-protected.
        registry.lock_holders[key] = agent.clock_slot
        try:
            yield key
        finally:
            registry.lock_holders.pop(key, None)
            held_since = registry.lock_held_since.pop(key, run.clock.virtual_ms())
            registry.lock_release_seq[key] = emit(
                run,
                EventType.LOCK_RELEASE,
                {
                    "lock_id": key,
                    "held_virtual_ms": run.clock.virtual_ms() - held_since,
                },
                agent_id=agent.agent_id,
                clock_slot=agent.clock_slot,
                span_id=_span_of(agent, "lock_release"),
                causes=(acquire_seq,),
            )
    finally:
        primitive.release()


@asynccontextmanager
async def transaction(name: str) -> AsyncIterator[StateHandle]:
    """Group state writes into one atomic, labelled intent (PRD §8.2 item 4).

    Guarantees:

    * Every write made through the yielded handle is buffered and emitted at commit, in the
      order it was made, all carrying the same `txn_id`. PRD §14.5 lets the detector treat
      the group as one intent instead of N unrelated conflicts.
    * **An exception rolls the transaction back and emits nothing.** A `state_write` in the
      log is a claim that the value changed; emitting one for a write that was abandoned
      would make every analyser downstream reason about a value nobody wrote.
    * The `txn_id` is derived from a per-run counter, so it is reproducible across replays
      (AGENTS.md §4.1 bans `uuid4`).

    Raises:
        RunContextError: no run is active (`E-INSTR-003`).
        AgentContextError: a write is attempted with no ambient agent (`E-INSTR-004`).
    """
    run = current_run()
    run.registry.txn_seq += 1
    handle = StateHandle(run, f"txn_{run.registry.txn_seq}_{name}")
    try:
        yield handle
    except BaseException:
        handle.rollback()
        raise
    else:
        handle.commit()


@asynccontextmanager
async def barrier(barrier_id: str, participants: Sequence[str]) -> AsyncIterator[str]:
    """Rendezvous with the other participants, emitting the two `barrier` events.

    Guarantees:

    * `participants` is emitted **sorted**. `barrier.participants` is marked `set_valued` in
      the event schema, and the structural validator rejects an unsorted array with
      `E-EVENT-028` — because a canonicaliser that silently sorted would hide a
      nondeterministic emitter and surface much later as an intermittent gate-G3 failure.
    * `phase="enter"` is emitted on arrival and `phase="release"` when the last participant
      arrives; `wait_virtual_ms` on the release event is that participant's own wait, which
      is what PRD §16.2's blocking-wait bucket needs.
    * The rendezvous is real. If fewer than `len(participants)` arrive, the waiters block
      and the scheduler reports a deadlock (`E-SCHED-002`, P06) — which is often a genuine
      finding, so it is surfaced rather than papered over with a timeout.

    Raises:
        RunContextError: no run is active (`E-INSTR-003`).
        AgentContextError: no ambient agent or no open span (`E-INSTR-004`).
    """
    run = current_run()
    agent = current_agent()
    ordered = sorted(str(name) for name in participants)

    state = run.registry.barriers.get(barrier_id)
    if state is None:
        state = BarrierState(expected=len(ordered))
        run.registry.barriers[barrier_id] = state

    arrived_at = run.clock.virtual_ms()
    enter_seq = emit(
        run,
        EventType.BARRIER,
        {
            "barrier_id": barrier_id,
            "participants": ordered,
            "phase": "enter",
            "wait_virtual_ms": 0,
        },
        agent_id=agent.agent_id,
        clock_slot=agent.clock_slot,
        span_id=_span_of(agent, "barrier"),
    )

    state.arrived += 1
    if state.arrived >= state.expected:
        state.released.set()
    await state.released.wait()

    emit(
        run,
        EventType.BARRIER,
        {
            "barrier_id": barrier_id,
            "participants": ordered,
            "phase": "release",
            "wait_virtual_ms": run.clock.virtual_ms() - arrived_at,
        },
        agent_id=agent.agent_id,
        clock_slot=agent.clock_slot,
        span_id=_span_of(agent, "barrier"),
        causes=(enter_seq,),
    )
    yield barrier_id


def _span_of(agent: AgentContext, event_type: str) -> str:
    """Return the innermost open span id of an agent context.

    Raises:
        AgentContextError: no span is open, so the event cannot be attributed
            (`E-INSTR-004`).
    """
    if agent.span_id is None:
        detail = (
            f"a {event_type} event needs an open span. Synchronisation primitives are used "
            f"inside an instrumented agent — `@agentdx.agent`, `@agentdx.tool` or an "
            f"instrumented LangGraph node — never at module scope"
        )
        raise AgentContextError(detail)
    return agent.span_id


__all__ = ["barrier", "lock", "transaction"]
