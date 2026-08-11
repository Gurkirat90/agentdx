"""Design constraint 4: the sync primitives emit the right events with the right causality.

P12's race detector consumes these signals and cannot repair a wrong one, so each test here
asserts the *causal* shape, not just that an event exists. The one that matters most is
`test_a_second_acquire_names_the_previous_release_as_a_causal_parent`: that edge is the entire
reason `agentdx.lock` reduces false positives, and a lock that emitted its events without it
would look correct and be useless.
"""

from __future__ import annotations

import asyncio

import pytest

import agentdx
from agentdx.events.schema import EventType
from agentdx.events.validators import validate_log
from agentdx.sdk.generic import use_run
from tests.unit.sdk.fakes import make_context


@pytest.mark.asyncio
async def test_a_lock_emits_acquire_and_release_around_the_block() -> None:
    context, recorder = make_context()

    @agentdx.agent("coder")
    async def coder() -> None:
        async with agentdx.lock("draft.module_a"):
            pass

    with use_run(context):
        await coder()

    acquires = recorder.payloads(EventType.LOCK_ACQUIRE)
    releases = recorder.payloads(EventType.LOCK_RELEASE)
    assert acquires == [{"lock_id": "draft.module_a", "wait_virtual_ms": 0}]
    assert releases == [{"lock_id": "draft.module_a", "held_virtual_ms": 0}]
    validate_log(recorder.events)


@pytest.mark.asyncio
async def test_a_second_acquire_names_the_previous_release_as_a_causal_parent() -> None:
    # This is the happens-before edge PRD §14.3 needs. Without it two correctly-locked writes
    # are concurrent in the causality graph and are reported as a race — a false positive,
    # which invariant I5 forbids outright.
    context, recorder = make_context()
    order: list[str] = []
    first_holds = asyncio.Event()

    @agentdx.agent("a")
    async def first() -> None:
        async with agentdx.lock("shared"):
            first_holds.set()
            order.append("a")
            await asyncio.sleep(0)

    @agentdx.agent("b")
    async def second() -> None:
        await first_holds.wait()
        async with agentdx.lock("shared"):
            order.append("b")

    with use_run(context):
        await asyncio.gather(first(), second())

    assert order == ["a", "b"]
    acquires = recorder.of_type(EventType.LOCK_ACQUIRE)
    releases = recorder.of_type(EventType.LOCK_RELEASE)
    assert len(acquires) == 2
    assert acquires[0].causal_parents == []
    assert releases[0].seq in acquires[1].causal_parents, (
        "the second acquire must happen-after the first release, or the two critical "
        "sections look concurrent to the race detector"
    )
    validate_log(recorder.events)


@pytest.mark.asyncio
async def test_a_lock_is_a_real_mutual_exclusion() -> None:
    context, _ = make_context()
    inside = 0
    peak = 0

    @agentdx.agent("worker")
    async def worker() -> None:
        nonlocal inside, peak
        async with agentdx.lock("critical"):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0)
            inside -= 1

    with use_run(context):
        await asyncio.gather(*(worker() for _ in range(5)))

    assert peak == 1


@pytest.mark.asyncio
async def test_a_write_under_a_lock_records_the_lock_id() -> None:
    context, recorder = make_context()

    @agentdx.agent("coder")
    async def coder() -> None:
        async with agentdx.lock("draft.module_a"), agentdx.state() as shared:
            await shared.write("draft.module_a", "v1")

    with use_run(context):
        await coder()

    write = recorder.payloads(EventType.STATE_WRITE)[0]
    assert write["lock_id"] == "draft.module_a"


@pytest.mark.asyncio
async def test_a_write_by_someone_who_does_not_hold_the_lock_records_no_lock_id() -> None:
    # OP-2 finding D5. `lock_id` on a `state_write` is a claim that *this* write was made
    # under *that* lock. The check was "is a lock of this name held by anyone right now",
    # so an unsynchronised writer racing a lock holder was stamped as protected — which is
    # precisely the write a lost-update detector must not be told to trust. The false
    # attribution suppresses the finding the primitive exists to make findable.
    context, recorder = make_context()
    holder_has_it = asyncio.Event()
    intruder_done = asyncio.Event()

    @agentdx.agent("a")
    async def holder() -> None:
        async with agentdx.lock("draft.module_a"), agentdx.state() as shared:
            holder_has_it.set()
            await intruder_done.wait()
            await shared.write("draft.module_a", "from the holder")

    @agentdx.agent("b")
    async def intruder() -> None:
        await holder_has_it.wait()
        async with agentdx.state() as shared:
            await shared.write("draft.module_a", "from someone with no lock")
        intruder_done.set()

    with use_run(context):
        await asyncio.gather(holder(), intruder())

    writes = {
        str(event.agent_id): event.payload["lock_id"]
        for event in recorder.of_type(EventType.STATE_WRITE)
    }
    assert writes["b"] is None, (
        "agent b never acquired the lock; stamping its write with the lock's id tells the "
        "race detector the write was declared-protected when it was not"
    )
    assert writes["a"] == "draft.module_a"
    validate_log(recorder.events)


@pytest.mark.asyncio
async def test_a_transaction_emits_its_writes_together_under_one_txn_id() -> None:
    context, recorder = make_context()

    @agentdx.agent("planner")
    async def planner() -> None:
        async with agentdx.transaction("plan_update") as txn:
            await txn.write("plan", "p")
            await txn.write("constraints", "c")

    with use_run(context):
        await planner()

    writes = recorder.payloads(EventType.STATE_WRITE)
    assert [write["key"] for write in writes] == ["plan", "constraints"]
    txn_ids = {write["txn_id"] for write in writes}
    assert len(txn_ids) == 1
    assert str(next(iter(txn_ids))).endswith("plan_update")
    validate_log(recorder.events)


@pytest.mark.asyncio
async def test_a_rolled_back_transaction_emits_nothing() -> None:
    # A state_write in the log is a claim that the value changed. Emitting one for a write
    # that was abandoned makes every analyser downstream reason about a value nobody wrote.
    context, recorder = make_context()

    class Abort(RuntimeError):
        pass

    @agentdx.agent("planner")
    async def planner() -> None:
        async with agentdx.transaction("doomed") as txn:
            await txn.write("plan", "never applied")
            raise Abort("rollback")

    with use_run(context), pytest.raises(Abort):
        await planner()

    assert recorder.of_type(EventType.STATE_WRITE) == []


@pytest.mark.asyncio
async def test_a_barrier_rendezvouses_and_emits_both_phases_sorted() -> None:
    context, recorder = make_context()
    arrived: list[str] = []

    async def participant(name: str) -> None:
        @agentdx.agent(name)
        async def body() -> None:
            async with agentdx.barrier("sync_point", ["worker_b", "worker_a"]):
                arrived.append(name)

        await body()

    with use_run(context):
        await asyncio.gather(participant("worker_a"), participant("worker_b"))

    barriers = recorder.payloads(EventType.BARRIER)
    assert len(barriers) == 4
    assert {str(event["phase"]) for event in barriers} == {"enter", "release"}
    for event in barriers:
        assert event["participants"] == ["worker_a", "worker_b"], (
            "barrier.participants is set_valued and must be emitted sorted (E-EVENT-028)"
        )
    assert sorted(arrived) == ["worker_a", "worker_b"]
    validate_log(recorder.events)


@pytest.mark.asyncio
async def test_a_sync_primitive_outside_a_span_refuses_to_attribute_itself() -> None:
    context, _ = make_context()

    async def unscoped() -> None:
        async with agentdx.lock("orphan"):
            pass

    with use_run(context), pytest.raises(agentdx.AgentContextError) as caught:
        await unscoped()
    assert "E-INSTR-004" in str(caught.value)
