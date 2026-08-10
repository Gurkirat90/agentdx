"""Authoring source for `tests/golden/event_log_40.jsonl` — the shared analyser fixture.

The *events* are hand-specified below, one line each, in the order a real `code_pipeline`
run would emit them: a planner fans work out to a coder and a reviewer, a latency fault
delays the reviewer's delivery, and both agents blind-write `draft.module_a` concurrently —
the lost update that gate G1 must find.

The *vector clocks* and *fault taint* are computed, not typed, by applying the PRD §14.2
clock rules and the §9.4 rule-2 inheritance. Hand-computing forty vector clocks would
produce a fixture that is wrong in a way nobody notices until an analyser disagrees with it.

Regenerate only on an explicit written instruction (AGENTS.md §5):

    python -m tests.golden.build_event_log_40
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from agentdx.events.canonical import encode_event
from agentdx.events.schema import SCHEMA_VERSION, Event, EventType, PayloadValue

RUN_ID = "r_f2a91"
OUTPUT = pathlib.Path(__file__).parent / "event_log_40.jsonl"


@dataclass
class Spec:
    """One hand-authored event, before clocks and taint are applied."""

    type: EventType
    agent: str | None = None
    span: str | None = None
    parents: Sequence[int] = ()
    dt: int = 0
    payload: Mapping[str, PayloadValue] = field(default_factory=dict)
    fault: str | None = None


def _h(label: str) -> str:
    """Return a stable fake content hash so the fixture reads like a real log."""
    return f"blake2b:{label:0<16}"[:24]


SPECS: list[Spec] = [
    # --- run opens -------------------------------------------------------------------
    Spec(
        EventType.RUN_START,
        dt=0,
        payload={
            "seed": 42,
            "mode": "chaos",
            "cache_mode": "replay",
            "scenario_id": "reviewer_latency",
            "scenario_hash": _h("scen"),
            "graph_hash": _h("graph"),
            "delay_schedule_hash": _h("delay"),
            "calibration_id": "cal_local_01",
            "agentdx_version": "0.1.0",
            "sdk_version": "0.1.0",
            "model": "llama-3.1-8b-instant",
            "provider_host": "api.groq.com",
            "provider_sdk_version": "openai-compat-1",
            "host": "build-01",
            "pid": 4242,
            "started_at_utc": "2026-08-10T09:00:00Z",
            "env": {"PYTHONHASHSEED": "0"},
        },
    ),
    # --- planner decomposes the task -------------------------------------------------
    Spec(
        EventType.SPAN_START,
        "planner",
        "p1",
        [0],
        5,
        {
            "kind": "agent_step",
            "name": "planner.plan",
            "parent_span_id": None,
            "attributes": {"role": "planner"},
        },
    ),
    Spec(
        EventType.LLM_CALL,
        "planner",
        "p1",
        [1],
        40,
        {
            "model": "llama-3.1-8b-instant",
            "params_hash": _h("params"),
            "prompt_hash": _h("pplan"),
            "response_hash": _h("rplan"),
            "prompt_tokens": 812,
            "completion_tokens": 240,
            "cache_status": "hit",
            "cache_key": _h("kplan"),
            "perturbed_from_run": None,
        },
    ),
    Spec(
        EventType.STATE_WRITE,
        "planner",
        "p1",
        [2],
        2,
        {
            "key": "plan.tasks",
            "value_hash": _h("vtasks"),
            "prev_value_hash": None,
            "reducer": None,
            "txn_id": None,
            "lock_id": None,
        },
    ),
    Spec(
        EventType.MESSAGE_SEND,
        "planner",
        "p1",
        [3],
        1,
        {
            "message_id": "m_0a17",
            "to": "coder",
            "edge": "planner->coder",
            "payload_hash": _h("mcoder"),
            "payload_bytes": 4096,
        },
    ),
    Spec(
        EventType.MESSAGE_SEND,
        "planner",
        "p1",
        [3],
        1,
        {
            "message_id": "m_0a18",
            "to": "reviewer",
            "edge": "planner->reviewer",
            "payload_hash": _h("mrev"),
            "payload_bytes": 3072,
        },
    ),
    Spec(
        EventType.SPAN_END,
        "planner",
        "p1",
        [5],
        1,
        {
            "status": "ok",
            "duration_virtual_ms": 50,
            "duration_wall_ms": 7,
            "error_type": None,
            "error_message": None,
        },
    ),
    # --- the fault fires on the reviewer edge ----------------------------------------
    Spec(
        EventType.FAULT_INJECTED,
        None,
        None,
        [5],
        0,
        {
            "fault_id": "f_01",
            "fault_type": "latency",
            "target": "planner->reviewer",
            "params": {"delay_ms": 400, "jitter_ms": 0, "pattern": "constant"},
            "trigger": {"at_virtual_ts": 50},
        },
        fault="f_01",
    ),
    # --- coder works, unblocked ------------------------------------------------------
    Spec(
        EventType.SPAN_START,
        "coder",
        "c1",
        [4],
        2,
        {
            "kind": "agent_step",
            "name": "coder.generate",
            "parent_span_id": None,
            "attributes": {"role": "coder"},
        },
    ),
    Spec(
        EventType.MESSAGE_RECV,
        "coder",
        "c1",
        [4, 8],
        1,
        {
            "message_id": "m_0a17",
            "from": "planner",
            "edge": "planner->coder",
            "delivered_virtual_ts_ms": 58,
            "reordered": False,
            "duplicate": False,
        },
    ),
    Spec(
        EventType.STATE_READ,
        "coder",
        "c1",
        [9],
        1,
        {
            "key": "plan.tasks",
            "value_hash": _h("vtasks"),
            "missing": False,
        },
    ),
    Spec(
        EventType.SPAN_START,
        "coder",
        "c2",
        [10],
        1,
        {
            "kind": "llm_call",
            "name": "coder.draft",
            "parent_span_id": "c1",
            "attributes": {},
        },
    ),
    Spec(
        EventType.LLM_CALL,
        "coder",
        "c2",
        [11],
        300,
        {
            "model": "llama-3.1-8b-instant",
            "params_hash": _h("params"),
            "prompt_hash": _h("pcode"),
            "response_hash": _h("rcode"),
            "prompt_tokens": 1204,
            "completion_tokens": 610,
            "cache_status": "hit",
            "cache_key": _h("kcode"),
            "perturbed_from_run": None,
        },
    ),
    Spec(
        EventType.SPAN_END,
        "coder",
        "c2",
        [12],
        1,
        {
            "status": "ok",
            "duration_virtual_ms": 301,
            "duration_wall_ms": 44,
            "error_type": None,
            "error_message": None,
        },
    ),
    Spec(
        EventType.STATE_READ,
        "coder",
        "c1",
        [13],
        1,
        {
            "key": "draft.module_a",
            "value_hash": _h("null"),
            "missing": True,
        },
    ),
    # --- reviewer starts late because of the fault -----------------------------------
    Spec(
        EventType.SPAN_START,
        "reviewer",
        "r1",
        [5],
        1,
        {
            "kind": "agent_step",
            "name": "reviewer.review",
            "parent_span_id": None,
            "attributes": {"role": "reviewer"},
        },
    ),
    Spec(
        EventType.FAULT_EFFECT,
        "reviewer",
        "r1",
        [7, 15],
        0,
        {
            "fault_id": "f_01",
            "effect": "delay",
            "target": "planner->reviewer",
            "delay_virtual_ms": 400,
            "exception_type": None,
            "message_id": "m_0a18",
        },
    ),
    Spec(
        EventType.MESSAGE_RECV,
        "reviewer",
        "r1",
        [5, 16],
        1,
        {
            "message_id": "m_0a18",
            "from": "planner",
            "edge": "planner->reviewer",
            "delivered_virtual_ts_ms": 452,
            "reordered": False,
            "duplicate": False,
        },
    ),
    Spec(
        EventType.STATE_READ,
        "reviewer",
        "r1",
        [17],
        1,
        {
            "key": "plan.tasks",
            "value_hash": _h("vtasks"),
            "missing": False,
        },
    ),
    Spec(
        EventType.SPAN_START,
        "reviewer",
        "r2",
        [18],
        1,
        {
            "kind": "llm_call",
            "name": "reviewer.assess",
            "parent_span_id": "r1",
            "attributes": {},
        },
    ),
    Spec(
        EventType.LLM_CALL,
        "reviewer",
        "r2",
        [19],
        220,
        {
            "model": "llama-3.1-8b-instant",
            "params_hash": _h("params"),
            "prompt_hash": _h("prev"),
            "response_hash": _h("rrev"),
            "prompt_tokens": 980,
            "completion_tokens": 310,
            "cache_status": "hit",
            "cache_key": _h("krev"),
            "perturbed_from_run": None,
        },
    ),
    Spec(
        EventType.SPAN_END,
        "reviewer",
        "r2",
        [20],
        1,
        {
            "status": "ok",
            "duration_virtual_ms": 221,
            "duration_wall_ms": 33,
            "error_type": None,
            "error_message": None,
        },
    ),
    Spec(
        EventType.STATE_READ,
        "reviewer",
        "r1",
        [21],
        1,
        {
            "key": "draft.module_a",
            "value_hash": _h("null"),
            "missing": True,
        },
    ),
    # --- THE RACE: two concurrent blind writes to the same key (gate G1) -------------
    Spec(
        EventType.STATE_WRITE,
        "coder",
        "c1",
        [14],
        1,
        {
            "key": "draft.module_a",
            "value_hash": _h("vcoder"),
            "prev_value_hash": None,
            "reducer": None,
            "txn_id": None,
            "lock_id": None,
        },
    ),
    Spec(
        EventType.STATE_WRITE,
        "reviewer",
        "r1",
        [22],
        1,
        {
            "key": "draft.module_a",
            "value_hash": _h("vrev"),
            "prev_value_hash": None,
            "reducer": None,
            "txn_id": None,
            "lock_id": None,
        },
    ),
    # --- both report back -------------------------------------------------------------
    Spec(
        EventType.TOOL_CALL,
        "coder",
        "c1",
        [23],
        40,
        {
            "tool": "lint",
            "args_hash": _h("alint"),
            "result_hash": _h("rlint"),
            "status": "ok",
            "duration_virtual_ms": 40,
        },
    ),
    Spec(
        EventType.MESSAGE_SEND,
        "coder",
        "c1",
        [25],
        1,
        {
            "message_id": "m_0b01",
            "to": "planner",
            "edge": "coder->planner",
            "payload_hash": _h("mback1"),
            "payload_bytes": 2048,
        },
    ),
    Spec(
        EventType.SPAN_END,
        "coder",
        "c1",
        [26],
        1,
        {
            "status": "ok",
            "duration_virtual_ms": 392,
            "duration_wall_ms": 61,
            "error_type": None,
            "error_message": None,
        },
    ),
    Spec(
        EventType.MESSAGE_SEND,
        "reviewer",
        "r1",
        [24],
        1,
        {
            "message_id": "m_0b02",
            "to": "planner",
            "edge": "reviewer->planner",
            "payload_hash": _h("mback2"),
            "payload_bytes": 1024,
        },
    ),
    Spec(
        EventType.SPAN_END,
        "reviewer",
        "r1",
        [28],
        1,
        {
            "status": "ok",
            "duration_virtual_ms": 240,
            "duration_wall_ms": 39,
            "error_type": None,
            "error_message": None,
        },
    ),
    # --- planner synthesises under a lock --------------------------------------------
    Spec(
        EventType.SPAN_START,
        "planner",
        "p2",
        [26, 28],
        2,
        {
            "kind": "agent_step",
            "name": "planner.synthesise",
            "parent_span_id": None,
            "attributes": {"role": "planner"},
        },
    ),
    Spec(
        EventType.MESSAGE_RECV,
        "planner",
        "p2",
        [26, 30],
        1,
        {
            "message_id": "m_0b01",
            "from": "coder",
            "edge": "coder->planner",
            "delivered_virtual_ts_ms": 700,
            "reordered": False,
            "duplicate": False,
        },
    ),
    Spec(
        EventType.MESSAGE_RECV,
        "planner",
        "p2",
        [28, 31],
        1,
        {
            "message_id": "m_0b02",
            "from": "reviewer",
            "edge": "reviewer->planner",
            "delivered_virtual_ts_ms": 701,
            "reordered": False,
            "duplicate": False,
        },
    ),
    Spec(
        EventType.STATE_READ,
        "planner",
        "p2",
        [32],
        1,
        {
            "key": "draft.module_a",
            "value_hash": _h("vrev"),
            "missing": False,
        },
    ),
    Spec(
        EventType.LOCK_ACQUIRE,
        "planner",
        "p2",
        [33],
        1,
        {
            "lock_id": "lk_final",
            "wait_virtual_ms": 0,
        },
    ),
    Spec(
        EventType.STATE_WRITE,
        "planner",
        "p2",
        [34],
        2,
        {
            "key": "final.module_a",
            "value_hash": _h("vfinal"),
            "prev_value_hash": None,
            "reducer": None,
            "txn_id": "txn_1",
            "lock_id": "lk_final",
        },
    ),
    Spec(
        EventType.LOCK_RELEASE,
        "planner",
        "p2",
        [35],
        1,
        {
            "lock_id": "lk_final",
            "held_virtual_ms": 3,
        },
    ),
    Spec(
        EventType.SPAN_END,
        "planner",
        "p2",
        [36],
        1,
        {
            "status": "ok",
            "duration_virtual_ms": 9,
            "duration_wall_ms": 2,
            "error_type": None,
            "error_message": None,
        },
    ),
    # --- the run closes ---------------------------------------------------------------
    Spec(
        EventType.ASSERTION_RESULT,
        None,
        None,
        [37],
        1,
        {
            "assertion_id": "success_check",
            "kind": "success_check",
            "passed": False,
            "expected": "final.module_a includes both contributions",
            "actual": "final.module_a includes the reviewer draft only",
        },
    ),
    # run_end's aggregates are filled in by `_close_run` — they are derived quantities, and
    # hand-typing them is how a golden fixture ends up internally inconsistent.
    Spec(EventType.RUN_END, None, None, [38], 1, {"status": "complete"}),
]


def _close_run(events: list[Event]) -> Event:
    """Return the final `run_end` with its aggregates computed from the log itself.

    Guarantees: `virtual_makespan_ms` is the last event's virtual timestamp and
    `wall_makespan_ms` the last event's wall timestamp, so I11 is visible in the fixture —
    the two differ, and only the virtual one survives the canonical projection.
    """
    run_end = events[-1]
    llm = [e for e in events if e.type is EventType.LLM_CALL]
    tools = [e for e in events if e.type is EventType.TOOL_CALL]
    return dataclasses.replace(
        run_end,
        payload={
            **run_end.payload,
            "virtual_makespan_ms": run_end.virtual_ts_ms,
            "wall_makespan_ms": run_end.wall_ts_ms,
            "event_count": len(events),
            "total_llm_calls": len(llm),
            "total_tool_calls": len(tools),
            "total_prompt_tokens": sum(int(e.payload["prompt_tokens"]) for e in llm),  # type: ignore[arg-type]
            "total_completion_tokens": sum(int(e.payload["completion_tokens"]) for e in llm),  # type: ignore[arg-type]
        },
    )


def build() -> list[Event]:
    """Return the fixture log with vector clocks and fault taint applied.

    Guarantees: seq is gapless from 0; virtual_ts_ms is non-decreasing; every
    `causal_parents` entry is < seq; vector clocks follow PRD §14.2 (merge every parent,
    then increment the emitting slot); fault taint follows PRD §9.4 rule 2. The result
    passes `validators.validate_log`.
    """
    clocks: dict[str, dict[str, int]] = {}
    events: list[Event] = []
    virtual_ts = 0

    for seq, spec in enumerate(SPECS):
        virtual_ts += spec.dt
        slot = spec.agent

        merged: dict[str, int] = dict(clocks.get(slot, {})) if slot else {}
        for parent in spec.parents:
            for name, count in events[parent].vclock.items():
                merged[name] = max(merged.get(name, 0), count)
        if slot:
            merged[slot] = merged.get(slot, 0) + 1
            clocks[slot] = dict(merged)

        taint = spec.fault
        if taint is None:
            for parent in spec.parents:
                if events[parent].fault_id is not None:
                    taint = events[parent].fault_id
                    break

        events.append(
            Event(
                schema_version=SCHEMA_VERSION,
                run_id=RUN_ID,
                seq=seq,
                sched_step=seq,
                virtual_ts_ms=virtual_ts,
                wall_ts_ms=seq * 4 + 1,
                vclock=merged,
                type=spec.type,
                causal_parents=list(spec.parents),
                payload=dict(spec.payload),
                agent_id=spec.agent,
                clock_slot=spec.agent,
                span_id=spec.span,
                fault_id=taint,
            )
        )
    events[-1] = _close_run(events)
    return events


def main() -> None:
    """Write the fixture to `event_log_40.jsonl`, validating it first."""
    from agentdx.events.canonical import canonical_log_hash
    from agentdx.events.validators import validate_log

    events = build()
    validate_log(events)
    OUTPUT.write_text("\n".join(encode_event(e) for e in events) + "\n", encoding="utf-8")
    print(f"{len(events)} events -> {OUTPUT}")  # noqa: T201
    print(f"canonical_log_hash: {canonical_log_hash(events)}")  # noqa: T201


if __name__ == "__main__":
    main()
