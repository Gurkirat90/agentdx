# AgentDX — Project Context Ledger

> **This file is the running state of the project. The PRD is the running spec of the project.**
> Read this file *first*, before any other file, at the start of every session — human or AI.
> **Never copy spec content into this file.** Cite `PRD §n` instead. A duplicated spec is a spec that will drift.

| Field | Value |
|---|---|
| Ledger version | 1.2 |
| Project | AgentDX — multi-agent coordination debugger, deterministic replay runtime, chaos harness |
| Spec of record | `AgentDX-PRD-v2.md` (PRD & Technical Product Specification v2.0, 8 Aug 2026) |
| Build window | 10 weeks, solo build |
| Last updated | 2026-08-10 · OP-3 after a P02 self-audit (F1/F2/F6 fixed; D-10, D-11 declared; E-EVENT-044/045 added) |
| Current phase | Week 1 — `events/` BUILT, **schema not yet frozen** (awaiting P05 fixtures + green CI on 3.12), starting P03 (`store/`) |

---

## 0. Read-first protocol (for any AI or human picking this project up)

1. Read **this file end to end**. It is capped at 500 lines; nothing here is optional.
2. Read the PRD sections named in your prompt's `AUTHORITATIVE INPUTS`. Do not read the whole PRD unless asked.
3. Read `AGENTS.md` for standing engineering rules.
4. Check §5 (build state) for what exists, §9 (deviations) for where reality already differs from the PRD, and §10 (open questions + known PRD conflicts) before you form any assumption.
5. **Precedence when sources conflict:**
   `AGENTS.md` (process) → §2 Invariants → §8 Decision Log (highest ADR number wins) → PRD → §5 Build State → anything else.
   A ledger ADR beats the PRD **only if** it names the PRD section it overrides in its `Overrides` column. Otherwise the PRD wins and the ledger is wrong — fix the ledger.
6. Never silently resolve a conflict. PRD-internal contradictions go in §10 with a ruling; code-vs-PRD divergence goes in §9. Surface it in your response either way.

**Operational prompts (`OP-n`) — defined here because §5, §11 and `AGENTS.md` §2 referenced them before anything said what they were.** They are not build prompts: none of them appears in the §5 roadmap, and any of them may run at any time.

| ID | Purpose | Reads | Writes | Trigger |
|---|---|---|---|---|
| **OP-1** | **Re-plan.** Re-sequence remaining prompts, re-price the scope-cut order (§12), re-check the hard floor against elapsed time | `CONTEXT.md`, PRD §40 | §5, §7, §12 + an ADR if the sequence changes | Schedule slip, or a prompt discovering that a later one is now impossible |
| **OP-2** | **Independent audit** of a `BUILT` module against its prompt's `DELIVERABLES`, the invariants it claimed to hold, and its acceptance gate. **Read-only: it changes no code.** It produces findings, and it must run each gate itself rather than trusting the build session's report | The module, its prompt, `CONTEXT.md`, the cited PRD sections | §5 `Status` → `VERIFIED` (or findings that trigger OP-3), §13 `Audit` column | A module reaches `BUILT`. Required before `VERIFIED`; **not** required before the next build prompt starts |
| **OP-3** | **Repair.** Fixes a defect found by an §11 tripwire or an OP-2 finding. Scoped to the named defect; it is not a licence to refactor | The finding, the offending module | The module + a regression test that fails before the fix (`AGENTS.md` §5) + §9 if it declares a deviation | An §11 tripwire fires, or OP-2 reports a finding |

**Best run by a different model than built the module** — an auditor that shares the builder's blind spots inherits them. `VERIFIED` means an OP-2 audit passed; a module the building session merely reports as working is `BUILT`, never `VERIFIED`.

---

## 1. Project identity — the one paragraph that must never drift

AgentDX is a **pre-deployment reliability and coordination-debugging system for multi-agent AI applications**. It takes an existing agent graph (LangGraph, or any Python multi-agent system wrapped with the decorator API), executes it under a **deterministic cooperative scheduler driven by a virtual clock**, serves every LLM call from a **record/replay cache**, optionally injects **controlled faults**, records every observable action into an **append-only event log**, and then runs **pure analytical passes over that log** to answer one question: *is this multi-agent system actually better than one agent doing the same work — and if not, exactly where did the time, tokens and correctness go?*

The thesis obliges eleven things to be **measurable, not merely visible** (PRD §1.2, table M1–M11). Every module traces to a row in that table. Work that serves no row is negotiable.

**The product is not an observability dashboard.** Observability watches production and shows what happened. AgentDX runs the system under controlled conditions before deployment and tells you *why*, reproducibly, offline, for free.

**If a proposed change makes the system less deterministic, less evidence-backed, or more like a generic tracing UI, it is the wrong change.**

---

## 2. Invariants — violating any of these is a project-level failure

Admission test: violating it invalidates the product thesis (§1) or a named acceptance gate (§6). Good practices are not invariants; they live in `AGENTS.md`.

| # | Invariant | Mechanically enforced by |
|---|---|---|
| **I1** | **Determinism.** Same seed + same cache + same scenario → byte-identical *canonical projection* (PRD §10.7) of the event log. 100/100 replays, ≥10 in fresh processes. | Gate G3, `tests/determinism/` |
| **I2** | **The event log is append-only and immutable.** Nothing edits or deletes an event after write. Analysis never mutates. | SQLite triggers in `store/sqlite.py` + kill-test suite |
| **I3** | **Analysis is pure.** `agentdx.analysis.*` must not import `agentdx.runtime.*` or `agentdx.sdk.*` — **no module is exempt, `analysis.baseline` included.** Baseline is the one analyser that must *execute* a run, and it does so via a `BaselineExecutor` protocol injected by the caller: the protocol type is declared in `analysis`, the implementation is constructed in `cli`. Injection is the mechanism that **avoids** the import; it is not a licence to import (PRD §24.3, "rather than importing the runtime directly"). | import-linter in CI, with **no allowlist entry** for `analysis.baseline` |
| **I4** | **Zero false positives on the healthy fixture.** `research_fanout` yields an empty race-findings set across all 100 determinism replays **and** the k=2 exploration frontier. A tool that reports races in correct code is worse than no tool. | Gate G2, `tests/false_positives/` — including its own k=2 harness, which is P0 and independent of FR-6 (ADR-002) |
| **I5** | **Race precision = 1.0** on the labelled benchmark set. Recall may be < 1.0 and is reported honestly. Precision is never traded for recall. | PRD §34.3 benchmark, `bench/results/` |
| **I6** | **Every verdict, finding and scorecard line carries evidence** = concrete event `seq` references. An empty evidence array fails schema validation and cannot be rendered. | Verdict schema (PRD §18.4) |
| **I7** | **Offline by default.** The three-fixture demo runs with the network disabled and no API keys in the environment. A `replay`-mode cache miss is a hard error (`E-CACHE-001`, exit 3) — never a silent live call, and no "fall back to live" flag may exist. | Gate G9, NFR-4 |
| **I8** | **Privacy by default.** Prompt/response bodies are never written to the event log unless explicitly opted in. | NFR-6, automated plaintext scan of the DB |
| **I9** | **No statistic ships without a reproducible measurement** in `bench/results/` (Rule E1). | CI check: every published number carries a `[bench:<file>]` marker resolving to a committed result — see `AGENTS.md` §6 |
| **I10** | **The bounded-exploration coverage statement appears verbatim** — *"Bounded search: absence of findings is not proof of absence."* — in CLI output, API responses and the UI. Removing it is a release blocker. | Required `coverage_statement` API field + string test (PRD §15.6) |
| **I11** | **Virtual time and wall time are never conflated.** Any unqualified duration in code, output or docs is virtual time. `wall_ts_ms` and the other PRD §10.7 volatile fields are excluded from the canonical projection. | Canonical projection test; naming lint (`*_wall_ms` suffix required) |
| **I12** | **Chaos is fixture-only by default.** A user graph requires `chaos_opt_in: true` *in the scenario file* plus a non-empty blast radius; every fault carries a declared blast radius, steady-state hypothesis and abort guards. | Scenario validation (`E-SCEN-004`) + runtime re-check (`E-CHAOS-001`), PRD §13.3–13.6 |
| **I13** | **No model inference anywhere in the analysis, verdict or scoring path.** Every finding is produced by a deterministic algorithm. LLM-as-judge and embedding-based semantic dedup are out of scope (PRD §4.4, §43.3.2, §43.3.3); a model in this path breaks I1 and NFR-14 at once. | import-linter: `analysis.*` may not import any provider/SDK client; NFR-14 (same log analysed 100× → identical findings) |

*Candidates tested and rejected as invariants (they are gate criteria or standing rules, not thesis-level):* comparability grading of the baseline (PRD §17.5 — enforced by G6), the event-schema freeze (§3 + tripwire 6), single-OS-thread execution (a consequence of I1).

---

## 3. Locked decisions — do not renegotiate without an ADR

| Area | Locked value |
|---|---|
| Name | **AgentDX** (43.1.1) |
| Language / runtime | Python 3.12, asyncio, single OS thread for the scheduler; user-spawned threads are rejected in strict mode (PRD §10.6) |
| Agent framework | LangGraph primary + generic Python decorator API. **No CrewAI adapter in v1** (43.1.3) |
| Model for agent brains | Groq, Llama 3.1 8B, free tier, via an **OpenAI-compatible shim** |
| Event store | SQLite (WAL), append-only; DuckDB over exported Parquet for analytics (threshold ~20 000 events) |
| LLM cache | SQLite. Four modes: `record` / `replay` / `perturb` / `passthrough`. **`replay` is the default**; `passthrough` exists only for the NFR-1 overhead benchmark (PRD §11.2) |
| MVP fault set | `latency`, `agent_crash`, `message_drop`, `tool_failure` **only**. The other six fault types (reorder, duplicate, agent_slow, rate_limit, byzantine, state_corrupt) are P1 (PRD §4.1) |
| API | FastAPI + `websockets`, bound to `127.0.0.1:8420`, no auth; `--host` is required to bind elsewhere and prints a no-auth warning. OpenAPI 3.1 at `/openapi.json`; the frontend client is generated from it |
| CLI | Typer; exit codes 0–7 are **authoritative and stable** (PRD §37.2) — changing one is a breaking change |
| Frontend | React 18 + Vite + TypeScript + React Flow + Zustand + Tailwind + visx |
| Packaging | `uv` (+ hatch), Docker Compose, `just` task runner |
| Determinism guarantee | **Canonical projection equality**, not literal byte equality (43.1.6) |
| Event schema | Frozen at end of week 1. `causal_parents`, `span_id`, `sched_step`, `schema_version`, hash chain all adopted (43.1.5) |
| Scenario YAML | P0, week 2 (FR-11a). `--ci` mode is P1 (FR-11b) (43.1.2, PRD §4.5) |
| Byzantine faults | Measured via curated wrong-output fixtures + the system's response, **no judge model** (43.1.4) |
| Redundancy detection | Exact hash of `(tool_name ‖ canonical_json(args))` only in v1. No embedding similarity (43.3.2) |
| Exploration reduction | Independence-based reduction in v1; sleep sets / full DPOR only if redundancy > 40 % (43.2.4) |
| Thresholds & weights | `src/agentdx/analysis/verdict_rules.toml` — versioned, printable via `agentdx analyze --explain`. No threshold inline in code |
| Platforms | macOS (Apple silicon + Intel) and Linux x86_64/arm64, Python 3.12+. **Windows unsupported in v1** |
| Design tokens | `--navy-900 #0A2947`, `--cream #F3E4C9`, `--sage #D3D4C0`, `--clay #8B5E3C` + derived status tokens (PRD §29.1). Mono face with tabular numerals for all numerics. `--sage-dim` is never body text |

**Dependency rule:** the stack above plus PRD §24.6 and PRD §25 is the complete permitted dependency set. Anything else requires an ADR before it enters `pyproject.toml` or `package.json`.

---

## 4. Architecture map — the shape that must stay true

```
user graph ──emit──▶ SDK ──stamp(seq, sched_step, vclock, virtual_ts)──▶ EventWriter ──▶ SQLite (append-only)
                                                                                          │
                                                              run sealed ─────────────────┤
                                                                                          ▼
                                                                     export Parquet ──▶ DuckDB views
                                                                                          │
                                                          analysers (PURE, ordered) ◀──────┘
                                                                 │
                       findings + scorecard + verdict ──▶ SQLite (analysis tables)
                                                                 │
                                                        FastAPI ──REST/WS──▶ Control Tower
```

**Layer contract** (PRD §24.3; enforced by import-linter — this table is the config's source of truth)

| Layer | May import | Must not import |
|---|---|---|
| `events/` | — | everything (it is the root contract) |
| `store/` | `events` | `runtime`, `sdk`, `analysis` |
| `runtime/` | `events`, `store` | `analysis`, `sdk` |
| `sdk/` | `events`, `runtime` | `analysis` |
| `analysis/` | `events`, `store` | **`runtime`, `sdk`, any model client** (I3, I13) |
| `explore/` | `runtime`, `analysis.race` | — |
| `api/` | `store`, `analysis` | **`runtime`** — the API server never imports the runtime; it launches runs as a **subprocess** and tails the event table (PRD §24.2) |
| `cli/` | everything | — |

Two processes share the SQLite file in WAL mode: the runner (single writer) and the API server (reader only, never writes events).

**The one thing about `events/` every later prompt must know.** The canonical projection (PRD §10.7) is **derived** from per-field `Volatility` marks in `events/schema.py`. It is not a list. **A hand-maintained set of excluded fields anywhere in this codebase is an I1 defect, not a shortcut** — including one transcribed from PRD §10.7, whose own list calls itself exhaustive and was already missing `run_end.payload.wall_makespan_ms` (§10 C-7). To change what participates in determinism equality, edit the field's mark; `canonical.py`, the validators, `docs/event-schema.md` and the property test all follow. Ask `schema.excluded_field_paths()`; never restate it.

---

## 5. Build state ledger

Status: `NOT STARTED` · `IN PROGRESS` · `BUILT` (code + tests exist, self-reported) · `VERIFIED` (passed an independent OP-2 audit) · `GATED` (its acceptance gate passes in CI).
`Wk` = PRD §40.1 roadmap week. Deviations from that schedule are ADR-logged in §8.

| # | Module / surface | Prompt | Wk | Tier | Status | Gate | Verified on |
|---|---|---|---|---|---|---|---|
| 1 | Repo scaffold, toolchain, CI spine | P01 | 1 | — | BUILT | — | — |
| 2 | `events/` — schema, validators, canonical form, writer | P02 | 1 | P0 | BUILT | schema freeze — **not yet frozen**; Q-P02.1 accepted, awaiting P05 + green 3.12 CI | — (OP-2 not run; see §0) |
| 3 | `store/` — SQLite, DuckDB, snapshots, bundles | P03 | 1 | P0 | NOT STARTED | — | — |
| 4 | `sdk/` — decorators, LangGraph adapter, provider shims | P04 | 1 | P0 | NOT STARTED | FR-1 (overhead < 10 %, NFR-1) | — |
| 5 | `fixtures/` — all three reference systems + golden corpus | P05 | 1 | P0 | NOT STARTED | FR-12 | — |
| 6 | `runtime/` — scheduler, clock, determinism traps | P06 | 2 | P0 | NOT STARTED | **G3** | — |
| 7 | `runtime/cache/` — record / replay / perturb | P07 | 3 | P0 | NOT STARTED | **G9** | — |
| 8 | `scenario/` — YAML schema, validation, assertions | P08 | 2 | P0 | NOT STARTED | FR-11a | — |
| 9 | `runtime/faults/` (4 MVP faults) + chaos safety | P09 | 4 | P0 | NOT STARTED | **G4** | — |
| 10 | `analysis/timing`, `overhead`, `redundancy` | P10 | 5 | P0 | NOT STARTED | **G5** | — |
| 11 | `analysis/baseline`, `verdict` | P11 | 6 | P0 | NOT STARTED | **G6, G7** | — |
| 11b | `analysis/resilience` | P11 | 9 | **P1, cut 3** | NOT STARTED | FR-9 | — |
| 12 | `analysis/causality`, `race` | P12 | 7 | P0 | NOT STARTED | **G1, G2** | — |
| 12b | `tests/false_positives/` k=2 harness — test-only schedule enumeration, no product surface | P12 | 7 | **P0** (ADR-002) | NOT STARTED | **G2** (frontier half) | — |
| 13 | `explore/` — bounded schedule exploration, shipped feature | P13 | 10 | **P1, cut 1** | NOT STARTED | FR-6 | — |
| 14 | `api/` — REST + WebSocket + OpenAPI | P14 | 8 | P0 | NOT STARTED | — | — |
| 15 | Control Tower shell, tokens, Waterfall, Scorecard | P15 | 8 | P0 | NOT STARTED | (G8 partial) | — |
| 16 | Graph, Findings, Chaos, Timeline panels + live stream | P16 | 9 | mixed | NOT STARTED | **G8** | — |
| 17 | `cli/` + CI mode + exit codes | P17 | 10 | CLI P0, `--ci` **P1, cut 2** | NOT STARTED | FR-11b | — |
| 18 | Test hardening, false-positive suite, benchmarks | P18 | cont. | P0 | NOT STARTED | **G1–G7** | — |
| 19 | Packaging, Docker demo, OTel, docs, release | P19 | 10 | OTel **P1, cut 5** | NOT STARTED | **G10** | — |

**Two sequencing hazards baked into this table — read before starting P11 or P15.**
- P11 bundles a hard-floor P0 item (baseline + verdict, week 6) with a cut-safe P1 item (resilience, week 9). Row 11b exists so cut 3 can be taken without unpicking a prompt. Build them as separate modules and separate commits.
- G8 is a **single** PRD gate (§44.1) covering waterfall + ghost baseline + scorecard + graph + findings + scrubber + cross-highlighting. It is therefore only satisfiable at P16, and it depends on the graph panel, which is scope-cut #4. If cut 4 is taken, G8 must be formally amended by ADR — it cannot simply be reported as passing.

---

## 6. Acceptance gate status

Criteria are short pointers; the binding text is PRD §44.1. Gates are met only when the command exits 0 in CI on a clean checkout.

| Gate | Criterion (short) | Verification command | Status |
|---|---|---|---|
| G1 | `code_pipeline` yields ≥1 `lost_update` on `draft.module_a`, severity `critical`, naming both writes | `agentdx run fixtures/code_pipeline --assert findings.race >= 1` | ✗ |
| G2 | Zero false positives on `research_fanout` (100 replays + k=2 frontier). Both halves are P0 and survive every scope cut (ADR-002) | `pytest tests/false_positives/ -q` | ✗ |
| G3 | Deterministic replay 100/100 at seed 42, ≥10 fresh processes | `pytest tests/determinism/test_replay_equality.py` | ✗ |
| G4 | Killing `reviewer` at t=3000 reproduces the same failure class and cascade shape, 20/20 | `agentdx scenario run scenarios/kill_reviewer.yaml --repeat 20` | ✗ |
| G5 | Σ(six critical-path buckets) + residual = **virtual** makespan, residual < 2 %, on all three fixtures (see §10 C-1) | `pytest tests/analysis/test_decomposition_invariant.py` | ✗ |
| G6 | Baseline generated for all three fixtures **with a comparability grade** and evidence-linked figures | `agentdx compare <run_id> --baseline` | ✗ |
| G7 | Scorecard prints achieved + ideal speedup and the signed six-bucket attribution summing to the delta | `agentdx analyze <run_id> --scorecard` | ✗ |
| G8 | Control Tower renders the complete workflow and cross-highlights a finding across span, node and timeline | `npm run test:e2e` (Playwright, 3 fixtures) | ✗ |
| G9 | Demo works offline, no API keys | `just demo-offline` (`--network none`) | ✗ |
| G10 | `docker compose up` → healthy `/api/health` + populated run list in < 180 s, cold, arm64 | `just bench-docker-cold` | ✗ |

**Never waived, regardless of schedule (PRD §44.3):** G2, G3, race precision = 1.0, the verbatim coverage statement, Rule E1, evidence on every verdict. These map to I4, I1, I5, I10, I9, I6.

---

## 7. Current position

**Active prompt:** *(none)*
**Next prompt:** P03 — `store/`: SQLite (WAL), DuckDB views, snapshots, bundles. It implements the `EventSink` protocol declared in `events/writer.py` and the `prev_hash`/`this_hash` columns ruled in C-4.
**Blocked on:** nothing. Q-P02.1 is accepted (with three amendments, §10). The freeze still waits on P05 and on a green `just ci` under Python 3.12.
**Week (roadmap):** 1 of 10

**Schema-freeze checklist (43.1.5, end of week 1):** ① ~~Q-P02.1 signed off~~ **done** · ② P05 fixtures prove the schema sufficient against all three reference systems — **outstanding** · ③ `just ci` green on Python 3.12 — **outstanding**. Recommended but not blocking: an OP-2 audit of `events/` once ② lands, re-examining the Q-P02.1 schemas, which were accepted by delegation rather than independent review.

---

## 8. Decision log — APPEND ONLY

Format: `ADR-NNN · date · decision · rationale · PRD sections overridden · consequences`.
Never edit or delete a row. To reverse a decision, append a higher-numbered ADR that supersedes it. Enforced by the CI check in `AGENTS.md` §10.

| ID | Date | Decision | Rationale | Overrides | Consequence |
|---|---|---|---|---|---|
| ADR-000 | 2026-08-08 | PRD v2.0 adopted as the spec of record | Single source of truth for the build | — | All prompts cite PRD sections; the PRD is read-only in `docs/` |
| ADR-001 | 2026-08-10 | **All three reference fixtures (`code_pipeline`, `support_triage`, `research_fanout`) are built in week 1 under P05**, rather than weeks 1 / 7 / 10 | (a) `research_fanout` is the false-positive control for I4/G2. Under the PRD schedule it does not exist until week 7, so every analyser shipped in weeks 5–6 would be developed with no known-zero-findings input to test against — the exact condition that produces a detector nobody trusts. (b) The event schema freezes at the end of week 1 (43.1.5). Fixtures are the only realistic consumers that can prove the schema is sufficient *before* the freeze; a fixture built in week 10 that needs a new field forces a post-freeze schema break (tripwire 6). (c) The redundancy detector lands in week 5, four weeks before `support_triage`, its only seeded-redundancy input | PRD §40.1 (wk 7 "research fan-out fixture", wk 10 "support-triage fixture"); PRD §44.2 FR-12 "Ships in Wk 1/7/10" | 1. P05 grows and sits on the project's own critical path (§40.2) — week-1 slippage costs a week downstream. 2. Week-1 fixtures run **without** the scheduler (wk 2) or the cache (wk 3), so their golden corpora are provisional; they **must** be regenerated at the end of P07 and that regeneration is an explicit deliverable of P07, not a convenience (`AGENTS.md` §5). 3. Building `support_triage` early does **not** promote it out of the scope-cut order — it remains cut #6 (§12). 4. G1/G2/G5 expectations for all three fixtures become writable in week 1 as failing tests |
| ADR-002 | 2026-08-10 | **The k=2 false-positive harness is split out of FR-6 and made P0.** A minimal, test-only schedule enumerator lands in `tests/false_positives/` at P12 (week 7). It enumerates the k=2 frontier and asserts an empty finding set; it has no CLI, no API field, no UI, no reduction reporting and no coverage panel. FR-6 remains the shipped exploration *product* — P1, week 10, scope-cut #1 | G2 is on the never-waived list (PRD §44.3.1) yet half of it depended on FR-6, the first thing to be cut. That is a gate that can be legally deleted by a schedule slip. The dependency is also accidental rather than essential: G2 needs schedule *enumeration*, roughly a day of code, while FR-6 is enumeration plus reduction, dedup, budgeting, reporting and a UI panel. Splitting them makes G2 genuinely unwaivable at a fraction of FR-6's cost. It also matters which half survives — the 100 replays all execute the *same* interleaving, so the frontier is the half that actually tests for false positives across differing schedules | PRD §40.1 (wk 10 "Bounded exploration (FR-6)") and §44.2 FR-6 "Ships in Wk 10", **partially**: the test-only harness moves to week 7. FR-6 itself is unmoved | 1. P12 grows by the harness; write it in the same prompt as the race detector so the detector is never developed without its control. 2. Taking cut 1 no longer touches G2 — §12 updated accordingly. 3. The harness is **not** a head start on FR-6 and must not accrete features; if it grows a flag or an output format, that is scope creep to surface, not progress. 4. Supersedes the C-3 ruling in §10 |
| ADR-003 | 2026-08-10 | **LangGraph pinned to `>=1.2,<1.3`; graph API only — no `@entrypoint` / functional API in v1** | Closes Q-43.2.1, whose deadline is week 1. 1.2 is the current minor and the one the adapter will be developed and tested against; a floating pin means a LangGraph patch release can change node-callback ordering, which is an I1 failure that looks like our bug. Excluding `@entrypoint` follows the PRD §43.2 default: the graph API makes structure explicit, which is what the SDK captures, and the functional API would need a second capture path for a v1 feature nobody has asked for | — (resolves open question Q-43.2.1) | 1. `sdk/langgraph.py` targets 1.2 only; a 1.3 bump is an ADR plus a re-run of the determinism suite. 2. A user on `@entrypoint` gets the generic decorator path and a clear error, not silent partial capture. 3. §10 Q-43.2.1 moves to Accepted |
| ADR-004 | 2026-08-10 | **The concrete distributions entailed by the §3 stack are enumerated in `pyproject.toml`: `uvicorn`, `pydantic`, `pyyaml`, `httpx`, alongside the literally-named `langgraph`, `fastapi`, `websockets`, `typer`, `duckdb`** | §3 names capabilities ("FastAPI + websockets", "scenario YAML", "OpenAI-compatible shim") rather than distributions, so AGENTS.md §2's "ask first" rule had no answer for the packages those capabilities require. FastAPI does not serve itself; §12 YAML needs a parser; §8.5 explicitly rejects a vendor SDK in favour of the OpenAI-compatible surface, which needs an HTTP client. Enumerating them once is cheaper than four half-decisions in P08/P14. Owner-approved at P01 before `pyproject.toml` was written | — (interprets §3, does not override it) | 1. The permitted set is now the enumerated list in `pyproject.toml`; anything else still needs an ADR. 2. `opentelemetry-*` sits in an optional extra, uninstalled by default, so scope-cut #5 stays a one-line cut. 3. No transitive dependency is sanctioned merely by having been pulled in |
| ADR-005 | 2026-08-10 | **MIT licence adopted** | The PRD names no licence and P01 asked for a permissive one. MIT is the lowest-friction choice for a tool whose primary evaluator (PRD §3.6) assesses it by cloning it | — | Contributions are inbound-MIT; changing this after publication requires contributor agreement, so it is effectively one-way |
| ADR-006 | 2026-08-10 | **The determinism-hygiene gate requires BOTH an allowlisted path AND a per-line `# determinism-exempt: <reason>` comment** | The P01 prompt exempted only `runtime/determinism.py`; AGENTS.md §4.1 sanctions four exceptions, and AGENTS.md wins on process (§0.5). Requiring both conditions is stricter than either source alone: a comment cannot exempt a file, a path cannot exempt a line, and an annotation on a non-allowlisted path is reported as a violation in its own right — so the escape hatch is always visible in a diff | — (implements AGENTS.md §4.1) | 1. Widening the allowlist is a reviewable edit to `scripts/check_determinism_hygiene.py` with a §4.1 clause quoted beside it. 2. `events/writer.py` and the `api/`, `cli/`, `store/` prefixes are pre-listed for clauses 3 and 4; those files do not exist yet, so the entries are inert until P02–P14 |
| ADR-007 | 2026-08-10 | **Floats are forbidden everywhere in the event log**, including inside the open sub-objects `span_start.attributes`, `fault_injected.params` and `run_start.payload.env`. Emitting one is `E-EVENT-013`. The restriction is encoded in the type system: `schema.PayloadValue` has no `float` member | The canonical projection cannot normalise cross-platform float formatting away, and PRD §10.6 already concedes floating-point results are not guaranteed across architectures — so a float in a canonical field is an I1 leak that no amount of care in `canonical.py` can close. Every quantity PRD §9.5 actually specifies is an integer, a hash string or an enum, so the rule costs nothing in the specified schema. It is an SDK-visible constraint, which is why it was raised before implementation rather than assumed. Owner-delegated at P02 | — (PRD is silent; this decides it) | 1. Durations are integer milliseconds and ratios integer per-mille, project-wide. 2. **PRD §12.2 gives the P1 fault `agent_slow` a `factor` of type `float ≥ 1.0`. When FR-4's P1 set lands it must carry `factor_milli: int`, or this ADR needs superseding.** 3. A user attaching a float to `@agentdx.span(attributes=…)` gets a hard error, not a silent coercion — P04 must say so in the SDK docs |

---

## 9. Deviations from the PRD — APPEND ONLY

Anything the code does that the PRD does not say, or says differently. **An undeclared deviation is the single most likely cause of a bad AI-to-AI handoff.** If you improvised, it goes here in the same commit. Enforced by the CI check in `AGENTS.md` §10.

| ID | Date | PRD § | What the PRD says | What the code does | Why | Reconciled? |
|---|---|---|---|---|---|---|
| *(none yet — no code exists)* | | | | | | |
| D-01 | 2026-08-10 | §25 | The tree contains no `scripts/` directory | `scripts/` holds `check_ledger.py`, `check_bench_markers.py`, `check_determinism_hygiene.py` | The P01 deliverables name these paths; they are repository-integrity gates, not tests, and do not belong under `tests/` | Not reconciled — propose adding `scripts/` to §25 at the next PRD revision |
| D-02 | 2026-08-10 | §25 | `bench/` is a single directory | `bench/results/` and `bench/harness/` exist as subdirectories | `bench/results/` is referenced by name throughout the PRD (Rule E1, I9) and is where `check_bench_markers.py` resolves every marker | Not reconciled — the PRD already assumes `bench/results/` in prose |
| D-03 | 2026-08-10 | §25 | `tests/` lists unit, integration, determinism, analysis, api, frontend, golden, benchmarks | `tests/false_positives/` also exists | Required by ADR-002, which makes the k=2 harness P0 and independent of FR-6. §25 predates that decision | Reconciled by ADR-002 |
| D-04 | 2026-08-10 | ADR-000 | The PRD is read-only | `docs/AgentDX-PRD-v2.md` is excluded from **two** auto-fixing toolchains: `ruff` (`extend-exclude`, it formats fenced Python blocks in Markdown) and the `trailing-whitespace` / `end-of-file-fixer` pre-commit hooks | Both would silently rewrite the spec of record. The pre-commit hole was found the hard way at P01: the hook rewrote the PRD on its first run and the change was reverted | Reconciled — the exclusions enforce ADR-000 rather than deviating from it |
| D-05 | 2026-08-10 | §33 | pytest exits 0 | `just test` treats pytest exit code 5 ("no tests collected") as success | There are no tests until P02. The shim tolerates only code 5; a real failure still fails the build, and it carries a `TODO(P02)` | To be removed at P02 — verify it is gone |
| D-06 | 2026-08-10 | AGENTS.md §10 | The ledger diff base is `origin/main` | `scripts/check_ledger.py` uses `DIFF_BASE = "HEAD"` | No remote existed at P01. Carries a `TODO(remote)`: until it is switched, a PR that deletes a row *and commits it* compares against that deletion and passes | Not reconciled — switch on first push to a remote |
| D-07 | 2026-08-10 | AGENTS.md §10 | The ledger diff base is `origin/main` | It now is: the remote went live at `988aa74` and `DIFF_BASE` was switched. The script additionally **fails** when the base ref cannot be resolved, rather than skipping the comparison — an unresolvable base is indistinguishable from an unenforced check | **Closes D-06.** Recorded as a new row rather than by editing D-06's `Reconciled?` column, because §9 is append-only and this script enforces that literally (see the note on AGENTS.md §10's correction procedure, below §9) | Reconciled — supersedes D-06 |
| D-08 | 2026-08-10 | AGENTS.md §4 (Python 3.12) | Python 3.12 is the target, and its idioms are available | `events/` uses `typing.TypeAlias` instead of PEP 695 `type`, and a local `ValueEnum(str, Enum)` base with an explicit `__str__` instead of `enum.StrEnum`. Six `# noqa: UP040/UP042` comments carry this ID. Runtime behaviour on 3.12 is identical — `ValueEnum` reproduces `StrEnum` semantics exactly | The P02 build host had only CPython 3.10 (3.12 unobtainable: GitHub release assets, python.org, deadsnakes and anaconda are all blocked by its network allowlist). mypy and pytest cannot *parse* PEP 695 under 3.10, so the alternative was shipping the project's most consequential module with `mypy --strict` and the entire 240-test suite unrun. Verifiability beat syntax fashion | Not reconciled — revert once a 3.12 interpreter is on the build host. The revert is mechanical and behaviour-free: `TypeAlias` → `type`, `ValueEnum` → `enum.StrEnum`, delete the six `noqa`s, re-run `just ci` |
| D-10 | 2026-08-10 | AGENTS.md §5 | Golden files are regenerated only on an explicit written instruction | `tests/golden/event_log_40.jsonl` was regenerated once during P02 without one, when `run_end`'s hand-typed makespans were found inconsistent with the log they summarised | The fixture existed only inside the in-progress commit and had never been committed, so no downstream expectation could move. Declared rather than left silent because §11 tripwire 1 covers exactly this shape. The two later regenerations (Q-P02.1 amendment 1, OP-3) were both instructed | Reconciled — no committed golden was overwritten |
| D-11 | 2026-08-10 | P02 DELIVERABLES | Deliverables named `tests/unit/events/` and "a hand-written 40-event log fixture" in `tests/golden/` | Also created `tests/__init__.py`, `tests/unit/__init__.py`, `tests/golden/__init__.py` (to make `tests.unit.events` importable) and `tests/golden/build_event_log_40.py`, a 317-line generator | The `__init__.py` files are packaging, not features. The generator exists because hand-computing 40 vector clocks and the §9.4 taint chain produces a fixture that is wrong in ways nobody notices; the *events* are hand-specified, the clocks are computed. Surfaced by the P02 self-audit §3 | Not reconciled — if the owner prefers a literal hand-written fixture, the generator can be deleted and its output kept |
| D-09 | 2026-08-10 | §33 | pytest exits 0 | `just test` is now plain `uv run pytest`; the exit-5 shim is gone | D-05 scheduled its removal for P02, which is when tests first exist. 240 now collect | **Closes D-05** — reconciled |

---

## 10. Open questions and known PRD conflicts

**Open questions.** `Decide by` is set by this ledger, not by the PRD (PRD §43.2 gives defaults without deadlines). Working to the recommended default is always permitted; changing it needs an ADR.

| ID | Question | Recommended default (PRD §43) | Decide by | Status |
|---|---|---|---|---|
| Q-43.1.1 | Final name before the repo goes public | Keep AgentDX | Before publication | Accepted — locked in §3; re-confirm at P19 |
| Q-43.2.1 | LangGraph version range to pin; support `@entrypoint`? | Pin a tested minor range; graph API only in v1 | Week 1 | Accepted — `>=1.2,<1.3`, graph API only (ADR-003) |
| Q-43.2.2 | SQLite → DuckDB threshold | 20 000 events, tune from benchmarks | Week 5 | OPEN |
| Q-43.2.3 | Calibration defaults with no profile | LLM 800 ms, tool 200 ms, agent_step 50 ms | Week 2 | OPEN |
| Q-43.2.4 | Sleep sets / full DPOR in exploration | Independence-based reduction; upgrade only if redundancy > 40 % | Week 10 | OPEN |
| Q-43.2.5 | Waterfall SVG → Canvas switchover | 20 000 spans | Week 8 | OPEN |
| Q-43.2.6 | Verdict weights in `verdict_rules.toml` | §18.2 defaults, tuned against fixtures, version any change | Week 6 | OPEN |
| Q-43.2.7 | Is `state_read` capture sampled in lightweight mode? | Full capture by default | Week 3 | OPEN |
| Q-43.2.8 | Bundle format | zip | Week 9 | OPEN |
| **Q-P02.1** | **PRD §9.5 specifies payloads for 9 of the 19 event types. The other 10 were derived at P02 — do they stand?** | Adopt the P02 derivation; every field cites its source and is marked `derived=True` in `schema.py` | Week 1 — before the freeze | **Accepted 2026-08-10 with three amendments** (see below). ⚠️ **Accepted on owner delegation, not independent review** — the same agent derived and accepted these schemas. Re-examining them is the highest-value thing an OP-2 audit of `events/` can do |

**Q-P02.1 amendments, applied 2026-08-10.** ① `lock_acquire.contended` **removed** — under the cooperative single-threaded scheduler (PRD §10.2) it is exactly `wait_virtual_ms > 0`, so it was a second source of truth for one fact, and a schema that can express `contended=false, wait_virtual_ms=50` is worse than one that cannot. `wait_virtual_ms` stays: the log records only the grant, never the attempt, so wait time is not otherwise derivable and PRD §16.2's coordination bucket needs it. ② `barrier.participants` and `schedule_decision.ready_task_ids` marked **`set_valued`**, with new code **`E-EVENT-028`** rejecting unsorted emission at write time — canonicalisation still refuses to reorder (that would hide a nondeterministic emitter), but the emitter now fails loudly and correctly attributed instead of surfacing as an intermittent G3 failure in week 2. ③ `nondeterminism_warning.source` **opened from a closed enum to a free string** — that event type exists to record surprises, and a closed enum would turn an unanticipated leak into `E-EVENT-005` and abort the run at the exact moment the system was trying to report something useful.

**The freeze is no longer blocked by Q-P02.1.** It remains blocked by the P05 fixtures (ADR-001: fixtures are the only realistic consumers that can prove the schema *sufficient* before the freeze) and by a green `just ci` on Python 3.12. No other open question blocks week-1 work.

**Known PRD-internal conflicts and their rulings.** Recorded so they are not re-litigated every session. A ruling here is binding until an ADR changes it.

| ID | Conflict | Ruling |
|---|---|---|
| C-1 | §40.1 (wk 5) says overhead buckets "sum to **wall clock** ±2%" and §44.1 G5 says "makespan" unqualified; §16.2.3 computes against `virtual_makespan` | **§16.2.3 is normative.** The quantity is *virtual* makespan, per I11. §40.1's "wall clock" is loose roadmap phrasing. Also note the shape: it is Σ(six buckets) + `residual` = makespan with residual < 2 %, not "buckets + critical path" — the critical path *is* what the buckets decompose |
| C-2 | §4.3 and §7 define **FR-13 = one-page HTML/PDF report export (P2)**; §44.2 lists **FR-13 = OTel export, Wk 10** | Treat them as two items: **FR-13 = report export (P2, not in v1)**; **OTel export = an unnumbered P1 item, week 10, scope-cut #5** (per §4.2 and §40.3). §44.2's label is a typo. Do not build a report exporter in v1. **Confirmed by the human owner, 2026-08-10 — closed, do not re-litigate** |
| C-3 | G2 (never-waived, §44.3) requires a clean k=2 exploration frontier, but exploration is FR-6 — P1 and scope-cut #1 | **Resolved by ADR-002:** the k=2 harness is split out of FR-6 and made P0 at P12. Both halves of G2 are now unwaivable and no scope cut reaches them. Closed |
| C-4 | §9.7 requires a `prev_hash`/`this_hash` chain and §38's DDL has both as `events` **columns**, but §9.2's canonical event schema lists neither field | **§38 is normative: the chain lives beside the event, never inside it.** Forced rather than chosen — an event whose canonical form contained its own hash is self-referential. The chain covers the canonical projection only, or every run gets a unique chain and bundle tamper-detection becomes useless. `docs/event-schema.md` §6, §11 C-4 |
| C-5 | §9.2 marks `run_id` volatile=`no` and §10.7 says "every other field participates in equality", which puts it in the projection; §6.1 defines it as `r_` + 5 hex of *a content hash* of unstated inputs | **`run_id` is `identity` — excluded from the projection.** Both literal readings break something: any per-execution component in the hash means no two replays agree and **G3 can never reach 100/100**; a pure content hash means §33.3's 100 replays collide on `runs.run_id PRIMARY KEY` (§38). A third volatility mark exists precisely because `run_id` is neither volatile nor stable. Covers `llm_call.payload.perturbed_from_run`, which is a run id. `docs/event-schema.md` §11 R-1 |
| C-6 | §10.7's code pops `payload.cache_key`; §10.7's prose exclusion list — which calls itself exhaustive — does not list it; §11.4 states the key never contains a machine-local salt | **`cache_key` is stable and stays in the projection.** Three of four sources agree; the popping line carries a comment that asks and answers its own question. Excluding it would hide a genuine prompt divergence — the silent-pass failure, which is worse than an unpassable gate. `docs/event-schema.md` §11 R-2 |
| C-7 | §9.3 gives `run_end` a wall makespan; §10.7's exclusion list calls itself exhaustive and omits it | **`run_end.payload.wall_makespan_ms` is volatile and excluded.** Left as specified it is the second field that would have made G3 unpassable. Found at P02 by deriving `run_end`'s payload. **Mitigation adopted: the exclusion list is now generated from the schema marks (`schema.excluded_field_paths()`) and asserted against the docs by a test, so this class of gap cannot recur.** `docs/event-schema.md` §11 R-3 |
| C-8 | §9.8 says the vclock must be "≥ previous vclock for the slot"; §14.2's rules increment on every local event, implying strict `>` | **§9.8 governs the validator (`E-EVENT-027` fires on `<` only).** It is the validation section, and the looser rule cannot produce a false rejection whereas the stricter one could. `docs/event-schema.md` §11 R-5 |

---

## 11. Drift tripwires — stop and run OP-3 if you observe any of these

1. A test is changed to make it pass, rather than the code being changed. **Especially** a determinism or false-positive test.
2. `sleep`, `time.time()`, `random`, `uuid4()` or `datetime.now()` without the seeded/virtual source appears under `src/agentdx/` outside the sanctioned points in `AGENTS.md` §4.
3. An `analysis/` module imports from `runtime/`, `sdk/`, or any model client.
4. A verdict, finding or scorecard is produced without event `seq` references in its evidence array.
5. A threshold, weight or magic number appears inline in code instead of `verdict_rules.toml` / `agentdx.toml`.
6. The event schema is modified after week 1 without an ADR and a `schema_version` bump.
7. A number appears in the README, docs or UI with no `[bench:<file>]` marker resolving to `bench/results/`.
8. A panel starts owning selection state instead of the Zustand store.
9. Someone proposes an LLM-as-judge, semantic dedup, hosted/auth features, production monitoring, a fifth+ MVP fault type, or a v1 report exporter (§10 C-2) — all are explicitly out of scope.
9b. The `tests/false_positives/` k=2 harness grows a CLI flag, an output format or a reduction report. It is a test fixture, not an early FR-6 (ADR-002).
10. A scope cut is taken out of order (§12) or below the hard floor.
11. A prompt's output covers modules its `DELIVERABLES` list did not name.
12. A `replay`-mode cache miss is handled by anything other than a hard error.
13. A row is edited or removed from §8 or §9.

---

## 12. Scope-cut order — if the schedule slips, cut in exactly this order

1. FR-6 bounded exploration — the shipped feature only. **The `tests/false_positives/` k=2 harness is P0 and is never cut with it (ADR-002); G2 remains fully met after this cut**
2. FR-11b CI mode
3. FR-9 resilience scoring *(row 11b, not the whole of P11)*
4. Graph panel — the waterfall carries the demo alone *(this also forces a G8 amendment, §5)*
5. OTel export
6. Support-triage fixture — **the healthy control (`research_fanout`) is never cut**

**Hard floor:** FR-1, FR-2, FR-3, FR-5, FR-7, FR-8, FR-12 (`code_pipeline` + `research_fanout`), waterfall + scorecard.
Below this floor the thesis is not demonstrable. **Ship late rather than incomplete.**

*Note: FR-4 (fault injection) is P0 but does not appear in the PRD's hard floor. That is the PRD's text (§40.3), preserved. It means the floor is "the speedup thesis", not "the chaos thesis" — do not read its absence as permission to cut FR-4 casually; cutting it needs an ADR.*

---

## 13. Session log — most recent 15 entries, newest first

Older entries roll into `docs/journal/YYYY-WW.md` when this section exceeds 15 rows.

| Date | Prompt | Model | Outcome | Files touched | Audit |
|---|---|---|---|---|---|
| 2026-08-10 | OP-3 | Claude Opus 5 | **Repair after a P02 self-audit — which is not a valid OP-2** (same agent built and reviewed; §0 requires independence, so `events/` stays `BUILT`, not `VERIFIED`). Six findings, three fixed. **F1: `E-EVENT-042` checked that fault taint EXISTED, never that it was the right fault** — a child could inherit `f_99` from a parent tainted `f_01` and validate cleanly, silently corrupting the §2.6 cascade tree. Added `E-EVENT-044` (taint must be the earliest contributing fault, PRD §9.4) and `E-EVENT-045` (taint must name a fault injected in this log); `fault_injected`/`fault_effect` exempt from 044 per §9.4 rule 1. Both regression tests confirmed failing before the fix. **F2: the stamping-boundary test asserted the absence of `_seq`/`_clock`/`_vclock` while the writer legitimately holds `_last_seq`** — a refactor that stamped via `_last_seq` would have passed it. Replaced with identity pass-through plus an AST check that `writer.py` never calls `Event(...)`, `replace(...)` or `from_draft(...)`. **F6: nothing outside `docs/` said the canonical projection is derived from the marks** — a later prompt could have transcribed PRD §10.7's own (incomplete) list; §4 now states it. Deferred: canonical-bytes golden file, `verify_chain` length branch, `normalise_vclock` sort-before-normalise, `DEFAULT_BATCH_SIZE` to config. 248 tests | `events/validators.py`, `docs/event-schema.md`, `CONTEXT.md`, 2 test files | this row *is* the (self-)audit — a real OP-2 is still owed |
| 2026-08-10 | P02 | Claude Opus 5 | `events/` built — the contract. 19-type closed enum; **volatility as first-class schema data** with the canonical projection *derived* from the marks rather than kept beside them; 3 separately-testable validation layers with 24 stable error codes; fully specified canonical bytes (NFC, code-point key order, minimal escaping, integers only) hand-written rather than delegated to `json.dumps`; hash chain over the projection only; writer whose type signature makes stamping impossible (`DraftEvent` + `Stamp` → `Event`); migration harness; 40-event golden fixture; 610-line cross-language contract in `docs/`. **Four STOP CONDITIONS fired before any code was written** and were ruled rather than guessed → C-4…C-8, ADR-007, Q-P02.1. **Found: `run_end.payload.wall_makespan_ms` is volatile and missing from §10.7's "exhaustive" list — the second field that would have made G3 unpassable** (C-7); `span_end.error_message` was wrongly non-nullable, caught when the golden fixture refused to build; PRD §12.2's P1 `agent_slow.factor` is a float and will collide with ADR-007 at P09. 240 tests pass, 125 of them the volatility property parametrised from the marks. All 7 gates green **except `typecheck`, unverified** — and everything ran on **CPython 3.10, not 3.12** (D-08); owner must re-run `just ci` on macOS/3.12. D-05 removed (D-09) | 16 files: `events/*`, `tests/unit/events/*`, `tests/golden/*`, `docs/event-schema.md`, `justfile`, `CONTEXT.md` | pending — OP-2 not yet run |
| 2026-08-10 | P01 | Claude Opus 5 | Repo scaffold, toolchain and CI spine. Seven gates wired and passing on the empty tree; each verified negatively (I3 broken deliberately on `analysis.baseline`, reverted). ADR-003…006 logged, D-01…06 declared. Found and fixed: the `trailing-whitespace` hook rewrote the read-only PRD on first run (D-04); the ledger check passed silently when its base ref was unresolvable; **the determinism gate was bypassable by import form** (`from time import time` → `time()` matched none of its text patterns) and was rewritten to resolve import aliases through the AST rather than grep. **Author's verification ran on Linux/Python 3.10 only** (the build host could not fetch CPython 3.12); the owner re-ran them on macOS arm64/3.12 — `just ci`, `pre-commit --all-files`, frontend `tsc`+`eslint`, `agentdx --help`. Remote live at `988aa74`; ledger check now enforced against `origin/main` (D-07). **CI green on GitHub** — all four jobs (`python` × ubuntu-latest and macos-14, `ledger`, `frontend`) | 78 files, whole tree | pending — OP-2 not yet run |
| 2026-08-10 | P00d | Gemini (cold read) | **Ledger sufficiency test, 8 questions, CONTEXT.md alone: 7/8.** Q6 failed — I3's "sole exception" wording was read as permission for `analysis.baseline` to import `runtime`, the opposite of PRD §24.3. I3 rewritten; no allowlist entry permitted in the import-linter config. Also fixed: bare `§25` read as a CONTEXT ref, now `PRD §25` | `CONTEXT.md`, `AGENTS.md` | this row *is* the audit |
| 2026-08-10 | P00c | Claude Opus 5 | Owner decisions applied: C-2 confirmed and closed; C-3 resolved by ADR-002 (k=2 harness split out of FR-6, made P0 at P12); §5 row 12b added | `CONTEXT.md` | pending |
| 2026-08-10 | P00b | Claude Opus 5 | Governance validation pass: 9 mismatches corrected, I13 added, ADR-001 logged, C-1…C-3 recorded, `AGENTS.md` §4/§2/§10 made enforceable | `CONTEXT.md`, `AGENTS.md` | pending |
| 2026-08-08 | P00 | — | Ledger seeded from PRD v2.0 | `CONTEXT.md`, `AGENTS.md` | n/a |

---

## 14. Handoff brief — regenerate before moving to a different AI or a new session

> **Paste this section, plus §1–§5 and §8–§12, into the new assistant.**

*Regenerated 2026-08-10, after P02 + OP-3. Stale the moment §5 changes — rewrite it, don't trust it.*

- **What this is:** a pre-deployment coordination debugger for multi-agent AI systems — deterministic scheduler, LLM record/replay, fault injection, append-only event log, pure analysis over that log. Week 1 of 10, solo build.

- **What is built and verified:** **nothing.** `VERIFIED` requires an independent OP-2 (§0) and none has run. Do not treat any module as audited.

- **What is built but unverified:**
  - **P01** repo scaffold, toolchain, seven CI gates. Green on GitHub.
  - **P02** `events/` — the event contract, and the module everything else depends on. 248 tests. Its `docs/event-schema.md` (610 lines) is the human-readable contract; **read it before touching anything in `events/`, and before writing anything that consumes an event log.** A self-audit found six defects, three fixed under OP-3; the other three are listed below.

- **What is next:** **P03 — `store/`**: SQLite (WAL), DuckDB views, snapshots, bundles. Five facts it must not get wrong: ① it implements the `EventSink` protocol declared in `events/writer.py` (`append(Sequence[ChainedEvent])`, `seal(run_id, final_hash)`); ② `prev_hash`/`this_hash` are **columns**, computed by the writer, never recomputed and never event fields (C-4); ③ the store never validates and never canonicalises — both belong to `events/`; ④ bundle JSONL uses `canonical.encode_event`/`decode_event`, never `json.dumps` — a second serialiser silently breaks byte-stability; ⑤ `store/` may import `events` and nothing else (§4).

- **What is fragile right now:**
  - **The schema freezes at the end of week 1 and is not frozen yet.** Outstanding: P05 fixtures must prove it *sufficient*, and `just ci` must go green on Python 3.12. Changing it after the freeze invalidates every recorded run.
  - **D-08:** `events/` is written in a 3.10-parseable dialect (`TypeAlias`, `ValueEnum`) because the build host had no 3.12. Behaviour is identical. **Do not "modernise" it to PEP 695 / `StrEnum` without checking the host first** — that silently disables mypy and pytest.
  - **`typecheck` has never been verified end to end**, and no gate has ever run on 3.12.
  - **Known, unfixed** (from the P02 self-audit, deferred by decision): no committed artifact pins the canonical *bytes* of an event, so `docs/event-schema.md` §12's porter checklist is not fully verifiable; `verify_chain`'s length-mismatch branch returns an arbitrary seq rather than the first failing one; `normalise_vclock` sorts before NFC-normalising, so its returned key order can differ from canonical order (final bytes are unaffected); `DEFAULT_BATCH_SIZE` is inline in `writer.py` rather than in `agentdx.toml`.

- **What must not be touched:** the §2 invariants and the §3 locked decisions. Two specifics that are easy to break by accident: **the canonical projection is derived from the volatility marks in `events/schema.py` — never write a second exclusion list** (§4, C-7); and **set-valued arrays must be emitted sorted** by their emitter (`E-EVENT-028`) — the canonicaliser deliberately does not reorder, because that would hide a nondeterministic emitter.

- **Known open decisions the new assistant must not guess at:** §10's `Q-43.2.*` rows (SQLite→DuckDB threshold, calibration defaults, DPOR, canvas switchover, verdict weights, `state_read` sampling, bundle format). **Q-P02.1 is Accepted but only by owner delegation, not independent review** — the ten payload schemas PRD §9.5 never specified were derived by the same agent that accepted them. Treat them as the softest part of the contract and the first target of a real OP-2.

- **Process rule most often broken here:** if the PRD contradicts itself, or is silent on something load-bearing, **stop and ask** (`AGENTS.md` §3). P02 hit four such cases before writing a line of code; two of them would have made gate G3 permanently unpassable had they been guessed instead.
