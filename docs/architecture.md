# Architecture

**Audience: a new contributor, on their first day.** This is PRD §24–§27 condensed to the
shape you need before you touch anything. It is a map, not a specification — where this
document and the PRD disagree, the PRD wins (`CONTEXT.md` §0.5), and the per-module contracts
live in their own docs, linked throughout.

Read `CONTEXT.md` first. This document tells you how the pieces fit; `CONTEXT.md` tells you
which pieces actually work today, and those are different questions.

---

## 1. The one-sentence version

AgentDX runs your multi-agent graph under a **deterministic cooperative scheduler on a virtual
clock**, serves every model call from a **record/replay cache**, writes every observable action
to an **append-only event log**, and then runs **pure analytical passes over that log**.

Everything below follows from that sentence. The scheduler exists so a run is reproducible; the
cache exists so it is reproducible *and* free; the log exists so analysis never has to touch a
live agent object; and analysis is pure so the same log analysed twice gives the same answer.

---

## 2. Layers, and the one rule that holds them apart

```
YOUR AGENT SYSTEM  (LangGraph, or plain Python behind @agent / @tool)
        │  instrumented via decorator or LangGraph callback adapter
        ▼
AGENTDX RUNTIME    single process, single OS thread for the scheduler
   deterministic scheduler + virtual clock
   fault injector · LLM record/replay cache · event writer
        ▼
EVENT STORE        SQLite (WAL, append-only) → Parquet → DuckDB views
        ▼
ANALYSIS LAYER     pure functions over the log
   causality · race · timing · overhead · redundancy · baseline
   resilience · verdict · exploration
        ▼
API                FastAPI: REST for runs and reports, WebSocket for the live stream
        ▼
CONTROL TOWER      React + Vite + TypeScript + React Flow + Zustand + Tailwind + visx
```

**The rule: `analysis/` may not import `runtime/` or `sdk/`, and may not import any model
client.** This is invariant I3 and I13 (`CONTEXT.md` §2), and it is enforced mechanically by
import-linter in CI (`.importlinter`), not by convention.

It is worth understanding *why* before you find yourself wanting to break it. Analysis that can
reach a live agent object is analysis that can accidentally depend on the state of a run rather
than on its record — and the moment it does, the same log stops producing the same findings, and
the product's central claim goes with it. The rule is what makes the analysis layer testable
against hand-authored event logs with hand-computed expected outputs, which is how most of this
codebase is tested (`AGENTS.md` §5).

`analysis.baseline` is the one analyser that must *execute* something. It does so through a
`BaselineExecutor` protocol declared in `analysis/` and constructed in `cli/`. Injection is what
**avoids** the import; it is not a licence to add one. There is deliberately no allowlist entry
for it in `.importlinter`.

The complete layer table is `CONTEXT.md` §4, which is the source of truth for the import-linter
config — not this document.

---

## 3. Processes

| Process | Contains | Lifetime |
|---|---|---|
| **CLI / runner** | Runtime, scheduler, SDK, injector, cache, event writer, analysis | One run |
| **API server** | FastAPI, analysis (read-only), static frontend | Long-lived (`agentdx ui`) |
| **Browser** | Control Tower | A user session |

Two processes share the SQLite file in **WAL mode**: the runner is the single writer, the API
server is a reader that never writes events. That is exactly the concurrency profile WAL is
designed for, and it is why the storage choice is a file rather than a server (PRD §27.1).

The API server never imports the runtime. When it starts a run (`POST /api/runs`) it launches a
**subprocess** and tails the event table. This is enforced by the same import-linter contract as
the analysis rule.

---

## 4. Where a run's determinism actually comes from

Four mechanisms, and it is useful to know which one is doing the work when something is
non-deterministic:

1. **The scheduler.** Every scheduling decision is a pure function of `(seeded RNG, sched_step,
   sorted runnable set)`. Nothing is ordered by hash iteration, arrival time, or set iteration.
2. **The virtual clock.** Time advances only at a yield point with nothing runnable. Wall time is
   never consulted for anything that reaches the log's canonical projection (invariant I11).
3. **The cache.** `replay` is the default mode, and a replay-mode miss is a **hard error**
   (`E-CACHE-001`, exit 3) — never a silent live call, and no "fall back to live" flag exists
   (invariant I7).
4. **The determinism traps.** `runtime/determinism.py` patches the ambient sources — `time.*`,
   `datetime.now`, `random.*`, `uuid4`, `asyncio.sleep` — for the duration of a run, and raises
   on a detected leak rather than letting one through quietly.

The traps have real blind spots (a name captured before the guard installs; an unpatchable C
extension). They are documented in [`determinism-guarantees.md`](determinism-guarantees.md) §9,
which is the contract for this layer and the thing to read before you change anything under
`runtime/`.

Because a trap that is bypassed is a silent invariant violation rather than a loud one, there is
also a **lint rule**: direct calls to the banned clock, random and id sources are rejected under
`src/agentdx/`, with four sanctioned exceptions, each annotated `# determinism-exempt:`. The
exact list is `AGENTS.md` §4.1 — check it before adding an exemption, because "I needed a
timestamp" is not one of the four.

**The canonical projection is derived, never listed.** What participates in determinism equality
comes from per-field `Volatility` marks in `events/schema.py`. A hand-maintained exclusion list
anywhere in this codebase is a defect, including one transcribed from the PRD — the PRD's own
list called itself exhaustive and was already missing a field (`CONTEXT.md` §10, C-7). Ask
`schema.excluded_field_paths()`.

---

## 5. The modules

| Module | Responsibility | Contract doc |
|---|---|---|
| `events/` | Schema, validation, canonical form, writer. **Imports nothing** — it is the root contract | [`event-schema.md`](event-schema.md) |
| `store/` | SQLite, DuckDB, snapshots, bundles | [`storage.md`](storage.md) |
| `runtime/` | Scheduler, virtual clock, determinism traps, run context | [`determinism-guarantees.md`](determinism-guarantees.md) |
| `runtime/cache/` | LLM record / replay / perturb | [`cache.md`](cache.md) |
| `runtime/faults/` | Fault registry, triggers, blast radius, abort guards | [`chaos-safety.md`](chaos-safety.md) |
| `sdk/` | Decorators, LangGraph adapter, provider shims | [`sdk.md`](sdk.md) |
| `scenario/` | YAML schema, validation, matrix expansion, assertions | [`scenario-reference.md`](scenario-reference.md) |
| `analysis/causality`, `race` | Vector clocks, happens-before, race detection | [`race-detection.md`](race-detection.md) |
| `analysis/timing`, `overhead`, `redundancy`, `aggregates` | Timing DAG, critical path, six-bucket decomposition | [`performance-analysis.md`](performance-analysis.md) |
| `analysis/baseline`, `verdict` | Baseline generation, comparability grading, the verdict | [`baseline-methodology.md`](baseline-methodology.md) |
| `explore/` | Delay-bounded schedule exploration, reduction, dedup | [`exploration.md`](exploration.md) |
| `api/` | FastAPI app, REST, WebSocket, OpenAPI | [`api.md`](api.md) |
| `cli/` | Commands, exit codes, output formatting | [`cli.md`](cli.md) |
| `fixtures/` | The three reference systems and their golden corpora | [`fixtures.md`](fixtures.md) |

Two things about that table are load-bearing. **`events/` imports nothing** — every other module
depends on it, so a change there is a change to everything, which is why the schema is frozen and
`schema_version` bumps are ADR-gated. And **`cli/` may import everything but contains no business
logic** — every computation it performs is delegated to a library module, so the CLI is a surface,
not a place where behaviour lives.

---

## 6. A run, end to end

```
CLI → Scenario.load → validate → Runtime.create_run
  → Scheduler.run
      loop:
        collect_runnable → FaultInjector.check(pre_schedule)
        choose(seeded)   → resume(task)
            task hits a yield point (llm / tool / state / message)
              → FaultInjector.check(pre_*)
              → Cache.get / tool exec / state op
              → EventWriter.write(...)   [stamped under the scheduler lock]
              → schedule completion at now + duration
        advance the clock if nothing is runnable
  → seal the log → run the analysers → write the verdict → print the scorecard → exit
```

`Scheduler.stamp(draft, causes=())` and `scheduler.recorder.emit(draft, causes)` are the **only**
places a stamp is constructed. The SDK builds unstamped `DraftEvent`s and cannot stamp — there is
an AST-level test that enforces this. Events flow *through* the stamping point, never around it,
and `causes` is what drives both `causal_parents` and the vector-clock merge.

After the seal, analysis reads the log and nothing else:

```
log ──▶ causality ──▶ race ──┐
    ├─▶ timing ──▶ overhead ─┼──▶ verdict ──▶ scorecard ──▶ SQLite analysis tables
    ├─▶ redundancy ──────────┤                                      │
    └─▶ baseline ────────────┘                            FastAPI ──┴──▶ Control Tower
```

Every finding, verdict and scorecard line carries concrete event `seq` references. An empty
evidence array is a schema failure and cannot be rendered — invariant I6, and one of the six
never-waived quality gates (PRD §44.3).

---

## 7. Storage, briefly

SQLite owns the write path: event ingestion (append-only, enforced by triggers on every
connection, not by discipline), run and finding metadata, state snapshots, and — in a separate
file — the LLM cache. Events are appended in batches inside one transaction; the exact trigger is
PRD §27.3.

DuckDB owns analytical aggregation, read-only, over Parquet exported at seal time. It never holds
authoritative data. Above a configurable event-count threshold a run is exported and analysed
through DuckDB; below it, analysis reads SQLite directly. **This is a performance decision and
never a semantic one** — both paths must produce identical analysis output, and a test asserts
exactly that. If DuckDB is unavailable, analysis falls back to SQLite with a warning rather than
hard-failing on an optional accelerator.

A run bundle (`.agentdx`) is a file, so export is a copy rather than a dump, and backing up the
data directory is copying a directory. Import is idempotent by `run_id` plus `canonical_log_hash`.

Full detail: [`storage.md`](storage.md), PRD §27.

---

## 8. What the architecture does not do today

An architecture document that describes the intended shape and stays silent about the parts that
do not connect yet is a document that will mislead its first reader. `CONTEXT.md` §5, §6 and §7
are authoritative and current. Both structural gaps this section used to describe here are now
closed; they are kept below, marked, rather than deleted, because the history — what was broken,
and what closing it actually took — is exactly what a reader forming an assumption would want to
check rather than take on faith:

- ~~**No fixture graph completes a run.**~~ **Closed 2026-09-03 (ADR-019, D-62).** Nothing in
  `sdk/` called `runtime.scheduler.Scheduler.spawn()`, so LangGraph's parallel fan-out had no way
  to become a scheduler task and the cooperative loop deadlocked — the control-flow diagram in §6
  was the design, not something that executed. `Scheduler.begin_call`'s dispatch gap was fixed
  (candidate β) and independently OP-2 reviewed; re-confirmed again this session against a fresh,
  empty data directory for all three reference fixtures (`code_pipeline`, `support_triage`,
  `research_fanout`), genuine cold runs, all exit 0. Eight of the ten PRD §44.1 acceptance gates
  now pass because of this; see `CHANGELOG.md`'s "Release readiness" section for which two do not
  and why.
- ~~**The Control Tower is not served by the API.**~~ **Closed 2026-09-07.** `api/app.py` now
  mounts a static-file route behind the REST and WebSocket routers, with SPA fallback to
  `index.html` for the frontend's real History-API router (`frontend/src/routes/router.tsx`) —
  PRD §39.4's "frontend shipped inside the wheel" and §39.5's serving model are both implemented.
  What is still true: this has been verified by test suite and code review, not by running the
  built Docker image, since no Docker daemon is available anywhere this project has been worked
  on. The Vite dev server remains how the frontend is reached during development.

Both gaps took real, dated, independently-verifiable work to close, not a documentation change —
the corrections above cite what actually closed each one, on the same evidence standard the rest
of this project holds itself to.

---

## 9. Where to look next

- `CONTEXT.md` — invariants, locked decisions, build state, and every open question. **Read this
  before forming an assumption**, not after.
- `AGENTS.md` — the standing engineering rules, including the determinism lint and Rule E1.
- [`limitations.md`](limitations.md) — what this tool cannot tell you, stated plainly.
- `.importlinter` — the machine-readable form of §2's layer contract.
- PRD §24–§27 — the sections this document condenses.
