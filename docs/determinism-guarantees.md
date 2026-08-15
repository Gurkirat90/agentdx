# Determinism Guarantees

> **This is the canonical reference for what AgentDX does and does not guarantee about
> determinism.** Read this before drawing any conclusion from a replay mismatch.

---

## 1. The Definition

> **Definition (PRD §10.1).** Given the same
> `(graph identity, scenario, seed, LLM cache contents, delay schedule, AgentDX version)`,
> two executions produce event logs whose **canonical projections are byte-identical**.

The **canonical projection** (§4 below) removes fields that cannot be deterministic by
nature — wall-clock times, host, PID, process memory addresses — and normalises
serialisation. This makes the claim precise enough to assert in CI.

The unit that is byte-identical is the **canonical log hash**: a single `blake2b-256`
digest over the sequence of canonicalised events. If it matches across 100 replays in ≥10
fresh processes, the run is deterministic.

---

## 2. What IS guaranteed

**Exactly one thing: the coordination structure and event log, in replay mode, on a fixed
AgentDX version.**

Concretely:
- The sequence of `schedule_decision` events — i.e., which agent ran in which order — is
  identical at the same seed.
- Every `span_start`, `span_end`, `state_read`, `state_write`, `message_send`,
  `message_recv`, `lock_acquire`, `lock_release`, `barrier`, `llm_call`, and `tool_call`
  event appears in the same position, with the same `seq`, `sched_step`, `vclock`,
  `virtual_ts_ms`, and payload, on every replay.
- Virtual time advances identically: the same task wakes at the same virtual millisecond.
- Seeded randomness (user calls to `random.random()`, `random.choice()`, etc.) produces
  the same sequence, because the global `random` module is patched to a seeded
  `random.Random` for the duration of the run.

**Two runs at the same seed print an identical schedule** — this is the week-2 demo
milestone and a hard gate.

---

## 3. What is NOT guaranteed

This list is published prominently, not buried. Honesty here is the same asset as the
bounded-exploration coverage statement (PRD §10.6, PRD §15.6).

| Not guaranteed | Why | Mitigation |
|---|---|---|
| **Live model calls (`passthrough`/`record` mode)** | The provider is non-deterministic even at `temperature=0` | Determinism is a property of **replay** mode; replay requires the cache |
| **Real network or filesystem I/O in user tools** | Outside AgentDX's control | Detected and reported as `nondeterminism_warning`; `--strict` aborts. Wrap such tools with `@agentdx.tool` and record them |
| **OS threads / `multiprocessing` spawned by user code** | The scheduler cannot observe or order work on a second OS thread | **Detected and rejected**: starting a thread inside an instrumented span raises `E-SCHED-004` in strict mode |
| **C-extension internal ordering (e.g., some BLAS reductions)** | Outside Python's control | Out of scope; affects values, not coordination structure |
| **Floating-point results across architectures** | Hardware | The event log stores hashes of values; a cross-architecture hash mismatch is reported as a value difference, not a scheduling difference — the schedule comparison still passes |
| **Memory addresses, `id()`, object identity** | CPython internals | Never used in any identity; all identity is content- or sequence-derived |
| **Wall-clock durations** | Physics | Excluded from the canonical projection |
| **`from datetime import datetime` held before run start** | A direct class reference bypasses the module-level patch | The leak detector catches subsequent calls; wrap such code or use `agentdx.wall_time()` |

### The verbatim domain statement

> *AgentDX guarantees determinism of the **coordination structure and the event log**, in
> replay mode, on a fixed AgentDX version. It does not guarantee determinism of model
> outputs in record mode, nor of user code that performs unmanaged I/O or concurrency.*

This statement appears verbatim in:
- CLI output (every run summary)
- API responses (`determinism_domain` field)
- This document

---

## 4. The canonical projection

The canonical projection excludes these fields before computing the log hash:

| Field | Reason excluded |
|---|---|
| `wall_ts_ms` | Real clock; varies between processes |
| `payload.duration_wall_ms` | Real measured wall time |
| `run_start.payload.host` | Machine name |
| `run_start.payload.pid` | Process ID |
| `run_start.payload.started_at_utc` | Wall-clock start time |
| `run_start.payload.env` | Environment variables (provenance, not identity) |

The exclusion list is **generated from the `Volatility.VOLATILE` marks in
`events/schema.py`**, not maintained here. To add or remove a field from the projection,
edit its `Volatility` mark; this document and the tests follow automatically. Do not
maintain a second list.

Source: `events.schema.excluded_field_paths()` → `events.canonical.canonical_log_hash()`.

---

## 5. How the trap works

At the start of every run, the runtime installs these patches (PRD §10.5):

| Source | Treatment |
|---|---|
| `random.*` module-level functions | Redirected to a seeded `random.Random(seed)` |
| `numpy.random` global state | Seeded if numpy is importable (best-effort) |
| `time.time`, `time.monotonic`, `time.perf_counter` | Return `clock.now_ms() / 1000` |
| `time.sleep` | Reports as a leak: blocks the single OS thread |
| `datetime.datetime.now`, `utcnow`, `today` | Return virtual epoch + virtual time |
| `uuid.uuid4`, `uuid.uuid1` | Seeded blake2b-derived deterministic UUIDs |
| `asyncio.sleep` | Redirected to virtual sleep (advances virtual clock) |
| `threading.Thread.start` | Reported as a leak (detected and rejected per PRD §10.6) |
| `hash()` randomisation | `PYTHONHASHSEED=0` required at process start; `agentdx doctor` checks |

All patches are removed on run exit, including on exception.

**The scheduler's seeded `Random` and the user's `random.random()` share one stream.**
This ensures that a user calling `random.random()` inside a task does not desynchronise
the scheduler's own `choose()` decisions. Both read from `guard.seeded_random`.

---

## 6. Error codes

| Code | Name | Trigger |
|---|---|---|
| `E-SCHED-001` | `SchedulerError` | Scheduler-internal bookkeeping broken; never user-triggered |
| `E-SCHED-002` | `LifecycleTransitionError` | Illegal lifecycle transition (e.g., COMPLETE → RUNNING) |
| `E-SCHED-003` | `DeadlockError` / `LivelockError` | No runnable task + no timer, or step budget exhausted |
| `E-SCHED-004` | `DeterminismLeakError` | An unpatchable nondeterminism source was reached (thread spawn, `time.sleep`) |

---

## 7. The PYTHONHASHSEED requirement

Python's hash randomisation (`PYTHONHASHSEED`) is seeded once at interpreter start and
cannot be changed mid-process. AgentDX requires `PYTHONHASHSEED=0` to ensure `dict` and
`set` iteration order is stable. The scheduler enforces this by sorting every collection
it iterates by an explicit stable key, so `PYTHONHASHSEED != 0` is caught at the gate
level, not silently tolerated.

The G3 gate runs ≥10 replays in fresh subprocesses with `PYTHONHASHSEED=0` in their
environment.

---

## 8. Auditing determinism

**What works today (P06, this build).** `agentdx run` and a `--print-schedule` CLI flag
are P17 surface — not built yet (`cli/main.py:run` is a declared `_not_implemented("run",
"P17")` stub; do not run the snippet below against it). What you can verify today is the
scheduler directly, against `tests/determinism/`:

```bash
# Gate G3 (formal) — 100 replays at seed 42, ≥10 in fresh subprocesses
PYTHONHASHSEED=0 pytest tests/determinism/test_replay_equality.py -v

# Same seed twice -> identical schedule_decision sequence (asserted in
# test_two_runs_at_the_same_seed_print_an_identical_schedule)

# Different seeds -> a different interleaving, not a crash or a silent identical one
# (asserted in test_different_seeds_produce_different_interleavings)

# A deliberately unpatched time.time() call in a fixture is caught with a stack frame
PYTHONHASHSEED=0 pytest tests/determinism/test_leak_detection.py -v
```

**Once P17 lands**, the CLI surface will be:

```bash
agentdx run scenarios/my_scenario.yaml --seed 42 --print-schedule
agentdx run scenarios/my_scenario.yaml --seed 42 --print-schedule   # same seed, twice
agentdx run scenarios/my_scenario.yaml --seed 99 --print-schedule   # different seed
```

This section will be updated to show real output once that command exists; until then the
`pytest` invocations above are the actual, runnable verification path.

---

## 9. Known determinism limits in this version

- **`from datetime import datetime` held before run start** bypasses the module-level
  patch. Code that holds such a direct reference and calls `.now()` inside a task will
  read the real clock. The leak detector catches and reports this; the run continues
  in non-strict mode.
- **C-extensions that read `/dev/urandom` or wall time internally** are outside
  Python's interception boundary. If you suspect a library does this, test with
  `--strict` and inspect the `nondeterminism_warning` events.
- **asyncio internals** maintain wall-clock state for timeouts and scheduling. AgentDX
  replaces `asyncio.sleep` with a virtual redirect, but does not patch internal asyncio
  timers (e.g., `asyncio.timeout`). Avoid `asyncio.timeout` inside instrumented tasks.

## 10. A non-guarantee that is easy to mistake for one: flush timing

`events/writer.py` flushes buffered events to the sink on two triggers — a batch-size
threshold (`DEFAULT_BATCH_SIZE`) and, since P06 (closing CONTEXT.md D-16), a wall-clock
interval (`DEFAULT_FLUSH_INTERVAL_MS`). **Neither trigger is part of the determinism
guarantee.** They govern *when*
already-decided bytes reach the store — a liveness property for the live API and the
Control Tower — never *what* those bytes are or the order they were validated and chained
in. Five different batch sizes over the same log produce the same canonical log hash.

---

*Last updated: P06 — `runtime/` built.*
