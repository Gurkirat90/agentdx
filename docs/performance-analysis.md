# The performance analysis engine — contract and error reference

> Implements PRD §16 (Performance Analysis Engine). Companion to `docs/event-schema.md` (the
> event contract every number here traces back to, I6) and `AGENTS.md`/`CONTEXT.md` (the
> process this module was built under — CONTEXT.md §10 C-1 in particular, referenced
> throughout §3 below). Every error code below is a stable part of the public contract: it
> appears in CI output, it is linked from error messages, and renumbering one is a breaking
> change.

---

## 1. What this is, and what it is not

`src/agentdx/analysis/{timing,overhead,redundancy}.py` (P10) build the **timing DAG** over a
sealed event log, compute its **critical path** and **parallelism metrics**, decompose the
critical path (and, separately, total work) into PRD §16.2's six overhead buckets, and detect
exact-hash tool-call redundancy (PRD §16.3). It is scoped strictly to those three files:
baseline generation (PRD §17), the verdict/scorecard, race detection, and any UI are all out
of scope (P11/P12/later, not built here).

**I3 purity.** All three modules import only `agentdx.events`, each other (`overhead.py` and
`redundancy.py` both depend on `timing.py`; `overhead.py` depends on `redundancy.py`), and the
standard library. `.importlinter`'s `analysis-is-pure` and `analysis-imports-no-model-client`
contracts have no allowlist entry for any file in this directory — verified by
`lint-imports --config .importlinter`, part of `just check-imports`.

**Not the causality graph.** PRD §14.1 names two distinct graphs over the same log. The
**causality graph** (pure happens-before: program order, message send/recv, lock
release/acquire, barrier) belongs to `analysis.race` (P12, not yet built) — shared-state
access deliberately does *not* create a causality edge there, or a race could never be
detected. The **timing DAG** below is a different graph: program order + message causality +
*observed data dependencies* + retry links, over **leaf spans**, built to answer "what
determined the elapsed time," not "what happened concurrently." `timing.py` must never import
`analysis.causality`/`analysis.race`, and is not imported by them except for the two small
pure vector-clock helpers, which are deliberately re-derived locally in each consumer rather
than shared (see §6).

---

## 2. The timing DAG (PRD §16.1.1)

### 2.1 Nodes

Every `llm_call`, `tool_call`, and `wait` leaf span becomes exactly one `TimingNode`, keyed by
its own `span_id`. An `agent_step` span is **not** a node — it is decomposed into zero or more
`agent_step_segment` nodes, one per interval of the span's own `[span_start, span_end)`
window that no child span occupies (interval subtraction over the *recorded* `virtual_ts_ms`
of every event in the span, not the span's declared `duration_virtual_ms`, which this build's
own golden fixtures declare as `0` on every leaf while still recording real elapsed time
between events — see `timing.py`'s module docstring, "Node kinds and what fills the gaps").
This decomposition guarantees every virtual millisecond of an `agent_step`'s own window is
counted at most once, either by a child leaf node or by exactly one segment.

A fifth span kind, `handoff`, is legal in the schema but unused by any code path in this
build; encountering one raises `E-ANLZ-002` rather than being silently guessed at.

### 2.2 Edges

| Kind | Built from | Weight |
|---|---|---|
| `program_order` | Per clock slot, consecutive nodes in that slot | `next.start - prev.end` |
| `message` | Matching `message_send`/`message_recv` pairs (by `message_id`) | `dst_node.start - src_node.end` — **node-relative**, not `recv.vts - send.vts` (see §3) |
| `data_dependency` | For each `state_read`, the nearest happens-before `state_write` to the same key, in a different clock slot | `dst_node.start - src_node.end` |
| `retry` | `span_start.attributes.retry_of` linking a retry to the span it retries | `this_node.start - prior_node.end` |
| `fan_in` | A `span_start` with 2+ `causal_parents` entries, one edge per extra parent | `dst_node.start - src_node.end` |
| `run_boundary` | `START` to every node with no other incoming edge; every node with no other outgoing edge to `END` | `node.start - run_start.virtual_ts_ms` / `run_end.virtual_ts_ms - node.end` |

Every weight is `max(0, ...)` and every edge carries `evidence_seq` — the event `seq`s that
justify it (I6). All six kinds are **node-relative**: a weight is always the real gap between
one node's own occupied end and the next node's own occupied start, never a raw point-event
timestamp. This is deliberate and load-bearing (§3).

`resolve_container(span_id, virtual_ts_ms)` maps a point event (a `state_read`, a
`message_send`, …) to the DAG node whose interval contains it — usually the enclosing leaf
span itself, or the right `agent_step_segment` for a point event inside an `agent_step`. This
build's golden fixtures contain a genuine structural surprise, confirmed by direct inspection:
a `message_send` is sometimes recorded *after* its own sending span's `span_end` (the sender's
own segment closed before it got around to sending). `resolve_container` handles this without
raising — it attributes the event to the *last* segment in the span, the closest real activity
there was — but it is the reason edge weights are computed node-relative rather than
event-relative (§3).

### 2.3 Fixing `E-ANLZ-003`: node-relative weights, not raw event timestamps

An early version of `_build_edges` computed `message`/`data_dependency`/`fan_in` weights as
`max(0, dst_event.virtual_ts_ms - src_event.virtual_ts_ms)` — the raw point-event gap. This
double-counts: `critical_path`'s `dist[]` recurrence (§4) already includes a node's own
duration once it is reached, so if the triggering event (e.g. a `state_write`) sits near the
*start* of its node rather than the node's occupied *end*, the node's duration gets counted
again inside the edge weight. On `tests/golden/support_triage.jsonl` this produced
`critical_path_length_ms=44` against `virtual_makespan_ms=40` — `E-ANLZ-003`, "the DAG is
wrong." Switching every cross-node edge kind to `dst_node.start - src_node.end` (matching
`program_order`, which was already correct) fixed it structurally: `dist[]` now telescopes to
exactly reconstruct each node's own absolute position regardless of which path reaches it, so
`critical_path_length_ms` can never exceed `virtual_makespan_ms` for a log where events
account for the whole run (§3.1 covers what happens when they don't).

### 2.4 The `run_boundary` weight: not zero, and not PRD-literal either (D-50, C-19)

`run_boundary` edges were originally hard-coded to `weight_ms=0`, matching PRD §16.1.1's edge
table literally. Given §2.3's fix, a literal `0` makes `critical_path_length_ms` fall short of
`virtual_makespan_ms` by exactly the run's own lead-in (before the first node starts) and
trail-off (after the last node ends). Measured directly on the three golden fixtures, this gap
alone pushes every one of them past gate G5's residual tolerance, for a reason that has nothing
to do with instrumentation quality — see `tests/analysis/test_decomposition_invariant.py`'s
pytest output for the exact residual figure on each fixture under this reading.

**This is a genuine, documented contradiction of PRD §16.1.1's literal table value, not a
silent bug fix** — a same-session independent review found the change had shipped narrated
only as a correctness fix, with no CONTEXT.md deviation or ruling, even though it decides gate
G5's pass/fail status on every fixture. It is now formalised: **CONTEXT.md D-50** records the
PRD-vs-code contradiction; **C-19** rules that `run_boundary` weight is computed against the
real gap to `run_start`/`run_end`'s own `virtual_ts_ms`, and that this weight is attributed to
`overhead.py`'s `blocking_wait` bucket (§4.2), not left to inflate `residual` — a run's own
lead-in/trail-off is measured, not missing, and `residual` (PRD §16.2.4) is meant for
genuinely unexplained time. All three golden fixtures decompose with `residual_ms == 0` under
this ruling (§5, §7). **Flagged for a PRD amendment** — §16.1.1's table should either read "gap
to `run_start`/`run_end`" instead of `0`, or PRD §16.2.3's residual tolerance (§8's
`residual_tolerance` config key) should account for runs with unavoidable boundary gaps; this
ruling settles the code's behavior, not which the PRD's own author intends.

---

## 3. Critical path (PRD §16.1.2)

`critical_path(dag)` returns the longest weighted `START -> ... -> END` path, via a
single Kahn's-algorithm topological order and one relaxation pass:
`dist[m] = max(dist[m], dist[n] + weight(n, m) + duration(m))`. Ties are broken by
`(evidence_seq[-1], span_id)`, read as "keep the first-seen predecessor on an exact tie,"
processed in the one deterministic topological order — reproducible across analyses of the
same log (NFR-14).

**`E-ANLZ-003`** is raised only when `length_ms > virtual_makespan_ms` — the DAG claiming more
elapsed time than the run recorded, which PRD §16.1.2 calls, verbatim, "the DAG is wrong." A
critical path *shorter* than the makespan is explicitly the normal case (§3.1) and never
raises.

### 3.1 A genuinely large residual: the `virtual_makespan_ms` field is authoritative

`build_timing_dag` trusts `run_end.payload.virtual_makespan_ms` as *the* makespan (PRD
§16.1.2) — it does not derive one from `run_end.virtual_ts_ms - run_start.virtual_ts_ms`.
Given §2.3–2.4's fixes, `critical_path_length_ms` always equals
`run_end.virtual_ts_ms - run_start.virtual_ts_ms` for a log whose nodes fully cover the run
(this falls out of §2.3's proof by induction). If a run's *declared* makespan is larger than
that — the runtime's own outer clock ran past the point where event instrumentation stopped
recording (PRD §16.2.4's "an un-shimmed provider, an unwrapped tool, an uninstrumented
subgraph") — the two numbers diverge, `critical_path` does not raise (this is the short-path
case), and the gap surfaces honestly as `overhead.residual_ms`. `tests/analysis/
test_large_residual.py` reproduces this directly: an otherwise-identical 12-event log with a
declared `virtual_makespan_ms` far larger than the actual recorded activity accounts for
yields a large, flagged residual — never absorbed into a bucket. See that test's assertions
for the exact figures.

### 3.2 Parallelism metrics (PRD §16.1.3)

`parallelism_metrics(dag, cp)` returns `total_work_ms` (Σ `duration_ms` of every
`llm_call`/`tool_call` leaf node in the whole log), `critical_path_length_ms`, and
`average_parallelism = total_work_ms / critical_path_length_ms` (`0.0` when the critical path
has zero length, never a division error). `overlap(a, b)` returns the fraction of the
smaller node's busy time that coincides with the other's.

---

## 4. Overhead decomposition (PRD §16.2)

### 4.1 The shape of gate G5 — CONTEXT.md §10 C-1

The original task brief for this build phrased the gate as "buckets + critical path =
makespan." **That phrasing conflicts with CONTEXT.md §10 C-1**, which had already ruled on
this exact question: the correct shape is **`Σ(six buckets) + residual = virtual makespan`**
— the critical path *is* what the six buckets decompose, not a separate term added alongside
them. This document follows C-1 (CONTEXT.md's ruling takes precedence over prompt phrasing
per the ledger's own precedence chain), and that conflict is recorded in this build's session
closing blocks rather than silently resolved.

Concretely:

```
critical_path_length_ms == Σ(bucket ms for every node/edge on the critical path)   (exact, by construction)
residual_ms              == virtual_makespan_ms - critical_path_length_ms          (>= 0, always)
```

The second line is non-negative because `critical_path` never returns a path longer than the
makespan (§3). `decompose_critical_path` asserts the first line as a runtime check
(`E-OVHD-001`) — not merely a pytest gate — because it is a code-correctness invariant, not a
property of the analysed run.

### 4.2 The six buckets and precedence

| Bucket | Computation | Notes |
|---|---|---|
| `productive_work` | Σ durations of `llm_call`/`tool_call` leaf nodes (and `agent_step_segment` nodes — §4.3) on the CP, role not orchestrator/router | The only bucket that is not overhead |
| `handoff` | Σ over CP `message` edges of `recv.virtual_ts - send.virtual_ts` (the literal raw-timestamp PRD formula — §4.4) | Attributed to the edge |
| `blocking_wait` | Σ of CP gaps where the agent had no runnable work — every non-`message`, non-`retry` edge kind, plus any `message` edge's node-relative remainder over its literal handoff figure (§4.4), plus `wait`-kind leaf node durations | Distinguished from handoff by cause |
| `redundant_work` | CP durations of nodes that are members of a redundancy group (§5) other than the group's representative | Computed via `redundancy.duplicate_node_ids` |
| `retry_recovery` | CP durations of nodes with `retry_of` set, plus `retry`-kind edge weights (backoff intervals) | Grows under chaos |
| `orchestration` | CP durations of `llm_call`/`tool_call`/`agent_step_segment` nodes whose role is `orchestrator`/`router` | Supervisor deliberation |
| `residual` | `virtual_makespan_ms - critical_path_length_ms` | Flagged, not hidden, when `>=` the configured tolerance (§4.5) |

Precedence for a node's own duration: `retry_recovery > redundant_work > orchestration >
productive_work`, with `wait`-kind leaf nodes always `blocking_wait` regardless (a `wait` span
means, definitionally, "this agent was blocked"). This is unit-tested
(`tests/analysis/test_timing_hand_computed.py`) so the classification is never ambiguous.

### 4.3 `agent_step_segment` bucket membership — a documented interpretive call

PRD §16.2.2's table names `productive_work`/`orchestration` in terms of `llm_call`/`tool_call`
leaf spans only; it is silent on the leftover, uncovered interval inside an `agent_step` span
that `timing.py` turns into an `agent_step_segment` node (§2.1). Routing that time into
`residual` (the most literal reading: "the PRD names it or it's unattributed") was tried and
rejected — it inflates residual well past gate G5's tolerance on all three golden fixtures
(§2.4's figures — the same fixed-boundary gap, before accounting for `agent_step_segment`
time at all), for time that is genuinely measured, not missing. The
reading used here instead treats an `agent_step_segment` exactly as a leaf span: role-gated
between `orchestration` and `productive_work`. This is a judgement call on a PRD-silent point,
surfaced here and in this build's session closing blocks (AGENTS.md §1) — not a silent
resolution — and is a reasonable candidate for a future CONTEXT.md ADR if a later prompt
wants to revisit it with real (non-synthetic) fixture data, where leaf spans carry non-zero
declared durations and this ambiguity may look different.

### 4.4 The literal `handoff` formula vs. edge-weight consistency

PRD §16.2.2 defines `handoff` using **raw** event timestamps
(`recv.virtual_ts - send.virtual_ts`), but §2.2/§2.3 established that `message` edges'
`weight_ms` is deliberately **node-relative** for `dist[]`'s consistency. The two can differ —
directly observed in the golden fixtures, where a send/recv point event does not always sit at
its node's own boundary. `decompose_critical_path` computes both: the literal raw-timestamp
figure for `handoff` (via `_raw_handoff_ms`, evidence-traceable straight to the
`message_send`/`message_recv` `seq`s), clamped to `[0, weight_ms]`, and assigns whatever
remainder of the edge's node-relative weight is left to `blocking_wait`. The two always sum to
exactly the edge's own `weight_ms`, so §4.1's accounting identity never breaks regardless of
how large or small the literal transport figure is.

### 4.5 Total validation and the configured tolerance

```python
assert abs(sum(bucket_ms.values()) + residual_ms - virtual_makespan_ms) <= 1   # E-OVHD-001
residual_flagged = residual_fraction >= residual_tolerance                      # not an error
```

`residual_tolerance` is read from `agentdx.toml`'s `[analysis] residual_tolerance` (default
`0.02`, matching PRD §16.2.3), via a `tomllib` read at call time — the same pattern
`scenario.loader._load_scenario_toml_section` already established (CONTEXT.md D-41), rather
than routing through `config.py`/`AgentDXConfig`.

### 4.6 The two decompositions (PRD §16.2.1)

`decompose_critical_path` (denominator `virtual_makespan_ms`) answers "where did the elapsed
time go" and is the one gate G5 validates. `decompose_total_work` (denominator `Σ all node
durations in the whole log`, every node, not just the ones on the critical path) answers
"where did the effort go," for redundancy/cost reporting. Total work has no gap concept — its
denominator is a sum of *durations*, not elapsed time — so `handoff`/`blocking_wait` are
always `0` there; the remaining four buckets partition it exactly, no residual needed.

### 4.7 `format_decomposition_table`

Renders a `OverheadDecomposition` as the week-5 demo-milestone terminal table: one line per
bucket in `_BUCKET_ORDER`, percentage of makespan, and the evidence `seq` range behind it,
followed by the residual line (flagged with `⚠ UNATTRIBUTED` when large) and the makespan/CP
summary line. See `tests/analysis/test_decomposition_invariant.py` for real output against
all three golden fixtures.

---

## 5. Redundancy detection (PRD §16.3)

Locked to exact-hash matching only (§43.3.2 — no embedding similarity, ever, in v1):

```
group_key = blake2b(tool_name + "\x00" + args_hash)
```

`args_hash` is the `tool_call` event's own already-computed hash of canonicalised arguments
(PRD event schema §7), not raw `args` re-hashed — raw arguments are present only under
`capture_bodies=True` (I8, the privacy default), and depending on them would make redundancy
detection silently blind on any run captured with default settings.

### 5.1 The qualifying-pair rule — a documented interpretive call

PRD §16.3 requires, for a reported group, that "at least two members are concurrent (by
vector clock) **or** occur within the same logical phase." **"Logical phase" is not defined
anywhere else in the 5505-line PRD** — the phrase occurs exactly once, at its own definition
site. Rather than invent an undefined concept, `redundancy.py` reads "same logical phase" as
coinciding with condition 3's own same-slot branch ("in the same slot without an intervening
state change") — the one case in condition 3 that does not already require concurrency to be
meaningful, since same-slot events are always totally ordered, never concurrent. Under this
reading, a pair qualifies iff:

```
not retry_linked(a, b)
and (
    (same_slot(a, b) and not intervening_state_change(a, b))
    or
    (not same_slot(a, b) and concurrent(a, b))
)
```

"Intervening state change" is itself operationalised conservatively: the schema does not link
a `tool_call`'s `args_hash` back to specific `state_write` keys, so `redundancy.py` treats any
other DAG node anchored on the same clock slot strictly between the two candidates as
disqualifying — over-disqualifying is the safe direction (never fabricates a redundancy
finding, I9). Both calls are surfaced here and in this build's closing blocks, not resolved
silently.

Retry linkage is resolved over the whole DAG's `retry_of` chains (not just the candidate
bucket), so a retry that also happened to change its arguments is never double-reported as
both `retry_recovery` and `redundant_work`.

### 5.2 Report shape

Each `RedundancyGroup` carries `group_key`, `tool_name`, `args_hash`, `member_node_ids`
(sorted, size >= 2), `representative_node_id` (the max-duration member — chosen to coincide
with PRD §16.3's own `wasted_virtual_ms = Σ durations - max(duration)` formula, which
subtracts the max regardless of which member is "kept"), `wasted_virtual_ms`, `wasted_tokens`
(always `0` in v1 — no token accounting on tool_call spans), and `evidence_seq`.
`duplicate_node_ids(groups)` returns every non-representative member across every group, which
is what `overhead.py`'s `redundant_work` bucket consumes.

---

## 6. Determinism (NFR-14)

Same log, analysed 100 times, byte-identical output — `tests/analysis/test_determinism.py`
covers `timing`, `overhead`, and `redundancy` together in one pass over a real golden fixture.
Every collection any of the three modules returns is a `tuple` built in a stable, explicit
sort order, or a `dict` whose keys were inserted in a fixed order (`_BUCKET_ORDER`); no code
in this directory iterates a bare `set` (`scripts/check_determinism_hygiene.py` enforces this
mechanically — `just check-determinism`). Ties (equal timestamps, equal weights, equal
`dist[]` values) are always broken by an explicit key, documented at the point of use, never
by insertion order.

`_happens_before`/`_concurrent` (pure vector-clock functions, PRD §14.2) are duplicated in
`timing.py` and `redundancy.py` rather than shared through an import — both are four-line pure
functions, and any divergence between the two copies would be caught immediately by both
modules' own tests running against the same golden fixtures. This mirrors the precedent in
`runtime.faults.taint`'s module docstring for deliberately duplicating small pieces of causal
logic across layers rather than coupling them.

---

## 7. Error code reference

| Code | Raised by | Meaning |
|---|---|---|
| `E-ANLZ-001` | `timing.build_timing_dag` | A malformed/truncated span — a `span_end` missing `duration_virtual_ms`, or a segmentless `agent_step` with no children to attribute an event to |
| `E-ANLZ-002` | `timing.build_timing_dag` | A span kind PRD §16.1.1 does not name as a timing-DAG node kind (currently only `handoff`, unused by this build) |
| `E-ANLZ-003` | `timing.critical_path` | `critical_path_length_ms > virtual_makespan_ms` — the DAG is wrong (PRD §16.1.2). A *shorter* critical path is normal and never raises this (§3.1) |
| `E-ANLZ-005` | `timing.build_timing_dag` | No `run_start`/`run_end` pair in the log, or `run_end` has no `virtual_makespan_ms` |
| `E-OVHD-001` | `overhead.decompose_critical_path` | Σ(six buckets) + residual does not match `virtual_makespan_ms` within integer rounding — a code-correctness assertion (§4.1), never expected on a real run |

`redundancy.py` raises nothing — a redundancy-detection failure mode is always "report zero
groups," never an exception, since an empty result is itself a valid, honest answer (I9).

---

## 8. Configuration

`agentdx.toml`'s `[analysis]` section (pre-existing before this build, now consumed):

```toml
[analysis]
residual_tolerance = 0.02      # gate G5: Σ(six buckets) + residual = virtual makespan
redundancy = "exact_hash"      # exact_hash only in v1 (43.3.2) — no other value is read yet
```

`redundancy` is not yet read by `redundancy.py` (there is only one supported value, and the
module has no other mode to switch on) — recorded as a known gap, not a silent omission, in
this build's closing blocks.

---

## 9. Per-edge and per-agent aggregates (PRD §16.4)

`aggregates.py` computes the eight PRD §16.4 metrics over an already-built `TimingDAG` and
`CriticalPathResult` — no new graph-construction logic, no change to `timing.py`'s or
`overhead.py`'s output. Added in a same-session repair after an independent review found §16.4
was named in this build's original scope but never implemented (CONTEXT.md §11 tripwire 14 —
"a PRD requirement inside a completed prompt's scope was neither implemented nor declared").

### 9.1 Edge aggregates

§16.4's table is written for the graph panel, which shows one edge per *agent pair*, not one
row per internal `TimingEdge` — a single agent pair can exchange many messages across a run.
`compute_edge_aggregates` groups every `message`-kind edge by `(src_agent_id, dst_agent_id)`:

| Field | Computation |
|---|---|
| `message_count` | Count of `message` edges between the pair |
| `total_handoff_ms` | Σ literal `recv.virtual_ts - send.virtual_ts` (PRD §16.2.2's formula, reused — not `timing.py`'s node-relative `weight_ms`) over every edge between the pair |
| `cp_handoff_ms` | Same sum, restricted to edges that are the actual critical-path hop for their `(src, dst)` — using the identical selection rule `overhead.decompose_critical_path` already uses, so this never silently disagrees with that module's own `handoff` bucket |
| `cp_share` | `cp_handoff_ms / virtual_makespan_ms` (`0.0` if the makespan is `0`) |

An edge whose endpoints cannot be attributed to a real agent (`TimingNode.agent_id is None` —
legal per the event schema) is excluded rather than attributed to a fabricated pair (I9).

### 9.2 Agent aggregates

`compute_agent_aggregates` returns one `AgentAggregate` per distinct `agent_id`:

| Field | Computation |
|---|---|
| `busy_ms` | Σ `duration_ms` of every node belonging to the agent, over the whole DAG (not just the critical path) |
| `idle_ms` | See §9.3 — **C-20**, a documented interpretive call |
| `cp_ms` | Σ `duration_ms` of the agent's own nodes that are on the critical path |
| `tokens` | Σ `prompt_tokens + completion_tokens` over every `llm_call` event with a matching `agent_id`, across the whole log |

None of this build's three golden fixtures contain an `llm_call` event (confirmed by direct
inspection, not assumed — all three are `tool_call`-only logs), so `tokens` has no real-fixture
coverage; it is exercised by a hand-authored log instead (`tests/analysis/test_aggregates.py`).

### 9.3 `busy_ms`/`idle_ms` — an undefined PRD term, ruled (C-20)

PRD §16.4 names `agent.busy_ms`/`agent.idle_ms` as "Occupancy" and gives no formula for either
— genuinely silent, not merely terse (no other PRD section elaborates). **CONTEXT.md C-20**
rules: an agent's occupancy window is `[min(start of its own nodes), max(end of its own
nodes)]` — the span it was observably in play. `busy_ms` is as in §9.2; `idle_ms` is whatever
of that window `busy_ms` does not cover (`max(0, (window_end - window_start) - busy_ms)`). Time
before an agent's first node or after its last counts as neither busy nor idle. An agent active
in one contiguous span has `idle_ms == 0` by construction; a real between-turns gap inside the
window is what registers — see `tests/analysis/test_aggregates.py`'s hand-computed case
(`idle_ms = 5` for an agent with a genuine gap, `0` for one without).

### 9.4 Determinism and evidence

Both `compute_edge_aggregates` and `compute_agent_aggregates` return tuples sorted by their own
key (`(src_agent_id, dst_agent_id)` / `agent_id`); every accumulation iterates `sorted(...)` or
an already-deterministic `TimingDAG` collection — no bare `set` iteration
(`check_determinism_hygiene.py` clean). Every `EdgeAggregate`/`AgentAggregate` carries
`evidence_seq` — the contributing event/node seqs, sorted ascending (I6).
