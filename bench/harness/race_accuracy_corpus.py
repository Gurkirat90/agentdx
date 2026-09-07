"""PRD §34.3's labelled 40-log corpus: 20 genuine races, 20 clean near-misses.

**Why this is a separate file from `race_accuracy.py`.** PRD §34.3: "a labelled corpus of 40
synthetic event logs ... Published as: a confusion matrix with the corpus committed so the
claim is auditable." Auditable means a reviewer can read the corpus — what each of the 40
logs actually is and why it is labelled the way it is — without first reading the timing/
confusion-matrix machinery that consumes it. This file is exactly that: 40 named, documented
log-builder functions and nothing else. `race_accuracy.py` imports `CORPUS` and does the
measuring.

**Every log is built through `CausalLog`** (`tests.analysis.race._causal_log`), not by
hand-setting `vclock`/`causal_parents`. That module's own docstring explains why: the real
scheduler stamps a synthetic linear `[seq - 1]` `causal_parents` fallback on any event with no
declared `causes`, and `CausalLog` reproduces that fallback by construction, so every log in
this corpus is shaped the way a real recorded run actually is, not a shape convenient for the
test writer. `tests/analysis/race/_events.py`'s builders (`state_write`, `state_read`,
`message_send`, `message_recv`) are reused unchanged, for the same reason
`test_true_positive_matrix.py` reuses them.

**The 20 "race" logs** (recall must be 1.0 — every one of these must produce at least one
`detect_conflicts` finding):

1. **write_write_divergent** ×3 — the plain case, PRD §33.8 row 1, different keys.
2. **three_agent_only_two_concurrent** ×2 — PRD §33.8 row 6's shape: a third agent's write is
   causally downstream of both others and must not suppress the genuinely concurrent pair.
3. **same_agent_two_subtasks** ×2 — PRD §33.8 row 7: one `agent_id`, two `clock_slot`s.
4. **stale_read** ×2 — PRD §33.8 row 4 (read concurrent with an earlier write).
5. **dirty_read** ×2 — PRD §33.8 row 5 (write concurrent with an earlier read).
6. **torn_write** ×2 — PRD §33.8 row 10 (a `txn_id`-carrying write racing a read; elevated).
7. **fault_tainted** ×2 — PRD §33.8 row 12 (one side carries a resolved `fault_id`).
8. **mismatched_locks** ×2 — both sides carry a non-null `lock_id`, but not the *same* one; G4
   only suppresses a *shared* lock (`test_guards.py::
   test_g4_explicit_lock_does_not_suppress_writes_under_different_locks`) — a naive "any lock
   present" guard would wrongly suppress this, which is exactly why it belongs in the
   must-detect set rather than the near-miss set.
9. **crdt_keys_names_a_different_key** ×2 — `crdt_keys` is non-empty (the guard is
   "configured") but does not name *this* conflict's key — G3 must not fire on a red herring
   (`test_guards.py::test_g3_declared_reducer_does_not_suppress_an_unrelated_crdt_key`).
10. **four_agent_multi_writer** ×1 — four agents, four divergent concurrent writes to one key;
    multiple simultaneous findings, not just a single pair.

3+2+2+2+2+2+2+2+2+1 = 20.

**The 20 "clean" logs** (precision must be 1.0 — none of these may produce even one finding),
covering PRD §34.3's five named near-miss categories directly:

1. **ordered_by_message** ×4 — a real `message_send`/`message_recv` pair establishes
   happens-before (G1) — PRD §33.8 row 2, "ordered writes."
2. **identical_concurrent_values** ×4 — same key, same value, concurrent (G2) — "identical
   values."
3. **lock_protected** ×4 — both sides share one `lock_id` (G4) — "locks."
4. **reducer_channel** ×4 — one side declares `reducer` (G3) — "reducer channels."
5. **same_agent_retry** ×4 — one agent writes the same key twice in its own program order,
   no `causes=` declared either time; `CausalLog` still carries the slot's vclock forward
   between calls (see its own docstring), so the second write's vclock already dominates the
   first's by construction — genuinely non-concurrent, the same way a real agent retrying its
   own tool call twice never races with itself — "retries."

4×5 = 20.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tests.analysis.race._causal_log import CausalLog
from tests.analysis.race._events import (
    message_recv,
    message_send,
    state_read,
    state_write,
)

from agentdx.events.schema import Event

Label = str  # "race" | "clean" — a plain str, not an enum: this file has no other consumer.


@dataclass(frozen=True, slots=True)
class CorpusCase:
    """One labelled log: an id, its ground truth, why, and the events themselves."""

    case_id: str
    label: Label
    category: str
    description: str
    events: tuple[Event, ...]
    crdt_keys: frozenset[str] = field(default_factory=frozenset)


def _race(case_id: str, category: str, description: str, events: list[Event]) -> CorpusCase:
    return CorpusCase(case_id, "race", category, description, tuple(events))


def _clean(
    case_id: str,
    category: str,
    description: str,
    events: list[Event],
    *,
    crdt_keys: frozenset[str] = frozenset(),
) -> CorpusCase:
    return CorpusCase(case_id, "clean", category, description, tuple(events), crdt_keys)


# ---------------------------------------------------------------------------------------
# 20 "race" logs — recall must be 1.0
# ---------------------------------------------------------------------------------------


def _write_write_divergent(key: str, agent_a: str, agent_b: str) -> list[Event]:
    log = CausalLog()
    log.add(state_write, agent_id=agent_a, span_id="s", key=key, value="v1")
    log.add(state_write, agent_id=agent_b, span_id="s", key=key, value="v2")
    return log.events


def _three_agent_only_two_concurrent(key: str, agents: tuple[str, str, str]) -> list[Event]:
    a, b, c = agents
    log = CausalLog()
    log.add(state_write, agent_id=a, span_id="s", key=key, value="v1")  # 0
    log.add(state_write, agent_id=b, span_id="s", key=key, value="v2")  # 1
    log.add(message_send, agent_id=a, span_id="s", message_id="m_a", to=c)  # 2
    log.add(message_send, agent_id=b, span_id="s", message_id="m_b", to=c)  # 3
    log.add(
        message_recv,
        causes=[2],
        agent_id=c,
        span_id="s",
        message_id="m_a",
        from_=a,
        delivered_virtual_ts_ms=4,
    )  # 4
    log.add(
        message_recv,
        causes=[3],
        agent_id=c,
        span_id="s",
        message_id="m_b",
        from_=b,
        delivered_virtual_ts_ms=5,
    )  # 5
    log.add(state_write, agent_id=c, span_id="s", key=key, value="v3")  # 6, not concurrent
    return log.events


def _same_agent_two_subtasks(key: str, agent: str) -> list[Event]:
    log = CausalLog()
    log.add(
        state_write,
        agent_id=agent,
        span_id="s1",
        key=key,
        value="v1",
        clock_slot=f"{agent}:sub1",
    )
    log.add(
        state_write,
        agent_id=agent,
        span_id="s2",
        key=key,
        value="v2",
        clock_slot=f"{agent}:sub2",
    )
    return log.events


def _stale_read(key: str, agent_a: str, agent_b: str) -> list[Event]:
    log = CausalLog()
    log.add(state_write, agent_id=agent_a, span_id="s", key=key, value="v1")
    log.add(state_read, agent_id=agent_b, span_id="s", key=key, value="v2")
    return log.events


def _dirty_read(key: str, agent_a: str, agent_b: str) -> list[Event]:
    log = CausalLog()
    log.add(state_read, agent_id=agent_a, span_id="s", key=key, value="v1")
    log.add(state_write, agent_id=agent_b, span_id="s", key=key, value="v2")
    return log.events


def _torn_write(key: str, agent_a: str, agent_b: str) -> list[Event]:
    log = CausalLog()
    log.add(state_read, agent_id=agent_a, span_id="s", key=key, value="v1")
    log.add(state_write, agent_id=agent_b, span_id="s", key=key, value="v2", txn_id="txn-1")
    return log.events


def _fault_tainted(key: str, agent_a: str, agent_b: str) -> list[Event]:
    log = CausalLog()
    log.add(
        state_write,
        agent_id=agent_a,
        span_id="s",
        key=key,
        value="v1",
        fault_id="flt_crash_1",
    )
    log.add(state_write, agent_id=agent_b, span_id="s", key=key, value="v2")
    return log.events


def _mismatched_locks(key: str, agent_a: str, agent_b: str) -> list[Event]:
    log = CausalLog()
    log.add(state_write, agent_id=agent_a, span_id="s", key=key, value="v1", lock_id="L1")
    log.add(state_write, agent_id=agent_b, span_id="s", key=key, value="v2", lock_id="L2")
    return log.events


def _crdt_keys_names_a_different_key(key: str, agent_a: str, agent_b: str) -> list[Event]:
    log = CausalLog()
    log.add(state_write, agent_id=agent_a, span_id="s", key=key, value="v1")
    log.add(state_write, agent_id=agent_b, span_id="s", key=key, value="v2")
    return log.events


def _four_agent_multi_writer(key: str, agents: tuple[str, str, str, str]) -> list[Event]:
    log = CausalLog()
    for i, agent in enumerate(agents):
        log.add(state_write, agent_id=agent, span_id="s", key=key, value=f"v{i}")
    return log.events


_RACE_CASES: list[CorpusCase] = [
    _race(
        "race_write_write_01",
        "write_write_divergent",
        "Two agents write different values to the same key, concurrently.",
        _write_write_divergent("draft.body", "a", "b"),
    ),
    _race(
        "race_write_write_02",
        "write_write_divergent",
        "Same shape, a different key and agent pair.",
        _write_write_divergent("plan.status", "coder", "reviewer"),
    ),
    _race(
        "race_write_write_03",
        "write_write_divergent",
        "Same shape again, a third key/agent pair.",
        _write_write_divergent("ticket.priority", "triage", "escalation"),
    ),
    _race(
        "race_three_agent_01",
        "three_agent_only_two_concurrent",
        "PRD §33.8 row 6: a and b race; c's later write is causally downstream of both.",
        _three_agent_only_two_concurrent("k", ("a", "b", "c")),
    ),
    _race(
        "race_three_agent_02",
        "three_agent_only_two_concurrent",
        "Same shape, different names/key.",
        _three_agent_only_two_concurrent("summary", ("planner", "worker", "merger")),
    ),
    _race(
        "race_same_agent_subtasks_01",
        "same_agent_two_subtasks",
        "One agent, two concurrent subtasks (distinct clock_slots), same key.",
        _same_agent_two_subtasks("shared.counter", "planner"),
    ),
    _race(
        "race_same_agent_subtasks_02",
        "same_agent_two_subtasks",
        "Same shape, a different agent/key.",
        _same_agent_two_subtasks("cache.entry", "researcher"),
    ),
    _race(
        "race_stale_read_01",
        "stale_read",
        "A read concurrent with an earlier write on the same key.",
        _stale_read("draft.body", "a", "b"),
    ),
    _race(
        "race_stale_read_02",
        "stale_read",
        "Same shape, different key/agents.",
        _stale_read("status.flag", "monitor", "worker"),
    ),
    _race(
        "race_dirty_read_01",
        "dirty_read",
        "A write concurrent with an earlier read on the same key.",
        _dirty_read("draft.body", "a", "b"),
    ),
    _race(
        "race_dirty_read_02",
        "dirty_read",
        "Same shape, different key/agents.",
        _dirty_read("queue.head", "consumer", "producer"),
    ),
    _race(
        "race_torn_write_01",
        "torn_write",
        "A txn_id-carrying write races a concurrent read — elevated severity.",
        _torn_write("ledger.balance", "a", "b"),
    ),
    _race(
        "race_torn_write_02",
        "torn_write",
        "Same shape, different key/agents.",
        _torn_write("account.state", "auditor", "writer"),
    ),
    _race(
        "race_fault_tainted_01",
        "fault_tainted",
        "One side carries a resolved agent_crash fault_id — still reported, tainted.",
        _fault_tainted("draft.body", "a", "b"),
    ),
    _race(
        "race_fault_tainted_02",
        "fault_tainted",
        "Same shape, different key/agents.",
        _fault_tainted("job.state", "worker_1", "worker_2"),
    ),
    _race(
        "race_mismatched_locks_01",
        "mismatched_locks",
        "Both sides hold a lock, but not the same one — G4 must not suppress this.",
        _mismatched_locks("resource.x", "a", "b"),
    ),
    _race(
        "race_mismatched_locks_02",
        "mismatched_locks",
        "Same shape, different key/agents/lock ids.",
        _mismatched_locks("resource.y", "worker_a", "worker_b"),
    ),
    _race(
        "race_crdt_wrong_key_01",
        "crdt_keys_names_a_different_key",
        "crdt_keys is configured but names a different key — G3 must not fire here.",
        _crdt_keys_names_a_different_key("draft.body", "a", "b"),
    ),
    _race(
        "race_crdt_wrong_key_02",
        "crdt_keys_names_a_different_key",
        "Same shape, different key/agents.",
        _crdt_keys_names_a_different_key("summary.text", "writer_1", "writer_2"),
    ),
    _race(
        "race_four_agent_multi_writer_01",
        "four_agent_multi_writer",
        "Four agents write four divergent values to one key, all concurrent.",
        _four_agent_multi_writer("shared.state", ("a", "b", "c", "d")),
    ),
]

# The two "race" cases whose surface features look like a suppression guard's trigger, run
# with the guard *configured* — proving the guard's own precision, not just its recall.
_RACE_CASES[17] = CorpusCase(
    _RACE_CASES[17].case_id,
    "race",
    _RACE_CASES[17].category,
    _RACE_CASES[17].description,
    _RACE_CASES[17].events,
    crdt_keys=frozenset({"some_other_key"}),
)
_RACE_CASES[18] = CorpusCase(
    _RACE_CASES[18].case_id,
    "race",
    _RACE_CASES[18].category,
    _RACE_CASES[18].description,
    _RACE_CASES[18].events,
    crdt_keys=frozenset({"a_third_key"}),
)

if len(_RACE_CASES) != 20:
    detail = f"expected 20 race cases, got {len(_RACE_CASES)}"
    raise RuntimeError(detail)


# ---------------------------------------------------------------------------------------
# 20 "clean" logs — precision must be 1.0
# ---------------------------------------------------------------------------------------


def _ordered_by_message(key: str, agent_a: str, agent_b: str) -> list[Event]:
    log = CausalLog()
    log.add(state_write, agent_id=agent_a, span_id="s", key=key, value="v1")
    log.add(message_send, agent_id=agent_a, span_id="s", message_id="m1", to=agent_b)
    log.add(
        message_recv,
        causes=[1],
        agent_id=agent_b,
        span_id="s",
        message_id="m1",
        from_=agent_a,
        delivered_virtual_ts_ms=2,
    )
    log.add(state_write, agent_id=agent_b, span_id="s", key=key, value="v2")
    return log.events


def _identical_concurrent_values(key: str, agent_a: str, agent_b: str, value: str) -> list[Event]:
    log = CausalLog()
    log.add(state_write, agent_id=agent_a, span_id="s", key=key, value=value)
    log.add(state_write, agent_id=agent_b, span_id="s", key=key, value=value)
    return log.events


def _lock_protected(key: str, agent_a: str, agent_b: str, lock_id: str) -> list[Event]:
    log = CausalLog()
    log.add(state_write, agent_id=agent_a, span_id="s", key=key, value="v1", lock_id=lock_id)
    log.add(state_write, agent_id=agent_b, span_id="s", key=key, value="v2", lock_id=lock_id)
    return log.events


def _reducer_channel(key: str, agent_a: str, agent_b: str, reducer: str) -> list[Event]:
    log = CausalLog()
    log.add(
        state_write,
        agent_id=agent_a,
        span_id="s",
        key=key,
        value="v1",
        reducer=reducer,
    )
    log.add(state_write, agent_id=agent_b, span_id="s", key=key, value="v2")
    return log.events


def _same_agent_retry(key: str, agent: str, retries: int) -> list[Event]:
    log = CausalLog()
    for i in range(retries):
        log.add(state_write, agent_id=agent, span_id=f"s{i}", key=key, value=f"attempt_{i}")
    return log.events


_CLEAN_CASES: list[CorpusCase] = [
    _clean(
        "clean_ordered_message_01",
        "ordered_by_message",
        "A real message_send/message_recv pair establishes happens-before.",
        _ordered_by_message("draft.body", "a", "b"),
    ),
    _clean(
        "clean_ordered_message_02",
        "ordered_by_message",
        "Same shape, different key/agents.",
        _ordered_by_message("plan.next_step", "planner", "executor"),
    ),
    _clean(
        "clean_ordered_message_03",
        "ordered_by_message",
        "Same shape, a third key/agent pair.",
        _ordered_by_message("handoff.payload", "upstream", "downstream"),
    ),
    _clean(
        "clean_ordered_message_04",
        "ordered_by_message",
        "Same shape, a fourth key/agent pair.",
        _ordered_by_message("review.notes", "author", "editor"),
    ),
    _clean(
        "clean_identical_values_01",
        "identical_concurrent_values",
        "Same key, same value, concurrent — idempotent, nothing lost.",
        _identical_concurrent_values("status.ready", "a", "b", "true"),
    ),
    _clean(
        "clean_identical_values_02",
        "identical_concurrent_values",
        "Same shape, a different key/value/agents.",
        _identical_concurrent_values("config.locale", "worker_1", "worker_2", "en-US"),
    ),
    _clean(
        "clean_identical_values_03",
        "identical_concurrent_values",
        "Same shape, a third case.",
        _identical_concurrent_values("mode.flag", "x", "y", "strict"),
    ),
    _clean(
        "clean_identical_values_04",
        "identical_concurrent_values",
        "Same shape, a fourth case.",
        _identical_concurrent_values("phase", "p1", "p2", "review"),
    ),
    _clean(
        "clean_lock_protected_01",
        "lock_protected",
        "Both writes carry the same non-null lock_id — already serialised.",
        _lock_protected("resource.x", "a", "b", "L1"),
    ),
    _clean(
        "clean_lock_protected_02",
        "lock_protected",
        "Same shape, different key/agents/lock.",
        _lock_protected("resource.y", "worker_a", "worker_b", "L2"),
    ),
    _clean(
        "clean_lock_protected_03",
        "lock_protected",
        "Same shape, a third case.",
        _lock_protected("resource.z", "c1", "c2", "L3"),
    ),
    _clean(
        "clean_lock_protected_04",
        "lock_protected",
        "Same shape, a fourth case.",
        _lock_protected("resource.w", "d1", "d2", "L4"),
    ),
    _clean(
        "clean_reducer_channel_01",
        "reducer_channel",
        "One writer declares a reducer on the channel — a real merge function applies.",
        _reducer_channel("findings", "a", "b", "operator.add"),
    ),
    _clean(
        "clean_reducer_channel_02",
        "reducer_channel",
        "Same shape, different key/agents/reducer.",
        _reducer_channel("log_lines", "collector_1", "collector_2", "list_append"),
    ),
    _clean(
        "clean_reducer_channel_03",
        "reducer_channel",
        "Same shape, a third case.",
        _reducer_channel("totals", "worker_a", "worker_b", "operator.add"),
    ),
    _clean(
        "clean_reducer_channel_04",
        "reducer_channel",
        "Same shape, a fourth case.",
        _reducer_channel("messages", "agent_x", "agent_y", "list_append"),
    ),
    _clean(
        "clean_same_agent_retry_01",
        "same_agent_retry",
        "One agent retries its own write to the same key twice — never races with itself.",
        _same_agent_retry("tool.result", "worker", 2),
    ),
    _clean(
        "clean_same_agent_retry_02",
        "same_agent_retry",
        "Same shape, three attempts.",
        _same_agent_retry("api.response", "caller", 3),
    ),
    _clean(
        "clean_same_agent_retry_03",
        "same_agent_retry",
        "Same shape, a different agent/key.",
        _same_agent_retry("draft.section", "writer", 2),
    ),
    _clean(
        "clean_same_agent_retry_04",
        "same_agent_retry",
        "Same shape, four attempts.",
        _same_agent_retry("fetch.result", "fetcher", 4),
    ),
]

if len(_CLEAN_CASES) != 20:
    detail = f"expected 20 clean cases, got {len(_CLEAN_CASES)}"
    raise RuntimeError(detail)


CORPUS: tuple[CorpusCase, ...] = tuple(_RACE_CASES + _CLEAN_CASES)
"""The full labelled 40-log corpus, race cases first, then clean cases — PRD §34.3."""

if len(CORPUS) != 40:
    detail = f"expected 40 total cases, got {len(CORPUS)}"
    raise RuntimeError(detail)
if len({c.case_id for c in CORPUS}) != 40:
    detail = "case_id collision in the corpus"
    raise RuntimeError(detail)

__all__ = ["CORPUS", "CorpusCase"]
