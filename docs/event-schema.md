# AgentDX event schema — the data contract

> **Status:** schema_version **1**. Freezes at the end of week 1 (CONTEXT.md §3).
> After the freeze, changing anything in this document invalidates every recorded run.
>
> **Normative sources:** PRD §9.1–9.9, §10.1, §10.7, §14.2. Where this document and the PRD
> differ, §11 below records the ruling and the reason. Nothing here was decided silently.

This document is written so that a compatible writer can be implemented in another
language from it alone. Everything a second implementation needs — field list, volatility
marks, exact serialisation bytes, hash construction, validation rules, error codes — is
specified here rather than left to "match what the Python does".

---

## 1. What the contract is for

The event log is append-only and is the single source of truth. Every analyser, the UI and
the replay engine read only from this log; no analyser touches live agent objects.

Two properties have to hold, and everything in this document exists to serve one of them.

**I1 — determinism.** Given the same `(graph identity, scenario, seed, LLM cache contents,
delay schedule, AgentDX version)`, two executions produce logs whose **canonical
projections are byte-identical**. The projection (§4) is the mechanism that makes this
assertable in CI, because a raw log can never be byte-identical — `wall_ts_ms` varies by
construction.

**I2 — append-only.** Nothing edits or deletes an event after write. The hash chain (§6)
makes a violation detectable rather than merely forbidden, which matters because a bundle
arriving from another machine is untrusted input.

There is a third property that is not an invariant but is the reason the design looks the
way it does: **the projection must be derived from the schema, not maintained beside it.**
A hand-kept exclusion list drifts. When it drifts one of two things happens — a volatile
field leaks into the projection and gate G3 can never pass, or a meaningful field is
excluded and G3 passes while the system is nondeterministic. The second failure is worse,
because it is silent. See §11 R-3 for the instance of it already present in the PRD.

---

## 2. Volatility is a property of the field

Every field carries exactly one mark. The mark lives in `src/agentdx/events/schema.py`
next to the field's type, and the canonical projection is computed from it.

| Mark | Meaning | In the canonical projection? |
|---|---|---|
| `stable` | Semantically meaningful. Two runs that differ here are different runs. | **yes** |
| `volatile` | Cannot be deterministic by nature: wall clock, host, pid, environment. | no |
| `identity` | Identifies the *recording*, not the run's behaviour. Differs between two otherwise identical executions by construction. | no |

The third mark is not decoration. `run_id` is neither volatile nor stable: nothing about
the machine leaks into it, yet two replays must not share one or they collide on
`runs.run_id PRIMARY KEY`. Marking it volatile would mislead every future reader; marking
it stable would make gate G3 unpassable. See §11 R-1.

**Rule for implementers:** a field with no mark is not a field. Do not add one without
deciding its mark, and do not decide a mark by intuition — it is a gate-level decision.

---

## 3. Event shape

Every event is a JSON object with the fields in §5, plus a type-specific `payload` (§7).

Ordering guarantees the writer enforces and every reader may assume:

* `seq` is gapless from 0 and totally ordered within a run.
* `virtual_ts_ms` is non-decreasing in `seq`.
* `wall_ts_ms` is non-decreasing in `seq` but is **not** a reflection of virtual order.
* every `causal_parents` entry is `< seq`, so a log is topologically sorted by construction
  and an analyser can process it in a single forward pass.

**Time.** Any unqualified duration is virtual time (invariant I11). Wall quantities carry a
`_wall_ms` suffix without exception, and every one of them is volatile.

---

## 4. The canonical projection

To canonicalise an event:

1. Drop every top-level field whose mark is not `stable`.
2. Drop every payload sub-field whose mark is not `stable`.
3. Normalise `vclock` (§5.1).
4. Serialise with the rules in §5.

The complete generated exclusion list is at the end of §7. It is emitted by
`schema.excluded_field_paths()` and asserted against this document by
`tests/unit/events/test_docs_agree_with_schema.py`, so the two cannot disagree.

The log hash is:

```
canonical_log_hash(events):
    h = blake2b(digest_size=32)
    for event in events:
        h.update(canonical_bytes(event) + b"\n")
    return "blake2b:" + h.hexdigest()
```

The `\n` separator is load-bearing: without it, two adjacent events could be re-partitioned
into a different pair with the same digest.

---

## 5. Exact serialisation — the bytes

This section is the part a second implementation must match exactly.

**Encoding.** UTF-8. No byte-order mark.

**Whitespace.** None. `,` between elements and members, `:` between key and value, nothing
else.

**Object keys.** NFC-normalised, then sorted ascending by Unicode code point. For valid
Unicode this is byte-for-byte identical to sorting the UTF-8 encodings. This deliberately
**differs from RFC 8785 (JCS)**, which sorts by UTF-16 code unit and therefore orders
astral-plane keys differently. If you are porting from a JCS library, this is the one place
you must not reuse it.

**Arrays.** Order is preserved and never sorted. A set-valued field — `barrier.participants`,
`schedule_decision.ready_task_ids` — is the **emitter's** responsibility to write in sorted
order. The canonicaliser does not reorder data, because a canonicaliser that silently
reorders cannot tell you that your emitter is nondeterministic.

**Strings.** NFC-normalised. Escape exactly these and nothing else:

| Character | Emitted as |
|---|---|
| `"` | `\"` |
| `\` | `\\` |
| U+0008 | `\b` |
| U+000C | `\f` |
| U+000A | `\n` |
| U+000D | `\r` |
| U+0009 | `\t` |
| other U+0000–U+001F | `\u00xx`, lowercase hex, four digits |

Every other character, including all non-ASCII, is emitted literally. Do not use
`ensure_ascii`-style escaping: libraries disagree about which characters they *may* escape
but agree on which they *must*, so minimal escaping is the only reproducible choice.

**Numbers.** Integers only, in decimal, no leading zeros, no `+`, no exponent, no
fractional part. `-5` is `-5`.

**Floats are forbidden anywhere in the log** — including inside open sub-objects such as
`span_start.attributes`, `fault_injected.params` and `run_start.payload.env`. Emitting one
is `E-EVENT-013`. Rationale and the one known casualty are in §11 R-4.

**Booleans and null.** `true`, `false`, `null`. Note that in Python `bool` is a subclass of
`int`; check it first or you will emit `1`.

### 5.1 Vector clocks

A vector clock is a sparse map from clock slot to counter. **An omitted slot reads as 0.**
Canonicalisation therefore must:

* drop every entry whose value is 0, and
* sort the remaining keys.

Without this, two semantically identical logs hash differently purely because of which
agents happened to exist when each event was stamped. `{"a":1}` and `{"a":1,"b":0}` are the
same clock and must produce the same bytes.

The clock carried on an event is the **post-increment** snapshot of the emitting slot's
clock, after applying the PRD §14.2 rule for that event kind (local increment, send, merge
on receive, merge on lock acquire, all-to-all on barrier).

---

## 6. The hash chain

Each stored event carries `prev_hash` and `this_hash` **beside** it — they are `events`
table columns (PRD §38), never fields of the event. An event whose canonical form contained
its own hash would be self-referential, which is why PRD §9.2 lists neither. See §11 C-4.

```
CHAIN_GENESIS = "blake2b:" + "0" * 64

this_hash(prev_hash, event) =
    "blake2b:" + blake2b_256( utf8(prev_hash) || 0x0A || canonical_bytes(event) )
```

The first event in a run uses `CHAIN_GENESIS` as `prev_hash`. Genesis is an explicit
constant rather than an empty string so that a truncated log cannot be mistaken for a
complete one whose chain happens to verify.

**The chain covers the canonical projection only.** Chaining the volatile fields would give
every run a unique chain and destroy the point: a recipient could no longer recompute the
chain of a bundle produced on another machine and compare it.


---

## 5.2 Top-level fields

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `schema_version` | `int` | yes | stable | Rejects logs written by an incompatible SDK. |
| `run_id` | `str` | yes | **identity** | `r_` + 5 hex of a content hash (PRD §6.1). IDENTITY, not stable: two replays of the same run must differ here or they collide on runs.run_id, so including it in the projection would make G3 unpassable by construction. Ruling R1. |
| `seq` | `int` | yes | stable | Assigned by the runtime under the scheduler lock. Gapless from 0, totally ordered. |
| `sched_step` | `int` | yes | stable | Scheduler decision index. Several events may share one step. |
| `virtual_ts_ms` | `int` | yes | stable | Virtual clock. Monotonic non-decreasing with seq. I11: unqualified time is virtual. |
| `wall_ts_ms` | `int` | yes | **volatile** | Real elapsed ms, for overhead accounting only. The archetypal volatile field. |
| `agent_id` | `str` \| null | no | stable | null for run-scope events emitted by the runtime itself. |
| `clock_slot` | `str` \| null | no | stable | Vector-clock slot. Defaults to agent_id; distinct for intra-agent concurrency. |
| `vclock` | `vclock` | yes | stable | Sparse map, post-increment snapshot per PRD §14.2. Omitted slots are implicitly 0. |
| `type` | `str` | yes | stable | Closed enum. An unknown type is a validation error, never a pass-through.<br>one of: `assertion_result`, `barrier`, `fault_effect`, `fault_injected`, `instrumentation_gap`, `llm_call`, `lock_acquire`, `lock_release`, `message_recv`, `message_send`, `nondeterminism_warning`, `run_end`, `run_start`, `schedule_decision`, `span_end`, `span_start`, `state_read`, `state_write`, `tool_call` |
| `span_id` | `str` \| null | no | stable | Required for span-scoped types, forbidden for run-scoped types (EVENT_SCOPES). |
| `causal_parents` | `int[]` | yes | stable | Every entry is a seq < this event's seq, so the log is topologically sorted. |
| `fault_id` | `str` \| null | no | stable | Taint marker. Present iff causally downstream of a fault (PRD §9.4). |
| `payload` | `object` | yes | per sub-field | Type-specific; see §7 |


## 7. Payload schemas, one per type


### `run_start`  ·  scope: run

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `seed` | `int` | yes | stable | *[derived — see §10]* PRD §10.10. |
| `mode` | `str` | yes | stable | *[derived — see §10]* Run mode per PRD §6.1. Distinct from cache_mode.<br>one of: `baseline`, `chaos`, `explore`, `replay` |
| `cache_mode` | `str` | yes | stable | *[derived — see §10]* LLM cache mode per PRD §11.2. Orthogonal to mode.<br>one of: `passthrough`, `perturb`, `record`, `replay` |
| `scenario_id` | `str` \| null | yes | stable | *[derived — see §10]*  |
| `scenario_hash` | `str` | yes | stable | *[derived — see §10]*  |
| `graph_hash` | `str` | yes | stable | *[derived — see §10]*  |
| `delay_schedule_hash` | `str` | yes | stable | *[derived — see §10]* The §14 delay-schedule signature, not the schedule itself. |
| `calibration_id` | `str` \| null | yes | stable | *[derived — see §10]*  |
| `agentdx_version` | `str` | yes | stable | *[derived — see §10]*  |
| `sdk_version` | `str` | yes | stable | *[derived — see §10]* PRD §9.9. |
| `model` | `str` | yes | stable | *[derived — see §10]* PRD §11.5. |
| `provider_host` | `str` | yes | stable | *[derived — see §10]*  |
| `provider_sdk_version` | `str` | yes | stable | *[derived — see §10]*  |
| `host` | `str` | yes | **volatile** | Named in PRD §10.7. |
| `pid` | `int` | yes | **volatile** | Named in PRD §10.7. |
| `started_at_utc` | `str` | yes | **volatile** | Named in §10.7. |
| `env` | `object` | yes | **volatile** | Named in PRD §10.7. |

### `run_end`  ·  scope: run

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `status` | `str` | yes | stable | *[derived — see §10]* <br>one of: `aborted`, `complete`, `failed`, `timeout` |
| `virtual_makespan_ms` | `int` | yes | stable | *[derived — see §10]*  |
| `wall_makespan_ms` | `int` | yes | **volatile** | *[derived — see §10]* VOLATILE by construction and ABSENT from the PRD §10.7 exclusion list that calls itself exhaustive — the second field that would have made G3 unpassable. Found at P02, ruling R3. |
| `event_count` | `int` | yes | stable | *[derived — see §10]*  |
| `total_llm_calls` | `int` | yes | stable | *[derived — see §10]*  |
| `total_tool_calls` | `int` | yes | stable | *[derived — see §10]*  |
| `total_prompt_tokens` | `int` | yes | stable | *[derived — see §10]*  |
| `total_completion_tokens` | `int` | yes | stable | *[derived — see §10]*  |

### `span_start`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `kind` | `str` | yes | stable | <br>one of: `agent_step`, `handoff`, `llm_call`, `tool_call`, `wait` |
| `name` | `str` | yes | stable |  |
| `parent_span_id` | `str` \| null | yes | stable |  |
| `attributes` | `object` | yes | stable | Open user-supplied map; carries retry_of for retry chains (PRD §10.9). |

### `span_end`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `status` | `str` | yes | stable | <br>one of: `cancelled`, `crashed`, `error`, `ok`, `timeout` |
| `duration_virtual_ms` | `int` | yes | stable |  |
| `duration_wall_ms` | `int` | yes | **volatile** | Named explicitly in the PRD §10.7 exclusion list. |
| `error_type` | `str` \| null | yes | stable |  |
| `error_message` | `str` \| null | yes | stable | STABLE per PRD §10.7 ('every other field participates'). See docs §11 R-3: a repr containing a memory address would leak here despite §10.6 claiming identity is never address-derived. Flagged, not silently excluded. |

### `message_send`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `message_id` | `str` | yes | stable |  |
| `to` | `str` | yes | stable |  |
| `edge` | `str` | yes | stable |  |
| `payload_hash` | `str` | yes | stable |  |
| `payload_bytes` | `int` | yes | stable |  |

### `message_recv`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `message_id` | `str` | yes | stable |  |
| `from` | `str` | yes | stable |  |
| `edge` | `str` | yes | stable |  |
| `delivered_virtual_ts_ms` | `int` | yes | stable |  |
| `reordered` | `bool` | yes | stable |  |
| `duplicate` | `bool` | yes | stable |  |

### `state_read`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `key` | `str` | yes | stable |  |
| `value_hash` | `str` | yes | stable |  |
| `missing` | `bool` | yes | stable |  |
| `value` | `str` | no | stable | Present only under capture_bodies=True; never instead of the hash (PRD §9.5, I8). |

### `state_write`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `key` | `str` | yes | stable |  |
| `value_hash` | `str` | yes | stable |  |
| `prev_value_hash` | `str` \| null | yes | stable |  |
| `reducer` | `str` \| null | yes | stable |  |
| `txn_id` | `str` \| null | yes | stable |  |
| `lock_id` | `str` \| null | yes | stable |  |
| `value` | `str` | no | stable | Present only under capture_bodies=True; never instead of the hash (PRD §9.5, I8). |

### `tool_call`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `tool` | `str` | yes | stable |  |
| `args_hash` | `str` | yes | stable |  |
| `result_hash` | `str` | yes | stable |  |
| `status` | `str` | yes | stable | <br>one of: `error`, `ok` |
| `duration_virtual_ms` | `int` | yes | stable |  |
| `args` | `str` | no | stable | Present only under capture_bodies=True; never instead of the hash (PRD §9.5, I8). |
| `result` | `str` | no | stable | Present only under capture_bodies=True; never instead of the hash (PRD §9.5, I8). |

### `llm_call`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `model` | `str` | yes | stable |  |
| `params_hash` | `str` | yes | stable |  |
| `prompt_hash` | `str` | yes | stable |  |
| `response_hash` | `str` | yes | stable |  |
| `prompt_tokens` | `int` | yes | stable |  |
| `completion_tokens` | `int` | yes | stable |  |
| `cache_status` | `str` | yes | stable | STABLE: PRD §10.1 pins 'LLM cache contents' as an input to the determinism definition, so two runs over the same cache agree here. G3 compares replays with replays, never a record run with a replay.<br>one of: `hit`, `miss_error`, `miss_recorded`, `perturbed` |
| `cache_key` | `str` | yes | stable | STABLE, ruling R2. The §10.7 code pops this; the §10.7 prose exclusion list (which calls itself exhaustive) does not, and §11.4 guarantees the key carries no machine-local salt so a cache is portable between machines. Excluding it would hide a genuine prompt divergence — the failure mode that is worse than an unpassable gate, because it passes. |
| `perturbed_from_run` | `str` \| null | yes | **identity** | A run_id, and inherits run_id's mark for the same reason (ruling R1). |
| `prompt` | `str` | no | stable | Present only under capture_bodies=True; never instead of the hash (PRD §9.5, I8). |
| `response` | `str` | no | stable | Present only under capture_bodies=True; never instead of the hash (PRD §9.5, I8). |

### `fault_injected`  ·  scope: run_or_span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `fault_id` | `str` | yes | stable |  |
| `fault_type` | `str` | yes | stable |  |
| `target` | `str` | yes | stable |  |
| `params` | `object` | yes | stable |  |
| `trigger` | `object` | yes | stable |  |

### `fault_effect`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `fault_id` | `str` | yes | stable | *[derived — see §10]*  |
| `effect` | `str` | yes | stable | *[derived — see §10]* One per MVP fault type (CONTEXT.md §3: latency, agent_crash, message_drop, tool_failure). The six P1 fault types will extend this enum in a minor bump.<br>one of: `crash`, `delay`, `drop`, `exception` |
| `target` | `str` | yes | stable | *[derived — see §10]*  |
| `delay_virtual_ms` | `int` \| null | yes | stable | *[derived — see §10]*  |
| `exception_type` | `str` \| null | yes | stable | *[derived — see §10]*  |
| `message_id` | `str` \| null | yes | stable | *[derived — see §10]*  |

### `lock_acquire`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `lock_id` | `str` | yes | stable | *[derived — see §10]*  |
| `wait_virtual_ms` | `int` | yes | stable | *[derived — see §10]* Feeds the coordination-overhead bucket in PRD §16.2. |
| `contended` | `bool` | yes | stable | *[derived — see §10]*  |

### `lock_release`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `lock_id` | `str` | yes | stable | *[derived — see §10]*  |
| `held_virtual_ms` | `int` | yes | stable | *[derived — see §10]*  |

### `barrier`  ·  scope: span

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `barrier_id` | `str` | yes | stable | *[derived — see §10]*  |
| `participants` | `str[]` | yes | stable | *[derived — see §10]* Canonicalised sorted; a set-valued field must not carry iteration order. |
| `phase` | `str` | yes | stable | *[derived — see §10]* <br>one of: `enter`, `release` |
| `wait_virtual_ms` | `int` | yes | stable | *[derived — see §10]*  |

### `schedule_decision`  ·  scope: run

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `chosen_task_id` | `str` | yes | stable | *[derived — see §10]*  |
| `ready_task_ids` | `str[]` | yes | stable | *[derived — see §10]* Canonicalised sorted, for the same reason as barrier.participants. |
| `reason` | `str` | yes | stable | *[derived — see §10]*  |
| `virtual_ready_ts_ms` | `int` | yes | stable | *[derived — see §10]*  |

### `instrumentation_gap`  ·  scope: run

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `construct` | `str` | yes | stable | *[derived — see §10]*  |
| `location` | `str` | yes | stable | *[derived — see §10]*  |
| `reason` | `str` | yes | stable | *[derived — see §10]*  |

### `nondeterminism_warning`  ·  scope: run

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `source` | `str` | yes | stable | *[derived — see §10]* One per row of the PRD §10.6 table that is detectable at runtime.<br>one of: `ambient_clock`, `ambient_random`, `ambient_uuid`, `live_model_call`, `os_thread`, `unmanaged_io`, `unordered_iteration` |
| `detail` | `str` | yes | stable | *[derived — see §10]*  |
| `location` | `str` \| null | yes | stable | *[derived — see §10]*  |

### `assertion_result`  ·  scope: run

| Field | Type | Required | Volatility | Notes |
|---|---|---|---|---|
| `assertion_id` | `str` | yes | stable | *[derived — see §10]*  |
| `kind` | `str` | yes | stable | *[derived — see §10]* <br>one of: `assertion`, `steady_state_hypothesis`, `success_check` |
| `passed` | `bool` | yes | stable | *[derived — see §10]*  |
| `expected` | `str` \| null | yes | stable | *[derived — see §10]*  |
| `actual` | `str` \| null | yes | stable | *[derived — see §10]*  |


### Generated exclusion list

```
llm_call.payload.perturbed_from_run
run_end.payload.wall_makespan_ms
run_id
run_start.payload.env
run_start.payload.host
run_start.payload.pid
run_start.payload.started_at_utc
span_end.payload.duration_wall_ms
wall_ts_ms
```

---

## 8. Validation

Three layers, each independently callable, matching PRD §9.8's requirement that they have
different failure behaviour.

**(a) Structural** — types, required fields, closed enum membership, no floats. Always
raises. A malformed event is a bug in the emitter, not data to be tolerated.

**(b) Semantic** — one event against its immediate predecessor: gapless `seq`,
non-decreasing `virtual_ts_ms`, `causal_parents < seq` with no duplicates, `span_id` present
exactly where the scope table requires it, no vector-clock regression for the emitting slot.

**(c) Cross-event** — whole-log: every causal parent exists, no parent's clock is ahead of
its child's in any slot, fault taint descends through `causal_parents`, one `run_id` per log.

### What cross-event validation deliberately does *not* check

PRD §9.4 gives three ways an event acquires a `fault_id`. Rules 1 and 2 are recorded in the
log and are checked. **Rule 3 is not checkable**: it taints an agent through its *context*
once it has consumed a faulted input, and that edge is not in the log. So an event carrying
a `fault_id` with no tainted causal parent is **accepted**, and the "iff" in the field
description is only half-enforced. This is pinned by a test so that nobody later "fixes" it
into a false positive.

Likewise, `E-EVENT-027` enforces PRD §9.8's `>=` for the emitting slot rather than §14.2's
implied strict `>`. The looser rule is the one in the validation section; see §11 R-5.

### Error codes

Codes are a public contract. Analysers, CI gates and bundle import branch on them, so
renumbering one is a breaking change.

| Code | Layer | Meaning |
|---|---|---|
| `E-EVENT-001` | structural | A required field is missing |
| `E-EVENT-002` | structural | Unknown event type — the enum is closed |
| `E-EVENT-003` | structural | Field has the wrong type (`bool` never satisfies `int`) |
| `E-EVENT-004` | structural | `null` in a field that is not nullable |
| `E-EVENT-005` | structural | Value outside a closed enum |
| `E-EVENT-006` | structural | Payload carries a field not in this type's schema |
| `E-EVENT-008` | structural | `schema_version` is not the version this build writes |
| `E-EVENT-012` | structural | `payload` is not an object |
| `E-EVENT-013` | structural / encode | A float appears in the log |
| `E-EVENT-014` | encode | A value outside the permitted set appears |
| `E-EVENT-015` | decode | A stored line does not have the required shape |
| `E-EVENT-020` | semantic | A `causal_parents` entry is not `< seq` |
| `E-EVENT-021` | semantic | `virtual_ts_ms` decreased |
| `E-EVENT-022` | semantic / writer | `seq` is not gapless from 0 |
| `E-EVENT-023` | semantic | `span_id` missing on a span-scoped type |
| `E-EVENT-024` | semantic | `span_id` present on a run-scoped type |
| `E-EVENT-025` | semantic | Duplicate entry in `causal_parents` |
| `E-EVENT-026` | semantic | Negative `seq`, step or timestamp |
| `E-EVENT-027` | semantic | Vector clock regressed for the emitting slot |
| `E-EVENT-040` | cross-event | A causal parent is not present in the log |
| `E-EVENT-041` | cross-event | A causal parent's clock is ahead of its child's |
| `E-EVENT-042` | cross-event | Fault taint was not inherited from a tainted parent |
| `E-EVENT-043` | cross-event | More than one `run_id` in a single log |
| `E-EVENT-050` | writer | Write attempted after the run was sealed |
| `E-EVENT-051` | writer | Event belongs to a different run than the writer |
| `E-EVENT-060` | migration | `schema_version` cannot be read by this build |

---

## 9. Writing, sealing and versioning

**The writer does not stamp.** PRD §9.6 assigns `seq`, `sched_step`, `virtual_ts_ms`,
`wall_ts_ms`, `vclock`, `causal_parents` and `fault_id` under the scheduler lock, before the
event reaches the writer. This is enforced by the type signature rather than by convention:
the SDK produces a `DraftEvent`, which carries no ordering information at all; the runtime
produces a `Stamp`; only `Event.from_draft(draft, stamp, run_id)` yields a writable `Event`,
and `EventWriter.write` accepts nothing else. The writer holds no counter, clock or vector
clock, so it has nothing to stamp with.

A second implementation should reproduce this boundary. It is the difference between "the
scheduler assigns seq" being a rule and being a fact.

**Batching and sealing.** Events are appended in batches inside one transaction. After
`run_end` the writer flushes, seals, and refuses everything thereafter (`E-EVENT-050`).

**Versioning.** `schema_version` is one integer. Writing is always at the current version.
Reading supports the current version and one previous, via forward-only migrations applied
**on read** — the stored bytes are never rewritten, which is what keeps an imported bundle's
hash chain verifiable. Adding an optional field or a new event type is a minor change and
needs no migration; removing a field, changing a field's meaning, or removing an event type
requires a version bump and a registered migration.

---

## 10. Payload schemas derived at P02, pending sign-off

PRD §9.5 specifies payloads for nine of the nineteen event types. The other ten are marked
*[derived]* in §7 and were reconstructed from PRD §9.3 (the type table), §10.7, §10.10 (the
reproducibility checklist), §11.5 (model identity), §12.2 (the fault catalogue) and §38 (the
SQL DDL).

**They need owner sign-off before the freeze — tracked as `CONTEXT.md` §10 Q-P02.1.** They are the part of this contract with the
weakest normative backing, and after the freeze a correction to any of them invalidates
every run recorded in the meantime.

The ten: `run_start`, `run_end`, `fault_effect`, `lock_acquire`, `lock_release`, `barrier`,
`schedule_decision`, `instrumentation_gap`, `nondeterminism_warning`, `assertion_result`.

Reviewing them is not a formality: deriving `run_end` is what surfaced R-3 below.

---

## 11. Rulings — where this document departs from a literal reading of the PRD

Each of these was raised before implementation and decided explicitly, and each is now
recorded in the project ledger — five as `CONTEXT.md` §10 rulings, one as an ADR. The IDs
below are the ledger's; the `R-n` labels are local to this document.

**R-1 (ledger C-5) · `run_id` is `identity`, not `stable`.**
PRD §9.2 marks `run_id` volatile=`no` and §10.7 says "every other field participates in
equality", which puts `run_id` in the projection. PRD §6.1 defines it as `r_` + 5 hex of *a
content hash* without saying of what. Both readings break something: if the hash input has
any per-execution component, no two replays hash alike and **G3 can never reach 100/100**;
if it is pure content, §33.3's hundred replays collide on `runs.run_id PRIMARY KEY`.
*Ruling:* `run_id` is excluded from the projection and remains a per-run unique identifier.
`llm_call.payload.perturbed_from_run` inherits the mark, being a run id.

**R-2 (ledger C-6) · `llm_call.payload.cache_key` is `stable`.**
PRD §10.7's code pops it; §10.7's prose exclusion list, which calls itself exhaustive, does
not; the inline comment asks and answers its own question; and §11.4 states the key *never
contains a machine-local salt, so a cache is portable between machines*. Three of four
sources say stable.
*Ruling:* included. Excluding it would hide a genuine prompt divergence — the silent-pass
failure, which is worse than an unpassable gate.

**R-3 (ledger C-7) · `run_end.payload.wall_makespan_ms` is `volatile`.**
PRD §9.3 says `run_end` carries a wall makespan. It is volatile by construction and is
**absent from PRD §10.7's "exhaustive" exclusion list**. Left as specified it is the second
field that would have made G3 unpassable.
*Ruling:* excluded, and the exclusion list is now generated from the marks so that the next
such gap cannot occur.

**R-4 (ledger ADR-007) · Floats are forbidden in canonical fields.**
PRD §9.5 uses no floats — every quantity is an integer, a hash string or an enum. But
`span_start.attributes`, `fault_injected.params` and `run_start.payload.env` are open
objects filled by user code.
*Ruling:* floats are rejected with `E-EVENT-013`. This is SDK-visible, which is why it was
raised rather than assumed.
*Known casualty:* PRD §12.2 gives the P1 fault `agent_slow` a parameter `factor` (float ≥
1.0). When that fault lands it must carry a scaled integer — `factor_milli` — or this rule
has to be revisited. Flagged now so it is not discovered at P09.

**R-5 (ledger C-8) · Vector-clock monotonicity is enforced at `>=`, not `>`.**
PRD §9.8 says "vclock ≥ previous vclock for the slot"; §14.2's rules increment on every
local event, implying strict `>`. *Ruling:* the validation section (§9.8) governs the
validator. The looser rule cannot produce a false rejection; the stricter one could.

**C-4 (ledger C-4) · `prev_hash` / `this_hash` are columns, not fields.**
PRD §9.7 introduces the chain but §9.2 lists neither field; §38's DDL has both as `events`
columns. This is forced rather than chosen — an event whose canonical form contained its own
hash is self-referential.

---

## 12. Reference implementation checklist

A port is compatible when all of the following hold against
`tests/golden/event_log_40.jsonl`:

1. Decoding all 40 lines and re-encoding them reproduces the file byte-for-byte.
2. `canonical_log_hash` over the decoded log equals the value pinned in
   `tests/unit/events/test_golden_log.py`.
3. Recomputing the chain from `CHAIN_GENESIS` verifies.
4. Mutating any field listed in the generated exclusion list leaves the log hash unchanged.
5. Mutating any other field changes it.
6. All three validation layers pass on the fixture, and each error code in §8 fires on a
   correspondingly malformed input.
