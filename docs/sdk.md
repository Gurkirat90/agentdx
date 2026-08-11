# The AgentDX instrumentation SDK — contract and error reference

> Companion to `docs/event-schema.md` (the event contract) and `docs/storage.md` (the store).
> Implements PRD §8. Every error code below is a stable part of the public contract: it
> appears in CI output, it is linked from error messages, and renumbering one is a breaking
> change.

---

## 1. What the SDK is, and the two things it never does

The SDK is the capture surface. It observes a running agent system and produces
`events.DraftEvent`s. It does exactly two things that are worth stating as prohibitions,
because both are load-bearing and both are easy to break by accident:

**It never stamps.** PRD §9.6 assigns `seq`, `sched_step`, `virtual_ts_ms`, `wall_ts_ms`,
`vclock`, `causal_parents` and `fault_id` under the scheduler lock. That is the runtime's
job (P06). Nothing under `src/agentdx/sdk/` constructs an `Event` or a `Stamp`;
`tests/unit/sdk/test_sdk_never_stamps.py` asserts it against the AST rather than trusting a
comment.

**It never reads a clock.** `AGENTS.md` §4.1 sanctions four places that may touch the real
clock and `sdk/` is not one of them. Both volatile time fields come from an injected `Clock`,
so the determinism rule stays mechanical: the SDK cannot read a real clock because it has no
way to name one.

Everything the SDK needs from the runtime is a `typing.Protocol`:

| Protocol | Supplies | Real implementation |
|---|---|---|
| `Clock` | `virtual_ms()`, `wall_ms()` | `runtime/clock.py` (P06) |
| `Recorder` | `emit(draft, causes) -> seq` | the scheduler (P06) |
| `Scheduler` | `yield_point(reason)` | the scheduler (P06) |
| `LlmCache` | `lookup(key)`, `store(key, response)` | `runtime/cache/` (P07) |
| `RunHost` | `open_run(...)`, `close_run(...)` | `cli/` + `runtime/` (P06) |

Until those exist, the defaults are `FrozenClock` (both scales return zero, because with no
scheduler there is no virtual time and inventing one from the wall clock is exactly the
conflation invariant I11 forbids), `ImmediateScheduler`, and `NoCache`. **A P04-only log has
zero durations, and that is honest rather than broken.**

---

## 2. The public surface (PRD §8.2)

| Group | Symbols |
|---|---|
| 1. Graph-level | `instrument(target, *, name, capture_bodies=None, agent_from=…, context=None, hooks=None)` |
| 2. Decorators | `@agent(agent_id, *, role=None, attributes=None)`, `@tool(name, *, attributes=None)` |
| 3. Explicit state | `state()` → `StateHandle.read(key)` / `.write(key, value)` |
| 4. Synchronisation | `lock(key)`, `transaction(name)`, `barrier(barrier_id, participants)` |
| 5. Run control | `run(graph, *, task, scenario=None, seed=None, host=None)`, `install_runtime(host)` |
| §8.4 messaging | `send(to, payload)`, `recv()` |
| §8.6 hooks | `LifecycleHooks`, `SpanRecord` |

Groups 1 and 2 require **zero changes to prompt or agent logic**. Groups 3 and 4 are optional
refinements that *reduce* false positives; the product is useful without them.

**`on_fault` is declared and is not invoked anywhere.** PRD §8.6 names five hooks and
`LifecycleHooks` carries all five, but `on_fault` fires "immediately after `fault_injected`"
and **nothing in this codebase injects a fault**: `runtime/faults/` is P09 and is empty. Four
hooks — `on_run_start`, `on_agent_start`, `on_span_end`, `on_run_end` — are called through
`call_hook`, which is the §8.6 guard. The fifth has **no call site in the tree**, so a user who
passes one will never see it fire until P09 wires it. It is declared now so the hook set is
complete and the signature is fixed before P09 arrives; building call sites for a module that
does not exist would be a stub presented as done (`AGENTS.md` §2). Deviation D-27.

Two symbols the project names elsewhere are deliberately absent: `agentdx.wall_time()` and
`agentdx.sorted_set()`. Both belong to the runtime (CONTEXT.md D-16), and nothing in `sdk/`
needs either.

---

## 3. The LangGraph adapter (PRD §8.3)

`instrument()` performs the five §8.3 bindings against a compiled graph. It **imports nothing
from `langgraph`**: every binding is duck-typed and probed before it is attached, so the
adapter drifts only when the shape it actually uses changes — and it finds out at bind time,
by probing, rather than at run time by silently recording less.

| # | Binding | Mechanism | Events |
|---|---|---|---|
| 1 | Node lifecycle | a recording **subclass of the node's own `bound` class** | `span_start` / `span_end` (`kind=agent_step`) |
| 2 | Channel writes | a recording subclass of each channel's own class, plus reducer detection | `state_write` |
| 3 | Channel reads | a `collections.abc.Mapping` over the node's input, recording first access per key | `state_read` |
| 4 | Edge traversal | the producer→consumer transition, recorded at the consumer | `message_send`, `message_recv` |
| 5 | LLM / tool calls | the provider shim (§8.5) and `@agentdx.tool` | `llm_call`, `tool_call` |

**Why a subclass rather than a wrapper.** An attribute-forwarding proxy breaks LangGraph,
which does `isinstance` checks on channels and runnables throughout Pregel. A subclass of the
object's *own* class passes all of them and survives the `copy()` / `from_checkpoint()`
round-trips Pregel performs between supersteps, because both construct `self.__class__`. The
subclass declares `__slots__ = ()`, which is required rather than cosmetic: LangGraph channels
use `__slots__`, and a subclass that added a `__dict__` would change the object layout and
make the class swap impossible.

**Where `state_write` is emitted, and why it is not where §8.3 implies.** Pregel applies
channel updates at the *end* of a superstep, outside every node's context. A proxy that
emitted `state_write` from `channel.update()` would therefore attribute every write to
nobody. So the event is emitted by the node that produced it, from the node's own return
value, inside its own span, with `prev_value_hash` read from the value as of the last
completed superstep — which is what every node in the current superstep saw, and therefore
what two concurrent writers must agree on. The channel proxy is still attached, and it earns
its place: it detects updates no node accounted for and turns the difference into an
`instrumentation_gap`. See deviation D-22.

**Reducer detection is a table, on purpose.** `CHANNEL_REDUCERS` in `sdk/langgraph.py` maps
every channel class of the pinned LangGraph minor (ADR-003: `>=1.2,<1.3`). A class absent from
that table is an *unrecognised channel type*, and for a user channel that is fatal. The
alternative — recording `reducer=null` for a channel that does reduce — turns every concurrent
write to it into a false `lost_update`, which PRD §8.3 calls the single highest-risk
false-positive source in the product. A LangGraph 1.3 bump lands in that table first, loudly.

Two of the table's entries — `BinaryOperatorAggregate` and `DeltaChannel` — carry the sentinel
`REDUCER_FROM_INSTANCE` rather than a name, because their reducer is a property of the
*instance*, not of the class. If that attribute ever moves, the class is reported
**unrecognised** and takes the same fatal path as a class nobody has heard of. `None` in that
table is a positive claim ("this channel does not reduce"); a failed instance read must never
be allowed to degrade into it.

**Binding 3 is a `Mapping`, not a `dict` subclass, and that is a correctness requirement.**
`dict(state)` and `{**state}` take a C-level fast path in CPython when the operand is a real
`dict` subclass: they copy the hash table directly and call neither `__getitem__` nor `keys()`.
A recording view that subclassed `dict` therefore recorded **nothing** for the most common way
a node reads its state. Deriving from `collections.abc.Mapping` removes the fast path, so
`dict(view)`, `{**view}`, `.keys()`, `.items()`, `.values()`, `in` and iteration all record.
The cost is that `isinstance(state, dict)` is now False; the pinned LangGraph does not require
it to be true, and a node that wants a real `dict` writes `dict(state)` — which now records the
read it performs. Deviation D-26, drift tripwire 16.

**A compiled subgraph mounted as a node is refused, loudly.** LangGraph lets a compiled graph
be a node of another graph. Its inner nodes are separate agents, and the binding walk never
sees them: no spans, no state events, no handoffs for anything inside. Recording it as one
opaque agent produces a log that looks complete and is not, so the adapter emits a fatal
`instrumentation_gap` naming the node and `instrument()` raises `E-INSTR-002`. Recursive
path-qualified subgraph capture is **not implemented**; instrument the subgraph separately, or
inline its nodes. Deviation D-29.

**Clock slots are per node, not per agent id, when the two differ.** `agent_from` may map
several nodes onto one agent id (PRD §8.2 item 1), and nodes in one Pregel superstep are
concurrent by that framework's semantics — so one slot for both would make the vector clock
assert an order the run did not have and hide every race between them. The adapter allocates
`agent_id` when the node name equals the agent id (the default identity map, unchanged) and
`agent_id#node_name` otherwise. Where a real ordering exists it is carried by binding 4's
`message_send`/`message_recv` pair, whose `causal_parents` merge the clocks. Deviation D-30.

### 3.1 What happens when a binding fails

Three things, always, and never fewer:

1. an `instrumentation_gap` event is written to the log;
2. an `InstrumentationGapWarning` is raised in the process;
3. the gap is recorded on `RunResult.gaps` and on `InstrumentedGraph.gaps`.

If the failed binding is one without which the log would *look* complete while being
structurally incomplete — node lifecycle, or a user channel whose reducer is therefore
unknown — `instrument()` additionally raises `InstrumentationError` and the run does not
start. That is not a contradiction of PRD §36's "partial results beat no results": a run with
a *named* gap is analysable with its limitation stated, whereas a log with no spans at all is
not partial, it is empty and indistinguishable from a graph that did nothing.

---

## 4. Privacy (PRD §8.11, NFR-6, invariant I8)

| Setting | The event log contains | The LLM cache contains |
|---|---|---|
| `capture_bodies=False` (default) | `prompt_hash`, `response_hash`, `params_hash`, token counts, model, cache key | full bodies |
| `capture_bodies=True` | bodies inline, **after redaction** | full bodies |

The cache necessarily holds bodies — replay is impossible otherwise. The distinction that
matters is that the *event log*, which is exported, shared in bundles by default and rendered
in the UI, does not.

`redact_patterns` is applied to **every** string the SDK writes: error messages always (PRD
§8.9), **span attribute string values always**, bodies only under the opt-in. Patterns are
compiled when configuration is resolved, not at emission — a redactor that raised while an
error was being recorded would destroy the error it was protecting.

Attributes are redacted **under the default configuration**, because they are not gated on
`capture_bodies` at all: `span_start.payload.attributes` is an open user-supplied map, is
marked `STABLE` (inside the canonical projection and every event hash) and is exported in
bundles by default, so an API key pasted into one was the single plaintext route the PRD §8.11
opt-in never covered. Attribute *keys* are left alone — a redacted key would silently rename
the user's field. This strengthens I8 rather than reinterpreting it. Deviation D-28.

`tests/integration/sdk/test_no_plaintext_bodies.py` enforces this by running a fixture with the
default configuration and searching the resulting SQLite file **as raw bytes** for known
plaintext. Three things make that scan mean something:

* the default case builds its run context through the **argument-omitted** entry point —
  `RunContext.create` with no `config=` and no `capture_bodies=` — so `AgentDXConfig`'s own
  resolution is the only thing that can decide the outcome. Passing `capture_bodies=False`
  explicitly would exercise the branch that takes the caller's word for it and leave the
  config→context wiring untested in both directions;
* a control injects `AGENTDX_PRIVACY_CAPTURE_BODIES=true` and asserts the scan **does** find
  the plaintext, and a second control does the same through the explicit argument;
* the scan globs `runs.db*`, not `runs.db`. The store runs in WAL mode, so uncheckpointed
  bytes live in `runs.db-wal` and a scan that named only the main file would have a blind spot
  the size of the most recent writes.

**Not recorded: the traceback.** PRD §8.9 says the full traceback is captured when
`capture_bodies=True`. The frozen event schema has no field for one, and the only candidate,
`span_end.payload.error_message`, is marked `STABLE` — inside the canonical projection. A
traceback contains absolute file paths, so putting one there would make gate G3 fail across
machines. The traceback is therefore **not** recorded. See deviation D-24; this is the one
place P04 knowingly under-implements the PRD rather than breaking an invariant to satisfy it.

---

## 5. Provider shims (PRD §8.5)

One interception point, three thin profiles. Groq, OpenAI and Anthropic all expose
`POST {base_url}/chat/completions`, so all three reach the provider through
`OpenAICompatibleClient` and differ only in a `ProviderProfile`: a base URL, a default model
and the environment variable holding the key. **No vendor SDK is a dependency** — PRD §8.5
rejects one explicitly, and ADR-004 fixes the permitted set.

Mode behaviour (PRD §11.2):

| Mode | Lookup | On miss | `cache_status` |
|---|---|---|---|
| `replay` | cache only | **hard error** `E-CACHE-001` | `hit` / `miss_error` |
| `perturb` | cache only | hard error | `perturbed` / `miss_error` |
| `record` | cache, then provider | call and store | `hit` / `miss_recorded` |
| `passthrough` | none | call and store | `miss_recorded` |

In `replay` mode no API key is read and no network call is possible, which is what gate G9 and
invariant I7 require. There is no fallback to a live call and no flag that would enable one.

> **Open question, raised before the schema freeze.** `llm_call.payload.cache_status` has four
> members and none of them means "the cache was bypassed", which is what `passthrough` does.
> The shim reports `miss_recorded`, and `run_start.payload.cache_mode` is what distinguishes a
> passthrough run from a record run. Adding a fifth member is a schema change and needs an ADR
> (CONTEXT.md §11 tripwire 6), so it is surfaced rather than taken. See §10 C-9.

---

## 6. Overhead (PRD §8.10, NFR-1)

The budget is under one tenth of wall clock in passthrough mode, and it is measured rather
than asserted: `bench/harness/sdk_overhead.py` writes `bench/results/sdk-overhead.json`, and
`tests/benchmarks/test_sdk_overhead.py` gates on the same constant the harness publishes.

Both ends of the range are published, because an overhead *ratio* depends entirely on its
denominator. The gated configuration simulates a model call per node; the zero-work
configuration has nothing to amortise instrumentation against and its ratio is large by
construction. The microseconds-per-event figure beside it is the one that transfers to another
workload. `[bench:sdk-overhead.json]`

### 6.1 What this measurement is NOT — read before quoting the number

The committed figure is honest about what it measured and it is **not the NFR-1 number**. PRD
§34.1 specifies the overhead benchmark as three configurations, reported as median and p90,
with the run executed in a real cache mode. What is committed is narrower on four counts, and
each one is stated here rather than left for a reader to infer:

1. **No provider call is in the measured path.** Node "work" is simulated latency; nothing
   contacts a provider and nothing is served from the record/replay cache, so neither the
   passthrough path §34.1 asks for nor the replay path a real run uses is exercised. The
   instrumentation side *is* the whole production path — spans, state, edge messages,
   `EventWriter` validation, canonical bytes, the blake2b chain and batched SQLite inserts
   with WAL and both append-only triggers — but the denominator is a simulation.
2. **Two configurations, not three**, and neither is a scenario-driven run.
3. **Worst-instrumented against best-uninstrumented, not median and p90.** That pairing is
   deliberately the least flattering the data supports, but it is not the statistic §34.1
   names, and a single worst sample is a different quantity from a p90.
4. **Measured on CPython 3.10 / Linux / aarch64, not the project's pinned CPython 3.12.**
   The build host could not obtain a 3.12 interpreter (the same constraint as CONTEXT.md
   D-08). A ratio is more portable across interpreters than an absolute time, but it is not
   invariant under one.

So: **FR-1's gate is met in the measured, caveated configuration and NFR-1 is not yet met in
full per §34.1.** The remaining work is a real benchmark, not a re-labelling of this one.
Deviation D-31, and CONTEXT.md §5 row 4 says the same thing where the build state is read.

---

## 7. Error codes

Every code carries a docs anchor of the form `docs/sdk.md#<code-lowercased>`.

<a id="e-instr-002"></a>
### `E-INSTR-002` — unsupported framework construct

The adapter could not attach to a construct it must capture. Always accompanied by an
`instrumentation_gap` event and a warning. Raised (rather than warned) only when the missing
binding would leave the log structurally incomplete: node lifecycle, or a user channel whose
reducer is unknown. **Fix:** pin LangGraph to the supported range (ADR-003), or use the
decorator API for the constructs the adapter cannot see.

<a id="e-instr-003"></a>
### `E-INSTR-003` — no active run

An SDK call needs a run and there is none. The SDK does not queue events for a run that may
never start: a buffered event has no `seq`, and dropping it silently is the partial capture
this design exists to prevent. **Fix:** call inside `agentdx.run(...)`, pass
`context=` to `instrument()`, or install a runtime with `agentdx.install_runtime(host)`.

<a id="e-instr-004"></a>
### `E-INSTR-004` — no ambient agent context

A span-scoped event was emitted with nothing to attribute it to, or a synchronisation
primitive was used outside a span. `contextvars` propagate into `asyncio` tasks and into
`asyncio.to_thread`, but **not** into a bare `threading.Thread`. Attributing the event to the
last agent seen would be a plausible-looking lie that every downstream analysis inherits.
**Fix:** hand work off with `asyncio.to_thread`, or wrap the callable with
`contextvars.copy_context().run`.

<a id="e-instr-005"></a>
### `E-INSTR-005` — unloggable span attribute

A user attached a value the event log cannot carry. **Floats are the case this exists for:**
ADR-007 forbids them everywhere in the log because cross-platform float formatting is a
determinism leak the canonical projection cannot normalise. **Fix:** use integer
milliseconds, integer per-mille, or a string. There is deliberately no coercion — a silent
one would move the user's number without telling them.

<a id="e-instr-006"></a>
### `E-INSTR-006` — unsupported instrumentation target

`instrument()` was handed something that is not a compiled graph, or `run()` something it
cannot invoke. **Fix:** use the decorator API (`@agentdx.agent`, `@agentdx.tool`) for
plain-Python systems.

<a id="e-instr-007"></a>
### `E-INSTR-007` — lifecycle hook violated its guard

A PRD §8.6 hook emitted an event or wrote state. §8.6 requires hooks to be synchronous, to
perform no I/O and to mutate nothing, "enforced by running them under a guard that raises on
any event emission or state write". **Fix:** move the emission into the instrumented code.

<a id="e-instr-008"></a>
### `E-INSTR-008` — value has no reproducible representation

A value fell back to the default `__repr__`, which embeds a memory address. Hashing it would
make `value_hash` differ between two replays of the same run. On the state and tool paths this
is caught and downgraded to an `instrumentation_gap` plus an all-zero digest, so the user's
graph still runs with the limitation stated. **Fix:** give the type a `__repr__`, or convert
it before writing it to state.

<a id="e-cache-001"></a>
### `E-CACHE-001` — cache miss in replay

PRD §36, exit code 3. The `llm_call` event is written before the error is raised, so the log
records the call that could not be served. **Fix:** re-record with `agentdx run --record`.
There is no fallback to a live call: a silent one would make CI non-hermetic, bundles
unreproducible and cost unpredictable, all three at once.

<a id="e-llm-001"></a>
### `E-LLM-001` — provider error

The provider refused or failed, or a live call was required and no API key was present. The
partial cache is retained; the run stops. **Fix:** the message names either the provider's
status or the environment variable that is unset, and the offline path.

<a id="e-config-001"></a>
### `E-CONFIG-001` — configuration could not be resolved

Raised by `agentdx.config`. A value that could not be coerced, a setting that does not exist,
or a redaction pattern that does not compile. Raised rather than defaulted: a threshold the
user believes they set, silently ignored, produces a benchmark number describing a
configuration nobody chose. *(This code and its anchor were introduced at P03; the anchor
lived nowhere until now — see deviation D-25.)*

---

## 8. Configuration (PRD §8.7)

Precedence, highest first: **CLI flag → environment variable → `agentdx.toml` → the
per-section argument → the dataclass default.** The order is stated once, in
`config._resolve`, and every section inherits it rather than re-implementing it.

Environment variables are `AGENTDX_<SECTION>_<KEY>`, upper-cased. A variable naming a setting
that does not exist is an error, not a no-op — a typo must not look like success.

Sections the SDK reads: `[run]` (`seed`, `mode`, `data_dir`), `[privacy]` (`capture_bodies`,
`redact_patterns`), `[llm]` (`provider`, `model`, `base_url`). `[store]` belongs to P03.
`[scheduler]` and `[analysis]` are present in `agentdx.toml` but are not yet read by anything:
they belong to P06 and P10, and a section declared before its consumer exists is a threshold
nobody is enforcing.

Two coercion rules are worth stating because getting them wrong is expensive:

* **`capture_bodies` is never coerced from an arbitrary string.** `bool("False")` is `True`,
  and a privacy default that flipped because of it would put prompt bodies in the log.
* **`redact_patterns` from the environment is JSON, never comma-separated.** The default
  pattern `sk-[A-Za-z0-9]{20,}` contains a comma; splitting on it would silently turn one
  working pattern into two broken ones.

---

## 9. Identity (PRD §8.8)

| Thing | Construction | Why |
|---|---|---|
| Agent id | user-supplied, stable across runs | PRD §17's baseline comparison breaks otherwise |
| Clock slot | the agent id; `agent#n` for a scope opened while another for the same agent is open | so an agent racing itself is detectable (PRD §14.2) |
| Span id | `sha1(run_id ‖ agent_id ‖ span_seq)[:12]` | PRD §8.8, verbatim; deterministic, so a UI selection survives a replay |
| Message id | `m_` + `sha1(run_id ‖ from ‖ to ‖ message_seq)[:12]` | PRD §6.1 requires an id and does not specify one; mirrors the span rule |
| Content hash | `blake2b:` + blake2b-256 of a canonical rendering | PRD §8.10 |

`sha1` is used only as a short identifier and never as a security primitive; content hashing
is blake2b throughout.

---

## 10. Rulings

* **C-9 — `cache_status` has no member for `passthrough`.** See §5. Recorded as
  `miss_recorded`; a fifth member needs an ADR before the schema freezes. Recorded as ruling
  C-9 in CONTEXT.md §10.
* **§8.1 vs §8.2 — `state`.** §8.1's package comment calls `@agentdx.state` a decorator; §8.2,
  which is labelled "complete", shows `async with agentdx.state()`. §8.2 is normative here.
* **§8.1 vs §8.2 — `barrier`.** §8.1 names `barrier()` in `sync.py`; §8.2's list omits it. It
  is implemented and exported: the `barrier` event type would otherwise have no emitter
  anywhere in the system, which is CONTEXT.md §11 tripwire 14 exactly.
