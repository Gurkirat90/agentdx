# AgentDX — Product Requirements Document & Technical Product Specification

**Multi-agent coordination debugger, deterministic replay runtime, and chaos-testing harness**

| Field | Value |
|---|---|
| Document version | **2.0 — implementation-ready** |
| Supersedes | AgentDX PRD v1.0 (8 August 2026, "Draft for build") |
| Date | 8 August 2026 |
| Status | **Single source of truth. Build from this document.** |
| Product surface | Local-first web application + Python SDK + CLI |
| Target build window | 10 weeks (solo build; ownership structured for a team) |
| Primary framework target | LangGraph, plus a generic Python decorator API |
| Licence posture | Open source, public repository |
| Sources synthesised | Perplexity Research, GPT 5.2 Comparison, Opus Comparison, Claude Opus 4.5 Thinking, Gemini concept screenshot, PRD v1.0 |

---

## How to read this document

This document is written so that a senior Python engineer, a distributed-systems engineer, an AI engineer, a backend engineer, a frontend engineer, a QA engineer and a DevOps engineer can each begin work **without an architectural clarification meeting**. It assumes no prior exposure to AgentDX.

Every non-obvious statement carries a provenance tag so the reader always knows whether they are looking at a decision that was already made, a decision this document is making on the team's behalf, or a decision still owed:

| Tag | Meaning | How to treat it |
|---|---|---|
| `[SOURCE]` | Existing requirement, terminology or decision carried forward from PRD v1.0 | Authoritative. Do not renegotiate without a written change. |
| `[DETAIL]` | Engineering detail added here because the source was under-specified and the feature is not implementable without it | Authoritative. Implement as written. |
| `[IMPROVEMENT]` | A recommended change to a source decision. The trade-off is stated and a recommendation is given. Nothing has been silently changed. | Implement the recommendation unless the owner overrides it in §43. |
| `[OPEN]` | A genuine product or engineering decision that has **not** been made | Do not guess. Resolve via §43 before the dependent milestone. |

**Section 43 is the authoritative index of every `[OPEN]` item.** Sections 43 and 44 together define "done".

**Terminology note.** *Virtual time* always means the AgentDX simulated clock. *Wall time* always means real elapsed time. Any duration in this document without a qualifier is virtual time. This distinction is load-bearing throughout: it is the reason a 30-second chaos matrix executes in under a second.

---

## Table of contents

1. Executive Summary
2. Problem Definition
3. Target Users
4. Product Scope
5. Core User Journeys
6. Core Concepts
7. Functional Requirements
8. Instrumentation SDK Specification
9. Event Model
10. Deterministic Runtime
11. LLM Record / Replay Architecture
12. Fault Injection Engine
13. Chaos Safety Architecture
14. Race Detection Architecture
15. Bounded Schedule Exploration
16. Performance Analysis Engine
17. Single-Agent Baseline
18. Verdict Engine
19. Resilience Scoring
20. Replay & Time Travel
21. Scenario System
22. CI/CD Integration
23. Reference Fixture Systems
24. System Architecture
25. Repository / Codebase Architecture
26. API Specification
27. Data Storage Architecture
28. Frontend / Control Tower
29. UI / UX
30. OpenTelemetry Interoperability
31. Security & Privacy
32. Non-Functional Requirements
33. Testing Strategy
34. Benchmarking & Evaluation
35. Observability of AgentDX Itself
36. Error Handling
37. CLI Specification
38. Developer Experience
39. Deployment
40. Project Roadmap
41. Team Responsibilities
42. Risks & Mitigations
43. Open Questions & Engineering Decisions
44. Acceptance Criteria
45. End-to-End Technical Walkthrough
46. Final Architecture Summary
- Appendix A — Source synthesis
- Appendix B — Glossary
- Appendix C — Corrections to PRD v1.0

---

# 1. Executive Summary

## 1.1 Product definition

**AgentDX is a pre-deployment reliability and coordination-debugging system for multi-agent AI applications.** `[SOURCE]`

A developer points AgentDX at an existing agent graph — a LangGraph application, or any Python multi-agent system wrapped with the decorator API. AgentDX executes that graph under its own **deterministic cooperative scheduler** driven by a **virtual clock**, serves every LLM call from a **record/replay cache**, optionally injects **controlled faults**, records every observable action into an **append-only event log**, and then runs a battery of **pure analytical passes over that log** to answer one question.

## 1.2 Product thesis

> **Does the multi-agent topology actually provide a benefit over a single-agent system doing the same task — and if not, exactly where is the coordination overhead or the reliability failure occurring?** `[SOURCE]`

Everything in the product exists to answer that question and to defend the answer with evidence. The dependency graph, the waterfall, the chaos panel, the replay scrubber, the findings list — these are not features in their own right, they are the *exhibits* behind the verdict.

**One-line positioning:** *Chaos Monkey and a profiler for agent graphs. Not another trace viewer.* `[SOURCE]`

The thesis obliges the product to make eleven things **measurable**, not merely visible. This table is the spine of the entire document; every subsequent section traces back to a row in it.

| # | Must be measurable | Produced by | Specified in |
|---|---|---|---|
| M1 | Coordination overhead | Overhead decomposer | §16.2 |
| M2 | Critical-path bottlenecks | Critical-path solver | §16.1 |
| M3 | Handoff latency | Overhead decomposer (bucket `handoff`) | §16.2 |
| M4 | Blocking waits | Overhead decomposer (bucket `blocking_wait`) | §16.2 |
| M5 | Redundant work | Redundancy detector (argument-hash matching) | §16.3 |
| M6 | Retry amplification | Resilience scorer + overhead bucket `retry_recovery` | §19.4 |
| M7 | State conflicts | Vector-clock race detector | §14 |
| M8 | Race conditions | Happens-before concurrency analysis | §14.4 |
| M9 | Fault resilience | Resilience scorer | §19 |
| M10 | Deterministic reproducibility | Deterministic runtime + replay verifier | §10, §20 |
| M11 | Speedup versus single-agent baseline | Baseline generator + verdict engine | §17, §18 |

## 1.3 The problem

Teams decompose a task across a planner, a coder, a reviewer and a tester expecting near-linear speedup. What they get instead is a system that is *slower*, *more expensive*, and *less reliable* than one agent with the same tools — and they cannot prove it, because:

- **Happy-path demos never hit the failure modes.** `[SOURCE]` A fault that is rare per call is near-certain across a twenty-step run.
- **Non-determinism makes failures unreproducible.** You cannot debug what you cannot re-run.
- **Existing tooling records what the agent did. It does not tell you whether the topology was worth it.** `[SOURCE]`
- **Nobody measures the single-agent baseline**, so "0.83× speedup" is never discovered. `[SOURCE]`

## 1.4 The solution

Five mechanisms, in dependency order. Each is necessary for the ones below it.

```
┌───────────────────────────────────────────────────────────────────────────┐
│ 1. DETERMINISM        Cooperative scheduler + virtual clock + seeded RNG   │
│                       + LLM record/replay cache                            │
│                       → the same (scenario, seed) always yields the same   │
│                         interleaving and the same event log                │
├───────────────────────────────────────────────────────────────────────────┤
│ 2. OBSERVATION        Append-only event log carrying vector clocks,        │
│                       virtual timestamps and causal parents                │
│                       → the single source of truth for every analyser      │
├───────────────────────────────────────────────────────────────────────────┤
│ 3. PERTURBATION       Fault injection at the scheduler layer, bounded by   │
│                       blast radius, steady-state hypothesis, abort guards  │
│                       → reliability becomes an experiment, not an anecdote │
├───────────────────────────────────────────────────────────────────────────┤
│ 4. ANALYSIS           Happens-before race detection · critical path ·      │
│                       overhead decomposition · redundancy · baseline ·     │
│                       resilience scoring · bounded schedule exploration    │
│                       → deterministic algorithms, never an LLM opinion     │
├───────────────────────────────────────────────────────────────────────────┤
│ 5. EXPLANATION        Verdict + evidence-linked findings + Control Tower   │
│                       + replay/time travel + CI gate                       │
│                       → a claim a developer can act on and defend          │
└───────────────────────────────────────────────────────────────────────────┘
```

Determinism is the foundation and the differentiator. Every claim AgentDX makes is reproducible on another machine from an exported `.agentdx` bundle. That property is what separates a reliability system from a dashboard.

## 1.5 Target users

Six personas, fully specified in §3. In priority order: the **Agent Engineer** (primary), the **AI Platform Engineer**, the **SRE / Reliability Engineer**, the **Developer Infrastructure Engineer**, the **Engineering Manager / Architect**, and the **Technical Reviewer** — a recruiter or interviewer opening the repository, who must grasp the thesis in thirty seconds and be impressed by the depth in five minutes. `[SOURCE]` The third of these is a real user of the artifact and the README/demo is designed for them explicitly.

## 1.6 Key differentiator

**AgentDX treats a multi-agent system as a distributed system.** It is the only tool that combines, in one harness:

1. Deterministic re-execution of an agent graph (scheduler + virtual clock + LLM cache).
2. Lamport happens-before race detection across shared agent state.
3. Critical-path profiling with coordination-overhead decomposition.
4. An automatically generated single-agent baseline and a defensible speedup number.
5. Fault injection with chaos-engineering safety rails.

Individually, items 1, 3 and 5 exist in other domains (deterministic testing frameworks, systems profilers, Chaos Toolkit). Item 2 is textbook computer science (Lamport, Eraser, FastTrack) never applied to LLM agent state. Item 4 does not exist anywhere. **The combination is the product.**

## 1.7 Why existing observability tools are insufficient — and why AgentDX complements them

PRD v1.0 correctly rejects the source research's claim of "zero competition" as false and reputationally dangerous. `[SOURCE]` That correction is preserved and hardened here.

| Layer | Representative players | Maturity | AgentDX relationship |
|---|---|---|---|
| Agent tracing / observability | LangSmith, Langfuse, AgentOps, Arize Phoenix, Braintrust, Helicone, Portkey, Datadog, Galileo, Confident AI, Latitude, Maxim | Mature, crowded, OTel-native is the norm | **Complementary.** AgentDX emits OTel GenAI spans (§30) so its runs appear in the user's existing platform. |
| Eval / LLM-as-judge | DeepEval, Braintrust, Confident AI, Pydantic Evals | Mature | **Complementary.** AgentDX delegates semantic correctness to a pluggable assertion hook (§21.6). It does not judge output quality. |
| Agent chaos / fault injection | BalaganAgent, `agent-chaos`, Chaos Toolkit adapters, ReliabilityBench (research) | Early, fragmented, **mostly single-agent** | **Overlapping but differentiated.** These inject faults into one agent and score that agent's recovery. AgentDX injects into the *coordination layer* between agents. |
| Coordination-level analysis — critical path across agents, speedup vs a generated baseline, cross-agent race detection, deterministic replay of an interleaving | — | **Genuinely open** | **This is AgentDX.** |

The distinction that must never blur, in the product, the README, or an interview:

| Discipline | Question it answers | Who owns it |
|---|---|---|
| **Observability** | What did the system do? | LangSmith / Langfuse / Phoenix |
| **Semantic evaluation** | Was the output correct? | DeepEval / Braintrust / a user-supplied assertion |
| **Coordination analysis** | Was the topology worth its overhead, and where does it leak? | **AgentDX** |
| **Chaos testing** | Does it degrade gracefully under fault? | **AgentDX** (multi-agent) / Chaos Toolkit (infra) |
| **Deterministic replay** | Can I re-run the exact failure? | **AgentDX** |

An observability platform is *always on, in production, sampling*. AgentDX is *pre-deployment, exhaustive within a bound, and reproducible*. A team should run both. Saying so is what makes the project read as informed rather than naive.

**Positioning claim to use verbatim** `[SOURCE]`:

> Agent observability is solved. Agent *coordination reliability* is not. AgentDX is the first tool that treats a multi-agent system as a distributed system — deterministic replay, happens-before race detection, critical-path profiling, and fault injection — and produces a single defensible verdict on whether the topology earns its overhead.

## 1.8 MVP definition

**MVP = the P0 set in §4.1.** In one sentence: *a developer can run a shipped fixture offline with no API keys, watch AgentDX find a real seeded race and a real coordination bottleneck, see a speedup verdict against an auto-generated single-agent baseline, replay the exact interleaving, and confirm the healthy fixture reports nothing.*

Concretely, MVP is complete when all of §44's gates pass. The load-bearing ones:

- Deterministic replay: 100 replays → 100 identical canonical event logs. `[SOURCE]`
- The seeded write-write race in the code-pipeline fixture is detected; the healthy research fan-out fixture produces **zero** findings. `[SOURCE]`
- Critical-path decomposition buckets sum to virtual makespan within ±2%. `[SOURCE]`
- The speedup scorecard from §17.4 prints, with evidence links for every line.
- `docker compose up` → working demo in under 3 minutes on Apple silicon, fully offline. `[SOURCE]`

## 1.9 Expected outcome

| Outcome | Evidence it happened |
|---|---|
| A developer can answer "is my topology worth it?" with a number and a cause | Scorecard + findings, both evidence-linked to event sequence numbers |
| A concurrency defect in agent state becomes reproducible | `.agentdx` bundle replays byte-identically on another machine |
| Coordination overhead becomes a budget line, not a mystery | Six-bucket decomposition summing to makespan ±2% |
| Reliability becomes a CI gate | `agentdx run scenarios/ --ci` exits non-zero on regression |
| The project reads as a distributed-systems credential | Self-measured benchmark table (§34) + one written technical artifact |

---

# 2. Problem Definition

Each failure class below follows the same four-part structure demanded by the thesis: **what happens**, **why traditional tooling misses it**, **how AgentDX detects it**, and **what the developer does about it**. The seven classes in PRD v1.0 §2.1 `[SOURCE]` are preserved and expanded to ten; the three additions (serialization, nondeterministic failure, irreproducibility) were implicit in the source's §2.2 and are promoted to first-class failure classes because each has its own detector.

## 2.1 Failure class F1 — Coordination overhead

**What happens.** Work is split across agents, but the time spent moving work between agents — serialising a handoff payload, waiting for a supervisor to route, re-establishing context in a new prompt — costs more than the work itself. The system is busy without being productive. Message round-trips dominate. `[SOURCE]`

**Why traditional tooling misses it.** A trace viewer shows spans and their durations. It has no concept of *productive* versus *coordinative* time, and no denominator to compare against. Every span looks like work.

**How AgentDX detects it.** The overhead decomposer (§16.2) partitions the **critical path** into six mutually exclusive buckets — productive work, handoff latency, blocking wait, redundant work, retry/recovery, orchestration — and validates that they sum to virtual makespan within a 2% residual. Handoff latency is measured as the virtual interval between a `message_send` event and the `span_start` of the receiving span it causes.

**Remediation offered.** Ranked edge list: "the `coder→reviewer` handoff accounts for 61% of critical-path time; merge the two roles, or move the reviewer off the critical path by making it advisory rather than blocking."

## 2.2 Failure class F2 — Serialization (fake parallelism)

**What happens.** The topology *looks* parallel — a fan-out of four workers — but every branch awaits the same upstream dependency, or contends on the same shared state key, or is throttled by one rate-limited provider. Achieved parallelism is 1.2× where the diagram implies 4×.

**Why traditional tooling misses it.** A DAG rendering shows the *declared* topology. Only the timing of actual execution reveals that the branches never overlapped. Gantt charts in trace viewers show overlap but do not compute parallelism.

**How AgentDX detects it.** Two independent measurements that must agree:
- **Average parallelism** = total productive work ÷ critical-path length (§16.1). A declared 4-way fan-out with average parallelism 1.2 is flagged.
- **Overlap matrix**: pairwise virtual-time overlap between agents. A fan-out whose branches show <10% pairwise overlap is `FAKE_FANOUT`.

This is exactly the defect seeded in the **support triage** fixture (§23.2). `[SOURCE]`

**Remediation offered.** "Branches `retriever_a` and `retriever_b` overlap for 4% of their duration; both block on `classifier` completion. Hoist the shared prefix, or start retrieval speculatively before classification resolves."

## 2.3 Failure class F3 — Race conditions

**What happens.** Two agents write the same shared-state key with no ordering relationship between them. The last writer wins silently; the other agent's work is discarded. `[SOURCE]` In LangGraph this occurs on any channel without a reducer that is written from two concurrently-scheduled nodes.

**Why traditional tooling misses it.** There is no error. Both writes succeed. The trace shows two successful spans. The lost update is only visible as a subtly wrong final answer, often many steps later.

**How AgentDX detects it.** Vector-clock happens-before analysis (§14). Every agent carries a vector clock updated on message send/receive. Every state access is recorded with the accessing agent's clock. Two accesses to the same key conflict when **neither happens-before the other** and **at least one is a write** and **the values diverge** and **no declared reducer or lock covers the key**. Classification: `write-write` (lost update), `read-write` (stale read), `write-read` (dirty read). `[SOURCE]`

**Remediation offered.** The two conflicting spans, the key, the diverging value hashes, a minimal reproducing interleaving, and a concrete fix: add a reducer to the channel, take an `agentdx.lock()`, or route both writers through the planner.

## 2.4 Failure class F4 — Stale state / dirty reads

**What happens.** Agent B reads a key while Agent A is mid-way through a logically atomic multi-key update. `[SOURCE]` B proceeds on a torn view of the world: it sees the new plan but the old constraints.

**Why traditional tooling misses it.** Every individual read and write is valid. Only the *relationship* between them is wrong, and no tool models that relationship.

**How AgentDX detects it.** The same happens-before machinery, classified as `read-write` or `write-read`. Multi-key atomicity is handled by the optional `agentdx.transaction()` context manager (§8.6), which tags a set of state ops with a shared `txn_id`; a read concurrent with an open transaction that touches any key in it is reported as a **torn read**.

**Remediation offered.** "Wrap the `plan` + `constraints` update in `agentdx.transaction()`, or make the consumer read a single versioned snapshot key."

## 2.5 Failure class F5 — Redundant work

**What happens.** Two agents independently issue the same retrieval or the same tool call with identical arguments, doubling cost and latency with no benefit. `[SOURCE]`

**Why traditional tooling misses it.** Both calls are legitimate, successful, and belong to different agents' traces. Nothing correlates them.

**How AgentDX detects it.** The redundancy detector (§16.3) groups `tool_call` events by `sha256(tool_name ‖ canonical_json(args))`. Groups with cardinality > 1 whose members are **concurrent** (not a legitimate retry, not sequential re-verification) are reported with wasted virtual time and wasted tokens. **v1 uses exact-hash matching only, not semantic/embedding similarity** `[SOURCE]` — semantic dedup introduces false positives and a model dependency, which violates the "deterministic over LLM opinion" rule (§43.3.2).

**Remediation offered.** "`vector_search(query=…)` executed twice concurrently by `retriever_a` and `retriever_b`; 1.9s and 3,400 tokens wasted. Add a shared memoised tool wrapper or assign retrieval to one agent."

## 2.6 Failure class F6 — Cascading failure / retry amplification

**What happens.** One agent times out. The supervisor retries the whole branch rather than the failed step. Three agents re-run. Token spend triples. `[SOURCE]` Under a rate limit, the retries themselves cause more rate limiting.

**Why traditional tooling misses it.** Each retry looks like a normal execution. Aggregate token dashboards show a spike but not its causal origin.

**How AgentDX detects it.** Faults carry a `fault_id` that propagates onto every event causally downstream of the injection (§9.4). Retry amplification is computed as `spans_after_fault ÷ spans_in_baseline` for the affected subgraph, and the cascade is rendered as a causal tree rooted at the `fault_injected` event.

**Remediation offered.** "Fault `tool_failure(search, 429)` at t=2400 caused 11 additional spans and 4.1× token spend. Retry at the step boundary, not the branch boundary; add a circuit breaker after 2 consecutive failures."

## 2.7 Failure class F7 — Silent semantic failure

**What happens.** The run completes. It returns plausible output. It is wrong. There is no error to alert on. `[SOURCE]`

**Why traditional tooling misses it.** There is, by construction, nothing anomalous in the trace.

**How AgentDX detects it.** AgentDX **does not judge output quality** `[SOURCE]` — that is an explicit non-goal (§4.4). What it does is make silent failure *structurally* detectable in two ways:
1. **Assertion hook.** A scenario declares a pluggable `success_check` (§21.6): any Python callable or shell command returning a boolean. AgentDX supplies the final state; the user supplies correctness. This keeps the judgement deterministic and outside AgentDX.
2. **Silent-failure classification in resilience scoring** (§19.5). If a fault is injected and the run still reports success while the assertion fails, the outcome is classified `SILENT_FAILURE` and **caps the aggregate resilience score at 49/100** regardless of other results. A system that fails without saying so is the worst outcome in the model.

**Remediation offered.** "Under `byzantine(reviewer, confident_wrong)` the run reported success but `success_check` failed. No agent surfaced uncertainty. Add a validation step, or require the reviewer to emit a confidence signal the supervisor can act on."

## 2.8 Failure class F8 — Negative speedup

**What happens.** The full multi-agent system is measurably *slower* than a single agent with the same tools and the same model. `[SOURCE]` The topology is a net loss and nobody has ever checked.

**Why traditional tooling misses it.** There is no baseline. No observability platform generates a counterfactual.

**How AgentDX detects it.** The baseline generator (§17) builds a single-agent execution with the same task, the same tool set, the same model, the same seed, and maximum reuse of the same cached LLM responses. The verdict engine (§18) reports achieved speedup, ideal parallel speedup, the gap, and the attribution of the gap to overhead buckets.

**Remediation offered.** The headline scorecard (§17.4), ending in a specific structural recommendation.

## 2.9 Failure class F9 — Nondeterministic failure

**What happens.** The bug appears in one run out of thirty. It depends on which of two agents happened to reach the state key first, which in turn depends on model latency, GC timing, and the event-loop scheduler.

**Why traditional tooling misses it.** By the time you have a trace of the failure you cannot re-run it; by the time you have re-run it you no longer have the failure.

**How AgentDX detects it.** Two mechanisms:
1. **Determinism.** `(scenario, seed)` fully determines the interleaving (§10). The failing run is a seed you can keep.
2. **Bounded schedule exploration** (§15). Rather than hoping the bad interleaving occurs, AgentDX systematically enumerates interleavings reachable within *k* scheduler delays (default `k=2`), deduplicated by partial-order reduction, capped at *N* schedules (default 200). Most concurrency bugs manifest at small *k*.

**Remediation offered.** The seed and the delay-schedule that reproduce the defect, plus the honest caveat that bounded search is not proof of absence (§15.6).

## 2.10 Failure class F10 — Inability to reproduce

**What happens.** A colleague cannot reproduce your failure. The incident cannot be regression-tested. The fix cannot be verified.

**Why traditional tooling misses it.** Traces are read-only records of a past execution. They cannot be re-executed.

**How AgentDX detects it — and fixes it.** The `.agentdx` run bundle (§20.7) contains the event log, the scenario, the seed, the LLM cache slice keyed to that run, and the fixture/graph identity. `agentdx replay bundle.agentdx --verify` re-executes and asserts canonical-log equality. This is the mechanism that turns a one-off failure into a CI regression test (§22).

## 2.11 Failure-class → detector → journey map

| Class | Primary detector | FR | Journey | Fixture that exhibits it |
|---|---|---|---|---|
| F1 Coordination overhead | Overhead decomposer | FR-7 | C | Code pipeline |
| F2 Serialization | Parallelism + overlap matrix | FR-7 | C | Support triage |
| F3 Race conditions | Vector-clock race detector | FR-5 | B | Code pipeline |
| F4 Stale state | Race detector (`read-write`) | FR-5 | B | Code pipeline |
| F5 Redundant work | Redundancy detector | FR-7 | C | Support triage |
| F6 Cascading failure | Fault causal tree + amplification | FR-4, FR-9 | D | Any, under fault |
| F7 Silent semantic failure | Assertion hook + resilience classifier | FR-9, FR-11 | D | Code pipeline under `byzantine` |
| F8 Negative speedup | Baseline + verdict engine | FR-8 | C | Code pipeline |
| F9 Nondeterministic failure | Bounded exploration | FR-6 | B | Code pipeline |
| F10 Irreproducibility | Deterministic runtime + bundles | FR-2, FR-3, FR-10 | B, E | All |

## 2.12 Evidence discipline `[SOURCE]` — non-negotiable

PRD v1.0 flagged that the widely-circulated "70%+ of multi-agent systems fail in production" figure is **not traceable to a primary study** and must not appear in the README, the documentation, or an interview. That prohibition is carried forward and generalised into a project rule:

> **Rule E1.** No statistic appears in AgentDX material unless it is (a) measured by AgentDX itself on a shipped fixture, with the measurement script in-repo, or (b) cited to a specific, linkable primary source.

Defensible claims available today `[SOURCE]`:
- LLM API calls fail at a non-trivial rate in production (rate limits, 5xx, timeouts); across a 10–20 tool-call task, at least one fault per task is likely.
- Published benchmark work (ReliabilityBench; chaos engineering for LLM multi-agent systems, arXiv 2505.03096) confirms that rate limiting and injected faults measurably degrade agent task success.
- Chaos engineering applied specifically to *LLM multi-agent coordination* is described in the literature as largely unexplored.

Everything else comes from §34's benchmark suite, run against the fixtures this project ships. Self-generated data is stronger than a borrowed statistic anyway. `[SOURCE]`

---

# 3. Target Users

Six personas. The first is primary and drives every default. The sixth is unusual for a PRD but is a real consumer of this artifact and is designed for explicitly. `[SOURCE]`

## 3.1 Persona P1 — Agent Engineer *(primary)*

**Profile.** Builds a 3–8 agent LangGraph workflow. Has a working demo. Cannot tell whether adding the fourth agent helped or hurt. Wants a number. `[SOURCE]`

| Dimension | Detail |
|---|---|
| **Goals** | Ship a multi-agent feature that is faster and more reliable than the single-agent version it replaces. Justify the topology to a reviewer. Stop chasing a bug that reproduces once in thirty runs. |
| **Problems** | No baseline to compare against. Concurrency bugs in shared state that surface as "the answer was slightly wrong". No way to re-run a specific interleaving. Token spend rising faster than quality. |
| **Workflow** | Writes graph → runs it locally → eyeballs the output → ships. Debugging is print statements and re-running until the bug shows up. |
| **Required information** | Speedup vs single agent. Which edge is on the critical path. Whether any two agents touch the same state key concurrently. Where the tokens went. |
| **Success criteria** | Can state, with evidence, "this topology is 1.4× and here is why" — or "this topology is 0.83× and here is the edge to remove". |
| **Primary features** | FR-8 baseline + speedup verdict, FR-7 critical path, FR-5 race detection, FR-1 SDK, FR-10 replay |
| **Journey** | A, then C, then B |

**Design implications.** Instrumentation must be one line and must not require changing agent logic. `[SOURCE]` The first run must produce a verdict without configuration. The scorecard must be readable in the terminal, not only in the browser.

## 3.2 Persona P2 — AI Platform Engineer

**Profile.** Owns the shared agent framework, the model gateway, and the prompt/tool registry for several product teams. Reviews other people's topologies.

| Dimension | Detail |
|---|---|
| **Goals** | Set org-wide guidance on when multi-agent is justified. Catch pathological topologies before they reach the model gateway budget. Standardise instrumentation. |
| **Problems** | Every team claims their fan-out is parallel. Model spend grows superlinearly with agent count and nobody can attribute it. No comparable metric across teams. |
| **Workflow** | Reviews a design doc → asks for numbers → gets a LangSmith trace that does not answer the question. |
| **Required information** | Token cost multiplier vs baseline. Average parallelism. Redundant tool calls across the fleet. A comparable score across projects. |
| **Success criteria** | Can compare two teams' topologies on the same axes with the same tool and the same fixtures. |
| **Primary features** | FR-8 (token cost multiplier), FR-7 (redundancy, parallelism), FR-13 report export, FR-11 scenarios as shareable artifacts, §30 OTel interop |
| **Journey** | C, then E |

**Design implications.** The scorecard must be exportable and diffable (`agentdx compare`). OTel export matters more to this persona than to P1 because they already run Langfuse or Phoenix and will not adopt a tool that cannot coexist. `[SOURCE]`

## 3.3 Persona P3 — SRE / Reliability Engineer

**Profile.** Owns the reliability bar. Wants to know the system degrades gracefully when a tool 429s or an agent dies. Thinks in blast radius and MTTR. `[SOURCE]`

| Dimension | Detail |
|---|---|
| **Goals** | Know the failure modes before production does. Prove graceful degradation. Prevent silent failure. Gate releases on a reliability metric. |
| **Problems** | Chaos engineering tooling targets infrastructure, not agent coordination. No steady-state hypothesis exists for an agent system. "It retried and eventually worked" is indistinguishable from "it burned 4× tokens and returned garbage confidently". |
| **Workflow** | Defines a hypothesis → injects a fault → measures deviation → scores → gates. Wants that loop in YAML and in CI. |
| **Required information** | Per-fault success ratio, recovery time, retry amplification, whether degradation was graceful or silent. The **per-fault breakdown**, never the aggregate alone. `[SOURCE]` |
| **Success criteria** | A scenario file in the repo, running on every PR, that fails the build when the resilience score regresses. |
| **Primary features** | FR-4 fault injection, FR-9 resilience scoring, FR-11 scenarios + CI, §13 chaos safety |
| **Journey** | D, then E |

**Design implications.** Safety rails are a *feature* for this persona, not a constraint. `[SOURCE]` Blast radius, steady-state hypothesis and abort guards must be first-class in the scenario schema and visible in the UI before a fault fires.

## 3.4 Persona P4 — Developer Infrastructure Engineer

**Profile.** Owns CI, developer tooling, and the local dev environment. Adopts a tool only if it is fast, hermetic, and does not require credentials in CI.

| Dimension | Detail |
|---|---|
| **Goals** | Add a reliability gate that runs in under a minute, offline, with no API keys and no flakiness. |
| **Problems** | Anything touching a live LLM is slow, expensive and non-deterministic — three properties CI cannot tolerate. Flaky gates get disabled within a week. |
| **Workflow** | Wires a GitHub Action → watches the first ten runs → deletes the gate if it flakes once. |
| **Required information** | Exit codes, machine-readable output (JSON/JUnit), artifact paths, cache provenance, run wall-time. |
| **Success criteria** | Green/red is trustworthy: no false failures across 100 consecutive CI runs on unchanged code. |
| **Primary features** | FR-11 CI mode, FR-3 replay cache (hermetic runs), FR-2 determinism, §22 CI integration, §37 CLI |
| **Journey** | E |

**Design implications.** Replay-mode cache misses must be a **hard error, not a silent live call** `[SOURCE]` — this is the property that makes CI hermetic. Exit codes must be stable and documented (§37.2).

## 3.5 Persona P5 — Engineering Manager / Architect

**Profile.** Approves or rejects the multi-agent design. Does not read traces. Reads verdicts and risk.

| Dimension | Detail |
|---|---|
| **Goals** | Make a defensible build/no-build call on a topology. Understand the cost multiplier. Know what will break. |
| **Problems** | Presented with an architecture diagram and enthusiasm, not evidence. Cannot evaluate a claim of "3× faster" without a baseline. |
| **Workflow** | Design review → asks "compared to what?" → currently gets no answer. |
| **Required information** | One verdict line, a confidence level, a token cost multiplier, top three findings by severity, and the recommendation. |
| **Success criteria** | Can approve or reject in five minutes with evidence attached to the decision record. |
| **Primary features** | FR-13 report export, FR-8 scorecard, §18 verdict with confidence and evidence |
| **Journey** | C (as a consumer of the report, not the operator) |

**Design implications.** The verdict must be explainable, evidence-backed, and **never a black-box LLM opinion**. Confidence must be stated, and its basis stated with it (§18.5).

## 3.6 Persona P6 — Technical Reviewer / Recruiter

**Profile.** Opens the repository from a link. Has 30 seconds for the thesis and 5 minutes for the depth. `[SOURCE]`

| Dimension | Detail |
|---|---|
| **Goals** | Determine quickly whether this is a genuine systems project or a wrapper around an LLM. |
| **Problems** | Most AI portfolio projects are indistinguishable. README screenshots prove nothing. |
| **Workflow** | README → GIF → one code file → maybe `docker compose up`. |
| **Required information** | The thesis in one sentence; the ghost-baseline visual; evidence of real computer science (vector clocks, delay-bounded exploration); self-measured benchmarks; honest limitations. |
| **Success criteria** | Runs the demo offline in under 3 minutes and sees a real defect found and explained. |
| **Primary features** | §38 first-5-minutes experience, §23 fixtures, §29.4 ghost baseline, §34 benchmark table, §15.6 honesty statement |
| **Journey** | A (abbreviated: clone → `docker compose up` → open → verdict) |

**Design implications.** The demo must work with **zero API keys** `[SOURCE]` from the committed cache. The honesty statements (bounded exploration is not proof of absence; the competitive landscape is real) are *assets* with this persona, not liabilities.

## 3.7 Persona → feature priority matrix

| Feature | P1 Agent Eng | P2 Platform | P3 SRE | P4 DevInfra | P5 EM | P6 Reviewer |
|---|---|---|---|---|---|---|
| FR-1 SDK | ●●● | ●●● | ● | ●● | — | ● |
| FR-2 Scheduler/clock | ●● | ● | ●● | ●●● | — | ●●● |
| FR-3 LLM cache | ●● | ● | ● | ●●● | — | ●● |
| FR-4 Fault injection | ● | ● | ●●● | ● | ● | ●● |
| FR-5 Race detection | ●●● | ●● | ●● | ● | ● | ●●● |
| FR-6 Bounded exploration | ●● | ● | ●● | ● | — | ●●● |
| FR-7 Critical path | ●●● | ●●● | ● | — | ●● | ●● |
| FR-8 Baseline + speedup | ●●● | ●●● | ● | — | ●●● | ●●● |
| FR-9 Resilience score | ● | ●● | ●●● | ●● | ●● | ● |
| FR-10 Replay | ●●● | ● | ●● | ● | — | ●●● |
| FR-11 Scenarios + CI | ● | ●● | ●●● | ●●● | ● | ● |
| FR-12 Fixtures | ●● | ● | ● | ●● | — | ●●● |
| FR-13 Report export | ● | ●●● | ●● | ● | ●●● | ● |

●●● critical · ●● important · ● useful · — not relevant

---

# 4. Product Scope

Scope boundaries from PRD v1.0 are preserved exactly. `[SOURCE]` The single scope change proposed anywhere in this document is stated openly in §4.5 with its trade-off. **Scope creep is the highest-probability project risk (§42.2); this section is the defence.**

## 4.1 MVP / P0 — must ship

The MVP is the smallest set that can produce a defensible verdict on a real graph and prove it is reproducible.

| ID | Requirement | Why it is P0 |
|---|---|---|
| FR-1 | Instrumentation SDK (decorator + LangGraph adapter) | Nothing is observable without it |
| FR-2 | Deterministic scheduler + virtual clock ⭐ | The foundation of every claim the product makes |
| FR-3 | LLM record/replay cache ⭐ | Determinism is impossible while the model is live; also what makes the offline demo possible |
| FR-4 | Fault injection engine (latency, crash, message drop, tool failure) | The chaos half of the thesis; four fault types is enough for MVP |
| FR-5 | Race / state-conflict detection ⭐ | The strongest depth signal and the code-pipeline fixture's seeded defect |
| FR-7 | Critical path + overhead decomposition ⭐ | Makes coordination overhead measurable |
| FR-8 | Single-agent baseline + speedup verdict ⭐ | **The headline feature.** If a user sees only this, AgentDX has done its job |
| FR-12 | Three reference fixture systems | The demo, the regression suite, and the false-positive proof |
| — | Control Tower: waterfall + scorecard + findings | Minimum UI that tells the story |
| — | Event store, replay verifier, CLI (`run`, `replay`, `analyze`) | Infrastructure for the above |

⭐ = load-bearing; if one of these is cut the product thesis fails. `[SOURCE]`

**MVP fault set** `[DETAIL]`: `latency`, `agent_crash`, `message_drop`, `tool_failure`. The remaining six fault types (§12) are P1. Rationale: these four cover transport, process and dependency classes and are sufficient to demonstrate cascade and resilience; `byzantine` and `state_corrupt` require the perturb-mode cache and the state-mutation API respectively, both of which are P1 work.

## 4.2 Version 1 / P1

| ID | Requirement | Rationale for P1 rather than P0 |
|---|---|---|
| FR-6 | Bounded schedule exploration | Valuable and highly interviewable, but the seeded race is findable in a single run; exploration generalises it |
| FR-9 | Resilience scoring | Needs the full fault catalogue and a multi-run harness |
| FR-10 | Replay + time travel UI | The replay *engine* is P0 (it is how determinism is verified); the *scrubbing UI* is P1 |
| FR-11 | Scenario YAML + CI mode | See §4.5 — scope decision |
| — | Remaining six fault types (reorder, duplicate, agent_slow, rate_limit, byzantine, state_corrupt) | |
| — | Control Tower: dependency graph panel + chaos control panel + cross-panel linking | |
| — | OpenTelemetry GenAI span export (§30) | Cheap to build, disproportionately strong in review `[SOURCE]` |
| — | `.agentdx` bundle export/import | |
| — | `agentdx compare` (run-to-run diff) | |

## 4.3 Stretch / P2

| ID | Requirement | Rationale |
|---|---|---|
| FR-13 | One-page HTML/PDF report export | High value to P2/P5 personas, zero risk to the core |
| — | CrewAI adapter | See §43.1.3 — recommended deferral |
| — | Semantic redundancy detection (embedding similarity) | Explicitly rejected for v1 `[SOURCE]`; revisit only with a false-positive study |
| — | Sleep-set / DPOR-grade partial-order reduction | v1 ships a simpler independence-based reduction (§15.4) |
| — | Multi-run statistical aggregation across seeds | |
| — | VS Code extension surfacing findings inline | |

## 4.4 Explicitly out of scope — with reasons `[SOURCE]`

| Excluded | Why |
|---|---|
| **Production monitoring / always-on APM** | This is the crowded lane (§1.7). AgentDX is pre-deployment. Entering the APM market means competing with Datadog on their terms and abandoning the differentiator. Also: the deterministic scheduler is fundamentally incompatible with production execution — you cannot virtualise the clock of a live user request. |
| **Hosted multi-tenant SaaS, auth, billing, teams** | Local-first is a product decision, not a cost decision: the whole value proposition depends on running a user's proprietary graph and prompts on their machine. Adding auth/billing adds attack surface, ops burden and zero thesis value. If ever added, it belongs in a separate product tier, not the MVP. |
| **Exhaustive model checking of the full state space** | Undecidable at scale. v1 does **bounded** exploration and says so, in the UI and the reports. `[SOURCE]` Claiming exhaustiveness would be the single fastest way to lose credibility with the reviewer persona. |
| **Judging output quality (LLM-as-judge)** | AgentDX judges *coordination* and delegates semantic correctness to a pluggable assertion hook. `[SOURCE]` A judge model would inject non-determinism into a system whose entire value is determinism — a direct contradiction of the product thesis. |
| **Framework support beyond LangGraph + generic decorator API** | Doing one framework properly beats three shallowly. The generic decorator API is the escape hatch for everything else. |
| **Fine-tuning / RL for coordination optimisation** | Listed in the source research; cut — no time, no eval signal. `[SOURCE]` |
| **Distributed/multi-machine agent execution** | `[DETAIL]` The deterministic scheduler assumes a single process. Cross-process determinism requires a distributed virtual clock and is a research project, not a 10-week feature. The event model (vector clocks) is *designed* to accommodate it later; the runtime is not. |
| **Automatic topology rewriting** | AgentDX recommends; it does not refactor. Automated rewriting requires semantic understanding of agent responsibilities — that is a different product with a different failure mode. |

## 4.5 The one scope decision this document raises `[IMPROVEMENT]`

**Source position:** FR-11 (scenario YAML + `--ci`) is P1, and PRD v1.0 open question #1 asks whether CI mode dilutes the pre-deployment story. `[SOURCE]`

**Analysis.** Scenario YAML and CI mode are separable, and the source treats them as one item.
- *Scenario YAML* is not optional: fault injection (FR-4, P0) needs a declarative place to specify faults, blast radius, steady-state hypothesis and abort guards. Without it, chaos configuration lives in Python call sites and the safety rails (§13) have no home. **Scenario YAML is therefore effectively a P0 dependency of FR-4.**
- *CI mode* (`--ci`, exit codes, machine-readable output, regression comparison) is genuinely P1 and does not dilute the story — it *extends* the pre-deployment story to its natural conclusion. Pre-deployment testing that cannot run in CI is a demo, not a tool.

**Recommendation.** Split FR-11 into **FR-11a Scenario definition (P0)** and **FR-11b CI mode (P1)**. This is a scope *clarification*, not an expansion: no new capability is added, and the scope-cut order in §40.3 still drops FR-11b early if the schedule slips.

**Trade-off accepted:** a small amount of week-1 schema work moves earlier, in exchange for FR-4 having a declarative surface from the day it lands.

## 4.6 Scope control mechanism `[DETAIL]`

Three rules, enforced in review:

1. **The ⭐ rule.** FR-2, FR-3, FR-5, FR-7, FR-8 are the product. `[SOURCE]` Any work that does not serve one of those five, or unblock them, is negotiable.
2. **The evidence rule.** No feature ships that produces a claim without a link to the event sequence numbers that justify it (§18.4).
3. **The two-week rule.** Anything discovered mid-build that would take more than two weeks goes to §43 as an `[OPEN]` item and into P2 by default, not into the current milestone.

---

# 5. Core User Journeys

Each journey specifies the trigger, the step-by-step flow with the system's internal behaviour, the artifacts produced, the failure branches, and the acceptance criteria that prove the journey works.

## 5.1 Journey A — First instrumentation

**Persona:** P1 Agent Engineer (and, abbreviated, P6 Reviewer).
**Trigger:** "I have a LangGraph app and I want to know if the topology is worth it."
**Target time to verdict:** under 5 minutes from `pip install` on a machine with no API keys. `[SOURCE]`

```
 install ──▶ wrap graph ──▶ run fixture ──▶ record events ──▶ open Control Tower ──▶ verdict
```

| # | User action | System behaviour | Artifact |
|---|---|---|---|
| A1 | `uv pip install agentdx` (or `docker compose up`) | Package installs; `agentdx doctor` validates Python version, writable data dir, port availability | `~/.agentdx/` created |
| A2 | `agentdx run fixtures/code_pipeline` | Runtime loads fixture, resolves scenario defaults, opens run record | `run_id` |
| A3 | — | Scheduler starts in **replay** LLM mode; the committed cache serves every model call; virtual clock advances per calibration profile | Events streaming |
| A4 | — | SDK emits `span_start`/`span_end`/`message_*`/`state_*`/`tool_call` events with vector clocks and virtual timestamps into SQLite | `events` table, append-only |
| A5 | — | Run completes; analysis layer executes: critical path → overhead decomposition → redundancy → race detection → baseline → verdict | `findings`, `scorecard` |
| A6 | — | CLI prints the scorecard block (§17.4) and the top three findings | Terminal output |
| A7 | `agentdx ui` (or the URL printed at A6) | FastAPI serves the Control Tower; the run loads from the event log | Browser at `/runs/{id}` |
| A8 | Reads verdict | Verdict banner, findings list, waterfall with ghost baseline | Understanding |

**Then, on the user's own graph:**

| # | User action | System behaviour |
|---|---|---|
| A9 | `agentdx instrument --framework langgraph app.py` | Static scan reports which nodes will be captured and what is missing; **writes nothing** without `--write` |
| A10 | Adds `graph = agentdx.instrument(graph)` (one line) | LangGraph callback adapter attaches; no agent logic changes `[SOURCE]` |
| A11 | `agentdx run --record ./app.py:graph --task tasks/x.md` | **Record mode**: live model calls, responses cached, wall-clock calibration profile captured |
| A12 | `agentdx run ./app.py:graph --task tasks/x.md` | **Replay mode**: fully offline, deterministic, free |

**Failure branches.**

| Branch | Detection | Behaviour |
|---|---|---|
| No instrumented spans found | Analysis layer sees 0 spans | Error `E-INSTR-001`: "No AgentDX spans recorded. Did you wrap the graph? See docs/instrumentation.md" — exit 2 |
| Only one agent detected | `distinct(agent_id) == 1` | Warning, plus verdict `SINGLE_AGENT` — baseline comparison is skipped with an explanation, not a crash |
| Cache miss in replay mode | Cache lookup fails | Hard error `E-CACHE-001` naming the missing key and offering `--record` — **never a silent live call** `[SOURCE]` |
| Port 8420 in use | Bind fails | `agentdx ui --port` suggestion in the message |

**Acceptance criteria.**
- A-AC1: On a clean machine with no `GROQ_API_KEY`, steps A1–A8 complete in <5 min wall time and produce a non-empty verdict.
- A-AC2: Steps A9–A12 on the reference LangGraph app require exactly one added line of user code.
- A-AC3: Instrumentation overhead in passthrough mode measures <10% (§34.1).

## 5.2 Journey B — Debugging a race

**Persona:** P1.
**Trigger:** the findings list shows a write-write conflict, or a nondeterministic bug is suspected.

```
 run ─▶ race detected ─▶ finding opened ─▶ conflicting spans highlighted ─▶ timeline inspected
     ─▶ reproduction generated ─▶ developer fixes ─▶ rerun ─▶ finding disappears
```

| # | Step | System behaviour | UI surface |
|---|---|---|---|
| B1 | Run completes | Race detector walks state ops, compares vector clocks, applies the four false-positive guards (§14.7) | — |
| B2 | Finding created | `finding` record: `type=state_conflict`, `subtype=write_write`, severity, the two event seqs, key, both value hashes, both vector clocks | Findings panel, severity-ranked |
| B3 | User clicks the finding | Cross-panel linking fires: the two conflicting spans highlight in the waterfall **and** the two agent nodes highlight in the graph, simultaneously `[SOURCE]` | Waterfall + graph |
| B4 | User scrubs the timeline to the conflict | State reconstruction replays events up to that virtual timestamp; state table shows `draft.module_a` before and after each write | Timeline + state table |
| B5 | User clicks "Generate reproduction" | Minimal repro generator (§14.8) emits a scenario YAML with the seed, the delay schedule, and an assertion `no_state_conflicts` | `scenarios/repro_<finding_id>.yaml` |
| B6 | Developer fixes the graph | (e.g. adds a LangGraph reducer to the channel, or takes `agentdx.lock('draft.module_a')`) | — |
| B7 | `agentdx run scenarios/repro_<id>.yaml` | Re-executes under the same seed and delay schedule | — |
| B8 | Finding is gone | Assertion passes; CLI prints `RESOLVED: state_conflict draft.module_a` and links to the previous run for diff | `agentdx compare` |

**Failure branches.**

| Branch | Behaviour |
|---|---|
| Fix removes the conflict but breaks the task | `success_check` assertion fails; the CLI reports both facts and does not claim resolution |
| The conflict was a false positive (channel has a reducer) | Detector should have classified it `benign_merge` and suppressed it; if it did not, this is a **P0 bug** — see §33.9 false-positive suite |
| Repro does not reproduce | `E-REPRO-001`. Indicates a determinism leak. Escalates to the determinism test suite (§33.3); this is treated as a runtime defect, not a user error |

**Acceptance criteria.**
- B-AC1: The seeded write-write race in the code-pipeline fixture is detected on a single default run.
- B-AC2: The generated reproduction scenario re-triggers the same finding on a fresh process, 10/10 times.
- B-AC3: After applying the documented fix, the finding disappears and the healthy assertions still pass.
- B-AC4: The research fan-out fixture produces **zero** state-conflict findings (false-positive gate).

## 5.3 Journey C — Speedup investigation

**Persona:** P1, consumed by P2/P5.
**Trigger:** "Is this worth it?"

```
 multi-agent run ─▶ single-agent baseline ─▶ critical path ─▶ overhead decomposition
                 ─▶ speedup calculation ─▶ recommendation
```

| # | Step | System behaviour |
|---|---|---|
| C1 | `agentdx run <graph> --baseline` | Multi-agent run executes as normal, producing run `A` |
| C2 | Baseline generation | Baseline generator (§17.2) constructs a single-agent execution: same task, same tool set, same model, same seed, sequential execution, maximum reuse of cached responses. Produces run `B` with `baseline_of = A` |
| C3 | Critical path | Timing DAG built from run `A`'s event log; longest weighted path computed by topological DP (§16.1) |
| C4 | Overhead decomposition | Critical path partitioned into six buckets; residual must be <2% or the run is flagged `UNATTRIBUTED_TIME` (§16.2.4) |
| C5 | Speedup calculation | `achieved = T_single / T_multi`; `ideal_parallel = W_total / CP_length`; per-bucket marginal attribution normalised to the gap (§17.3) |
| C6 | Comparability grading | Cache reuse rate between A and B computed; comparability graded A/B/C and **always shown** (§17.5) |
| C7 | Verdict | Deterministic rules (§18.2) select a verdict class, severity, confidence and a ranked recommendation |
| C8 | Output | Scorecard block in terminal; scorecard panel + ghost-baseline waterfall in UI |

**Failure branches.**

| Branch | Behaviour |
|---|---|
| Baseline cannot reuse enough cache (reuse rate <40%) | Comparability grade **C**; the speedup number is reported with an explicit "low comparability" badge and the UI de-emphasises it. Never silently reported as if grade A |
| Task is not expressible as a single-agent prompt | `E-BASE-002`; the baseline is skipped, the critical path and overhead decomposition are still reported. Partial value, never a crash |
| Residual (unattributed) time >2% | Verdict is still produced but carries `analysis_warning`; the residual bucket is displayed rather than hidden |

**Acceptance criteria.**
- C-AC1: Buckets sum to virtual makespan within ±2% on all three fixtures. `[SOURCE]`
- C-AC2: On the code-pipeline fixture the achieved speedup is <1.0 and the top overhead bucket is `handoff`.
- C-AC3: On the research fan-out fixture the achieved speedup is >1.0 and the verdict class is `BENEFICIAL`.
- C-AC4: Every number in the scorecard resolves to at least one event sequence number when clicked.

## 5.4 Journey D — Chaos test

**Persona:** P3 SRE.
**Trigger:** "Prove it degrades gracefully."

```
 define scenario ─▶ set fault ─▶ declare blast radius ─▶ set steady-state hypothesis
                 ─▶ execute ─▶ observe degradation ─▶ score resilience
```

| # | Step | System behaviour |
|---|---|---|
| D1 | Author `scenarios/reviewer_crash.yaml` | Schema validation on load; unknown keys are errors, not warnings (§21.3) |
| D2 | Declare `faults:` | Fault spec validated against the catalogue (§12.2); target must exist in the graph |
| D3 | Declare `blast_radius:` | **Required.** A scenario with faults and no blast radius fails validation (§13.4). Default when targeting a user graph is empty — nothing is in scope until named |
| D4 | Declare `hypothesis:` | Steady-state metrics with comparison operators; evaluated on the *baseline* run first |
| D5 | `agentdx run scenarios/reviewer_crash.yaml` | Phase 1: baseline run (no faults) establishes steady state. Phase 2: fault run |
| D6 | Steady-state check | If the baseline itself violates the hypothesis, the experiment **aborts before injecting** — you cannot measure deviation from a state you never had |
| D7 | Fault injection | Injector arms at the scheduler layer; at `at_virtual_ts=2400` the `agent_crash` fires; a `fault_injected` event is written; all causally downstream events inherit `fault_id` |
| D8 | Guards | Abort guards evaluated continuously: max virtual duration, max token spend, max retries (§13.6) |
| D9 | Observe | UI shows the cascade: the crashed node rings red, downstream spans re-run, the retry bucket grows in the waterfall |
| D10 | Score | Resilience scorer computes per-fault success ratio, recovery time, retry amplification, degradation class; aggregates with the silent-failure cap (§19.6) |

**Failure branches.**

| Branch | Behaviour |
|---|---|
| Blast radius omitted on a user graph | Validation error `E-SCEN-004` before any execution |
| Guard trips (e.g. token budget) | Run aborted, marked `ABORTED_GUARD`, partial event log retained and analysable, resilience score **not** computed (an aborted run cannot be scored) |
| Fault target does not exist | Validation error at load, naming the valid targets |
| Run succeeds under fault but `success_check` fails | Classified `SILENT_FAILURE`; aggregate capped at 49 |

**Acceptance criteria.**
- D-AC1: Killing the reviewer agent mid-run produces a visible cascade in the event log and a non-zero `retry_recovery` bucket.
- D-AC2: A scenario with faults and no blast radius fails validation 100% of the time.
- D-AC3: The per-fault breakdown is present in every resilience report; the aggregate never appears alone. `[SOURCE]`

## 5.5 Journey E — CI reliability gate

**Persona:** P4, authored by P3.
**Trigger:** every pull request.

```
 scenario YAML ─▶ agentdx run --ci ─▶ execution ─▶ assertions ─▶ pass/fail ─▶ CI result
```

| # | Step | System behaviour |
|---|---|---|
| E1 | PR opened | GitHub Action triggers (`.github/workflows/agentdx.yml`, shipped as an example `[SOURCE]`) |
| E2 | Checkout + install | `uv sync`; the committed LLM cache is restored from the repo or from an actions cache |
| E3 | `agentdx run scenarios/ --ci --format junit --out results/` | Every scenario in the directory executes in **replay** mode; no network access required |
| E4 | Execution | Runs execute sequentially by default (deterministic ordering); `--jobs N` allowed but each run is independently deterministic |
| E5 | Assertion evaluation | Each assertion evaluated against the analysis output; results collected (§22.3) |
| E6 | Regression comparison | If `--baseline-run <path>` is supplied, current metrics are diffed against the stored baseline with per-metric tolerances |
| E7 | Exit | Exit 0 on all-pass; exit 1 on assertion failure; other codes per §37.2 |
| E8 | Artifacts | JUnit XML + JSON summary + `.agentdx` bundles for failed runs uploaded as CI artifacts |
| E9 | PR annotation | Failure message names the scenario, the assertion, expected vs actual, and the finding IDs |

**Failure branches.**

| Branch | Behaviour |
|---|---|
| Cache miss (someone added an agent but did not re-record) | Exit code 3 with `E-CACHE-001` and the exact `agentdx run --record` command needed |
| Non-determinism detected (replay verification fails) | Exit code 6. This is treated as a **product defect in AgentDX**, and the bundle is attached for triage |
| Scenario schema invalid | Exit code 2, before any execution |
| Guard aborted | Exit code 4 |

**Acceptance criteria.**
- E-AC1: 100 consecutive CI runs on unchanged code produce 100 identical results (zero flake).
- E-AC2: Total CI wall time for the three shipped fixture scenarios is under 60 seconds on a standard GitHub runner.
- E-AC3: A deliberately regressed fixture (race re-introduced) fails the gate with a message naming the key and the two agents.

---

# 6. Core Concepts

These definitions are normative. Where the source model is extended, the extension is tagged. Implementations must use these exact names in code, in the API and in the UI.

## 6.1 Entity definitions

| Concept | Definition | Identity | Notes |
|---|---|---|---|
| **Project** | A directory containing scenarios, fixtures or an instrumented user graph, plus an AgentDX data store. The unit of local organisation. `[SOURCE]` | Path + `agentdx.toml` | No server-side concept; projects are not shared or synced |
| **Scenario** | A declarative execution definition: task + seed + fault profile + steady-state hypothesis + blast radius + guards + assertions. `[SOURCE]` | `scenario_id` = slug of filename | Versioned YAML (§21). A scenario is reusable and is the unit of CI |
| **Run** | One execution of a scenario against a graph. Modes: `baseline` \| `chaos` \| `replay` \| `explore`. `[SOURCE]` | `run_id` = `r_` + 5 hex chars of a content hash | Immutable once complete. A run owns exactly one event log |
| **Agent** | A named node in the graph that performs work and holds a vector-clock slot. `[SOURCE]` | `agent_id` (stable string, e.g. `coder`) | Roles: `worker` \| `orchestrator` \| `router` \| `tool_proxy`. Role affects overhead bucketing (§16.2) |
| **Span** | A unit of work with a virtual start and end. Types: `llm_call` \| `tool_call` \| `handoff` \| `wait` \| `agent_step`. `[SOURCE]` | `span_id` | Spans nest within an agent; a span belongs to exactly one agent |
| **Event** | An immutable, sequence-numbered record of one observable action. **The single source of truth.** `[SOURCE]` | `(run_id, seq)` | Append-only. Every analyser reads only events (§9) |
| **Message** | A directed agent→agent communication. `[SOURCE]` | `message_id` | Produces exactly one `message_send` and at most one `message_recv` event. The **only** carrier of happens-before between agents (§14.3) |
| **State Operation** | A read or write against a shared-state key. `[SOURCE]` | `(run_id, seq)` | Carries `key`, `value_hash`, `prev_value_hash`, and (if applicable) `txn_id`, `reducer`, `lock_id` |
| **Fault** | A declared perturbation, its target, its trigger and its parameters. `[SOURCE]` | `fault_id` | Produces one `fault_injected` event; taints all causally downstream events |
| **Finding** | A detected problem with severity, evidence and a remediation. `[DETAIL]` | `finding_id` | Every finding MUST carry ≥1 event `seq` as evidence (§18.4) |
| **Verdict** | The single top-level judgement on the topology, with class, score, confidence, evidence and recommendation. `[SOURCE]` | one per run | Deterministic; never an LLM opinion (§18) |
| **Baseline** | A single-agent run generated to be comparable to a multi-agent run. `[SOURCE]` | a `Run` with `baseline_of` set | Carries a comparability grade (§17.5) |
| **Replay** | Re-execution of a recorded run from its event log, seed and cache slice, verified against the original. `[SOURCE]` | a `Run` with `replay_of` set | Equality is over the **canonical projection** (§10.7) |
| **Virtual Time** | The AgentDX simulated clock, in integer milliseconds since run start. Advanced by the scheduler, never by real elapsed time. `[SOURCE]` | `virtual_ts_ms` | Injecting "+500 ms" advances virtual time by 500 with no wall-clock cost |
| **Wall Time** | Real elapsed milliseconds since run start, recorded for calibration and overhead accounting only. `[SOURCE]` | `wall_ts_ms` | **Excluded from replay equality** (§10.7) |
| **Vector Clock** | A per-agent map `agent_id → counter`, giving a partial order without a global clock. `[SOURCE]` | `vclock` on every event | Updated per §14.2 |
| **Happens-Before** | Lamport's partial order. `a → b` iff same agent and `a` precedes `b`, or `a` is a send and `b` its receive, or by transitivity. If neither `a → b` nor `b → a`, they are **concurrent**. `[SOURCE]` | — | Shared-state access does **not** create a happens-before edge (§14.3) |
| **Critical Path** | The longest weighted path through the timing DAG; the floor on virtual makespan. `[SOURCE]` | ordered list of spans | Computed by topological DP (§16.1) |
| **Coordination Overhead** | The portion of the critical path not spent on productive work: handoff + blocking wait + retry/recovery + orchestration (+ redundant work where it lands on the path). `[SOURCE]` | six buckets | Must sum with productive work to makespan ±2% |
| **Blast Radius** | The set of agents, tools, edges and state keys a chaos experiment is permitted to affect. `[SOURCE]` | declared in scenario | Empty by default for user graphs (§13.4) |
| **Steady-State Hypothesis** | The measurable property asserted to hold *before* a fault is injected. `[SOURCE]` | declared in scenario | Verified on the baseline phase; failure aborts the experiment |
| **Resilience Score** | 0–100 aggregate of per-fault outcomes, with the per-fault breakdown always visible. `[SOURCE]` | per scenario | Silent failure caps the aggregate (§19.6) |

## 6.2 Additional concepts introduced by this document `[DETAIL]`

| Concept | Definition | Why needed |
|---|---|---|
| **Causality Graph** | The happens-before graph. Nodes = events; edges = program order + message send→recv + explicit sync primitives. **Excludes shared-state data flow.** | Race detection is only correct if shared-state access is *not* treated as synchronisation (§14.3) |
| **Timing DAG** | The performance graph. Nodes = spans; edges = program order + message causality + observed data dependencies + fault-induced retries; weights = virtual durations. | Critical path needs data dependencies; race detection must not have them. Two graphs, two purposes |
| **Schedule** | The ordered sequence of scheduler decisions for a run: `[(step, chosen_task_id)]`. | The unit of bounded exploration; the reproduction artifact (§15) |
| **Delay Schedule** | A schedule expressed as up to *k* deviations from the default priority order. | Compact, comparable, and the thing a minimal reproduction ships |
| **Cache Slice** | The subset of the LLM cache required to replay one run, exported in the `.agentdx` bundle. | Bundles must be portable without shipping the whole cache |
| **Comparability Grade** | A/B/C rating of how validly a baseline can be compared to a multi-agent run, based primarily on cache reuse rate. | Prevents a headline speedup number from being quoted out of context (§17.5) |
| **Canonical Projection** | The deterministic serialisation of an event log with volatile fields (wall time, host, pid) removed, used for replay equality. | Makes "byte-identical" a precise, testable claim (§10.7) |

## 6.3 Entity relationships

```
Project 1──n Scenario
Scenario 1──n Run
Run     1──1 EventLog            (append-only, immutable after completion)
Run     1──n Agent               (derived from events; agents are not pre-declared)
Run     1──n Span                (derived: span_start/span_end pairs)
Run     1──n Message             (derived: message_send/message_recv pairs)
Run     1──n StateOp             (derived: state_read/state_write)
Run     1──n Fault               (declared in scenario, realised as fault_injected events)
Run     1──n Finding             (produced by analysers; each cites ≥1 event seq)
Run     1──1 Verdict             (produced by the verdict engine)
Run     0──1 Run    as baseline_of   (a baseline run points at the run it explains)
Run     0──1 Run    as replay_of     (a replay points at its original)
Run     0──1 Run    as explore_parent (an exploration child points at its root run)

Span    n──1 Agent
Span    0──n Event               (a span is materialised from its events)
Message 1──1 Event(message_send)
Message 0──1 Event(message_recv) (absent if dropped by a fault)
StateOp 1──1 Event
Finding n──n Event               (evidence links)
Fault   1──n Event               (fault_id taint on downstream events)
```

**The derivation rule** `[DETAIL]`: *Agent, Span, Message and StateOp are projections of the event log, not independently stored entities.* They are materialised into DuckDB views (or SQLite views for small runs) at analysis time. This enforces the design rule that the event log is the single source of truth `[SOURCE]` and guarantees that anything visible in the UI can be traced to an event.

## 6.4 Lifecycle state machine

```
                ┌──────────┐
                │  CREATED │  run record inserted, scenario resolved
                └────┬─────┘
                     ▼
                ┌──────────┐
        ┌───────│ RUNNING  │───────┐  scheduler active, events appending
        │       └────┬─────┘       │
        │            ▼             │
        │       ┌──────────┐       │
        │       │ ANALYSING│       │  event log sealed; analysers run
        │       └────┬─────┘       │
        │            ▼             │
        │       ┌──────────┐       │
        │       │ COMPLETE │       │  verdict written; run immutable
        │       └──────────┘       │
        │                          │
        ▼                          ▼
  ┌──────────────┐        ┌────────────────┐
  │ ABORTED_GUARD│        │     FAILED     │
  └──────────────┘        └────────────────┘
   guard tripped;          runtime/user error;
   log retained,           log retained,
   NOT scored              analysis best-effort
```

Runs are **immutable after `COMPLETE`**. Re-analysis with a newer analyser version creates a new `analysis_version` row, never mutates events.

---

# 7. Functional Requirements

Priority: **P0** = MVP, must ship. **P1** = v1.0. **P2** = stretch. `[SOURCE]`
⭐ marks the five load-bearing requirements: cutting any of them breaks the thesis. `[SOURCE]`

Each requirement below is specified with: ID · Priority · Purpose · User value · Inputs · Outputs · Internal behaviour · Dependencies · Failure modes · Edge cases · Acceptance criteria · Test requirements.

---

## FR-1 — Instrumentation SDK  `P0`

**Purpose.** Capture every coordination-relevant action of an agent graph as events, with zero required changes to prompt or agent logic. `[SOURCE]`

**User value.** One line of code turns an existing LangGraph app into a subject AgentDX can reason about. (P1, P2, P6)

**Inputs.** A LangGraph `StateGraph`/compiled graph, or arbitrary Python functions decorated with `@agentdx.agent(...)`; runtime configuration (`AgentDXConfig`); the ambient run context.

**Outputs.** A stream of events conforming to §9's schema, written through the event writer; an OTel span stream (P1, §30).

**Internal behaviour.**
1. `agentdx.instrument(graph)` wraps the compiled graph and attaches a callback adapter (§8.3).
2. Node entry/exit → `span_start`/`span_end` with `agent_id` resolved from the node name.
3. LLM client calls → intercepted at the provider shim (§8.5) → `llm_call` span + cache interaction.
4. Tool invocations → `tool_call` span with `args_hash`.
5. Channel/state reads and writes → `state_read`/`state_write` with `key`, `value_hash`, `prev_value_hash`, and the channel's declared reducer if any.
6. Edge traversal / node-to-node payload → `message_send` on the producing side, `message_recv` on the consuming side.
7. Every event is stamped by the runtime with `seq`, `vclock`, `virtual_ts_ms`, `wall_ts_ms`, `causal_parents`.

**Dependencies.** FR-2 (the runtime supplies seq/clock/vclock), §9 event schema, §27 event store.

**Failure modes.**

| Mode | Handling |
|---|---|
| Graph uses an unsupported LangGraph construct | Emit `instrumentation_gap` event naming the construct; continue; surface as an analysis warning, never a crash |
| User calls an LLM through an un-shimmed client | Detected by absence of `llm_call` spans inside an `agent_step`; `agentdx doctor` reports it |
| Event writer backpressure | Bounded in-memory queue (default 10 000); on overflow, block the scheduler rather than drop events — **dropping events is never acceptable** |
| Exception inside a user node | Captured as `span_end` with `status=error` + exception type and message (not the full traceback unless body capture is on) |

**Edge cases.** Nested graphs/subgraphs (agent_id must be path-qualified, e.g. `research/worker_2`); the same node executed multiple times (span sequence, not identity, distinguishes them); streaming LLM responses (a single `llm_call` span; token-level events are out of scope); tools that spawn threads (rejected — see §10.6).

**Acceptance criteria.**
- FR1-AC1: The reference LangGraph app is instrumented with exactly one added line.
- FR1-AC2: All five event families (`span_*`, `message_*`, `state_*`, `tool_call`, `llm_call`) appear for the code-pipeline fixture.
- FR1-AC3: Passthrough overhead <10% wall clock, measured per §34.1. `[SOURCE]`
- FR1-AC4: No prompt or response body appears in the event log unless `capture_bodies=True`. `[SOURCE]`

**Test requirements.** Unit tests per interceptor; a golden-event test asserting exact event sequence for a 3-node fixture; an overhead benchmark in CI with a regression threshold; a privacy test asserting no plaintext prompt substring exists in the DB after a default run.

---

## FR-2 — Deterministic scheduler + virtual clock  `P0` ⭐

**Purpose.** Agents do not run on the real event loop; they run on an AgentDX cooperative scheduler, so that `(scenario, seed)` fully determines the interleaving. `[SOURCE]`

**User value.** Failures become reproducible; chaos matrices become cheap; the same bug can be handed to a colleague as a seed.

**Inputs.** Seed (int); the set of runnable agent tasks; a calibration profile mapping span kinds to virtual durations; fault schedule; optional delay schedule from the exploration engine.

**Outputs.** A total order of scheduling decisions; monotonic `seq`; `virtual_ts_ms` on every event; the executed `schedule` record.

**Internal behaviour.**
1. Every agent runs as a cooperative task; the scheduler owns the run loop.
2. At each scheduling point the set of runnable tasks is computed and **sorted by a stable key** (`(virtual_ready_ts, agent_id, task_seq)`); the seeded RNG breaks any remaining ties and makes non-priority choices. `[SOURCE]`
3. The **virtual clock** advances to the earliest ready time when no task is runnable now. Injecting "+500 ms latency" advances virtual time, not real time. `[SOURCE]`
4. A **wall-clock sampling/calibration mode** records real per-span durations so virtual timings stay realistic. `[SOURCE]`
5. All ambient non-determinism is trapped: `random`, `time`, `uuid`, `asyncio` sleeps and the event loop (§10.5).

**Dependencies.** None upward; everything else depends on it.

**Failure modes.**

| Mode | Handling |
|---|---|
| Deadlock (no runnable task, no pending timer) | Detected in one scheduler tick; run ends `FAILED` with `E-SCHED-002` and a dump of each task's wait reason |
| Determinism leak (user code reads wall clock or spawns a thread) | Leak detector (§10.6) emits `nondeterminism_warning` and, in `--strict` mode, aborts |
| Livelock (virtual clock not advancing over N steps) | `E-SCHED-003` after 10 000 steps with no virtual-time advance |
| Starvation | Not prevented by design (priority order is deterministic), but reported: an agent runnable for >50% of makespan without being scheduled raises an analysis warning |

**Edge cases.** Zero-duration spans (must still get distinct `seq`); simultaneous ready times (broken by stable sort, then seeded RNG); a task that never yields (bounded by a per-step virtual budget); nested awaits inside user code (permitted only through the AgentDX-provided awaitables).

**Acceptance criteria.**
- FR2-AC1: Same `(scenario, seed)` → identical schedule and identical canonical event log, 100/100 runs. `[SOURCE]`
- FR2-AC2: Different seeds produce at least two distinct interleavings on the code-pipeline fixture (proving the seed is actually load-bearing).
- FR2-AC3: A scenario with 30 s of injected virtual latency completes in <1 s wall time. `[SOURCE]`
- FR2-AC4: Virtual makespan is within ±10% of measured wall makespan on a calibrated run (§10.4).

**Test requirements.** 100× replay-equality test in CI from week 3 `[SOURCE]`; a property test over random seeds asserting `schedule(seed) == schedule(seed)`; a deadlock fixture; a determinism-leak fixture that must be caught; a virtual/wall calibration test.

---

## FR-3 — LLM record/replay cache  `P0` ⭐

**Purpose.** Determinism is impossible while the model is live; solve it the way HTTP test suites do. `[SOURCE]`

**User value.** Runs become free, offline and repeatable after one recording pass. This is what makes the no-API-key demo and hermetic CI possible.

**Inputs.** `(model, canonical messages, parameters)`; the cache mode (`record` \| `replay` \| `perturb` \| `passthrough`); the cache database.

**Outputs.** A response (from the provider or the cache); a `llm_call` event with `cache_status` ∈ {`hit`,`miss_recorded`,`miss_error`,`perturbed`}; token counts; the cache key.

**Internal behaviour.**
- **Record mode:** hash `(model, messages, params)` → call the provider → store the response. `[SOURCE]` Cache lives in SQLite. `[SOURCE]`
- **Replay mode:** serve from cache; **a cache miss is a hard error, not a silent live call.** `[SOURCE]`
- **Perturb mode:** deliberately return a cached response from a *different* run, to simulate a byzantine or drifting agent. `[SOURCE]` Selection is seeded, so perturbation is itself deterministic.
- Key construction, canonicalisation and privacy rules: §11.4.

**Dependencies.** FR-2 (cache lookups occur at deterministic points); §27 storage.

**Failure modes.**

| Mode | Handling |
|---|---|
| Cache miss in replay | `E-CACHE-001`, exit 3, message contains the key prefix, the agent, the model, and the exact `--record` command to fix it |
| Provider error in record mode | Retried per policy, then `E-LLM-001`; partial cache is retained (records are committed per call, not per run) |
| Non-determinism inside the provider (e.g. `temperature>0` during record) | Allowed — the *recording* is the fixed point. A warning is emitted if `temperature>0` and `--record` is used, since re-recording will produce a different cache |
| Cache key collision | Cryptographic hash; treated as impossible. A stored `key_material_hash` allows detection if it ever occurs |
| Prompt exceeds storage limits | Body stored compressed; if `capture_bodies=False`, only hashes are stored and the response body is stored separately in the cache DB with the same privacy controls (§11.9) |

**Edge cases.** Streaming responses (recorded as the concatenated final message plus the chunk boundaries, so replay can reproduce streaming timing); tool-calling responses (recorded verbatim including tool-call structures); multi-modal inputs (P2 — v1 hashes non-text parts by content digest); identical prompts issued twice in one run (same key → same response; this is correct and also the mechanism by which redundancy becomes visible).

**Acceptance criteria.**
- FR3-AC1: After one record pass, a full fixture run completes offline with `GROQ_API_KEY` unset. `[SOURCE]`
- FR3-AC2: A deliberately removed cache row causes a hard error, never a live call. Verified by running with network disabled.
- FR3-AC3: Perturb mode changes at least one agent's output while leaving the schedule seed unchanged, and is reproducible under the same seed.
- FR3-AC4: Cache hit rate is reported in the run metadata and displayed in the scorecard's comparability section.

**Test requirements.** Key-stability tests (same logical prompt → same key across process restarts, dict orderings and Python versions); a network-disabled replay test; a perturb determinism test; a cache-migration test across schema versions.

---

## FR-4 — Fault injection engine  `P0`

**Purpose.** Inject a defined catalogue of faults without modifying user agent code, at the scheduler layer, under declarative control. `[SOURCE]`

**User value.** Reliability becomes an experiment rather than an anecdote.

**Inputs.** Fault specifications from the scenario (`type`, `target`, trigger, parameters); the scheduler's virtual clock; the blast radius; abort guards.

**Outputs.** `fault_injected` events; modified execution (delays, dropped messages, raised exceptions, substituted responses); `fault_id` taint on downstream events; a fault timeline for the UI.

**Internal behaviour.** The injector registers **interception points** in the scheduler and the transports (§12.3). At each point it evaluates armed faults whose trigger matches (virtual time, event count, span kind, or predicate) and whose target is inside the blast radius. Firing is recorded before the effect is applied, so the log always explains the behaviour that follows.

**MVP fault set (P0):** `latency`, `agent_crash`, `message_drop`, `tool_failure`.
**P1 fault set:** `message_reorder`, `message_duplicate`, `agent_slow`, `rate_limit`, `byzantine`, `state_corrupt`. `[SOURCE]`

**Dependencies.** FR-2 (all faults are scheduled in virtual time), FR-11a (scenario schema), §13 (safety).

**Failure modes.**

| Mode | Handling |
|---|---|
| Target outside blast radius | Validation error at scenario load; never at runtime |
| Fault never fires (trigger unreachable) | `fault_not_triggered` recorded in run metadata and shown in the UI — a silently un-fired fault would invalidate the experiment |
| Fault makes the run un-completable | Guards abort; run marked `ABORTED_GUARD` |
| Crash fault applied to the only remaining agent | Permitted, but the resulting run is classified `TOTAL_FAILURE` rather than scored as resilience |

**Edge cases.** Two faults targeting the same agent at the same virtual timestamp (applied in declaration order, deterministically); a `latency` fault on an edge that is never traversed; `agent_crash` with `recoverable=true` (agent restarts with cleared local state but retained shared state); a `message_drop` that removes the only path to run completion (the supervisor's timeout path must then be exercised — this is the point).

**Acceptance criteria.**
- FR4-AC1: `agent_crash` on the reviewer at `t=2400` produces the documented cascade in the event log. `[SOURCE]`
- FR4-AC2: Every fired fault has exactly one `fault_injected` event, and every causally downstream event carries its `fault_id`.
- FR4-AC3: Injection is deterministic: the same scenario and seed inject at the same virtual timestamps and the same sequence numbers, 100/100.
- FR4-AC4: No fault can fire against a target outside the declared blast radius (property test).

**Test requirements.** One unit test per fault type asserting the expected effect and expected event; a taint-propagation test; a blast-radius property test; a determinism test over faulted runs.

---

## FR-5 — Race and state-conflict detection  `P0` ⭐

**Purpose.** Classic dynamic race detection applied to agent shared state. `[SOURCE]`

**User value.** Finds the class of defect that is otherwise invisible — silent lost updates and stale reads across agents.

**Inputs.** The event log's `state_read` / `state_write` / `message_send` / `message_recv` events, with vector clocks; declared reducers, locks and transactions.

**Outputs.** `Finding` records of type `state_conflict` with subtype `write_write` \| `read_write` \| `write_read`; the two spans; the key; both value hashes; both vector clocks; a minimal reproducing interleaving. `[SOURCE]`

**Internal behaviour.** Full specification in §14. In brief:
1. Maintain vector clocks per agent, updated on every message send/receive. `[SOURCE]`
2. For each shared-state key track the last write and the per-agent read set with their vector clocks. `[SOURCE]`
3. Flag a conflict when two accesses to the same key are concurrent (neither happens-before the other) and at least one is a write. `[SOURCE]`
4. Classify and report with a minimal reproduction. `[SOURCE]`
5. Apply the four false-positive guards (§14.7) **before** emitting.

**Dependencies.** §9 (vector clocks in the event schema), FR-1 (state ops captured), FR-2 (deterministic ordering).

**Failure modes.**

| Mode | Handling |
|---|---|
| Missing vector clock on an event | Schema validation rejects the event at write time; the detector never sees partial data |
| Agent set changes mid-run (dynamic spawn) | Vector clocks are sparse maps, not fixed-width vectors; a new agent starts at zero and is comparable |
| Key cardinality explosion (state keyed by UUID) | Per-key access lists are pruned to the last write plus one read per agent (FastTrack-style); memory is O(agents × live keys) |
| Value hashes unavailable (non-serialisable state) | Conflict is reported at reduced confidence with `value_divergence=unknown`, and is severity-capped at `warning` |

**Edge cases.** Idempotent writes of the same value (suppressed — no divergence); writes to a channel with a declared reducer (`benign_merge`, suppressed, but counted and shown in a "suppressed" drawer so the user can audit the suppression); reads with no prior write (no conflict possible); a key written once and read by many (no conflict); the same agent racing itself across two concurrent sub-tasks (**is** a conflict — vector clocks are per-agent, so §14.2 assigns sub-tasks their own clock slots).

**Acceptance criteria.**
- FR5-AC1: Detects the seeded write-write race in the code-pipeline fixture on a default run. `[SOURCE]`
- FR5-AC2: Reports **zero** findings on the healthy research fan-out fixture. `[SOURCE]`
- FR5-AC3: Every finding carries both event sequence numbers and a reproduction scenario that re-triggers it.
- FR5-AC4: Concurrent writes through a declared reducer are suppressed and auditable.

**Test requirements.** A synthetic event-log suite with hand-computed expected verdicts (12 cases minimum: true positives per subtype, and the false-positive families of §33.9); a fuzz test over generated logs asserting no crash and no finding without evidence; a memory test at 10 000 keys.

---

## FR-6 — Bounded schedule exploration  `P1`

**Purpose.** Full state-space exploration is intractable; do delay-bounded systematic exploration (the CHESS approach). `[SOURCE]`

**User value.** Finds concurrency bugs that a single interleaving misses, without claiming exhaustiveness.

**Inputs.** A root run (scenario + seed); delay bound `k` (default 2 `[SOURCE]`); schedule cap `N` (default 200 `[SOURCE]`); a time budget.

**Outputs.** A set of child runs; aggregate report — interleavings explored, unique schedules, conflicts found, coverage statement, and the honesty caveat. `[SOURCE]`

**Internal behaviour.** Specified in §15. Enumerate interleavings reachable within `k` scheduler delays; deduplicate by schedule signature; apply partial-order reduction to skip provably equivalent schedules; cap at `N`.

**Dependencies.** FR-2 (schedules are first-class), FR-5 (findings are the payoff), FR-3 (each child run must be free — this is only affordable because of the cache).

**Failure modes.**

| Mode | Handling |
|---|---|
| Combinatorial blowup | Hard cap `N`; report states explored vs estimated reachable |
| Time budget exceeded | Partial results reported with explicit coverage; never presented as complete |
| A child run diverges (different agent set) | Recorded as `schedule_infeasible`, excluded from coverage counts, not an error |

**Edge cases.** A graph with no concurrency (exploration finds exactly one schedule and says so); a schedule that deadlocks (recorded as a finding — a reachable deadlock is a real defect); non-deterministic user code detected during exploration (aborts exploration with a determinism error, because the results would be meaningless).

**Acceptance criteria.**
- FR6-AC1: Finds the code-pipeline race within `k=2`. `[SOURCE]`
- FR6-AC2: Explores ≤ `N` schedules and terminates within the declared budget, always.
- FR6-AC3: Duplicate schedules are never executed twice (assert via signature set).
- FR6-AC4: Every report — CLI, API and UI — carries the statement: *"Bounded search: absence of findings is not proof of absence."* `[SOURCE]`

**Test requirements.** A fixture with a known bug at `k=1` and another at `k=2`; a determinism test that the same `(root, k, N)` explores the same schedule set in the same order; a reduction-correctness test asserting that reduced-away schedules are genuinely equivalent on a small enumerable example.

---

## FR-7 — Critical path + overhead decomposition  `P0` ⭐

**Purpose.** Build the span DAG, compute the longest weighted path, and decompose wall clock into productive work versus coordination overhead. `[SOURCE]`

**User value.** Turns "it feels slow" into a budget with named line items and a named worst offender.

**Inputs.** The event log (spans, messages, state ops, faults); the agent role map; virtual durations.

**Outputs.** An ordered critical path (list of spans with virtual start/end); six-bucket decomposition with absolute virtual ms and percentages; a residual/unattributed figure; per-edge handoff aggregates; redundancy groups; parallelism metrics (average parallelism, pairwise overlap matrix).

**Internal behaviour.** Fully specified in §16. Buckets `[SOURCE]`:

| Bucket | Definition |
|---|---|
| **Productive work** | LLM inference + tool execution on the critical path |
| **Handoff latency** | Time between message send and receiver span start |
| **Blocking wait** | Agent idle, awaiting a dependency |
| **Redundant work** | Duplicate tool/retrieval calls detected by argument hash |
| **Retry / recovery** | Time spent re-running after a fault |
| **Orchestration** | Supervisor/router deliberation not doing task work |

**Dependencies.** FR-1, FR-2, §9.

**Failure modes.**

| Mode | Handling |
|---|---|
| Cycle in the timing DAG | Rejected with `E-ANLZ-002`; a cycle indicates an instrumentation defect (a span depending on its own descendant). The offending edge is named |
| Residual >2% | Verdict still produced, tagged `analysis_warning: unattributed_time`; the residual bucket is **shown**, not hidden |
| Missing `span_end` (crashed agent) | Span closed at the fault's virtual timestamp with `status=crashed`; duration is attributed to `retry_recovery` if a retry follows, else to productive work up to the crash |
| Zero-length run | Analysis returns an empty decomposition and verdict `INSUFFICIENT_DATA` |

**Edge cases.** Overlapping spans within one agent (permitted only for a parent `agent_step` containing child `llm_call`/`tool_call`; double counting is prevented by charging only leaf spans); a single-agent run (critical path = the whole run; decomposition still valid; handoff bucket is 0); ties for longest path (broken deterministically by `(end_seq, span_id)` so the reported path is stable across runs).

**Acceptance criteria.**
- FR7-AC1: Buckets + residual sum to virtual makespan within ±2% on all three fixtures. `[SOURCE]`
- FR7-AC2: On a hand-constructed synthetic log, the computed critical path equals the analytically derived one.
- FR7-AC3: The support-triage fixture reports `FAKE_FANOUT` with average parallelism <1.5.
- FR7-AC4: Redundant retrieval in the support-triage fixture is detected with the wasted virtual ms and tokens quantified.

**Test requirements.** Golden tests over synthetic logs with known answers; a summation invariant test (property-based, over generated logs); a determinism test that the reported critical path is identical across 100 analyses of the same log.

---

## FR-8 — Single-agent baseline + speedup verdict  `P0` ⭐ *(headline feature)*

**Purpose.** Auto-generate a single-agent baseline and report a defensible speedup number with attribution. `[SOURCE]`

**User value.** The one output that answers the product's central question.

**Inputs.** A completed multi-agent run; the task definition; the tool registry; the model identity; the seed; the LLM cache.

**Outputs.** A baseline run; the scorecard block (§17.4); verdict class, severity, confidence, recommendation; comparability grade.

**Internal behaviour.** Specified in §17. The baseline must use the same task, same model, same tools, same relevant cached responses, comparable inputs and equivalent evaluation conditions. `[SOURCE]` Run both under identical seeds and the same cached LLM responses where possible. `[SOURCE]`

Target output `[SOURCE]`:

```
Coordination Efficiency:  0.83×   ⚠  slower than single-agent
─────────────────────────────────────────────────
Ideal parallel speedup      2.40×   (total work / critical path)
Achieved speedup            0.83×
Overhead cost              -1.57×
  handoff latency           -0.71×
  blocking wait             -0.52×
  redundant tool calls      -0.24×
  orchestration             -0.10×

Token cost multiplier       3.1×    vs single-agent
Verdict: merge `reviewer` into `coder`; the handoff on that
edge accounts for 61% of critical-path time.
```

> **Correction carried from v1.0** `[IMPROVEMENT]`: the source annotates ideal parallel speedup as *(critical path / total work)*. That ratio is ≤1 by construction and cannot yield 2.40×. The correct definition is **total work ÷ critical-path length** (classic average parallelism). The displayed number, the example and the intent are unchanged; only the parenthetical formula is corrected. See Appendix C.

**Dependencies.** FR-3 (cache reuse is what makes the baseline cheap and comparable), FR-7 (ideal speedup needs the critical path), FR-2.

**Failure modes.**

| Mode | Handling |
|---|---|
| Baseline cannot be constructed (task not expressible single-agent) | `E-BASE-002`; skip baseline, still report critical path and overhead. Partial value, never a crash |
| Cache reuse rate <40% | Comparability grade **C**; the speedup is reported with a prominent low-comparability badge and excluded from CI assertions by default |
| Baseline fails the task while multi-agent succeeds | Reported explicitly: speedup is meaningless when the baseline did not do the job. Verdict becomes `BASELINE_FAILED` with the note that the topology may be justified on capability rather than speed |
| Multi-agent fails, baseline succeeds | Verdict `NEGATIVE_CAPABILITY` — the strongest possible negative result, reported as such |

**Edge cases.** Tools with side effects (the baseline must not re-execute destructive tools — see §13.9: baseline runs inherit the same blast radius and sandbox); tasks whose prompt exceeds the single-agent context window (reported as `BASELINE_CONTEXT_EXCEEDED`, which is itself a legitimate justification for the topology and is stated as one); non-deterministic task success.

**Acceptance criteria.**
- FR8-AC1: The scorecard prints from the terminal on the code-pipeline fixture. `[SOURCE]`
- FR8-AC2: Achieved speedup on code-pipeline is <1.0; on research fan-out it is >1.0.
- FR8-AC3: Bucket attributions sum to the total overhead cost within 0.02×.
- FR8-AC4: The comparability grade and cache reuse rate appear alongside every speedup number, in every surface.

**Test requirements.** A synthetic pair of runs with analytically known speedup; a comparability-grading test at reuse rates 0.9/0.6/0.3; a golden-output test on the scorecard formatting.

---

## FR-9 — Resilience scoring  `P1`

**Purpose.** Per fault scenario: success rate under fault ÷ baseline success rate, plus recovery time (virtual), retry amplification, and whether degradation was graceful or silent. `[SOURCE]`

**User value.** Converts chaos results into a number a team can gate on — without letting the number hide the detail.

**Inputs.** A baseline (no-fault) run; one or more fault runs; the `success_check` assertion result per run; token and retry counts.

**Outputs.** Per-fault records (success ratio, recovery time, amplification, degradation class) and a 0–100 aggregate **with the per-fault breakdown always visible; never show the aggregate alone**. `[SOURCE]`

**Internal behaviour.** Formulas in §19.

**Dependencies.** FR-4, FR-11a, and the assertion hook.

**Failure modes / edge cases.** Aborted runs are not scored; a baseline that itself fails invalidates the whole experiment (abort before injecting); a fault that never fires is excluded and flagged; `SILENT_FAILURE` in any fault caps the aggregate at 49 (§19.6).

**Acceptance criteria.** FR9-AC1: the aggregate never appears in any surface without its breakdown. FR9-AC2: a seeded silent failure caps the score. FR9-AC3: scores are deterministic given the same runs.

**Test requirements.** Formula unit tests with hand-computed values; a rendering test asserting breakdown presence; a cap test.

---

## FR-10 — Replay and time travel  `P1` *(replay engine is P0; the scrubbing UI is P1)*

**Purpose.** Scrub any run on the virtual timeline; graph, state table and message log all reflect the selected instant; jump directly to any detected conflict; export a `.agentdx` bundle that reproduces exactly on another machine. `[SOURCE]`

**Inputs.** A completed run's event log; optionally a `.agentdx` bundle.

**Outputs.** Reconstructed state at any virtual timestamp; synchronised UI panels; bundle export/import; a verification result on `--verify`.

**Internal behaviour.** §20. State reconstruction is a fold over events up to the selected `seq`, accelerated by periodic snapshots (§20.4).

**Dependencies.** FR-2, FR-3, §9.

**Failure modes.** Bundle from an incompatible schema version → migration attempted, else `E-BUNDLE-002` naming both versions. Missing cache slice → replay fails hard rather than calling a model. Verification mismatch → `E-REPLAY-001` with a diff of the first divergent event.

**Acceptance criteria.** FR10-AC1: a bundle exported on machine A replays byte-identically (canonical projection) on machine B. FR10-AC2: scrubbing to any timestamp reconstructs state in <100 ms for a 5 000-event run. FR10-AC3: `j`/`k` jump between findings; `←`/`→` step events; `shift` ×10. `[SOURCE]`

**Test requirements.** Cross-platform bundle test in CI (macOS + Linux runners); snapshot-correctness test (fold from scratch == fold from snapshot); performance test at 5 000 and 50 000 events.

---

## FR-11a — Scenario definition  `P0`  ·  FR-11b — CI mode  `P1`

*Split per the scope decision in §4.5.* `[IMPROVEMENT]`

**Purpose.** A declarative, validated, versioned description of what to run, what to break, what must hold, and what to assert. `[SOURCE]`

**Inputs.** A YAML file per §21's schema.

**Outputs.** A resolved scenario object; a run; assertion results; machine-readable output and exit codes in CI mode.

**Internal behaviour.** §21 (schema, validation, defaults, versioning) and §22 (CI execution, exit codes, artifacts, regression comparison). Source example preserved verbatim in §21.2. `[SOURCE]` `agentdx run scenarios/ --ci` exits non-zero on assertion failure and ships with a GitHub Action example. `[SOURCE]`

**Failure modes.** Unknown key → error, not warning. Missing blast radius with faults on a user graph → error. Assertion referencing an uncomputed metric → error naming the required analysis.

**Acceptance criteria.** FR11-AC1: an invalid scenario fails before any execution. FR11-AC2: exit codes match §37.2 exactly. FR11-AC3: 100 CI runs on unchanged code → zero flake.

**Test requirements.** Schema conformance suite (valid + 20 invalid cases); exit-code matrix test; a GitHub Actions integration test running the shipped workflow.

---

## FR-12 — Reference fixture systems  `P0`

**Purpose.** Three deliberately imperfect multi-agent systems shipped in-repo, each seeded with a *known* defect so the demo always finds something real. `[SOURCE]`

1. **Code pipeline** (planner → coder → reviewer → tester) — contains a genuine write-write race on the shared draft key. `[SOURCE]`
2. **Support triage** (classifier → retriever ×2 → responder) — contains redundant retrieval; the parallel fan-out is fake because both branches await the same classifier. `[SOURCE]`
3. **Research fan-out** (supervisor → 4 workers → synthesiser) — genuinely parallel; **use it to prove AgentDX doesn't cry wolf. A tool that only ever reports problems is a tool nobody trusts.** `[SOURCE]`

Full specification in §23, including agents, tools, state keys, the exact seeded defect, and the exact expected findings.

**Acceptance criteria.** FR12-AC1: each fixture runs offline from the committed cache. FR12-AC2: fixtures 1 and 2 produce their documented findings and no others above `info` severity. FR12-AC3: fixture 3 produces **zero** findings above `info`. FR12-AC4: fixture runs are the regression suite — a change that alters their findings fails CI.

**Test requirements.** Golden findings files per fixture, diffed in CI.

---

## FR-13 — Report export  `P2`

**Purpose.** A one-page HTML/PDF run report: scorecard, graph snapshot, waterfall, findings list; shareable as a link in a PR comment. `[SOURCE]`

**Inputs.** A completed run. **Outputs.** A self-contained HTML file (no external assets) and optionally a PDF.

**Internal behaviour.** Server-side render of the same components used by the Control Tower, with interactivity stripped and SVG inlined.

**Acceptance criteria.** FR13-AC1: the HTML opens with no network access and contains the verdict, the scorecard, the ghost-baseline waterfall and all findings. FR13-AC2: file size <2 MB for a 5 000-event run.

---

# 8. Instrumentation SDK Specification

This section is written so a Python engineer can implement the SDK without reference to any other document.

## 8.1 Package structure

```
agentdx/
├── __init__.py               # public surface: instrument, agent, tool, run, lock, transaction
├── config.py                 # AgentDXConfig, env resolution, precedence rules
├── context.py                # RunContext, AgentContext; contextvars propagation
├── events/
│   ├── schema.py             # dataclasses + JSON Schema + validators (§9)
│   ├── writer.py             # buffered, ordered, append-only writer
│   └── canonical.py          # canonical projection + serialisation (§10.7)
├── sdk/
│   ├── decorators.py         # @agentdx.agent, @agentdx.tool, @agentdx.state
│   ├── langgraph.py          # LangGraph callback adapter + channel interception
│   ├── generic.py            # plain-Python instrumentation helpers
│   ├── providers/            # LLM client shims (openai-compatible, groq, anthropic)
│   └── sync.py               # lock(), transaction(), barrier()
└── otel/
    └── exporter.py           # OTel GenAI span emission (P1, §30)
```

## 8.2 Public surface (complete)

```python
import agentdx

# 1. Graph-level instrumentation — the one-line path
graph = agentdx.instrument(
    compiled_graph,                    # LangGraph CompiledGraph, or any supported object
    name="code_pipeline",
    capture_bodies=False,              # privacy default (§31.2)
    agent_from=lambda node_name: node_name,   # node → agent_id mapping
)

# 2. Decorator path — for plain Python / non-LangGraph systems
@agentdx.agent("coder", role="worker")
async def coder(state: dict) -> dict: ...

@agentdx.tool("vector_search")
async def vector_search(query: str, k: int = 5) -> list[str]: ...

# 3. Explicit state access (only needed when state is not a LangGraph channel)
async with agentdx.state() as s:
    plan = await s.read("plan")
    await s.write("draft.module_a", value)

# 4. Synchronisation primitives — teach the race detector about your intent
async with agentdx.lock("draft.module_a"):
    ...
async with agentdx.transaction("plan_update") as txn:
    await txn.write("plan", p); await txn.write("constraints", c)

# 5. Programmatic run control (what the CLI calls)
result = await agentdx.run(graph, task="...", scenario="scenarios/x.yaml", seed=42)
```

**Design constraint** `[SOURCE]`: items 1 and 2 require **zero changes to prompt or agent logic**. Items 3–4 are optional refinements that *reduce* false positives; the product must be useful without them.

## 8.3 LangGraph callback adapter — exact integration `[DETAIL]`

`agentdx.instrument(compiled_graph)` performs five bindings. None modifies user node functions.

| # | Binding | Mechanism | Events produced |
|---|---|---|---|
| 1 | **Node lifecycle** | A `BaseCallbackHandler` subclass registered on the graph's config, plus a wrapper around each node's callable in the compiled graph's node registry | `span_start`/`span_end` (`kind=agent_step`) |
| 2 | **Channel writes** | Wrap each channel object's `update`/`invoke` with a recording proxy. The proxy records the key, the value hash, the previous value hash and the channel's declared reducer | `state_write` |
| 3 | **Channel reads** | Wrap the state-read path that assembles a node's input into a recording view; a key is recorded as read the first time a node accesses it | `state_read` |
| 4 | **Edge traversal** | The scheduler records the transition from producing node to consuming node as a message with the serialised delta's hash and size | `message_send`, `message_recv` |
| 5 | **LLM / tool calls** | Provider shim (§8.5) plus LangChain callback events (`on_llm_start/end`, `on_tool_start/end`) | `llm_call`, `tool_call` spans |

**Why the proxy approach rather than monkey-patching LangGraph internals:** proxies survive minor version changes and fail loudly (an unwrapped channel produces an `instrumentation_gap` event) rather than silently producing an incomplete log. An incomplete log would produce *wrong analysis*, which is worse than no analysis.

`[OPEN] §43.2.1` — the exact LangGraph version range to pin, and whether to support the imperative `@entrypoint`/functional API in v1.

**Reducer awareness is mandatory.** LangGraph channels annotated with a reducer (e.g. `Annotated[list, operator.add]`) are *designed* for concurrent writes. The adapter records `reducer="operator.add"` on every `state_write` to such a channel, and the race detector suppresses conflicts on reduced channels as `benign_merge` (§14.7). **Without this, AgentDX would report a false positive on almost every real LangGraph application** — this is the single highest-risk false-positive source in the product.

## 8.4 Generic Python instrumentation

For systems not using LangGraph, `@agentdx.agent(...)` wraps a coroutine (or function) and:
1. Establishes an `AgentContext` in a `contextvar` for the duration of the call.
2. Emits `span_start`/`span_end`.
3. Registers the agent in the run's agent set (allocating a vector-clock slot on first use).
4. Makes nested `@agentdx.tool` calls and provider-shim LLM calls attribute to this agent automatically.

Message passing in generic mode is explicit: `await agentdx.send(to="reviewer", payload=x)` / `await agentdx.recv()`. This is the one place where generic mode requires code the user would not otherwise write, and it is required because **without explicit send/recv there is no happens-before edge and race detection cannot work**. This trade-off is documented in the SDK docs, not hidden.

## 8.5 Provider shim and event interception

A thin wrapper around the OpenAI-compatible client interface (which Groq, OpenAI, Together and vLLM all implement) that:
1. Canonicalises `(model, messages, params)` into a cache key (§11.4).
2. Consults the cache per the active mode.
3. Emits an `llm_call` span with `cache_status`, `prompt_tokens`, `completion_tokens`, `model`, `params_hash`, `prompt_hash`, `response_hash`.
4. Yields control to the scheduler around the call so that concurrency is scheduler-visible even in passthrough mode.

`[IMPROVEMENT]` **Do not hard-couple to the Groq SDK.** PRD v1.0 names Groq/Llama 3.1 8B as the model layer `[SOURCE]`; that stays as the *default recording configuration*, but the shim targets the OpenAI-compatible surface so that a model deprecation cannot break the product. Trade-off: a small abstraction cost now; the alternative is a hard dependency on one vendor's SDK for a project whose entire premise is that the model layer is replayed from cache anyway. **Recommendation: implement the shim against the OpenAI-compatible interface; ship Groq as the default provider config.**

## 8.6 Lifecycle hooks

| Hook | When | Typical use |
|---|---|---|
| `on_run_start(ctx)` | After run record created, before first schedule | Register custom tools; emit run-level metadata |
| `on_agent_start(ctx, agent_id)` | First span for an agent | Attach domain tags |
| `on_span_end(ctx, span)` | Every span close | Custom timing attribution |
| `on_fault(ctx, fault)` | Immediately after `fault_injected` | User-defined compensating behaviour under test |
| `on_run_end(ctx, result)` | After the log is sealed, before analysis | Emit the `success_check` result |

Hooks are synchronous, must not perform I/O, and must not mutate state — enforced by running them under a guard that raises on any event emission or state write.

## 8.7 Configuration

Precedence (highest wins): CLI flag → environment variable → `agentdx.toml` → `AgentDXConfig` argument → default.

```toml
# agentdx.toml
[run]
seed = 42
mode = "replay"                # record | replay | perturb | passthrough
data_dir = "~/.agentdx"

[privacy]
capture_bodies = false         # NFR-6: hashes only by default
redact_patterns = ["sk-[A-Za-z0-9]{20,}", "AKIA[0-9A-Z]{16}"]

[scheduler]
strict_determinism = true      # abort on determinism leaks
step_budget = 100000

[llm]
provider = "groq"
model = "llama-3.1-8b-instant"
base_url = "https://api.groq.com/openai/v1"

[analysis]
residual_tolerance = 0.02
redundancy = "exact_hash"      # exact_hash only in v1
```

## 8.8 Context propagation and identity

- `RunContext` (run_id, seed, mode, clock, writer) and `AgentContext` (agent_id, current span stack, vector clock slot) live in `contextvars`, so `asyncio` tasks inherit correctly and no explicit threading of parameters is required.
- **Agent identity** is a stable string. For LangGraph it defaults to the node name; for subgraphs it is path-qualified (`research/worker_2`). Identity must be stable across runs or baseline comparison breaks.
- **Span identity** is `sha1(run_id ‖ agent_id ‖ span_seq)[:12]`, deterministic and therefore stable across replays.
- Concurrent sub-tasks within one agent receive derived clock slots (`coder#1`, `coder#2`) so that an agent racing itself is detectable (§14.2).

## 8.9 Error capture

`span_end.status ∈ {ok, error, crashed, timeout, cancelled}`. On exception: `error_type` (class name) and `error_message` (truncated to 512 chars, redacted per `redact_patterns`) are recorded. The full traceback is recorded **only** when `capture_bodies=True`. Exceptions always propagate to the user's code unchanged — instrumentation never swallows an error.

## 8.10 Performance overhead

**Budget: <10% wall clock in passthrough mode.** `[SOURCE]` Implementation constraints that make this achievable:
- Event construction is a `__slots__` dataclass; serialisation happens on the writer thread, not the hot path.
- Hashing uses `blake2b` (faster than sha256) internally, with the digest labelled by algorithm in the event; `[IMPROVEMENT]` the source's `sha256:` prefix format is retained as a *format*, with the algorithm named explicitly so it can change without breaking the contract.
- Vector-clock updates are copy-on-write sparse dicts; typical size is the number of agents (≤10).
- The writer batches into SQLite with a single transaction per N events (default 64) or 50 ms, whichever first.
- No analysis runs during execution. All analysis is post-hoc over the log.

Measured and published per §34.1. `[SOURCE]`

## 8.11 Opt-in body capture and privacy behaviour

Default: **never write user prompt/response bodies to the event log; store hashes.** `[SOURCE]` (NFR-6.)

| Setting | Event log contains | LLM cache contains |
|---|---|---|
| `capture_bodies=False` (default) | `prompt_hash`, `response_hash`, token counts, model, params hash | Full bodies (required for replay), in a separate DB file with its own file permissions |
| `capture_bodies=True` | Bodies inline, after redaction | Same |

The cache necessarily contains bodies — replay is impossible otherwise. The distinction that matters is that the *event log* (which is exported, shared in bundles by default, and rendered in the UI) does not. Bundle export therefore has an explicit `--include-cache-bodies` flag, default off, with a warning printed when it is on (§31.3).

---

# 9. Event Model

> **Design rule (carried verbatim from PRD v1.0)** `[SOURCE]`: *the event log is append-only and is the single source of truth. Every analyser, the UI, and the replay engine read only from this log. No analyser touches live agent objects. This is what makes replay honest.*

The event schema is a **formal data contract**. It is versioned, validated on write, and changes to it are breaking changes requiring a migration (§9.9).

## 9.1 Source schema, preserved and extended

The v1.0 example `[SOURCE]`:

```jsonc
{
  "run_id": "r_f2a91",
  "seq": 1043,                    // monotonic, scheduler-assigned
  "vclock": {"planner": 12, "coder": 8, "reviewer": 3},
  "virtual_ts_ms": 2418,          // virtual clock, not wall clock
  "wall_ts_ms": 191,              // real elapsed, for overhead accounting
  "agent_id": "coder",
  "type": "state_write",
  "payload": { "key": "draft.module_a",
               "value_hash": "sha256:9f2c…",
               "prev_value_hash": "sha256:11ab…" },
  "causal_parent": 1039,
  "fault_id": null
}
```

Two changes are proposed, both explicitly, neither weakening the model:

**`[IMPROVEMENT]` 9.1.1 — `causal_parent` → `causal_parents: int[]`.**
*Trade-off.* A single parent cannot express a join: a synthesiser span that begins because four workers finished has four causal parents. Keeping a scalar forces the analyser to reconstruct joins heuristically, which weakens critical-path attribution at exactly the points that matter most (fan-in). Cost: a slightly larger event and an array index in SQLite. **Recommendation: adopt the array.** Single-parent events carry a one-element array; the migration is mechanical.

**`[IMPROVEMENT]` 9.1.2 — add `span_id`, `schema_version`, and `sched_step`.**
*Trade-off.* The source implies spans exist but gives events no span association, so the waterfall would have to infer it from ordering — brittle under concurrency. `sched_step` (the scheduler decision index) is what makes a schedule comparable across exploration children. `schema_version` is required for §9.9. Cost: three fields. **Recommendation: adopt all three.**

## 9.2 Canonical event schema (v1)

```jsonc
{
  "schema_version": 1,                  // int, required
  "run_id": "r_f2a91",                  // string, required
  "seq": 1043,                          // int, required, monotonic per run from 0
  "sched_step": 417,                    // int, required — scheduler decision index
  "virtual_ts_ms": 2418,                // int, required — virtual clock
  "wall_ts_ms": 191,                    // int, required — VOLATILE, excluded from canonical form
  "agent_id": "coder",                  // string|null (null for runtime-level events)
  "clock_slot": "coder",                // string|null — vector-clock slot (may be "coder#2")
  "vclock": {"planner": 12, "coder": 8}, // object<string,int>, required, sparse
  "type": "state_write",                // enum, required (§9.3)
  "span_id": "a3f19c22b0d1",            // string|null — the span this event belongs to
  "causal_parents": [1039],             // int[], required (may be empty for run_start)
  "fault_id": null,                     // string|null — taint from an injected fault
  "payload": { }                        // object, type-specific (§9.5)
}
```

**Field rules.**

| Field | Required | Volatile? | Notes |
|---|---|---|---|
| `schema_version` | yes | no | Rejects logs written by an incompatible SDK |
| `run_id` | yes | no | — |
| `seq` | yes | no | Assigned by the runtime under the scheduler lock; gapless |
| `sched_step` | yes | no | Multiple events may share a step (e.g. a span_end and the message_send it causes) |
| `virtual_ts_ms` | yes | no | Monotonic non-decreasing with `seq` |
| `wall_ts_ms` | yes | **yes** | Excluded from the canonical projection (§10.7) |
| `agent_id` | no | no | `null` for `run_start`, `run_end`, `fault_injected` at runtime scope |
| `clock_slot` | no | no | Defaults to `agent_id`; distinct for intra-agent concurrency |
| `vclock` | yes | no | Sparse map; omitted zero entries are implicitly 0 |
| `type` | yes | no | Closed enum; unknown types are a validation error |
| `span_id` | no | no | Required for all span-scoped events |
| `causal_parents` | yes | no | Every entry must be a `seq` < this event's `seq` |
| `fault_id` | no | no | Present iff this event is causally downstream of a fault |
| `payload` | yes | partial | Type-specific; certain sub-fields are volatile (e.g. `duration_wall_ms`) |

## 9.3 Event types (closed enum)

| Type | Scope | Emitted by | Purpose |
|---|---|---|---|
| `run_start` | run | runtime | Seed, mode, scenario id, graph identity, schema version |
| `run_end` | run | runtime | Status, virtual makespan, wall makespan, totals |
| `span_start` | span | SDK | Opens a span: `kind`, `name`, `agent_id` |
| `span_end` | span | SDK | Closes a span: `status`, durations, error info |
| `message_send` | span | SDK/runtime | Directed communication; **carries the vector clock** |
| `message_recv` | span | SDK/runtime | Receipt; **merges the vector clock** |
| `state_read` | span | SDK | Key, value hash |
| `state_write` | span | SDK | Key, value hash, previous value hash, reducer |
| `tool_call` | span | SDK | Tool name, args hash, result hash, status |
| `llm_call` | span | SDK | Model, prompt hash, response hash, tokens, cache status |
| `fault_injected` | run/span | fault injector | Fault id, type, target, parameters |
| `fault_effect` | span | fault injector | The concrete effect applied (delay ms, exception raised, message dropped) |
| `lock_acquire` / `lock_release` | span | SDK | Explicit synchronisation — creates happens-before edges |
| `barrier` | span | SDK | Multi-agent synchronisation point |
| `schedule_decision` | run | scheduler | Chosen task at a scheduling point (`--trace-scheduler` only) |
| `instrumentation_gap` | run | SDK | A construct that could not be instrumented — analysis-quality signal |
| `nondeterminism_warning` | run | runtime | A determinism leak was detected (§10.6) |
| `assertion_result` | run | runtime | `success_check` / hypothesis evaluation outcome |

**Extension rule:** new types may be added in a minor schema version; **removing or repurposing a type is a major version bump.** Consumers must ignore unknown types when `schema_version.minor` is higher than they know, and must fail when `major` is higher.

## 9.4 Fault taint propagation `[DETAIL]`

`fault_id` marks causal descent, not temporal coincidence. Rule:

```
event.fault_id = first non-null value among:
    1. the fault_id of the fault that directly produced this event
    2. the fault_id inherited from any event in causal_parents
    3. the fault_id carried on the agent's context after it observed a faulted input
    4. null
```

Rule 3 is the subtle one: once an agent consumes a message whose send event was tainted, everything that agent does afterwards in that logical task is tainted, until the task completes. This is what makes the cascade tree (§2.6) accurate rather than merely temporal. Where multiple faults contribute, `fault_id` holds the earliest and `payload.fault_ids` holds the full set.

## 9.5 Payload schemas by type

```jsonc
// span_start
{"kind": "llm_call|tool_call|agent_step|handoff|wait", "name": "coder.generate",
 "parent_span_id": "…|null", "attributes": {}}

// span_end
{"status": "ok|error|crashed|timeout|cancelled",
 "duration_virtual_ms": 812, "duration_wall_ms": 63,   // wall is volatile
 "error_type": "TimeoutError|null", "error_message": "…|null"}

// message_send
{"message_id": "m_0a17", "to": "reviewer", "edge": "coder->reviewer",
 "payload_hash": "blake2b:…", "payload_bytes": 4096}

// message_recv
{"message_id": "m_0a17", "from": "coder", "edge": "coder->reviewer",
 "delivered_virtual_ts_ms": 2431, "reordered": false, "duplicate": false}

// state_read
{"key": "draft.module_a", "value_hash": "blake2b:…", "missing": false}

// state_write
{"key": "draft.module_a", "value_hash": "blake2b:…",
 "prev_value_hash": "blake2b:…|null", "reducer": "operator.add|null",
 "txn_id": "…|null", "lock_id": "…|null"}

// tool_call
{"tool": "vector_search", "args_hash": "blake2b:…", "result_hash": "blake2b:…",
 "status": "ok|error", "duration_virtual_ms": 400}

// llm_call
{"model": "llama-3.1-8b-instant", "params_hash": "blake2b:…",
 "prompt_hash": "blake2b:…", "response_hash": "blake2b:…",
 "prompt_tokens": 812, "completion_tokens": 240,
 "cache_status": "hit|miss_recorded|miss_error|perturbed",
 "cache_key": "…", "perturbed_from_run": "r_…|null"}

// fault_injected
{"fault_id": "f_01", "fault_type": "agent_crash", "target": "reviewer",
 "params": {"recoverable": false}, "trigger": {"at_virtual_ts": 2400}}
```

With `capture_bodies=True`, body fields (`prompt`, `response`, `args`, `result`, `value`) are added alongside the hashes — never instead of them, so analysis code has one code path.

## 9.6 Event creation and ordering

1. The SDK constructs a partial event (type, agent, payload).
2. The runtime stamps it under the scheduler lock: `seq`, `sched_step`, `virtual_ts_ms`, `wall_ts_ms`, `vclock` (after applying the §14.2 clock rule), `causal_parents`, `fault_id`.
3. Validation runs (§9.8).
4. The event is enqueued to the writer.

**Ordering guarantees.**
- `seq` is gapless and totally ordered.
- `virtual_ts_ms` is non-decreasing in `seq`.
- `wall_ts_ms` is non-decreasing in `seq` but is **not** a guaranteed reflection of virtual order.
- Every `causal_parents` entry is `< seq`, so the log is topologically sorted by construction — analysers can process it in a single forward pass.

## 9.7 Persistence and immutability

- Events are written to `events` (SQLite, WAL) in batches within a single transaction; `seq` is the primary key alongside `run_id`.
- **Append-only is enforced at three levels:** no `UPDATE`/`DELETE` statements exist in the writer; a SQLite trigger raises on update/delete of `events`; and a rolling hash chain (`prev_hash`, `this_hash`) makes tampering detectable. `[IMPROVEMENT]` The hash chain is an addition; cost is one hash per event (~1 µs), and the benefit is that a bundle received from another machine can be verified as unmodified before its claims are trusted. **Recommendation: adopt.**
- After `run_end`, the log is sealed: a `runs.sealed_at` timestamp is set and the writer refuses further events for that run.

## 9.8 Validation

| Level | When | Behaviour on failure |
|---|---|---|
| Structural (types, required fields, enum membership) | On write, always | Raise immediately — a malformed event is a bug, not data |
| Referential (`causal_parents` < `seq`; `span_id` exists; `fault_id` known) | On write, always | Raise |
| Semantic (`virtual_ts_ms` monotonic; vclock ≥ previous vclock for the slot) | On write, `strict` mode; sampled otherwise | Raise in strict; `nondeterminism_warning` otherwise |
| Log-level invariants (every `span_start` has a `span_end`; every `message_recv` has a `message_send`) | On seal | Recorded as `analysis_warning`; unmatched spans are closed synthetically at `run_end` |

## 9.9 Schema versioning and backward compatibility

- `schema_version` is a single integer for the event contract, with a companion `sdk_version` string in `run_start`.
- **Reading:** analysers support the current major version and one previous, via an upgrade function `migrate_v{n}_to_v{n+1}(event)` applied on read.
- **Writing:** always the current version.
- **Bundles** carry their schema version; import migrates on load or fails with both versions named (`E-BUNDLE-002`).
- **Breaking-change policy:** removing a field, changing a field's meaning, or removing an event type requires a major bump and a migration. Adding an optional field or a new event type is minor.

---

# 10. Deterministic Runtime

## 10.1 What "deterministic" means here — precisely `[DETAIL]`

> **Definition.** Given the same `(graph identity, scenario, seed, LLM cache contents, delay schedule, AgentDX version)`, two executions produce event logs whose **canonical projections are byte-identical**.

The canonical projection (§10.7) removes fields that cannot be deterministic by nature — wall-clock times, host, pid, process memory addresses — and normalises serialisation. This is a *refinement* of PRD v1.0's NFR-2 ("replay must be byte-identical on the event log" `[SOURCE]`), not a weakening: it makes the claim precise enough to assert in CI, which the looser phrasing was not. Without this refinement the CI test could never pass, since `wall_ts_ms` varies by construction. See Appendix C.

## 10.2 Cooperative scheduling model

Agents do not run on the real event loop; they run on the AgentDX scheduler. `[SOURCE]`

```
class Scheduler:
    tasks: dict[task_id, Task]          # each agent step is a Task
    ready: heap[(virtual_ready_ts, agent_id, task_seq)]
    blocked: dict[task_id, WaitReason]
    clock: VirtualClock
    rng:   random.Random(seed)
    step:  int = 0

    def run(self):
        while self.tasks_remaining():
            runnable = self.collect_runnable()          # ready_ts <= clock.now
            if not runnable:
                if not self.blocked_on_timer():
                    raise DeadlockError(self.wait_reasons())
                self.clock.advance_to(self.earliest_timer())
                continue
            chosen = self.choose(runnable)               # <-- the only decision point
            self.step += 1
            self.resume(chosen)                          # runs until next yield point
```

`choose()` is the entire source of scheduling non-determinism, and it is seeded:

```
def choose(self, runnable):
    runnable.sort(key=lambda t: (t.virtual_ready_ts, t.agent_id, t.task_seq))  # stable, total
    if self.delay_schedule and self.step in self.delay_schedule:
        return runnable[self.delay_schedule[self.step] % len(runnable)]        # exploration
    if self.policy == "priority":
        return runnable[0]
    return runnable[self.rng.randrange(len(runnable))]                          # seeded
```

**Yield points** (the only places a task can be preempted) are: every `await` on an AgentDX-provided awaitable — LLM calls, tool calls, message send/recv, state read/write, `agentdx.sleep`, lock acquisition. This is what makes the interleaving space finite and enumerable, and it is why user code must not use raw `asyncio.sleep` or threads (§10.6).

## 10.3 Virtual clock

```
class VirtualClock:
    now_ms: int = 0
    def advance_to(self, ts): assert ts >= self.now_ms; self.now_ms = ts
    def advance_by(self, ms): self.now_ms += ms
```

- Time advances **only** when the scheduler has no runnable task, or when a task explicitly consumes virtual duration (an LLM call of calibrated duration 812 ms sets its completion at `now + 812`).
- Injecting "+500 ms latency" advances the virtual clock, not real time; a 30-second chaos matrix runs in under a second of wall time. `[SOURCE]`
- Virtual time is integer milliseconds. **No floats anywhere in the clock** — float accumulation is a determinism leak across architectures.

## 10.4 Wall-clock calibration

A calibration pass records real per-span durations so virtual timings stay realistic. `[SOURCE]`

| Step | Behaviour |
|---|---|
| Calibration run | `agentdx run --record --calibrate` executes against live providers and records `duration_wall_ms` per span |
| Profile construction | Durations are grouped by `(agent_id, span kind, name, prompt-token bucket)`; the profile stores the **median** and the p90 |
| Profile application | In replay, a span's virtual duration = profile median for its group; if absent, the global median for its kind; if that is absent, a documented default (LLM 800 ms, tool 200 ms, agent_step 50 ms) |
| Jitter | **Off by default.** If `jitter=true`, it is drawn from the seeded RNG so it remains deterministic |
| Drift check | After each run, virtual makespan is compared against the calibration's expected makespan; >10% divergence raises `analysis_warning: clock_drift` |

**Always show measured wall clock next to virtual** `[SOURCE]` — in the CLI, the API and the UI. The mitigation for "virtual clock diverges from reality" is transparency, not hidden correction.

## 10.5 Trapping ambient non-determinism

Inside a run context, the runtime installs the following, and removes them on exit:

| Source | Treatment |
|---|---|
| `random` module-level functions | Redirected to the seeded run RNG |
| `numpy.random` global state | Seeded at run start if numpy is importable |
| `time.time`, `time.monotonic`, `time.perf_counter` | Patched to return virtual time (wall time remains available as `agentdx.wall_time()`) |
| `datetime.now`, `datetime.utcnow` | Patched to a virtual epoch derived from the seed |
| `uuid.uuid4` | Seeded deterministic generator |
| `asyncio.sleep` | Redirected to `agentdx.sleep` (virtual) |
| `asyncio` event loop | Replaced by the AgentDX loop for the duration of the run |
| `hash()` randomisation | `PYTHONHASHSEED=0` required; `agentdx doctor` checks and the CLI re-execs itself with it set if necessary |
| `set` / `dict` iteration | Lint rule bans iteration over `set` in instrumented paths; helper `agentdx.sorted_set()` provided |
| `os.environ` reads | Not patched; recorded in `run_start` for provenance |

## 10.6 What cannot be guaranteed deterministic `[DETAIL]`

This list is published in the documentation, not buried. Honesty here is the same asset as §15.6.

| Not guaranteed | Why | Mitigation |
|---|---|---|
| Live model calls (`passthrough`/`record` mode) | The provider is non-deterministic even at `temperature=0` | Determinism is a property of **replay**, and replay requires the cache |
| Real network or filesystem I/O in user tools | Outside AgentDX's control | Detected and reported as `nondeterminism_warning`; `--strict` aborts. Recommend wrapping such tools with `@agentdx.tool` and recording them |
| OS threads / `multiprocessing` spawned by user code | The scheduler cannot observe or order them | **Detected and rejected**: creating a thread inside an instrumented span raises in `strict` mode |
| C-extension internal ordering (e.g. some BLAS reductions) | Outside Python's control | Out of scope; affects values, not coordination structure |
| Floating-point results across architectures | Hardware | Event log stores hashes of values; a cross-architecture hash mismatch is reported as a value difference, not a scheduling difference — and the schedule comparison still passes |
| Memory addresses, `id()`, object identity | CPython internals | Never used in identity; all identity is content- or sequence-derived |
| Wall-clock durations | Physics | Excluded from the canonical projection |

**Determinism domain statement (use verbatim in docs):** *AgentDX guarantees determinism of the **coordination structure and the event log**, in replay mode, on a fixed AgentDX version. It does not guarantee determinism of model outputs in record mode, nor of user code that performs unmanaged I/O or concurrency.*

## 10.7 The canonical projection

```
def canonicalise(event) -> bytes:
    e = dict(event)
    del e["wall_ts_ms"]
    e["payload"].pop("duration_wall_ms", None)
    e["payload"].pop("cache_key", None)        # cache key includes storage-local salt? no — see §11.4
    return json.dumps(e, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")

def canonical_log_hash(events) -> str:
    h = blake2b(digest_size=32)
    for ev in events: h.update(canonicalise(ev) + b"\n")
    return h.hexdigest()
```

**Excluded (volatile) fields, exhaustively:** `wall_ts_ms`, `payload.duration_wall_ms`, `run_start.payload.host`, `run_start.payload.pid`, `run_start.payload.started_at_utc`, `run_start.payload.env` (recorded for provenance, not compared). Every other field participates in equality. Anything added to this list later is a documented change requiring sign-off, because each addition weakens the guarantee.

## 10.8 Deterministic tool execution

- Tools wrapped with `@agentdx.tool` are recorded and replayed exactly like LLM calls, keyed by `(tool_name, canonical args)`. This makes retrieval, web search and database reads deterministic in replay.
- Unwrapped tools execute live and are flagged. This is permitted (the product must be useful on day one) but is reported in the run's *determinism quality* field, which is shown in the UI and included in bundles.

## 10.9 Retries and external I/O

Retries are ordinary execution: each attempt is its own span, linked by `retry_of` in `span_start.attributes`, and every attempt after the first is attributed to the `retry_recovery` bucket. Retry backoff sleeps consume **virtual** time, which is why a scenario with aggressive retry policy is cheap to explore.

## 10.10 Reproducibility checklist (what a bundle must pin)

| Pinned | Where |
|---|---|
| Seed | `run_start.payload.seed` |
| Scenario content hash | `run_start.payload.scenario_hash` |
| Graph identity hash (node names, edges, tool names) | `run_start.payload.graph_hash` |
| Delay schedule | `run_start.payload.delay_schedule` |
| Cache slice manifest (keys + digests) | Bundle manifest |
| AgentDX version + schema version | `run_start` |
| Calibration profile id | `run_start.payload.calibration_id` |

A replay whose `graph_hash` differs from the bundle's is refused with `E-REPLAY-002` — the graph changed, so the comparison would be meaningless.

---

# 11. LLM Record / Replay Architecture

## 11.1 Why this exists

Determinism is impossible while the model is live. Solve it the way HTTP test suites do. `[SOURCE]` This mechanism makes the whole product work, cuts API spend to near zero after the first record, and enables the offline demo, hermetic CI, and affordable bounded exploration. `[SOURCE]`

## 11.2 Modes

| Mode | Behaviour on lookup | On miss | Determinism | Typical use |
|---|---|---|---|---|
| `record` | Check cache; on miss call the provider and store | Store and return | Not deterministic (provider is live) | First pass on a new graph or task |
| `replay` | Serve from cache only | **Hard error** `E-CACHE-001` `[SOURCE]` | Deterministic | Demo, CI, exploration, everyday use |
| `perturb` | Serve a *different* cached response for the same logical call, selected by seeded RNG from a declared perturbation pool `[SOURCE]` | Hard error | Deterministic | Simulating byzantine/drifting agents |
| `passthrough` | Always call the provider; do not consult the cache | n/a | Not deterministic | Overhead benchmarking (§34.1) only |

**The hard-error rule is load-bearing.** A silent fallback to a live call would make CI non-hermetic, make bundles unreproducible, and make cost unpredictable — three failures at once. Implementations must not offer a "fall back to live" flag; the correct response to a miss is `--record`.

## 11.3 Interaction with the deterministic scheduler `[DETAIL]`

The cache and the scheduler are coupled through **virtual duration**, and this is subtle enough to specify precisely:

1. An agent reaches an LLM call and awaits it — this is a scheduler yield point.
2. The scheduler asks the cache for the response. A cache hit returns immediately in wall time.
3. The scheduler does **not** complete the call immediately in *virtual* time. It schedules completion at `now + duration`, where `duration` comes from (a) the fault injector, if a `latency` or `agent_slow` fault applies, else (b) the calibration profile for this call group, else (c) the recorded duration stored alongside the cached response, else (d) the documented default.
4. Meanwhile the scheduler runs other tasks — which is exactly how a replayed run still exhibits realistic concurrency, and how a 30-second run costs a fraction of a second.

Consequence: **the cache determines *what* the model said; the scheduler determines *when* it said it.** These are deliberately independent, which is what allows AgentDX to explore many interleavings over one recording.

## 11.4 Cache key construction

```
key_material = canonical_json({
    "model":        model_id,                 # exact provider model string
    "messages":     normalise(messages),      # see below
    "params":       {k: params[k] for k in SIGNIFICANT_PARAMS},
    "tools":        normalise_tools(tools),   # tool schemas offered to the model
    "response_fmt": response_format or None,
    "key_version":  2,
})
cache_key = "blake2b:" + blake2b(key_material, digest_size=32).hexdigest()
```

- `SIGNIFICANT_PARAMS` = `temperature`, `top_p`, `max_tokens`, `stop`, `seed`, `frequency_penalty`, `presence_penalty`, `tool_choice`. Params outside this set (e.g. `user`, `stream`, timeouts) **do not** affect the key.
- `normalise(messages)`: strip whitespace-only differences at message boundaries only; preserve content exactly otherwise; sort no keys inside content; represent images/audio by content digest.
- `key_version` allows the key algorithm to change without silently invalidating everything — a version bump makes every entry a miss with a clear message rather than a wrong hit.
- **The key never contains a machine-local salt**, so a cache is portable between machines. This is what makes bundles work.

**Non-determinism trap** `[DETAIL]`: if a prompt embeds a timestamp, a UUID or a random sample, its key changes every run and replay always misses. The runtime's patched `time`/`uuid` (§10.5) removes the common cases. For the rest, the CLI reports a **prompt-volatility diagnostic**: keys that missed in replay but whose prompt differs from a cached prompt by <5% edit distance are reported as "likely volatile prompt", with the differing region shown. This turns the most common adoption failure into a legible message.

## 11.5 Model identity

The key includes the exact provider model string (e.g. `llama-3.1-8b-instant`). Changing the model invalidates every entry — correctly, because a different model is a different experiment. `run_start` records the model string, the provider base URL host, and the provider SDK version for provenance. Model deprecation is a real risk (§42.5); the mitigation is that **an existing cache keeps working forever**, since replay never contacts a provider.

## 11.6 Response storage

```sql
CREATE TABLE llm_cache (
  cache_key         TEXT PRIMARY KEY,
  key_version       INTEGER NOT NULL,
  model             TEXT NOT NULL,
  prompt_hash       TEXT NOT NULL,
  response_body     BLOB NOT NULL,       -- zstd-compressed JSON of the full provider response
  response_hash     TEXT NOT NULL,
  prompt_tokens     INTEGER, completion_tokens INTEGER,
  duration_wall_ms  INTEGER,             -- feeds the calibration profile
  recorded_at       TEXT NOT NULL,
  recorded_run_id   TEXT NOT NULL,
  provider          TEXT NOT NULL,
  finish_reason     TEXT,
  stream_chunks     BLOB                 -- optional: chunk boundaries for streaming replay
);
CREATE INDEX idx_cache_prompt ON llm_cache(prompt_hash);
CREATE INDEX idx_cache_run    ON llm_cache(recorded_run_id);
```

Full provider responses are stored verbatim (including tool-call structures and `finish_reason`) so replay can reproduce not just text but control flow.

## 11.7 Cache misses and invalidation

| Situation | Behaviour |
|---|---|
| Miss in `replay` | Hard error listing: agent, model, key prefix, the nearest cached prompt by edit distance, and the exact `agentdx run --record …` command |
| Miss in `record` | Normal path: call provider, store |
| Model string changed | Every key misses; the error names the previous model recorded for this scenario |
| `key_version` bumped | Everything misses; the CLI offers `agentdx cache migrate` where a mechanical re-key is possible |
| Prompt template changed | Misses; this is correct — the experiment changed |
| Explicit invalidation | `agentdx cache prune --run <id>` / `--older-than 30d` / `--model X`. **There is no automatic eviction**: silent eviction would break reproducibility of old bundles |

## 11.8 Perturb mode specification

```yaml
faults:
  - type: byzantine
    agent: reviewer
    mode: stale_output          # stale_output | contradictory | confident_wrong
    pool: run:r_9c113           # where substituted responses come from
```

- `stale_output`: serve the response this agent gave at an earlier step of the same run.
- `contradictory`: serve a response recorded for a *different* input in the declared pool, chosen by seeded RNG.
- `confident_wrong`: serve from a curated pool of hand-authored wrong-but-plausible responses shipped with the fixtures (`fixtures/perturbations/*.json`).

Every perturbed call emits `llm_call.cache_status = "perturbed"` with `perturbed_from_run`, so no analysis can mistake a perturbed response for a genuine one.

`[OPEN] §43.1.4` — PRD v1.0 open question 3 asks whether "confidently wrong output" is measurable without an eval layer. **Recommended resolution:** yes, without a judge model, by (a) sourcing wrong outputs from a curated fixture pool rather than generating them, and (b) measuring the *system's response* to them (did any agent detect it? did the run report success?) rather than judging the text. This keeps the pipeline deterministic and avoids introducing a model dependency into analysis.

## 11.9 Privacy

- The cache necessarily stores bodies; the **event log does not, by default** `[SOURCE]` (§8.11).
- Cache DB is a separate file (`cache.db`) with `0600` permissions, so it can be excluded from sharing independently of the event store.
- `redact_patterns` are applied to prompts **before** hashing and storage; a redaction changes the key, which is correct — a redacted prompt is a different prompt.
- `agentdx cache export --sanitise` produces a cache slice with bodies replaced by deterministic synthetic text of the same token length. Such a slice replays the *structure and timing* of a run but not its content — useful for sharing a reproduction with a vendor without shipping proprietary prompts.

## 11.10 Replay validation

`agentdx replay <run_id> --verify` performs:

1. Re-execute with the recorded seed, delay schedule, scenario hash and graph hash.
2. Compute `canonical_log_hash` of the new log.
3. Compare against the stored hash of the original.
4. On mismatch, emit `E-REPLAY-001` with the **first divergent event**, side by side, and the diverging field named.

This is the CI test that proves the product's central claim, run 100× per §33.3. `[SOURCE]`

---

# 12. Fault Injection Engine

Faults are declared in a scenario file and injected at the scheduler layer. `[SOURCE]` No user agent code is modified.

## 12.1 Architecture

```
        scenario.yaml
             │  (validated, blast radius resolved)
             ▼
     ┌───────────────┐      arms faults at
     │ FaultRegistry │──────interception points───┐
     └───────┬───────┘                            │
             │ evaluate(trigger, clock, event)    ▼
             │                        ┌────────────────────────┐
             │                        │ INTERCEPTION POINTS    │
             │                        │ • pre_schedule(task)   │
             ▼                        │ • pre_send(message)    │
     ┌───────────────┐                │ • pre_deliver(message) │
     │ FaultInjector │───applies────▶ │ • pre_llm(call)        │
     └───────┬───────┘   effect       │ • pre_tool(call)       │
             │                        │ • pre_state_write(op)  │
             │ emits                  │ • pre_resume(agent)    │
             ▼                        └────────────────────────┘
   fault_injected + fault_effect events (written BEFORE the effect applies)
```

**Ordering rule:** the `fault_injected` event is written *before* the effect is applied, so the log always explains the behaviour that follows it. If the process dies mid-fault, the log still says what was attempted.

## 12.2 Complete fault catalogue

Source catalogue preserved exactly `[SOURCE]`, with execution semantics, safety constraints and reproducibility added `[DETAIL]`.

### Transport class

| Field | `latency` `P0` | `message_drop` `P0` | `message_reorder` `P1` | `message_duplicate` `P1` |
|---|---|---|---|---|
| **Target** | edge or agent | edge | edge | edge |
| **Trigger** | `at_virtual_ts` \| `after_n_messages` \| `always` | same | same | same |
| **Parameters** | `delay_ms`, `jitter_ms`, `pattern: constant\|spike\|degrade` | `probability` | `window` (messages) | `probability`, `copies` |
| **Interception** | `pre_deliver` | `pre_deliver` | `pre_deliver` (buffer + reorder within window) | `pre_deliver` |
| **Semantics** | Delivery scheduled at `now + delay`; `degrade` grows delay linearly across the run; `spike` applies to one delivery | Message discarded; no `message_recv`; sender is unaware | Up to `window` in-flight messages on the edge are delivered in a seeded permutation | Delivered `copies` times with distinct `message_recv` events flagged `duplicate=true` |
| **Expected effect** | Handoff bucket grows; may reveal a timeout path | Exercises the receiver's absence handling; may deadlock (a real finding) | Exercises out-of-order assumptions | Exercises idempotency |
| **Safety** | Bounded by `max_virtual_duration` | Cannot drop a `run_end` control message | Window ≤ 16 | `copies` ≤ 5 |
| **Rollback** | None needed (virtual time only) | None | Buffer flushed at run end | None |
| **Reproducibility** | Probabilities drawn from the seeded RNG; identical under the same seed |

### Process class

| Field | `agent_crash` `P0` | `agent_slow` `P1` |
|---|---|---|
| **Target** | agent | agent |
| **Trigger** | `at_virtual_ts` \| `at_span_n` \| `on_state_write(key)` | `at_virtual_ts` \| `always` |
| **Parameters** | `recoverable: bool`, `restart_after_ms` | `factor` (float ≥ 1.0) |
| **Interception** | `pre_resume` | `pre_schedule` |
| **Semantics** | The agent's current task raises `AgentCrashed`; its in-flight span closes with `status=crashed`; unsent messages are discarded. If `recoverable`, the agent restarts after `restart_after_ms` with **cleared local context but intact shared state** | Every span duration for that agent multiplied by `factor` in virtual time |
| **Expected effect** | Supervisor timeout/retry path exercised; retry bucket grows; possible cascade | The agent moves onto the critical path |
| **Safety** | Cannot crash the last live agent unless `allow_total_failure: true` | `factor` ≤ 100 |
| **Rollback** | Restart if recoverable; otherwise the agent stays down for the run | Removed at run end |

### Dependency class

| Field | `tool_failure` `P0` | `rate_limit` `P1` |
|---|---|---|
| **Target** | tool name | provider |
| **Trigger** | `always` \| `first_n` \| `after_virtual_ts` \| `probability` | `always` |
| **Parameters** | `mode: timeout\|429\|500\|malformed`, `count` | `rps_cap`, `retry_after_ms` |
| **Interception** | `pre_tool` | `pre_llm` |
| **Semantics** | `timeout` → the call never returns and the caller's timeout fires in virtual time; `429`/`500` → provider-shaped exception; `malformed` → a schema-invalid result returned successfully (this is the nastiest and most realistic mode) | A virtual token bucket; calls exceeding the cap receive a 429 with `retry_after`, consuming virtual time |
| **Expected effect** | Retry amplification, cascade, possibly silent failure under `malformed` | Serialisation of a "parallel" fan-out — often the real cause of fake parallelism |
| **Safety** | Only tools inside the blast radius | Applies to the sandboxed provider shim only |

### Semantic and State classes

| Field | `byzantine` `P1` | `state_corrupt` `P1` |
|---|---|---|
| **Target** | agent | state key |
| **Trigger** | `at_span_n` \| `always` | `at_virtual_ts` \| `on_write` |
| **Parameters** | `mode: stale_output\|contradictory\|confident_wrong`, `pool` | `mutation: drop\|truncate\|swap\|stale\|type_change` |
| **Interception** | `pre_llm` (via perturb-mode cache, §11.8) | `pre_state_write` / direct store mutation |
| **Semantics** | A plausible-but-wrong cached response is substituted; the agent behaves normally otherwise | The stored value is mutated after (or instead of) the write; a `fault_effect` event records both hashes |
| **Expected effect** | Tests whether any downstream agent detects the error, or whether the run reports success anyway (`SILENT_FAILURE`) | Tests state validation and recovery |
| **Safety** | Only from a declared pool; never generated by a model | Only keys in the blast radius; never on a key marked `protected` |
| **Reproducibility** | Selection is seeded | Mutation is deterministic given the seed |

## 12.3 Trigger evaluation

```
def should_fire(fault, clock, ctx) -> bool:
    if fault.fired and not fault.repeating: return False
    if fault.target not in ctx.blast_radius: return False        # hard invariant
    match fault.trigger:
        case AtVirtualTs(ts):        return clock.now_ms >= ts
        case AtSpanN(agent, n):      return ctx.span_count(agent) == n
        case AfterNMessages(edge,n): return ctx.message_count(edge) >= n
        case OnStateWrite(key):      return ctx.current_op_key == key
        case Probability(p):         return ctx.rng.random() < p   # seeded
        case Always():               return True
```

Triggers are evaluated **only at interception points**, never on a timer, so firing is a function of the deterministic execution and nothing else.

## 12.4 Scenario execution lifecycle

```
 LOAD ──▶ VALIDATE ──▶ RESOLVE ──▶ BASELINE PHASE ──▶ STEADY-STATE CHECK ──┐
                                                                            │
                          ┌─────────────────────────────────────────────────┘
                          ▼
                    ARM FAULTS ──▶ FAULT PHASE ──▶ GUARD MONITOR ──▶ SEAL
                          │                              │
                          │                              └─▶ (guard trip) ABORT
                          ▼
                    ANALYSE ──▶ SCORE ──▶ ASSERT ──▶ REPORT
```

| Phase | Behaviour | Failure |
|---|---|---|
| LOAD | Parse YAML, check `version` | `E-SCEN-001` |
| VALIDATE | Schema, unknown keys, fault targets exist in the graph, blast radius present if faults present | `E-SCEN-002…005`, before any execution |
| RESOLVE | Merge defaults; resolve task file; resolve graph identity; compute `scenario_hash` | `E-SCEN-006` |
| BASELINE PHASE | Execute with **no faults armed**, same seed | Failure here aborts the experiment |
| STEADY-STATE CHECK | Evaluate `hypothesis` against baseline metrics | Violation → `ABORT_PRECONDITION` (§13.5) |
| ARM FAULTS | Register faults with the injector | — |
| FAULT PHASE | Execute with faults armed | — |
| GUARD MONITOR | Continuous evaluation of abort guards | Trip → `ABORTED_GUARD` |
| ANALYSE / SCORE / ASSERT | Post-hoc, over the sealed logs | — |

## 12.5 Logging and observability of faults

Every fault produces, at minimum: one `fault_injected` event; zero or more `fault_effect` events (one per concrete application — a probabilistic drop that fires four times produces four); a `fault_summary` entry in run metadata recording `fired_count`, `first_fired_at`, `targets_affected`; and a `fault_not_triggered` flag if it never fired.

**A fault that never fires must be surfaced prominently.** A chaos experiment whose fault silently did not apply produces a falsely reassuring result — the most dangerous failure mode this subsystem has.

---

# 13. Chaos Safety Architecture

**This section is non-negotiable.** `[SOURCE]` Safety rails are also the strongest talking point in the design: they are what separates chaos *engineering* from breaking things.

## 13.1 Threat model

What could go wrong if AgentDX injected faults carelessly:

| Hazard | Concrete example | Consequence |
|---|---|---|
| Real external mutation | A `tool_failure` on a tool that has already sent an email; a retry sends it twice | Real-world side effect |
| Runaway spend | A retry storm under `rate_limit` in record mode burns tokens | Real money |
| Destructive tool execution | An agent's `shell` tool runs `rm -rf` during a corrupted-state experiment | Data loss |
| Production endpoint targeting | The instrumented graph points at a production database | Production incident |
| Infinite run | A `message_drop` prevents termination | Hung CI |
| Leaked secrets | A crash dump written to the event log includes an API key | Credential exposure |

Each is addressed below.

## 13.2 Sandbox boundaries

```
┌──────────────── AgentDX process ────────────────────────────────────┐
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ SANDBOXED EXECUTION CONTEXT                                  │   │
│  │  • provider shim (network egress only to configured base_url)│   │
│  │  • tool registry (only registered tools callable)            │   │
│  │  • state store (in-process, per-run, discarded after)        │   │
│  │  • filesystem: run data dir + read-only fixture dir          │   │
│  └──────────────────────────────────────────────────────────────┘   │
│  Faults may only affect objects inside this context                  │
└──────────────────────────────────────────────────────────────────────┘
```

In `replay` mode — the default `[DETAIL]` — **there is no network egress at all**, because every model call is served from cache and every registered tool is replayed. This single property removes most of §13.1's hazards for the default path.

## 13.3 Fixture-only default `[SOURCE]`

> *Chaos runs only ever touch the sandboxed fixture or an explicitly opted-in user graph.* `[SOURCE]`

Implementation:
- A graph is either a **fixture** (shipped in `fixtures/`, signed by a manifest hash) or a **user graph**.
- Faults against a fixture: permitted by default; the fixture's blast radius defaults to "everything in the fixture".
- Faults against a user graph: **refused** unless the scenario contains `chaos_opt_in: true` **and** a non-empty `blast_radius`. Missing either is `E-SCEN-004`, raised at validation, before execution.
- `chaos_opt_in` cannot be set by a CLI flag alone; it must be in the scenario file, so that opting in is a reviewable, committed act.

## 13.4 Blast radius `[SOURCE]`

```yaml
blast_radius:
  agents:     [reviewer, tester]        # who may be affected
  tools:      [vector_search]           # which tools may be failed
  edges:      ["coder->reviewer"]       # which transports may be perturbed
  state_keys: ["draft.*"]               # glob; which keys may be corrupted
  providers:  [groq]                    # which providers may be rate-limited
```

Rules:
1. **Default is empty** for user graphs. Nothing is in scope until named.
2. Enforced at two layers: validation (a fault whose target is outside the radius fails to load) and runtime (`should_fire` re-checks; a violation raises `E-CHAOS-001` and aborts the run — a defence-in-depth check that should be unreachable).
3. Keys may use globs; agents and tools may not (explicit naming only, to avoid a typo widening the radius).
4. The resolved blast radius is displayed in the UI **before** a fault can be fired, and is printed by the CLI at run start.

## 13.5 Steady-state hypothesis `[SOURCE]`

```yaml
hypothesis:
  task_success: ">= 0.9"
  p95_virtual_duration_ms: "<= 45000"
  max_token_spend: "<= 150000"
```

- Evaluated against the **baseline phase**, before any fault is armed.
- If the baseline violates the hypothesis, the experiment aborts with `ABORT_PRECONDITION`: you cannot measure deviation from a steady state you never had. This is the discipline that distinguishes an experiment from a stunt.
- The hypothesis is re-evaluated after the fault phase; the *delta* is the experimental result.

## 13.6 Abort guards `[SOURCE]`

| Guard | Default | Evaluated | On trip |
|---|---|---|---|
| `max_virtual_duration_ms` | 120 000 `[SOURCE]` | Every scheduler step | Abort, `ABORTED_GUARD` |
| `max_tokens` | 200 000 `[SOURCE]` | Every `llm_call` | Abort |
| `max_retries` | 20 | Every retry span | Abort |
| `max_wall_duration_s` | 300 | Every 100 steps | Abort — protects CI from a hang |
| `max_events` | 500 000 | Every write batch | Abort — protects the disk |
| `max_llm_calls` | 500 | Every call | Abort — protects spend in record mode |

On trip: the injector disarms, in-flight tasks are cancelled, the log is sealed with `run_end.status = aborted_guard`, and the partial log **is retained and analysable**. An aborted run is never scored for resilience (§19.7) — a partial experiment must not produce a number that looks like a result.

## 13.7 External API protection

- **Record mode is opt-in per run** (`--record`), never a fallback. There is no path where a replay silently becomes a live call.
- The provider shim enforces the configured `base_url`; requests to any other host raise.
- A spend estimator prints projected token usage before a record run and requires `--yes` above a configurable threshold (default 100 000 tokens).
- `rate_limit` faults are simulated in the shim; AgentDX never deliberately triggers a real provider's rate limiter.

## 13.8 Destructive-operation prevention

- Tools must be registered. An unregistered callable invoked from an instrumented span raises in `strict` mode and warns otherwise.
- Registered tools may be marked `destructive: true`. **A destructive tool is never executed under a fault run** — it is stubbed and its call is recorded as `tool_call.status = "skipped_destructive"`. Chaos experiments that require a destructive tool to actually run are out of scope for v1 and are documented as such.
- Filesystem access from fixtures is confined to the run data dir plus a read-only fixture dir.
- `state_corrupt` cannot target a key marked `protected: true` in the graph's declaration.

## 13.9 Baseline runs inherit the sandbox

A generated baseline (§17) executes under the same sandbox, the same blast radius, and the same destructive-tool stubbing as its parent run. Without this, generating a baseline could re-execute a side-effecting tool a second time — the sort of subtle hazard that destroys trust in a testing tool.

## 13.10 Authorisation summary

| Action | Requires |
|---|---|
| Run a fixture in replay mode | Nothing |
| Run a fixture with faults | Nothing (fixture sandbox) |
| Run a user graph in replay mode | Nothing |
| **Run a user graph with faults** | `chaos_opt_in: true` **and** non-empty `blast_radius` in a committed scenario file |
| Record against a live provider | `--record` flag + configured API key + spend confirmation above threshold |
| Execute a destructive tool | Not permitted under fault runs in v1 |

---

# 14. Race Detection Architecture

This section is written so a distributed-systems engineer can implement it directly. The lineage is Lamport happens-before, with the access-tracking structure borrowed from Eraser/FastTrack. `[SOURCE]`

## 14.1 The two graphs — and why they must not be confused `[DETAIL]`

| Graph | Nodes | Edges | Used by |
|---|---|---|---|
| **Causality graph** (happens-before) | events | program order within a clock slot; `message_send → message_recv`; `lock_release → lock_acquire` on the same lock; `barrier` participants | Race detection (§14) |
| **Timing DAG** | spans | program order; message causality; **observed data dependencies**; retry links | Critical path (§16) |

**Critical rule: shared-state access does NOT create a happens-before edge.** If a write to `k` by A followed by a read of `k` by B were treated as ordering A before B, then by construction no shared-state race could ever be detected — the accesses would always appear ordered. Shared memory is exactly the unsynchronised channel we are auditing.

Conversely, the timing DAG *must* include data dependencies, or the critical path would ignore the real reason a span waited.

Implementations must keep these in separate modules (`analysis/causality.py`, `analysis/timing.py`) so the distinction cannot erode.

## 14.2 Vector clock rules

Each **clock slot** (normally one per agent; more for intra-agent concurrency, §8.8) holds a sparse map `slot → counter`.

```
# local event (state op, tool call, llm call, span boundary)
VC[self] += 1

# send
VC[self] += 1
message.vclock = copy(VC)

# receive
for slot, n in message.vclock.items():
    VC[slot] = max(VC.get(slot, 0), n)
VC[self] += 1

# lock acquire (explicit synchronisation)
for slot, n in lock.release_vclock.items():
    VC[slot] = max(VC.get(slot, 0), n)
VC[self] += 1

# lock release
VC[self] += 1
lock.release_vclock = copy(VC)

# barrier: all-to-all merge, then each participant increments
```

Comparison:

```
def happens_before(a: VC, b: VC) -> bool:
    return all(a.get(s,0) <= b.get(s,0) for s in a) and any(a.get(s,0) < b.get(s,0) for s in set(a)|set(b))

def concurrent(a: VC, b: VC) -> bool:
    return not happens_before(a, b) and not happens_before(b, a)
```

Sparse maps are used because agents can be created dynamically; an absent slot reads as 0.

## 14.3 Access tracking structures

```
@dataclass
class KeyState:
    last_write:  AccessRecord | None          # seq, slot, vclock, value_hash, reducer, lock_id, txn_id
    writes_by_slot: dict[slot, AccessRecord]  # last write per slot (needed for W-W across >2 agents)
    reads_by_slot:  dict[slot, AccessRecord]  # last read per slot
```

Memory is **O(live keys × slots)**, not O(accesses) — the pruning is what makes this tractable on long runs. Retaining only the last access per slot is sound for detecting *at least one* race per key-pair, which is the reporting granularity we want (we report a representative conflict per key, with a count, not every instance).

## 14.4 The detection algorithm

```
def detect_conflicts(events) -> list[Finding]:
    vc      = defaultdict(dict)     # slot -> vector clock
    keys    = defaultdict(KeyState)
    inflight_msgs = {}
    findings = []

    for e in events:                       # single forward pass; log is topologically sorted
        slot = e.clock_slot

        if e.type == "message_send":
            bump(vc[slot], slot); inflight_msgs[e.payload.message_id] = copy(vc[slot])

        elif e.type == "message_recv":
            merge(vc[slot], inflight_msgs.pop(e.payload.message_id, {})); bump(vc[slot], slot)

        elif e.type in ("lock_acquire", "lock_release", "barrier"):
            apply_sync_rule(vc, e)

        elif e.type == "state_write":
            bump(vc[slot], slot)
            k = keys[e.payload.key]
            # write-write
            for other_slot, w in k.writes_by_slot.items():
                if other_slot != slot and concurrent(w.vclock, vc[slot]):
                    findings += maybe_report("write_write", w, access(e, vc[slot]))
            # write-read (this write races an earlier concurrent read = dirty read risk)
            for other_slot, r in k.reads_by_slot.items():
                if other_slot != slot and concurrent(r.vclock, vc[slot]):
                    findings += maybe_report("write_read", r, access(e, vc[slot]))
            k.writes_by_slot[slot] = access(e, vc[slot]); k.last_write = k.writes_by_slot[slot]

        elif e.type == "state_read":
            bump(vc[slot], slot)
            k = keys[e.payload.key]
            # read-write (this read races a concurrent write = stale read)
            for other_slot, w in k.writes_by_slot.items():
                if other_slot != slot and concurrent(w.vclock, vc[slot]):
                    findings += maybe_report("read_write", access(e, vc[slot]), w)
            k.reads_by_slot[slot] = access(e, vc[slot])

        else:
            bump(vc[slot], slot)

    return dedupe_by_key_and_pair(findings)
```

Complexity: **O(E × S)** where E = events and S = slots (typically ≤ 10). At 50 000 events and 8 agents this is trivially fast; the target in §32 is <2 s for a 50 000-event log.

## 14.5 Conflict classification `[SOURCE]`

| Subtype | Pattern | User-facing name | Typical consequence |
|---|---|---|---|
| `write_write` | Two concurrent writes to the same key | **Lost update** | One agent's work silently discarded |
| `read_write` | A read concurrent with a write | **Stale read** | Agent acted on an outdated value |
| `write_read` | A write concurrent with an earlier read that has not yet been acted on | **Dirty read** | Downstream decision based on a value being replaced |

Torn reads across a `txn_id` group are reported as `write_read` with `torn: true` and elevated severity.

## 14.6 Value divergence

Concurrency alone is not enough. `[SOURCE]` Divergence tests:

| Case | Divergent? |
|---|---|
| Two concurrent writes, `value_hash` differs | **Yes** — report |
| Two concurrent writes, identical `value_hash` | No — idempotent, suppress (counted as `benign_idempotent`) |
| Read then concurrent write, `value_hash` at read ≠ final value | **Yes** — report |
| `value_hash` unavailable (unserialisable value) | Unknown — report at reduced confidence, severity capped at `warning` |

## 14.7 False-positive prevention — the four guards

> *Require **both** concurrency and value divergence before reporting; ship the healthy fixture as a no-findings regression test.* `[SOURCE]`

This document adds two further guards, both essential in practice:

| Guard | Rule | Rationale |
|---|---|---|
| **G1 Concurrency** | Neither access happens-before the other | The definition |
| **G2 Divergence** | Values actually differ (§14.6) | Concurrent identical writes harm nothing |
| **G3 Declared merge** `[IMPROVEMENT]` | Suppress if the key's channel has a declared reducer (`operator.add`, `merge`, any user reducer), or the key matches `analysis.crdt_keys` | LangGraph channels with reducers are *designed* for concurrent writes. Without G3, AgentDX reports a false positive on nearly every real LangGraph app — the single largest trust risk in the product |
| **G4 Explicit synchronisation** | Suppress if both accesses are covered by the same `lock_id`, or ordered by a barrier | The user told us the ordering is managed |

Suppressed conflicts are **not discarded** — they are stored with `suppressed_by: G2|G3|G4` and shown in a collapsible "suppressed (n)" drawer in the findings panel, so a user can audit the suppression. Silent suppression would be as untrustworthy as a false positive.

## 14.8 Minimal reproduction generation

```
def minimal_repro(finding, root_run) -> Scenario:
    # 1. Identify the two scheduling decisions that placed the accesses concurrently
    d1, d2 = scheduling_decisions_for(finding.event_a, finding.event_b, root_run.schedule)
    # 2. Construct the shortest delay schedule that reproduces the ordering
    delays = minimal_delay_set(root_run.schedule, target_order=(d1, d2))
    # 3. Shrink: greedily drop delays while the conflict still reproduces (bounded re-runs)
    for candidate in shrink_candidates(delays, max_trials=16):
        if reruns_with_conflict(root_run, candidate, finding.key):
            delays = candidate
    # 4. Emit a scenario pinned to seed + delay schedule + an assertion
    return Scenario(seed=root_run.seed, delay_schedule=delays,
                    assertions=["no_state_conflicts"], derived_from=finding.id)
```

Shrinking is bounded at 16 re-runs (each free, thanks to the cache). The emitted scenario is written to `scenarios/repro_<finding_id>.yaml` and is directly usable as a CI regression test — this is the mechanism that closes Journey B.

## 14.9 Reporting format

```
● LOST UPDATE  draft.module_a                                     severity: high
  coder      wrote  blake2b:9f2c…  at seq 1043  (t=2418ms, vclock {planner:12, coder:8})
  reviewer   wrote  blake2b:44e1…  at seq 1051  (t=2431ms, vclock {planner:12, reviewer:3})
  Neither write happens-before the other; values diverge; no reducer declared on this channel.
  reviewer's value survived. coder's edit to module_a was discarded.

  Fix: declare a reducer on the `draft` channel, take agentdx.lock("draft.module_a"),
       or route both writers through `planner`.
  Reproduce: agentdx run scenarios/repro_f_0117.yaml
  Evidence: seq 1039, 1043, 1047, 1051
```

Copy follows the source's voice rules `[SOURCE]`: state what happened and what to do; never apologetic, never emoji-hedged.

---

# 15. Bounded Schedule Exploration

Full state-space exploration is intractable; AgentDX does delay-bounded systematic exploration (the CHESS approach). `[SOURCE]`

## 15.1 Delay bounds and defaults

| Parameter | Default | Range | Meaning |
|---|---|---|---|
| `k` (delay bound) | **2** `[SOURCE]` | 0–5 | Maximum number of scheduling points at which the scheduler deviates from its default choice |
| `N` (schedule cap) | **200** `[SOURCE]` | 1–10 000 | Hard cap on schedules executed |
| `time_budget_s` | 120 | — | Wall-clock ceiling; partial results are reported honestly |
| `strategy` | `delay_bounded` | `delay_bounded` \| `random` \| `replay_set` | `random` is a comparison baseline for the benchmark suite |

Rationale for `k=2`: the empirical claim underlying CHESS-style tools is that most concurrency bugs manifest within a small number of preemptions. AgentDX does not need to defend that claim in the abstract — §34.4 measures, on the shipped fixtures, at what `k` each seeded defect is found, and publishes that table.

## 15.2 Schedule representation

```
Schedule       = list[(sched_step, chosen_index)]      # full record of decisions
DelaySchedule  = dict[sched_step, delay_count]         # deviations from default order; |keys| <= k
Signature      = blake2b(canonical_json(sorted(delay_schedule.items())))
```

Delay schedules are compact (≤ k entries), comparable, and are exactly what a minimal reproduction ships (§14.8).

## 15.3 Schedule generation

```
def explore(root_run, k, N, budget):
    frontier = [DelaySchedule({})]              # the default schedule
    seen     = {signature({})}
    results  = []
    while frontier and len(results) < N and not budget.exceeded():
        ds = frontier.pop(0)                    # BFS: fewer delays first (bug-finding is better at low k)
        run = execute(root_run.scenario, root_run.seed, delay_schedule=ds)
        results.append(run)
        if len(ds) < k:
            for step in interesting_points(run):          # §15.4 reduction
                for alt in range(1, run.choices_at(step)):
                    child = ds | {step: alt}
                    sig = signature(child)
                    if sig not in seen:
                        seen.add(sig); frontier.append(child)
    return Report(results, explored=len(results), unique=len(seen),
                  capped=(len(results) >= N), budget_exceeded=budget.exceeded())
```

BFS by delay count is deliberate: it finds the *simplest* reproducing schedule first, which is also the most useful one for the user.

## 15.4 Partial-order reduction

v1 ships an **independence-based reduction** (not full DPOR):

A scheduling point is *interesting* only if the choice can affect the outcome. A point is skipped when:
1. Only one task is runnable (no choice exists).
2. All runnable tasks' next operations are **independent**: they touch disjoint state keys, do not send on the same edge, and do not contend on the same lock or tool.
3. The point is inside a region where all runnable tasks are executing purely local computation with no observable event.

```
def independent(op_a, op_b) -> bool:
    if op_a.kind == op_b.kind == "state" and op_a.key == op_b.key: return False
    if op_a.edge and op_a.edge == op_b.edge: return False
    if op_a.lock_id and op_a.lock_id == op_b.lock_id: return False
    if op_a.tool and op_a.tool == op_b.tool and op_a.args_hash == op_b.args_hash: return False
    return True
```

**Soundness caveat, stated in the report:** this reduction is sound with respect to *observable coordination events*. It may collapse schedules that differ only in unobserved internal computation. That is acceptable and is stated; claiming more would violate §15.6.

`[OPEN] §43.2.4` — whether to implement sleep sets / full DPOR in P2. Recommendation: only if benchmarking shows redundant exploration >40% on the fixtures.

## 15.5 Duplicate elimination, seeds and termination

- **Duplicates:** eliminated by `Signature`; a schedule is never executed twice.
- **Seeds:** the base seed is fixed across exploration; only the delay schedule varies. This isolates the variable under study — otherwise a "new finding" might be a new seed's artifact rather than a genuine interleaving.
- **Termination** is guaranteed by three independent bounds: `k` bounds tree depth, `N` bounds executions, `time_budget_s` bounds wall clock. Exploration cannot run forever even on a pathological graph.

## 15.6 Reporting and the honesty requirement

> **Mandatory statement, verbatim, in the CLI output, the API response, the UI panel and any exported report:**
> **"Bounded search: absence of findings is not proof of absence."** `[SOURCE]`

The exploration report always contains:

```
Bounded schedule exploration
  delay bound (k)        2
  schedules executed     143  (cap 200)
  unique schedules       143
  reduced away           612   (provably equivalent under independence)
  new findings           1     (write_write draft.module_a, first seen at k=1)
  coverage               bounded — absence of findings is not proof of absence
```

In the UI this appears as a persistent caption under the exploration panel, not a dismissible tooltip. In the API it is a required field `coverage_statement` on the exploration resource, so downstream consumers cannot render the result without it. This intellectual honesty is worth more than an overclaim. `[SOURCE]`

---

# 16. Performance Analysis Engine

All analysis is a **pure function over the sealed event log**. `[SOURCE]` No analyser touches live agent objects; the same log always yields the same analysis, which is itself asserted in CI (§33.10).

## 16.1 Critical path

### 16.1.1 Constructing the timing DAG

Nodes are **leaf spans** (`llm_call`, `tool_call`, `wait`, and `agent_step` segments not covered by a child span). Parent `agent_step` spans are decomposed into segments so that no virtual millisecond is counted twice.

Edges, with weights in virtual milliseconds:

| Edge kind | From → To | Weight | Source |
|---|---|---|---|
| Program order | span *n* end → span *n+1* start, same slot | gap (idle within the agent) | span events |
| Message causality | `message_send` span → the span opened by `message_recv` | `recv.virtual_ts − send.virtual_ts` (the handoff) | message pair |
| Data dependency | `state_write` span → the first `state_read` span of that key in another slot **that happens-after it** | gap | state ops + causality |
| Retry | crashed/failed span → its retry span | gap | `retry_of` |
| Fan-in join | all producers → the consuming span | per-producer gap | `causal_parents` (this is why §9.1.1 matters) |
| Run boundary | virtual `START` node → first span; last span → `END` | 0 | run events |

Only data dependencies that are **also** ordered by happens-before are added as edges; an unordered (racing) access is not a dependency, it is a finding. This keeps the two graphs consistent.

### 16.1.2 Longest weighted path

```
def critical_path(dag) -> tuple[list[Span], int]:
    order = topological_sort(dag)          # log is already topologically sorted by seq
    dist  = {n: 0 for n in dag.nodes}
    prev  = {n: None for n in dag.nodes}
    for n in order:
        for m, w in dag.successors(n):
            cand = dist[n] + w + duration(m)
            if cand > dist[m] or (cand == dist[m] and tiebreak(n) < tiebreak(prev[m])):
                dist[m] = cand; prev[m] = n
    end  = max(dag.nodes, key=lambda n: dist[n])
    return reconstruct(prev, end), dist[end]
```

- **Determinism:** ties are broken by `(end_seq, span_id)`, so the reported path is stable across analyses of the same log — a requirement, since the UI highlights it and the verdict cites it.
- **Validation invariant:** `critical_path_length ≤ virtual_makespan`. If `critical_path_length < makespan`, the difference is *unexplained idle time* and appears as the residual bucket. If `critical_path_length > makespan`, the DAG is wrong — raise `E-ANLZ-003`. This invariant is the primary correctness check on the whole engine.

### 16.1.3 Parallelism metrics

```
total_work        = Σ duration(leaf spans of kind llm_call|tool_call)     # virtual ms
average_parallelism = total_work / critical_path_length
overlap(a, b)     = |virtual intervals of a ∩ intervals of b| / min(busy(a), busy(b))
```

`average_parallelism` is the honest measure of how much concurrency actually occurred. A four-way fan-out with `average_parallelism = 1.2` is fake parallelism (§2.2), regardless of what the diagram shows.

## 16.2 Overhead decomposition

### 16.2.1 The two decompositions `[DETAIL]`

The source defines six buckets `[SOURCE]` but does not say *what they decompose*. This matters, and both are needed:

| Decomposition | Denominator | Answers | Used by |
|---|---|---|---|
| **Critical-path decomposition** | `virtual_makespan` | "Where did the elapsed time go?" | Speedup attribution, the headline scorecard |
| **Total-work decomposition** | `Σ all span durations` | "Where did the effort/tokens go?" | Redundancy and cost analysis |

Redundant work usually sits *off* the critical path: two agents duplicating a retrieval in parallel wastes tokens without necessarily extending elapsed time. Reporting redundancy only in the critical-path decomposition would understate it; reporting it only in total work would misattribute the speedup gap. **Both are computed; the scorecard shows the critical-path decomposition and lists redundancy with its own wasted-work figures.**

### 16.2.2 Bucket definitions and computation

For each interval on the critical path, classify by the span or gap that occupies it:

| Bucket | Computation | Notes |
|---|---|---|
| `productive_work` | Σ durations of `llm_call` and `tool_call` leaf spans on the CP, excluding retries and excluding orchestrator-role agents | The only bucket that is not overhead |
| `handoff` | Σ over CP message edges of `(recv.virtual_ts − send.virtual_ts)` | Attributed to the edge, enabling the per-edge ranking in the verdict |
| `blocking_wait` | Σ of CP gaps where the agent had no runnable work and was awaiting a dependency | Distinguished from handoff by cause: handoff is transport, blocking wait is dependency |
| `redundant_work` | Σ of CP durations of spans belonging to a redundancy group (§16.3), minus one representative member | Only the *duplicate* portion is overhead |
| `retry_recovery` | Σ of CP durations of spans with `retry_of` set, plus retry backoff intervals, plus post-fault re-execution | Grows under chaos, which is the point |
| `orchestration` | Σ of CP durations of spans whose agent has `role ∈ {orchestrator, router}` and whose kind is `llm_call`/`tool_call` | Supervisor deliberation that is not task work |
| `residual` | `makespan − Σ(all above)` | **Must be < 2%** `[SOURCE]`; if not, flagged, not hidden |

**Mutual exclusivity:** each critical-path millisecond belongs to exactly one bucket, resolved by this precedence: `retry_recovery` > `redundant_work` > `orchestration` > `productive_work` > `handoff` > `blocking_wait`. Precedence is documented and unit-tested so the classification is never ambiguous.

### 16.2.3 Total validation

```
assert abs(sum(buckets.values()) + residual - virtual_makespan) <= 1     # integer ms
assert residual / virtual_makespan < 0.02                                 # else analysis_warning
```

This is the gate the roadmap sets at week 5: *time buckets sum to wall clock ±2%.* `[SOURCE]`

### 16.2.4 When the residual is large

A residual >2% means AgentDX cannot explain part of the elapsed time — usually an instrumentation gap (an un-shimmed provider, an unwrapped tool, an uninstrumented subgraph). The run is still analysed, and the UI shows the residual as its own bucket labelled **"unattributed"** with a link to `agentdx doctor`. Hiding it would make every other number quietly wrong.

## 16.3 Redundancy detection

```
group_key = blake2b(tool_name ‖ canonical_json(args))          # exact hash only in v1 [SOURCE]
```

A group of size > 1 is reported as redundancy when **all** hold:
1. At least two members are **concurrent** (by vector clock) or occur within the same logical phase, and
2. They are not linked by `retry_of` (a retry is not redundancy), and
3. They are in different clock slots, or in the same slot without an intervening state change to the tool's inputs.

Report: the tool, the argument hash, the member spans, `wasted_virtual_ms = Σ durations − max(duration)`, and `wasted_tokens`.

`[SOURCE]` **v1 stays on exact-hash matching. Semantic dedup (embedding similarity of tool args) is rejected** because it introduces false positives and a model dependency into a deterministic analysis pipeline. Revisit only with a false-positive study (§43.3.2).

## 16.4 Per-edge and per-agent aggregates

For the graph panel and the verdict's recommendation:

| Metric | Definition |
|---|---|
| `edge.message_count` | Messages sent on the edge |
| `edge.total_handoff_ms` | Σ handoff intervals on the edge (all, not just CP) |
| `edge.cp_handoff_ms` | Σ handoff intervals on the edge **on the critical path** — this is what the recommendation ranks by |
| `edge.cp_share` | `cp_handoff_ms / virtual_makespan` — the "61% of critical-path time" figure `[SOURCE]` |
| `agent.busy_ms`, `agent.idle_ms` | Occupancy |
| `agent.cp_ms` | Time this agent occupies on the critical path |
| `agent.tokens` | Prompt + completion tokens |

---

# 17. Single-Agent Baseline

The headline feature's foundation. `[SOURCE]`

## 17.1 Comparability requirements

A baseline must use `[SOURCE]`: the same task, the same model, the same tools, the same relevant cached responses, comparable inputs, and equivalent evaluation conditions.

Operationally:

| Requirement | Implementation |
|---|---|
| Same task | The identical task definition (file or string) from the scenario |
| Same model | The identical model string; a mismatch invalidates the comparison and raises |
| Same tools | The multi-agent run's registered tool set, unioned, with identical schemas |
| Same cached responses where possible | Cache reuse by prompt-suffix matching (§17.2 step 4); reuse rate is measured and graded |
| Comparable inputs | The same initial state; the same seed; the same calibration profile |
| Equivalent evaluation | The same `success_check`; the same sandbox and blast radius (§13.9) |

## 17.2 Baseline generation algorithm

```
def generate_baseline(multi_run) -> Run:
    # 1. Extract the task and the tool set actually used
    task  = multi_run.scenario.task
    tools = union(spans.tool for spans in multi_run.tool_spans)

    # 2. Build a single-agent graph: one node, sequential ReAct-style loop, same model
    graph = SingleAgentGraph(model=multi_run.model, tools=tools,
                             system_prompt=compose_baseline_prompt(multi_run),
                             max_steps=heuristic_step_budget(multi_run))

    # 3. Execute under the same seed, same calibration profile, same sandbox
    run = execute(graph, task=task, seed=multi_run.seed, mode=multi_run.llm_mode,
                  calibration=multi_run.calibration_id, sandbox=multi_run.sandbox)

    # 4. Cache reuse: tool calls with identical (tool,args) hashes reuse recorded results;
    #    LLM calls reuse only on exact key match (prompts differ by construction —
    #    a single agent's prompt is not a multi-agent node's prompt)
    run.cache_reuse_rate = reused_calls / total_calls
    run.baseline_of = multi_run.id
    return run
```

**`compose_baseline_prompt`** concatenates the multi-agent system prompts in topological order into one role description, preserving the tool descriptions verbatim. It is a deterministic template, checked into the repo, printed by `agentdx analyze --show-baseline-prompt`, and **shown in the UI** — a hidden baseline prompt would make the headline number unauditable.

**Honest limitation, stated in the product** `[DETAIL]`: LLM cache reuse between a multi-agent run and its single-agent baseline is necessarily partial, because the prompts genuinely differ. **Tool-call reuse is usually high (60–95%); LLM-call reuse is usually low (0–30%).** This is why a first baseline generation typically needs one `--record` pass, and why §17.5's comparability grade exists.

## 17.3 Metrics and formulas

```
T_multi        = virtual makespan of the multi-agent run           (ms)
T_single       = virtual makespan of the baseline run              (ms)
W_total        = Σ productive span durations in the multi run      (ms)
CP             = critical-path length of the multi run             (ms)

achieved_speedup        = T_single / T_multi
ideal_parallel_speedup  = W_total / CP            # average parallelism — see correction below
overhead_cost           = achieved_speedup - ideal_parallel_speedup      # negative when overhead bites
token_cost_multiplier   = tokens_multi / tokens_single
cost_efficiency         = achieved_speedup / token_cost_multiplier       # speed bought per unit of spend
```

> **`[IMPROVEMENT]` Correction.** PRD v1.0 annotates ideal parallel speedup as *(critical path / total work)* `[SOURCE]`. That ratio is ≤ 1 and cannot produce the 2.40× shown in the same block; the intended quantity is **total work ÷ critical path**. The number and the intent are unchanged. Recorded in Appendix C.

### Attribution of the speedup gap `[DETAIL]`

The source shows per-bucket contributions summing to the overhead cost `[SOURCE]` but does not define how they are computed. Removing overhead is non-additive (removing handoff may expose blocking wait), so a naive proportional split would be wrong. AgentDX uses **normalised marginal attribution**:

```
for bucket b with critical-path duration d_b:
    T_without_b   = T_multi - d_b
    speedup_wo_b  = T_single / T_without_b
    marginal_b    = speedup_wo_b - achieved_speedup            # >= 0

gap = ideal_parallel_speedup - achieved_speedup
attribution_b = -gap * (marginal_b / Σ marginal)               # reported as a negative contribution
```

Displayed with the note *"attribution is normalised; overhead removals are not independent."* This is deterministic, explainable, and does not overstate precision.

## 17.4 The scorecard (canonical output)

```
Coordination Efficiency:  0.83×   ⚠  slower than single-agent
─────────────────────────────────────────────────────────────
Ideal parallel speedup      2.40×   (total work 14.4s / critical path 6.0s)
Achieved speedup            0.83×   (baseline 5.0s / multi-agent 6.0s)
Overhead cost              -1.57×
  handoff latency           -0.71×   3.66s on critical path  [seq 1039→1051]
  blocking wait             -0.52×   2.68s                   [seq 1102→1140]
  redundant tool calls      -0.24×   1.24s, 3,400 tokens     [seq 880, 884]
  orchestration             -0.10×   0.52s                   [seq 210→240]
  unattributed               0.00×   0.04s (0.7%)

Token cost multiplier       3.1×    vs single-agent (42,100 vs 13,600)
Cost efficiency             0.27    speedup per unit token cost

Comparability               B       cache reuse 62% (tools 91%, llm 18%)
Wall time                   1.9s    virtual makespan 6.0s

Verdict: merge `reviewer` into `coder`; the handoff on that edge accounts
         for 61% of critical-path time.
```

`[SOURCE]` for the shape and the verdict line; `[DETAIL]` for the evidence columns, comparability line, and cost efficiency. **Every numeric line carries the event sequence numbers that justify it** — this is the concrete implementation of "every recommendation must connect to measurable evidence".

## 17.5 Comparability grading `[DETAIL]`

| Grade | Condition | Presentation |
|---|---|---|
| **A** | Cache reuse ≥ 80% and identical model/tools/task and both runs succeeded | Speedup shown plainly |
| **B** | Reuse 40–79%, or one run used live calls | Speedup shown with the reuse figure adjacent |
| **C** | Reuse < 40%, or a model/tool mismatch, or the baseline failed the task | Speedup shown **de-emphasised** with a "low comparability" badge; excluded from CI assertions unless `allow_low_comparability: true` |

The grade appears in every surface that shows a speedup number, without exception. A headline number that can be quoted without its comparability is a number that will eventually be quoted wrongly.

## 17.6 Limitations (published, not buried)

1. A single-agent baseline may fail tasks the multi-agent system completes (context limits, capability). That is a legitimate justification for the topology and is reported as `BASELINE_CONTEXT_EXCEEDED` or `BASELINE_FAILED` — never as a speedup.
2. The baseline prompt is a mechanical composition, not an optimised single-agent prompt. A hand-tuned single agent might beat it. The product says so, and `--baseline-prompt <file>` lets a user supply their own.
3. Speedup is measured in **virtual** time, calibrated from recorded wall durations. Virtual and measured wall makespan are always displayed together. `[SOURCE]`
4. Token comparison excludes provider-side caching effects.

---

# 18. Verdict Engine

> **The verdict must never be a black-box LLM opinion. Prefer deterministic calculations wherever possible.** All rules below are pure functions of analysis outputs.

## 18.1 Verdict classes

| Class | Meaning | Primary trigger |
|---|---|---|
| `BENEFICIAL` | The topology earns its overhead | `achieved_speedup ≥ 1.15` and no `critical` findings |
| `NEUTRAL` | No meaningful gain or loss | `0.95 ≤ achieved_speedup < 1.15`, no `critical` findings |
| `NEGATIVE_SPEEDUP` | Measurably slower than one agent | `achieved_speedup < 0.95` |
| `COORDINATION_BOTTLENECK` | A single edge or agent dominates the critical path | any `edge.cp_share ≥ 0.40` or `agent.cp_ms / makespan ≥ 0.60` |
| `STATE_CONFLICT_RISK` | Concurrent conflicting state access detected | ≥1 `state_conflict` finding at `high`/`critical` |
| `UNRELIABLE_TOPOLOGY` | Fails or degrades badly under fault | `resilience_score < 60` or any `SILENT_FAILURE` |
| `NEGATIVE_CAPABILITY` | Multi-agent failed where the baseline succeeded | success flags |
| `BASELINE_FAILED` / `BASELINE_CONTEXT_EXCEEDED` | The comparison is unavailable, with the reason | baseline outcome |
| `INSUFFICIENT_DATA` | Not enough signal to judge | <2 agents, or <5 spans, or residual >20% |

Classes are **not** mutually exclusive in evidence, but exactly one is reported as the headline. Precedence (highest first):

```
UNRELIABLE_TOPOLOGY > STATE_CONFLICT_RISK > NEGATIVE_CAPABILITY > NEGATIVE_SPEEDUP
   > COORDINATION_BOTTLENECK > BASELINE_* > NEUTRAL > BENEFICIAL > INSUFFICIENT_DATA
```

Rationale: reliability outranks performance. A fast topology that loses updates is not a good topology. Secondary classes are shown as badges beneath the headline so nothing is lost.

## 18.2 Scoring formula

A 0–100 **coordination score**, deterministic, used for ranking and regression comparison (never as a replacement for the class):

```
speedup_component     = clamp(achieved_speedup / max(1.0, ideal_parallel_speedup), 0, 1) * 40
efficiency_component  = clamp(productive_work_share_on_cp, 0, 1)                      * 25
reliability_component = (resilience_score / 100)                                      * 25   # 25 if no chaos run
conflict_penalty      = min(25, 10 * count(high|critical state_conflicts))
coordination_score    = round(speedup + efficiency + reliability - conflict_penalty)
```

Thresholds and weights live in `analysis/verdict_rules.toml`, are printed by `agentdx analyze --explain`, and are versioned — because a score whose formula changes silently is worthless for regression comparison.

## 18.3 Severity

| Severity | Assignment rule |
|---|---|
| `critical` | Lost update with divergent values; silent failure under fault; total failure |
| `high` | Any state conflict; negative speedup; a single edge ≥40% of the critical path |
| `medium` | Redundant work >10% of total work; retry amplification >2×; fake fan-out |
| `low` | Redundancy <10%; orchestration >15% of the critical path |
| `info` | Observations with no recommended action |

## 18.4 Evidence contract `[DETAIL]`

Every finding and every verdict line must satisfy:

```jsonc
{
  "claim": "handoff on coder->reviewer accounts for 61% of critical-path time",
  "metric": {"name": "edge.cp_share", "value": 0.61, "unit": "ratio"},
  "evidence": {"event_seqs": [1039, 1043, 1047, 1051],
               "spans": ["a3f19c22b0d1", "b7e2…"],
               "computation": "sum(recv.virtual_ts - send.virtual_ts for CP edges on coder->reviewer) / makespan"},
  "confidence": "high"
}
```

The `computation` string names the deterministic formula used. **A claim without evidence cannot be rendered** — the API rejects it, and the UI has no code path to display one. This is enforced by a schema validator in the analysis pipeline, so the invariant cannot decay.

## 18.5 Confidence

Confidence is a function of data quality, not of statistical inference — AgentDX does not fabricate confidence intervals from single runs.

| Confidence | Conditions |
|---|---|
| `high` | Residual <2%; comparability A; no instrumentation gaps; ≥1 completed baseline |
| `medium` | Residual 2–5%, or comparability B, or ≤2 instrumentation gaps |
| `low` | Residual >5%, or comparability C, or a missing baseline, or value hashes unavailable |

Low confidence de-emphasises the verdict in the UI and excludes it from CI assertions by default.

## 18.6 Recommendations

Recommendations are generated by a **deterministic rule table**, not by a model. Each rule has a trigger, a template and a required evidence set.

| Trigger | Recommendation template |
|---|---|
| `edge.cp_share ≥ 0.40` between A and B, and B's productive work < 25% of the run | "Merge `{B}` into `{A}`; the handoff on that edge accounts for {share}% of critical-path time." |
| Fake fan-out (`average_parallelism < 1.5` with ≥3 branches) | "Branches {list} overlap for only {overlap}%; all block on `{blocker}`. Hoist the shared prefix or start {branch} speculatively." |
| Redundancy group | "`{tool}({args_summary})` executed {n}× concurrently by {agents}; {ms}ms and {tokens} tokens wasted. Memoise the tool or assign it to one agent." |
| `write_write` conflict | "Declare a reducer on `{channel}`, take `agentdx.lock(\"{key}\")`, or route both writers through `{orchestrator}`." |
| `orchestration > 0.15` of CP | "Supervisor deliberation is {pct}% of critical-path time; consider static routing for the {n} deterministic branches." |
| `retry_amplification > 2` | "Retry at the step boundary rather than the branch boundary; add a circuit breaker after {n} consecutive failures." |
| `token_cost_multiplier > 3` with `achieved_speedup < 1.2` | "The topology costs {x}× the tokens for {y}× the speed; a single agent is the better trade at this task size." |

Adding a rule requires adding a test that fires it and a test that does not (§33.11).

---

# 19. Resilience Scoring

Per fault scenario: task success under fault ÷ baseline success, recovery time (virtual), retry amplification, and whether degradation was graceful or silent. `[SOURCE]`

## 19.1 Inputs

| Input | Source |
|---|---|
| `baseline_success` | The no-fault phase's `success_check` result (0 or 1; or a rate across repeats) |
| `fault_success` | The fault phase's `success_check` result |
| `recovery_time_virtual_ms` | Virtual ms from `fault_injected` to the first successful completion of the affected subgraph |
| `retries_base`, `retries_fault` | Count of spans with `retry_of` |
| `degradation_class` | Classified per §19.5 |

## 19.2 Success ratio

```
success_ratio = clamp(fault_success / max(baseline_success, epsilon), 0, 1)
```

If `baseline_success == 0`, the experiment is invalid — abort at the steady-state check (§13.5) rather than dividing.

## 19.3 Recovery time

```
recovery_component = clamp(1 - recovery_time_virtual_ms / recovery_budget_ms, 0, 1)
recovery_budget_ms = scenario.recovery_budget_ms  or  2 × baseline_virtual_makespan   # default
```

Never recovering (the run fails) scores 0 for this component and is also caught by `success_ratio`.

## 19.4 Retry amplification

```
amplification       = retries_fault / max(retries_base, 1)
amplification_component = clamp(1 - (amplification - 1) / amplification_budget, 0, 1)
amplification_budget    = 4.0    # 5× retries scores 0
```

Token amplification is reported alongside but does not enter the score (it is a cost signal, not a reliability signal); it appears in the breakdown and in the verdict's recommendations.

## 19.5 Degradation classification

| Class | Definition | Weight |
|---|---|---|
| `graceful` | The run either succeeded, or failed **and reported the failure** (a surfaced error, a declared fallback, a partial result marked partial) | 1.0 |
| `degraded_flagged` | Succeeded with reduced quality **and** the system emitted a signal (a warning, a low-confidence marker, a fallback path taken) | 0.6 |
| `hard_failure` | Failed loudly with a clear error | 0.4 |
| `silent_failure` | **Reported success while `success_check` failed** | 0.0 |

`silent_failure` is the worst outcome in the model because it is the one that reaches users undetected (§2.7).

## 19.6 Per-fault score and aggregation

```
fault_score = 100 × ( 0.50 × success_ratio
                    + 0.20 × recovery_component
                    + 0.15 × amplification_component
                    + 0.15 × degradation_weight )

aggregate   = weighted_mean(fault_scores, weights = scenario.fault_weights or equal)
if any(f.degradation_class == "silent_failure"):
    aggregate = min(aggregate, 49)          # hard cap
resilience_score = round(aggregate)
```

Also always reported: `worst_fault_score`, `n_faults`, and the full per-fault table.

## 19.7 Aggregation rules (non-negotiable)

1. **The aggregate never appears without the per-fault breakdown** `[SOURCE]` — enforced in the API (the `resilience` resource embeds `per_fault[]` as a required field) and in the UI (the score component will not render without its table).
2. Aborted runs (`ABORTED_GUARD`) are **excluded and listed**, never scored as 0 — a guard trip is an experimental artifact, not a system failure.
3. Faults that never fired are excluded and listed prominently.
4. A single `silent_failure` caps the aggregate below 50, regardless of every other result.
5. Scores are comparable only within the same scenario set; comparing across different fault catalogues is meaningless and the CLI refuses to do it (`agentdx compare` checks scenario hashes).

## 19.8 Example output

```
Resilience: 71 / 100        (4 faults, worst 38)

  fault                          success  recovery  retry-amp  degradation  score
  ─────────────────────────────────────────────────────────────────────────────
  agent_crash(reviewer)            1.00    1.8s      1.0×       graceful      96
  tool_failure(search, 429)        1.00    4.2s      3.2×       degraded      74
  message_drop(coder->tester,0.2)  0.50    n/a       1.0×       hard_failure  38
  latency(planner->coder,+2s)      1.00    0.0s      1.0×       graceful      95

  not fired: none      aborted: none
```

---

# 20. Replay & Time Travel

## 20.1 Run loading

```
GET /api/runs/{id}                → metadata + verdict
GET /api/runs/{id}/events?from_seq=0&limit=1000   → paginated log
GET /api/runs/{id}/waterfall      → spans with virtual start/end + CP flags
GET /api/runs/{id}/graph          → nodes, edges, aggregates
GET /api/runs/{id}/findings       → severity-ranked
```

The frontend loads metadata and the waterfall first (the panels that carry the story), then streams events in the background. For runs above 20 000 events the client fetches lazily by virtual-time window rather than by page.

## 20.2 Timeline scrubbing

The scrubber is indexed by **virtual timestamp**, not by event index, because virtual time is what the waterfall renders. A scrub position maps to an event via binary search on `virtual_ts_ms`, breaking ties by taking the **highest `seq`** at that timestamp (so the state shown is "after everything that happened at t").

Keyboard controls `[SOURCE]`: `←`/`→` step one event; `shift+←`/`shift+→` step ten; `j`/`k` jump between findings; `space` play/pause; `home`/`end` jump to run bounds.

## 20.3 Event selection and cross-panel linking

Selecting an event, a span or a finding updates one shared selection object in the store; every panel derives its highlight from it. `[SOURCE]` Clicking a finding highlights the two conflicting spans in the waterfall **and** the two nodes in the graph simultaneously — cross-panel linking is the whole reason for three panels. `[SOURCE]`

## 20.4 State reconstruction at an arbitrary virtual timestamp

```
def state_at(run_id, virtual_ts) -> dict[str, ValueRef]:
    target_seq = last_seq_at_or_before(run_id, virtual_ts)
    snap = nearest_snapshot(run_id, target_seq)              # snapshots every 500 events
    state = dict(snap.state) if snap else {}
    for e in events(run_id, from_seq=snap.seq + 1 if snap else 0, to_seq=target_seq):
        if e.type == "state_write":
            state[e.payload.key] = ValueRef(hash=e.payload.value_hash,
                                            writer=e.agent_id, seq=e.seq,
                                            body=e.payload.get("value"))   # body only if captured
    return state
```

- **Snapshots** are materialised at every 500th event during ingestion (cheap: a dict of key → hash) and stored in `state_snapshots`. Reconstruction is therefore O(500) worst case regardless of run length. Target: <100 ms for a 5 000-event run (§32).
- **With `capture_bodies=False`, the state table shows value hashes, sizes and writers, not values.** This is honest and still useful: the *identity* of the value and *who wrote it* is what conflict analysis needs. A tooltip explains how to enable bodies.
- Correctness is asserted by a test that folds from scratch and compares to the snapshot-accelerated fold at 50 random timestamps (§33.7).

## 20.5 Graph synchronisation

At a given scrub position the graph panel renders: nodes coloured by their state at that instant (`idle` / `running` / `blocked` / `crashed`), edges weighted by messages delivered **so far**, a ring on any agent with an active fault, and the critical path highlighted along its edges.

## 20.6 Finding navigation and conflict jumping

The findings list is severity-ranked; selecting one seeks the timeline to `min(event_a.virtual_ts, event_b.virtual_ts)` and marks both events on the scrubber. `j`/`k` move through findings in rank order.

## 20.7 Replay bundles — the `.agentdx` format

A `.agentdx` file is a **zip archive** (chosen over tar for cross-platform tooling and random access):

```
run.agentdx
├── manifest.json          # schema_version, agentdx_version, run_id, hashes, created_at
├── run.json               # run metadata, verdict, scorecard, analysis version
├── events.jsonl.zst       # the full canonical event log
├── events.sha256          # canonical_log_hash for verification
├── scenario.yaml          # the exact scenario, including resolved defaults
├── cache/
│   ├── manifest.json      # cache keys + response hashes required by this run
│   └── entries.jsonl.zst  # the cache slice (bodies) — omitted unless --include-cache-bodies
├── calibration.json       # the calibration profile used
└── graph.json             # graph identity: nodes, edges, tools, hashes
```

**Export:** `agentdx export <run_id> -o run.agentdx [--include-cache-bodies] [--sanitise]`
**Import:** `agentdx import run.agentdx` → registers the run locally; `--verify` re-executes and asserts canonical-log equality (§11.10).

Without cache bodies a bundle is **viewable and analysable** but not re-executable; the manifest records which, and the CLI says so on import. The default excludes bodies for privacy (§31.3).

---

# 21. Scenario System

## 21.1 Schema (v1, complete)

```yaml
version: 1                              # required; schema version
scenario: reviewer_crash_midflight      # required; unique slug
description: "Reviewer dies mid-flight; does the pipeline recover?"   # optional

target:                                 # required (one of)
  fixture: code_pipeline                # a shipped fixture, OR
  graph: "./app.py:graph"               # a user graph (import path)

task: fixtures/tasks/refactor_module.md # required; path or inline string
seed: 42                                # required for reproducibility (default 0)

mode: replay                            # replay | record | perturb | passthrough  (default: replay)
repeats: 1                              # number of executions per phase (default 1)

chaos_opt_in: false                     # REQUIRED true to fault a user graph (§13.3)

hypothesis:                             # steady-state; evaluated on the baseline phase
  task_success: ">= 0.9"
  p95_virtual_duration_ms: "<= 45000"

blast_radius:                           # REQUIRED when faults target a user graph
  agents: [reviewer]
  tools: []
  edges: []
  state_keys: []
  providers: []

faults:
  - type: agent_crash
    agent: reviewer
    at_virtual_ts: 2400
    recoverable: false

guards:
  max_virtual_duration_ms: 120000
  max_tokens: 200000
  max_retries: 20
  max_wall_duration_s: 300

baseline:
  generate: true                        # auto-generate the single-agent baseline
  prompt: null                          # optional override file
  allow_low_comparability: false

exploration:                            # optional; FR-6
  enabled: false
  k: 2
  max_schedules: 200

success_check:                          # pluggable semantic assertion (§21.6)
  type: python                          # python | shell | none
  ref: "fixtures.code_pipeline.checks:module_a_compiles"

assertions:
  - no_state_conflicts
  - speedup_vs_baseline: ">= 1.0"
  - resilience_score: ">= 70"
  - max_findings: {severity: high, count: 0}
```

The source example is preserved exactly within this schema `[SOURCE]`; every added key is optional with a documented default, except `chaos_opt_in`/`blast_radius`, which are required only in the case §13.3 defines.

## 21.2 Source scenario, unchanged and still valid

```yaml
scenario: reviewer_crash_midflight
task: fixtures/tasks/refactor_module.md
seed: 42
hypothesis:
  task_success: ">= 0.9"
  p95_virtual_duration_ms: "<= 45000"
faults:
  - type: agent_crash
    agent: reviewer
    at_virtual_ts: 2400
guards:
  max_virtual_duration_ms: 120000
  max_tokens: 200000
assertions:
  - no_state_conflicts
  - speedup_vs_baseline: ">= 1.0"
```
`[SOURCE]` — loads under v1 with `version: 1` and `target.fixture` inferred from the task path, or supplied by `--fixture`.

## 21.3 Validation rules

| Rule | Error |
|---|---|
| Unknown top-level or nested key | `E-SCEN-002` — **errors, not warnings**; a typo in `blast_radius` must never silently widen scope |
| `version` missing or unsupported | `E-SCEN-001` |
| Both/neither of `target.fixture` and `target.graph` | `E-SCEN-003` |
| Faults present, target is a user graph, and (`chaos_opt_in != true` or `blast_radius` empty) | `E-SCEN-004` |
| Fault target not present in the graph | `E-SCEN-005`, listing valid targets |
| Assertion references an unavailable metric (e.g. `resilience_score` with no faults) | `E-SCEN-007` |
| Guard value above the hard ceiling | `E-SCEN-008` |
| `success_check.ref` not importable | `E-SCEN-009`, at load time, not at the end of the run |

All validation happens **before any execution**. Failing after a 40-second run because of a typo is unacceptable in CI.

## 21.4 Defaults

| Key | Default | Rationale |
|---|---|---|
| `mode` | `replay` | Offline and free is the right default |
| `seed` | `0` | Deterministic even when unspecified |
| `repeats` | `1` | |
| `guards.*` | §13.6 | Safety by default |
| `baseline.generate` | `true` for graphs with ≥2 agents | The headline feature should not need opting in |
| `exploration.enabled` | `false` | Cost control |
| `success_check` | `none` (run completion is the criterion) | Works without user code |

## 21.5 Versioning and reuse

- `version` is the schema version; unknown minor keys within a known major are ignored with a warning, unknown keys within the current version are errors.
- **Composition:** `extends: base_scenario.yaml` performs a deep merge with lists replaced (not concatenated) — replacement avoids the classic "inherited fault I did not intend" hazard.
- **Matrices:** `matrix: {fault_type: [agent_crash, tool_failure], seed: [1,2,3]}` expands to the cross product, each expansion getting a derived scenario id. This is how a chaos matrix is expressed, and it is cheap because virtual time makes each run sub-second.

## 21.6 The pluggable assertion hook

AgentDX judges coordination and **delegates semantic correctness** `[SOURCE]`:

```python
# fixtures/code_pipeline/checks.py
def module_a_compiles(final_state: dict, run: RunSummary) -> bool | tuple[bool, str]:
    src = final_state.get("draft.module_a")
    return compile_ok(src), "syntax error at line 12"
```

- Executed after the run is sealed, in the sandbox, with a time limit (5 s wall).
- Result recorded as an `assertion_result` event, so it is part of the log and therefore part of the evidence.
- Shell form: `type: shell, ref: "pytest -q fixtures/code_pipeline/test_output.py"`, exit code 0 = success.
- **No LLM judge in v1**, by design (§4.4).

## 21.7 Built-in assertions

| Assertion | Passes when |
|---|---|
| `no_state_conflicts` | Zero `state_conflict` findings at `high`/`critical` |
| `speedup_vs_baseline: "<op> <v>"` | Achieved speedup satisfies the comparison (requires comparability ≥ B unless overridden) |
| `resilience_score: "<op> <v>"` | Aggregate satisfies the comparison |
| `max_findings: {severity, count}` | Findings at or above `severity` ≤ `count` |
| `critical_path_share: {edge, "<= 0.4"}` | Named edge's CP share within bound |
| `token_cost_multiplier: "<op> <v>"` | Token multiplier within bound |
| `no_silent_failures` | No fault classified `silent_failure` |
| `task_success` | `success_check` passed |
| `deterministic_replay` | A verification replay matched the canonical hash |

---

# 22. CI/CD Integration

## 22.1 Command

```
agentdx run scenarios/ --ci [--format junit|json|github] [--out DIR]
                            [--baseline-run PATH] [--jobs N] [--fail-on SEVERITY]
```

`[SOURCE]` — `agentdx run scenarios/ --ci` exits non-zero on assertion failure and ships with a GitHub Action example.

Behaviour in `--ci` mode, beyond a normal run:
1. **Replay mode is forced.** A scenario requesting `record` fails with `E-CI-001` — CI must never spend money or depend on a network.
2. **Non-interactive output**: no spinners, no colour unless `FORCE_COLOR`, one line per scenario.
3. **Machine-readable artifacts** written to `--out` (default `.agentdx/ci`).
4. **Bundles for failures** exported automatically, so a failing CI run is immediately reproducible locally.
5. **Deterministic ordering**: scenarios execute in sorted-path order; `--jobs N` parallelises across processes, each independently deterministic.

## 22.2 Exit codes

| Code | Meaning |
|---|---|
| 0 | All scenarios passed all assertions |
| 1 | At least one assertion failed |
| 2 | Usage / configuration / scenario validation error |
| 3 | LLM cache miss in replay mode |
| 4 | Guard aborted a run |
| 5 | Internal error (AgentDX defect) |
| 6 | Determinism verification failed |
| 7 | No scenarios found at the given path |

Stable across releases; changing one is a breaking change (§37.2 is the authoritative table).

## 22.3 Assertion evaluation

```
for scenario in sorted(discover(path)):
    result = execute_and_analyse(scenario)
    for assertion in scenario.assertions:
        outcome = evaluate(assertion, result)     # pure function over analysis output
        record(scenario, assertion, outcome)      # expected, actual, evidence seqs
```

Every assertion outcome records **expected, actual, and the evidence event sequence numbers**, so a CI failure message is actionable without opening the UI.

## 22.4 Machine-readable output

**JUnit XML** (for native CI rendering): one `<testsuite>` per scenario, one `<testcase>` per assertion, `<failure>` carrying expected/actual and the bundle path.

**JSON summary:**

```jsonc
{
  "agentdx_version": "0.4.0", "schema_version": 1,
  "started_at": "2026-08-08T10:22:11Z", "duration_wall_s": 41.2,
  "scenarios": [{
      "scenario": "reviewer_crash_midflight", "run_id": "r_f2a91",
      "status": "failed",
      "assertions": [
        {"name": "no_state_conflicts", "status": "failed",
         "expected": 0, "actual": 1,
         "evidence": {"event_seqs": [1043, 1051], "finding_ids": ["f_0117"]}},
        {"name": "speedup_vs_baseline", "status": "passed", "expected": ">= 1.0", "actual": 1.42}
      ],
      "verdict": {"class": "STATE_CONFLICT_RISK", "coordination_score": 61, "confidence": "high"},
      "bundle": ".agentdx/ci/r_f2a91.agentdx"
  }],
  "totals": {"scenarios": 3, "passed": 2, "failed": 1, "assertions": 11, "failed_assertions": 1}
}
```

## 22.5 GitHub Actions example (shipped in-repo)

```yaml
name: agentdx
on: [pull_request]
jobs:
  reliability:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --frozen
      - name: Restore LLM cache
        uses: actions/cache@v4
        with:
          path: .agentdx/cache.db
          key: agentdx-cache-${{ hashFiles('fixtures/**', 'scenarios/**') }}
      - name: Run reliability scenarios
        run: uv run agentdx run scenarios/ --ci --format junit --out ci-out/
      - uses: actions/upload-artifact@v4
        if: always()
        with: {name: agentdx-results, path: ci-out/}
```

The committed fixture cache means this workflow runs with **no secrets configured** — a property worth stating in the README, since it is unusual for anything LLM-adjacent.

## 22.6 Regression comparison

```
agentdx run scenarios/ --ci --baseline-run .agentdx/baselines/main.json
```

| Metric | Default tolerance | Fails when |
|---|---|---|
| `achieved_speedup` | −5% relative | New value below baseline − 5% |
| `resilience_score` | −3 absolute | New score below baseline − 3 |
| `coordination_score` | −5 absolute | Below baseline − 5 |
| `token_cost_multiplier` | +10% relative | Above baseline + 10% |
| `findings[high+]` | 0 new | Any new high/critical finding |

Baselines are refreshed by an explicit `agentdx baseline update` on the trunk branch — never automatically, because an auto-refreshing baseline silently ratchets regressions in.

## 22.7 Failure messages

```
FAIL  scenarios/reviewer_crash_midflight.yaml
  ✗ no_state_conflicts
      expected: 0 conflicts (severity >= high)
      actual:   1  → LOST UPDATE on draft.module_a
                     coder wrote at seq 1043 (t=2418ms)
                     reviewer wrote at seq 1051 (t=2431ms)
      reproduce: agentdx import ci-out/r_f2a91.agentdx && agentdx ui
  ✓ speedup_vs_baseline  (1.42 >= 1.0)

1 scenario failed, 2 passed  (11 assertions, 1 failed)  in 41.2s
```

Every failure message names what failed, the concrete values, and **the exact command to reproduce locally**. A gate that fails without telling you how to reproduce it gets disabled (§3.4).

---

# 23. Reference Fixture Systems

Three deliberately imperfect multi-agent systems shipped in-repo, each seeded with a *known* defect so the demo always finds something real. `[SOURCE]` They are simultaneously the demo, the regression suite and the false-positive proof.

Common properties: LangGraph-based; Llama 3.1 8B via the OpenAI-compatible shim; committed LLM cache so every fixture runs offline; a `checks.py` with the semantic `success_check`; a golden findings file diffed in CI.

## 23.1 Fixture 1 — Code Pipeline `[SOURCE]`

**Architecture:** `planner → coder → reviewer → tester`, with `coder` and `reviewer` able to run concurrently on different modules after planning.

```
            ┌──────────┐
            │ planner  │  role=orchestrator
            └────┬─────┘
        ┌────────┴────────┐
        ▼                 ▼
   ┌─────────┐       ┌──────────┐
   │  coder  │◀─────▶│ reviewer │      ← both write draft.module_a
   └────┬────┘       └────┬─────┘
        └────────┬────────┘
                 ▼
            ┌─────────┐
            │ tester  │
            └─────────┘
```

| Element | Detail |
|---|---|
| **Agents** | `planner` (orchestrator), `coder` (worker), `reviewer` (worker), `tester` (worker) |
| **Tools** | `read_file`, `write_draft`, `run_tests` (simulated, deterministic), `lint` |
| **State keys** | `plan`, `draft.module_a`, `draft.module_b`, `review_notes`, `test_results` |
| **Task** | `fixtures/tasks/refactor_module.md` — refactor a small Python module and make its tests pass |
| **Seeded defect** | **Write-write race on `draft.module_a`.** `[SOURCE]` The planner dispatches `coder` and `reviewer` concurrently; `reviewer` writes a corrected version of the draft while `coder` writes its own revision. The channel has **no reducer**. Last writer wins; `coder`'s edit is silently lost |
| **Secondary defect** | The `coder→reviewer` handoff is deliberately heavy (full draft serialised each way), making it ≈60% of critical-path time — this drives the `NEGATIVE_SPEEDUP` verdict |

**Expected AgentDX findings** (the golden file):

| # | Finding | Severity |
|---|---|---|
| 1 | `state_conflict/write_write` on `draft.module_a`, `coder` vs `reviewer`, divergent values | critical |
| 2 | `coordination_bottleneck`: `coder→reviewer` CP share ≥ 0.40 | high |
| 3 | Verdict `STATE_CONFLICT_RISK` (precedence over `NEGATIVE_SPEEDUP`, which appears as a secondary badge) | — |
| 4 | Achieved speedup < 1.0 | — |

**Healthy behaviour after the documented fix** (add a reducer or route through the planner): finding 1 disappears; findings 2 and 4 remain (they are a topology problem, not a concurrency problem) — a deliberate teaching moment about the two distinct classes.

**Test expectations:** race detected on a default single run; reproduction scenario re-triggers 10/10; exploration finds it at `k ≤ 2`; after the fix, `no_state_conflicts` passes.

## 23.2 Fixture 2 — Support Triage `[SOURCE]`

**Architecture:** `classifier → retriever_a ∥ retriever_b → responder`.

| Element | Detail |
|---|---|
| **Agents** | `classifier` (worker), `retriever_a`, `retriever_b` (workers), `responder` (worker) |
| **Tools** | `vector_search`, `kb_lookup`, `ticket_history` |
| **State keys** | `ticket`, `category`, `context.docs`, `response_draft` |
| **Task** | Classify a support ticket and draft a response grounded in retrieved docs |
| **Seeded defect 1** | **Redundant retrieval.** `[SOURCE]` Both retrievers call `vector_search` with identical arguments derived from the ticket text |
| **Seeded defect 2** | **Fake parallelism.** `[SOURCE]` Both branches await `classifier` completion, so the fan-out never overlaps meaningfully; pairwise overlap <10% |

**Expected findings:**

| # | Finding | Severity |
|---|---|---|
| 1 | `redundant_work`: `vector_search` ×2, identical args, wasted virtual ms and tokens quantified | medium |
| 2 | `fake_fanout`: average parallelism <1.5 across a declared 2-way fan-out; both branches blocked on `classifier` | medium |
| 3 | Verdict `COORDINATION_BOTTLENECK` or `NEUTRAL` depending on measured timings; token cost multiplier >2× | — |
| 4 | **Zero state conflicts** — this fixture must not produce a race finding | — |

Point 4 matters: fixture 2 is a *partial* false-positive control. It has performance defects and no concurrency defects, so it proves the detectors are independent rather than one alarm wired to everything.

## 23.3 Fixture 3 — Research Fan-out (the healthy control) `[SOURCE]`

**Architecture:** `supervisor → worker_1..4 → synthesiser`. Genuinely parallel; use it to prove AgentDX doesn't cry wolf. `[SOURCE]`

| Element | Detail |
|---|---|
| **Agents** | `supervisor` (orchestrator), `worker_1..4` (workers), `synthesiser` (worker) |
| **Tools** | `web_search` (replayed), `summarise` |
| **State keys** | `question`, `subtopics`, `findings` (**declared with a list reducer** — concurrent appends are intentional and must be recognised as `benign_merge`), `report` |
| **Task** | Answer a research question by dividing it into four subtopics |
| **Seeded defect** | **None.** This is the control |

**Expected findings: none above `info`.**

| Check | Requirement |
|---|---|
| State conflicts | **0** — despite four agents writing `findings` concurrently, because the channel declares a reducer (guard G3, §14.7) |
| Redundancy | 0 — subtopics are disjoint by construction |
| Fake fan-out | Not flagged — average parallelism ≥ 3.0 |
| Verdict | `BENEFICIAL`, achieved speedup > 1.0 |
| Resilience under `agent_crash(worker_3)` | Graceful — the synthesiser proceeds with three findings and marks the result partial |

> **A tool that only ever reports problems is a tool nobody trusts.** `[SOURCE]` Fixture 3 is the most important fixture in the repository. Its golden findings file must be **empty above `info`**, and any change that adds a finding to it fails CI (§33.9).

## 23.4 Fixture matrix

| Property | Code Pipeline | Support Triage | Research Fan-out |
|---|---|---|---|
| Agents | 4 | 4 | 6 |
| Concurrency | Real (unsafe) | Fake | Real (safe) |
| Seeded race | ✅ write-write | ❌ | ❌ |
| Seeded redundancy | ❌ | ✅ | ❌ |
| Seeded bottleneck | ✅ handoff | ✅ serialisation | ❌ |
| Reducer-declared channel | ❌ (the defect) | ❌ | ✅ (`findings`) |
| Expected verdict | `STATE_CONFLICT_RISK` | `COORDINATION_BOTTLENECK` | `BENEFICIAL` |
| Expected findings | 2 | 2 | **0** |
| Role in the suite | Demo + race regression | Perf regression | **False-positive control** |

---

# 24. System Architecture

## 24.1 Layered view

The source architecture `[SOURCE]` is preserved and annotated with process boundaries and interfaces.

```
┌──────────── YOUR AGENT SYSTEM (LangGraph / plain Python) ──────────────────────┐
│    planner ──▶ coder ──▶ reviewer ──▶ tester        shared state store          │
└──────┬─────────────────────────────────────────────────────────────┬───────────┘
       │  instrumented via decorator / LangGraph callback adapter    │
┌──────▼─────────────────────────────────────────────────────────────▼───────────┐
│ AGENTDX RUNTIME  (single Python process, single OS thread for the scheduler)   │
│  ┌────────────────┐  ┌──────────────┐  ┌────────────────┐  ┌────────────────┐  │
│  │ Deterministic  │──│ Fault        │──│ LLM record/    │──│ Event writer   │  │
│  │ scheduler +    │  │ injector     │  │ replay cache   │  │ (append-only)  │  │
│  │ virtual clock  │  │              │  │ (SQLite)       │  │                │  │
│  └────────────────┘  └──────────────┘  └────────────────┘  └───────┬────────┘  │
│         ▲ interception points ▲                                     │           │
└─────────────────────────────────────────────────────────────────────┼───────────┘
                                                                      ▼
                                                            ┌──────────────────┐
                                                            │ EVENT STORE      │
                                                            │ SQLite (WAL)     │
                                                            │ + Parquet/DuckDB │
                                                            └────────┬─────────┘
┌──────────────────────────────────────────────────────────────────▼────────────┐
│ ANALYSIS LAYER — pure functions over the event log                             │
│  causality (vector clocks) · race detector · timing DAG · critical path ·      │
│  overhead decomposer · redundancy detector · baseline comparator ·             │
│  resilience scorer · verdict engine · exploration coordinator                  │
└──────────────────────────────────────────────────────────────────┬────────────┘
┌──────────────────────────────────────────────────────────────────▼────────────┐
│ API — FastAPI: REST for runs/reports, WebSocket for the live event stream      │
└──────────────────────────────────────────────────────────────────┬────────────┘
┌──────────────────────────────────────────────────────────────────▼────────────┐
│ CONTROL TOWER — React + Vite + TypeScript + React Flow + Zustand + Tailwind    │
│                 + visx                                                         │
└────────────────────────────────────────────────────────────────────────────────┘
```

## 24.2 Process boundaries

| Process | Contains | Lifetime | Notes |
|---|---|---|---|
| **CLI / runner** | Runtime, scheduler, SDK, injector, cache, event writer, analysis | One run (or one exploration batch) | The scheduler runs on **one OS thread**; user code that spawns threads is rejected (§10.6) |
| **API server** | FastAPI + analysis (read-only) + static frontend | Long-lived (`agentdx ui`) | Reads the same SQLite files; never writes events |
| **Browser** | Control Tower | User session | Talks REST + WS only |

The runner and the API server are separate processes sharing SQLite in WAL mode (one writer, many readers — exactly the concurrency profile WAL is designed for). The API server may **launch** a run as a subprocess (`POST /api/runs`) and stream its events over WebSocket by tailing the event table.

## 24.3 Module map and interfaces

| Module | Responsibility | Key interface | Depends on |
|---|---|---|---|
| `agentdx.events` | Schema, validation, canonical form, writer | `EventWriter.write(Event)`, `canonical_log_hash(events)` | — |
| `agentdx.runtime.clock` | Virtual clock | `VirtualClock.now/advance_to/advance_by` | — |
| `agentdx.runtime.scheduler` | Cooperative scheduling, seeded choice, yield points | `Scheduler.run()`, `Scheduler.register(task)` | clock, events |
| `agentdx.runtime.faults` | Fault registry, triggers, interception, blast radius | `FaultInjector.check(point, ctx)` | scheduler, events |
| `agentdx.runtime.cache` | LLM record/replay/perturb | `Cache.get(key)`, `Cache.put(key, resp)` | storage |
| `agentdx.sdk` | Decorators, LangGraph adapter, provider shims | `instrument(graph)`, `@agent`, `@tool` | runtime, events |
| `agentdx.store` | SQLite/DuckDB access, migrations, snapshots, bundles | `Store.append(events)`, `Store.events(run, from_seq)` | — |
| `agentdx.analysis.causality` | Vector clocks, happens-before | `build_causality(events)` | events |
| `agentdx.analysis.race` | Race detection, guards, repro | `detect_conflicts(events) -> [Finding]` | causality |
| `agentdx.analysis.timing` | Timing DAG, critical path, parallelism | `critical_path(events)` | events |
| `agentdx.analysis.overhead` | Six-bucket decomposition, redundancy | `decompose(events, cp)` | timing |
| `agentdx.analysis.baseline` | Baseline generation and comparison | `generate_baseline(run)`, `compare(a, b)` | runtime, cache |
| `agentdx.analysis.resilience` | Per-fault and aggregate scoring | `score(baseline, fault_runs)` | events |
| `agentdx.analysis.verdict` | Class, score, confidence, recommendations | `verdict(analysis) -> Verdict` | all analysers |
| `agentdx.explore` | Delay-bounded exploration | `explore(root, k, N)` | scheduler, race |
| `agentdx.scenario` | YAML schema, validation, matrices, assertions | `load(path) -> Scenario`, `evaluate(assertion, analysis)` | — |
| `agentdx.api` | FastAPI app, REST, WebSocket | HTTP | store, analysis |
| `agentdx.cli` | Commands, exit codes, output formatting | argv | everything |

**Dependency rule (enforced by an import-linter check in CI):** `analysis.*` may import `events` and `store` but **must not import `runtime.*` or `sdk.*`**. This is the mechanical enforcement of "analysis operates on the event log, never on live agent objects". `[SOURCE]` The one exception is `analysis.baseline`, which must execute a run; it therefore lives behind an explicit `BaselineExecutor` protocol injected by the caller rather than importing the runtime directly.

## 24.4 Data flow (record → analysis → UI)

```
user graph ──emit──▶ SDK ──stamp(seq, vclock, virtual_ts)──▶ writer ──batch──▶ SQLite events
                                                                             │
                                          run sealed ─────────────────────────┤
                                                                             ▼
                                                      export Parquet ──▶ DuckDB views
                                                                             │
                                              analysers (pure, ordered) ◀────┘
                                                    │
                          findings + scorecard + verdict ──▶ SQLite (analysis tables)
                                                    │
                                            FastAPI ──REST/WS──▶ Control Tower
```

## 24.5 Control flow during a run

```
CLI → Scenario.load → validate → Runtime.create_run
  → Scheduler.run
      loop:
        collect_runnable → FaultInjector.check(pre_schedule)
        choose(seeded)   → resume(task)
            task hits a yield point (llm/tool/state/message)
              → FaultInjector.check(pre_*)
              → Cache.get / tool exec / state op
              → EventWriter.write(...)  [stamped under the scheduler lock]
              → schedule completion at now + duration
        advance clock if nothing runnable
  → seal log → run analysers → write verdict → print scorecard → exit
```

## 24.6 Stack `[SOURCE]`

| Layer | Choice | Why |
|---|---|---|
| Runtime | Python 3.12, asyncio | Matches the agent ecosystem |
| Agent framework | LangGraph (primary), generic decorator API (fallback) | Largest install base; graph structure is already explicit |
| Model | Groq — Llama 3.1 8B for agent brains, free tier | Fast + free makes hundreds of runs viable. Accessed through an **OpenAI-compatible shim** (§8.5 `[IMPROVEMENT]`) |
| LLM cache | SQLite | Zero-ops; ships in the repo |
| Event store | SQLite (WAL) → DuckDB for analytical queries | No server, fast aggregations over event logs |
| API | FastAPI + `websockets` | Async-native, matches the runtime |
| Frontend | React 18 + Vite + TypeScript | — |
| Graph | React Flow | Custom node rendering, good perf to ~200 nodes; Cytoscape only if node count explodes |
| Charts | visx (or D3 directly) | The waterfall is custom — a chart library will fight you |
| State | Zustand | Trivial for a scrubbable timeline |
| Styling | Tailwind + CSS custom properties | Tokens map straight to CSS vars |
| Packaging | `uv`, Docker Compose for a one-command demo | — |

---

# 25. Repository / Codebase Architecture

```
agentdx/
├── README.md                     # thesis in one screen + the ghost-baseline GIF
├── pyproject.toml                # uv/hatch; entry point `agentdx`
├── agentdx.toml                  # default project config
├── docker-compose.yml            # one-command demo (§39.2)
├── Dockerfile
│
├── src/agentdx/
│   ├── __init__.py               # public API surface (§8.2)
│   ├── cli/                      # Typer commands; one module per command; exit codes
│   ├── config.py
│   │
│   ├── events/                   # THE CONTRACT. schema, validation, canonical form, writer
│   │   ├── schema.py  validators.py  canonical.py  writer.py  migrations/
│   │
│   ├── runtime/
│   │   ├── scheduler.py          # cooperative scheduler, seeded choice, yield points
│   │   ├── clock.py              # virtual clock + calibration profiles
│   │   ├── context.py            # RunContext / AgentContext (contextvars)
│   │   ├── determinism.py        # patching of random/time/uuid/loop; leak detection
│   │   ├── faults/               # registry, triggers, one module per fault class
│   │   └── cache/                # LLM record/replay/perturb + key construction
│   │
│   ├── sdk/
│   │   ├── decorators.py  generic.py  sync.py
│   │   ├── langgraph.py          # callback adapter, channel proxies, reducer detection
│   │   └── providers/            # openai_compatible.py, groq.py, anthropic.py
│   │
│   ├── store/
│   │   ├── sqlite.py             # schema, WAL, migrations, append-only triggers
│   │   ├── duckdb.py             # analytical views over exported Parquet
│   │   ├── snapshots.py          # state snapshots for time travel
│   │   └── bundle.py             # .agentdx export/import/verify
│   │
│   ├── analysis/                 # PURE. must not import runtime or sdk (§24.3)
│   │   ├── causality.py          # vector clocks, happens-before
│   │   ├── race.py               # detection, guards, classification, repro
│   │   ├── timing.py             # timing DAG, critical path, parallelism
│   │   ├── overhead.py           # six buckets, validation invariant
│   │   ├── redundancy.py         # exact-hash grouping
│   │   ├── baseline.py           # generation (via injected executor) + comparison
│   │   ├── resilience.py         # per-fault + aggregate scoring
│   │   ├── verdict.py            # classes, score, confidence, recommendations
│   │   └── verdict_rules.toml    # thresholds & weights — versioned, printable
│   │
│   ├── explore/                  # delay-bounded exploration, reduction, dedup
│   ├── scenario/                 # YAML schema, validation, matrices, assertions
│   ├── otel/                     # OTel GenAI span export (§30)
│   └── api/
│       ├── app.py  routes/  ws.py  models.py     # FastAPI + Pydantic response models
│
├── frontend/
│   ├── src/
│   │   ├── panels/               # Waterfall, Graph, Findings, Scorecard, Chaos, Timeline
│   │   ├── components/           # primitives (Badge, Bar, SeverityDot, EvidenceLink)
│   │   ├── store/                # Zustand slices: run, selection, timeline, findings
│   │   ├── api/                   # generated client from the OpenAPI schema
│   │   ├── tokens.css            # design tokens (§29.1) as CSS custom properties
│   │   └── routes/
│   └── vite.config.ts
│
├── fixtures/
│   ├── code_pipeline/            # graph.py, checks.py, cache/, golden_findings.json
│   ├── support_triage/
│   ├── research_fanout/
│   ├── tasks/                    # task definitions referenced by scenarios
│   └── perturbations/            # curated confident-wrong responses (§11.8)
│
├── scenarios/                    # shipped scenarios incl. the CI set
├── tests/
│   ├── unit/  integration/  determinism/  analysis/  api/  frontend/
│   ├── golden/                   # golden event logs and findings files
│   └── benchmarks/               # §34 suite
├── docs/                         # architecture, event schema, determinism guarantees, limits
├── bench/                        # benchmark harness + published results
└── .github/workflows/            # ci.yml, determinism.yml, bench.yml, release.yml
```

| Directory | Responsibility |
|---|---|
| `events/` | The single most important directory. Owns the data contract; changes here are breaking changes |
| `runtime/` | Everything that executes. The only place non-determinism can enter, and therefore the only place it must be trapped |
| `sdk/` | The user-facing capture surface. Must survive LangGraph version drift and fail loudly, not silently |
| `store/` | Persistence, migrations, bundles. No analysis logic |
| `analysis/` | Pure functions over the log. Deterministic, testable without a runtime, import-restricted |
| `explore/` | Schedule generation and reduction; the only module that runs many runs |
| `scenario/` | The declarative surface and its validation — the safety gate for chaos |
| `api/` | Thin transport over `store` + `analysis`. No business logic |
| `frontend/` | Control Tower. Panels are dumb; the store owns selection and derived state |
| `fixtures/` | Demo, regression suite and false-positive control |
| `tests/` | Mirrors the source layout; `determinism/` is its own top-level suite because it gates everything |
| `bench/` | Self-measured numbers for §34 — the only statistics the project publishes |
| `docs/` | Includes the published limitations: determinism boundaries, bounded-search caveat, comparability rules |

---

# 26. API Specification

**Base URL** `http://127.0.0.1:8420/api` · **Content type** `application/json` · **Schema** OpenAPI 3.1 served at `/openapi.json`, from which the frontend client is generated.

**Authentication:** none. The server binds to `127.0.0.1` only. `[DETAIL]` This is a deliberate local-first decision (§4.4); binding to `0.0.0.0` requires the explicit `--host` flag, which prints a warning that there is no authentication. Adding auth is future scope, not MVP.

**Common error envelope:**

```jsonc
{"error": {"code": "E-RUN-404", "message": "Run r_f2a91 not found",
           "detail": {...}, "docs": "https://…/errors#E-RUN-404"}}
```

## 26.1 Endpoint reference

Source endpoints `[SOURCE]` are preserved; details are added.

### `POST /api/runs`

Start a run. `[SOURCE]` `{ scenario_id, mode: baseline|chaos|replay, seed } → run_id`

| Aspect | Specification |
|---|---|
| Request | `{"scenario_id": "code_pipeline_default", "mode": "chaos", "seed": 42, "explore": {"enabled": false}}` |
| Validation | Scenario exists and validates; mode legal; seed is int32; concurrent-run limit (default 4) not exceeded |
| Response `202` | `{"run_id": "r_f2a91", "status": "running", "ws": "/ws/runs/r_f2a91"}` |
| Errors | `400 E-SCEN-*` validation · `409 E-RUN-409` too many concurrent runs · `429` rate limit |
| Performance | Returns in <200 ms; execution proceeds in a subprocess |

### `GET /api/runs`

`[DETAIL]` List runs. Query: `?status=&fixture=&limit=50&cursor=`. Response: `{"runs": [RunSummary], "next_cursor": "…"}`. Cursor-based (by `created_at desc, run_id`) so pagination is stable while runs are being created.

### `GET /api/runs/{id}`

Run metadata + verdict. `[SOURCE]`

```jsonc
{"run_id":"r_f2a91","status":"complete","mode":"chaos","seed":42,
 "scenario":{"id":"reviewer_crash_midflight","hash":"blake2b:…"},
 "graph":{"hash":"blake2b:…","agents":["planner","coder","reviewer","tester"]},
 "timing":{"virtual_makespan_ms":6012,"wall_makespan_ms":1904,"critical_path_ms":6001},
 "counts":{"events":4183,"spans":212,"messages":54,"llm_calls":38,"tokens":42100},
 "determinism":{"canonical_log_hash":"blake2b:…","cache_hit_rate":1.0,
                "unwrapped_tools":0,"nondeterminism_warnings":0},
 "verdict":{"class":"STATE_CONFLICT_RISK","coordination_score":61,"confidence":"high",
            "headline":"…","recommendations":[…]},
 "baseline_run_id":"r_7c02b","comparability":"B"}
```
Errors: `404 E-RUN-404`. Performance: <50 ms (all precomputed).

### `GET /api/runs/{id}/events`

Paginated event log. `[SOURCE]` `?from_seq=0&limit=1000&types=state_write,state_read&agent=coder`

- `limit` max 5 000; default 1 000.
- Response `{"events":[…],"next_from_seq":1000,"total":4183}`.
- Cursor is `seq` — stable, gapless, and monotonic, so pagination is correct even during a live run.
- Performance: <100 ms for 1 000 events (indexed on `(run_id, seq)`).

### `GET /api/runs/{id}/graph`

Nodes, edges, aggregate edge latency. `[SOURCE]`

```jsonc
{"nodes":[{"id":"coder","role":"worker","busy_ms":2100,"idle_ms":900,
           "cp_ms":2100,"tokens":18400,"status":"ok"}],
 "edges":[{"from":"coder","to":"reviewer","messages":6,"total_handoff_ms":3660,
           "cp_handoff_ms":3660,"cp_share":0.61,"on_critical_path":true}],
 "critical_path":["planner","coder","reviewer","tester"]}
```
Query `?at_virtual_ts=2418` returns the graph state as of that instant (for replay sync).

### `GET /api/runs/{id}/waterfall`

Spans with virtual start/end and critical-path flags. `[SOURCE]`

```jsonc
{"virtual_makespan_ms":6012,"baseline_makespan_ms":5000,
 "lanes":[{"agent":"coder","spans":[
    {"span_id":"a3f1…","kind":"llm_call","name":"coder.generate",
     "start_ms":1200,"end_ms":2012,"bucket":"productive_work",
     "on_critical_path":true,"status":"ok","fault_id":null,"seq_start":880,"seq_end":905}]}]}
```
Supports `?from_ms=&to_ms=` windowing for virtualised rendering beyond 5 000 spans.

### `GET /api/runs/{id}/findings`

Conflicts, redundancy, bottlenecks, severity-ranked. `[SOURCE]` `?severity=high&type=state_conflict&include_suppressed=false`

Each finding carries `id`, `type`, `subtype`, `severity`, `title`, `description`, `evidence{event_seqs, span_ids, computation}`, `recommendation`, `repro_scenario` and, when applicable, `suppressed_by`.

### `GET /api/runs/{id}/scorecard`

Speedup decomposition + resilience. `[SOURCE]` Returns the §17.4 structure as data: `speedup{}`, `buckets[]` with `evidence`, `tokens{}`, `comparability{}`, `resilience{score, per_fault[]}` — with `per_fault` **required** whenever `score` is present (§19.7).

### `POST /api/runs/{id}/faults`

Inject into a live run. `[SOURCE]`

| Aspect | Specification |
|---|---|
| Request | `{"type":"agent_crash","target":"reviewer","params":{"recoverable":false},"trigger":{"immediate":true}}` |
| Validation | Run is `running`; target inside the run's blast radius; **`chaos_opt_in` was set for a user graph** — an interactive fault cannot bypass §13.3 |
| Response `202` | `{"fault_id":"f_02","armed_at_virtual_ts":3120}` |
| Errors | `409` run not running · `403 E-CHAOS-001` outside blast radius · `400` unknown fault type |

**Reproducibility note** `[DETAIL]`: an interactively injected fault is recorded into the run's *effective* scenario, and `agentdx export` emits that scenario, so an ad-hoc chaos session remains reproducible. Without this, the UI's most engaging feature would produce unreproducible runs — a direct contradiction of the thesis.

### `POST /api/runs/compare`

`{run_a, run_b} → diff`. `[SOURCE]` Returns metric deltas, findings added/removed/unchanged, and verdict change. Refuses (`400 E-CMP-001`) when scenario hashes differ unless `force: true` — comparing incomparable runs is worse than not comparing.

### `GET /api/scenarios` · `GET /api/scenarios/{id}` · `POST /api/scenarios/validate`

`[DETAIL]` List, fetch and dry-run-validate scenarios. `validate` returns the resolved scenario with defaults applied plus any warnings, and is what the UI's chaos panel calls before arming.

### `GET /api/runs/{id}/exploration`

`[DETAIL]` Exploration results. Response includes `k`, `schedules_executed`, `unique_schedules`, `reduced_away`, `new_findings[]`, `capped`, and the **required** `coverage_statement` field (§15.6).

### `GET /api/runs/{id}/state?at_virtual_ts=`

`[DETAIL]` Reconstructed state (§20.4): `{"at_virtual_ts":2418,"at_seq":1043,"keys":[{"key":"draft.module_a","value_hash":"…","value":null,"writer":"coder","written_at_seq":1043,"size_bytes":4096}]}`. Target <100 ms.

### `GET /api/runs/{id}/export` · `POST /api/import`

`[DETAIL]` Bundle download (`application/zip`) and upload (multipart). Import validates the manifest, migrates the schema if needed, and returns the new local `run_id`.

### `GET /api/health` · `GET /api/version` · `GET /api/metrics`

`[DETAIL]` Liveness; version + schema version; self-observability counters (§35), Prometheus text format.

## 26.2 WebSocket protocol

`WS /ws/runs/{id}` — live event stream; **replays the backlog on connect** `[SOURCE]`, so a client that attaches late sees the whole run.

**Client → server**

```jsonc
{"type":"subscribe","from_seq":0,"filters":{"types":["state_write","fault_injected"]}}
{"type":"ack","through_seq":2400}          // flow control
{"type":"unsubscribe"}
{"type":"ping"}
```

**Server → client**

```jsonc
{"type":"hello","run_id":"r_f2a91","status":"running","current_seq":1200,"schema_version":1}
{"type":"events","events":[…],"from_seq":0,"to_seq":999}     // backlog, batched
{"type":"event","event":{…}}                                  // live, one at a time
{"type":"finding","finding":{…}}                              // emitted as analysis progresses
{"type":"verdict","verdict":{…}}                              // once, at completion
{"type":"status","status":"analysing|complete|failed|aborted_guard"}
{"type":"pong"}
{"type":"error","code":"E-WS-002","message":"…"}
```

| Aspect | Specification |
|---|---|
| Backlog | Sent in batches of 1 000 before any live event; ordering is `seq`-monotonic across the boundary |
| Flow control | Server pauses after 5 000 unacked events; client `ack` resumes |
| Heartbeat | `ping`/`pong` every 15 s; server closes after 45 s of silence |
| Reconnect | Client reconnects with `from_seq = last_seen + 1`; no event is ever delivered twice |
| Backpressure | If the client cannot keep up, the server switches to **sampled** mode (every Nth event) and sends `{"type":"status","sampling":N}` — the UI shows a badge; the full log remains available over REST |
| Close codes | 1000 normal · 4004 run not found · 4013 too many connections (limit 8 per run) |

## 26.3 Performance expectations

| Endpoint | Target p95 | Load assumption |
|---|---|---|
| `GET /runs/{id}` | <50 ms | Precomputed |
| `GET /runs/{id}/events` (1 000) | <100 ms | Indexed scan |
| `GET /runs/{id}/waterfall` | <150 ms | 5 000 spans |
| `GET /runs/{id}/findings` | <50 ms | Precomputed |
| `GET /runs/{id}/state` | <100 ms | Snapshot + ≤500 events |
| `POST /runs` | <200 ms | Returns before execution |
| WS backlog (5 000 events) | <1 s | Batched |

---

# 27. Data Storage Architecture

## 27.1 Why SQLite and DuckDB rather than a server database `[SOURCE]`

| Reason | Detail |
|---|---|
| **Local-first is a product decision** | The tool runs a user's proprietary graph and prompts. Requiring a database server means data leaves the process, ops burden appears, and the "clone and run" property is lost |
| **Zero-ops demo** | `docker compose up` in under 3 minutes `[SOURCE]` is impossible with a database that needs provisioning, migration and a health-check wait |
| **The workload fits exactly** | One writer (the runner), many readers (API, analysis, CLI) — precisely what SQLite WAL is designed for |
| **Bundles are files** | A run bundle is a portable artifact. File-based storage makes export a copy, not a dump |
| **DuckDB covers the analytical gap** | Event-log aggregations (group-by-agent, window functions over spans) are columnar workloads; DuckDB executes them in-process, over Parquet, with no server |
| **$0 stack** `[SOURCE]` | Both are embedded and free |

**Division of responsibility:**

| Concern | SQLite | DuckDB |
|---|---|---|
| Event ingestion (write path) | ✅ append-only, WAL, transactional | ❌ |
| Run/scenario/finding metadata | ✅ | ❌ |
| LLM cache | ✅ (separate DB file) | ❌ |
| State snapshots | ✅ | ❌ |
| Point reads by `seq` | ✅ (indexed) | ❌ |
| Analytical aggregation over ≥50 000 events | ❌ (slow) | ✅ over Parquet |
| Cross-run comparison and benchmarks | ❌ | ✅ |

**Threshold rule** `[DETAIL]`: runs under 20 000 events are analysed directly from SQLite (simpler, no export step). At or above 20 000 events the runner exports `events.parquet` on seal and analysis switches to DuckDB. The threshold is configurable and the behaviour is identical either way — this is a performance decision, never a semantic one, and a test asserts both paths produce identical analysis output.

## 27.2 Schema (SQLite)

```sql
PRAGMA journal_mode=WAL;  PRAGMA synchronous=NORMAL;  PRAGMA foreign_keys=ON;

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, scenario_id TEXT, scenario_hash TEXT NOT NULL,
  graph_hash TEXT NOT NULL, mode TEXT NOT NULL, seed INTEGER NOT NULL,
  status TEXT NOT NULL,                       -- created|running|analysing|complete|failed|aborted_guard
  created_at TEXT NOT NULL, sealed_at TEXT,
  virtual_makespan_ms INTEGER, wall_makespan_ms INTEGER,
  canonical_log_hash TEXT, event_count INTEGER,
  baseline_of TEXT REFERENCES runs(run_id),
  replay_of  TEXT REFERENCES runs(run_id),
  explore_parent TEXT REFERENCES runs(run_id),
  agentdx_version TEXT NOT NULL, schema_version INTEGER NOT NULL,
  delay_schedule TEXT, calibration_id TEXT, determinism_quality TEXT
);

CREATE TABLE events (
  run_id TEXT NOT NULL, seq INTEGER NOT NULL,
  sched_step INTEGER NOT NULL,
  virtual_ts_ms INTEGER NOT NULL, wall_ts_ms INTEGER NOT NULL,
  agent_id TEXT, clock_slot TEXT, type TEXT NOT NULL, span_id TEXT,
  vclock TEXT NOT NULL,            -- canonical JSON
  causal_parents TEXT NOT NULL,    -- canonical JSON array
  fault_id TEXT, payload TEXT NOT NULL,
  prev_hash TEXT, this_hash TEXT,  -- append-only hash chain (§9.7)
  PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;

CREATE INDEX idx_events_type   ON events(run_id, type, seq);
CREATE INDEX idx_events_agent  ON events(run_id, agent_id, seq);
CREATE INDEX idx_events_vts    ON events(run_id, virtual_ts_ms);
CREATE INDEX idx_events_span   ON events(run_id, span_id);
CREATE INDEX idx_events_fault  ON events(run_id, fault_id) WHERE fault_id IS NOT NULL;

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE TABLE findings (
  finding_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
  type TEXT NOT NULL, subtype TEXT, severity TEXT NOT NULL,
  title TEXT NOT NULL, description TEXT NOT NULL,
  evidence TEXT NOT NULL,           -- JSON: event_seqs, span_ids, computation
  recommendation TEXT, suppressed_by TEXT, repro_scenario_path TEXT,
  analysis_version TEXT NOT NULL
);

CREATE TABLE scorecards (
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
  payload TEXT NOT NULL, analysis_version TEXT NOT NULL, computed_at TEXT NOT NULL
);

CREATE TABLE state_snapshots (
  run_id TEXT NOT NULL, seq INTEGER NOT NULL, state TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE scenarios (
  scenario_id TEXT PRIMARY KEY, path TEXT, content TEXT NOT NULL,
  content_hash TEXT NOT NULL, version INTEGER NOT NULL
);

CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- db_version, created_at
```

`llm_cache` lives in a **separate database file** (`cache.db`, §11.6) so it can be shared, sanitised, pruned or excluded independently of run data — and so a cache shared for reproduction does not carry someone's run history with it.

## 27.3 Write path and WAL

- Events are appended in batches (64 events or 50 ms) inside one transaction.
- WAL permits concurrent readers (API, `agentdx analyze`) during a run with no writer blocking.
- `synchronous=NORMAL` is chosen deliberately: a crash may lose the last batch, which is acceptable because an interrupted run is not analysable anyway. Bundle export uses `synchronous=FULL`.
- WAL checkpoint at run seal; the DB is then read-only for that run.

## 27.4 DuckDB usage

```sql
-- attach the exported Parquet for a large run
CREATE VIEW ev AS SELECT * FROM read_parquet('runs/r_f2a91/events.parquet');

-- span materialisation (the derivation rule of §6.3, in SQL)
CREATE VIEW spans AS
SELECT s.span_id, s.agent_id,
       s.virtual_ts_ms                        AS start_ms,
       e.virtual_ts_ms                        AS end_ms,
       json_extract_string(s.payload,'$.kind') AS kind,
       json_extract_string(e.payload,'$.status') AS status
FROM ev s JOIN ev e
  ON s.span_id = e.span_id AND s.type='span_start' AND e.type='span_end';
```

DuckDB is read-only over Parquet; it never owns authoritative data. If DuckDB is unavailable, analysis falls back to the SQLite path with a warning — the product must not hard-fail on an optional accelerator.

## 27.5 Retention, export/import, migration

| Concern | Policy |
|---|---|
| **Retention** | No automatic deletion. `agentdx prune --older-than 30d --keep-tagged` is explicit. Cache is **never** auto-evicted (§11.7) — silent eviction would break reproducibility of old bundles |
| **Export/import** | `.agentdx` bundles (§20.7); import is idempotent by `run_id` + `canonical_log_hash` |
| **Migration** | Sequential numbered migrations in `store/migrations/`; `db_version` in `schema_meta`; every migration has an up-test on a fixture DB. Migration runs automatically on open for minor versions and requires `agentdx migrate` for major ones |
| **Backup** | The data dir is a directory of files; copying it is a backup. This is stated in the docs as a feature of the storage choice |
| **Size guidance** | ~700 bytes/event uncompressed; a 5 000-event run ≈ 3.5 MB; Parquet export ≈ 0.6 MB; a fixture cache ≈ 2–8 MB |

---

# 28. Frontend / Control Tower

The source design philosophy is preserved in full `[SOURCE]`: three panels, cross-linked, with the ghost-baseline waterfall as the signature element and the rest of the interface quiet and dense.

## 28.1 Application architecture

```
main.tsx
└── App (router)
    ├── /                       RunListRoute      — recent runs, empty state → CLI hint
    ├── /runs/:id               RunRoute          — the Control Tower
    │   ├── TopBar              run id · mode · seed · verdict pill · replay toggle · Scorecard
    │   ├── <Split vertical>
    │   │   ├── <Split horizontal>
    │   │   │   ├── GraphPanel          (React Flow)
    │   │   │   └── <Stack>
    │   │   │       ├── ChaosPanel      (armed → fired, blast radius shown first)
    │   │   │       └── FindingsPanel   (severity-ranked, suppressed drawer)
    │   │   └── WaterfallPanel          (visx; ghost baseline; virtualised)
    │   └── TimelineBar          (scrubber, keyboard-driven)
    ├── /runs/:id/scorecard     ScorecardRoute    — full §17.4 with evidence links
    └── /compare/:a/:b          CompareRoute      — P1
```

Rendering strategy: panels are **presentational**; all derived data comes from selectors over the store. No panel fetches on its own except its initial resource, so cross-panel state can never diverge.

## 28.2 State management (Zustand slices)

| Slice | Holds | Notes |
|---|---|---|
| `runSlice` | Run metadata, verdict, scorecard, load status | Fetched once per run |
| `eventsSlice` | Event pages, `maxSeqLoaded`, WS connection state | Append-only in the client too; events are never mutated |
| `timelineSlice` | `virtualTs`, `playing`, `speed`, `mode: live\|replay` | The single source of "when" |
| `selectionSlice` | `{kind: span\|event\|finding\|agent\|edge, id}` | **The single source of "what"** — every highlight in every panel derives from it |
| `findingsSlice` | Findings, filters, `includeSuppressed` | |
| `chaosSlice` | Armed fault, resolved blast radius, fire state | Two-step arm/fire `[SOURCE]` |
| `prefsSlice` | Reduced motion, density, panel sizes | Persisted to `localStorage` |

**Cross-panel linking** `[SOURCE]` is a direct consequence of `selectionSlice`: clicking a finding sets the selection to that finding; the waterfall highlights `finding.evidence.span_ids`, the graph highlights the involved agents and edge, and the timeline seeks to the earliest evidence event. One write, three reactions — no imperative coordination.

## 28.3 Panels

### Waterfall (build first — it carries the demo alone `[SOURCE]`)

- One lane per agent; bars segmented by overhead bucket with distinct fills: `█ work · ░ blocking wait · ▓ handoff · ▒ retry` `[SOURCE]`.
- **Ghost baseline**: a dashed vertical line where the single-agent baseline finished, labelled with the speedup `[SOURCE]`.
- Critical-path spans outlined; everything else at reduced opacity — the path should be readable at a glance.
- Virtualised beyond 5 000 spans (NFR-3 `[SOURCE]`); windowed by virtual time.
- Hover → span detail; click → select; the scrubber position is a vertical rule across all lanes.

### Graph (React Flow)

Nodes = agents; edge width = message volume; edge colour = latency (the heat ramp); pulse = live message; ring = fault active. `[SOURCE]` Layout is deterministic (dagre with a fixed seed) so the graph does not reshuffle between runs — a reshuffling graph destroys the ability to compare two runs visually.

### Findings

Severity-ranked list; each row shows the type, the key/edge, the two agents and timestamps `[SOURCE]`. Expanding shows evidence sequence numbers (clickable, seeking the timeline), the recommendation, and the reproduction command. A collapsed "suppressed (n)" drawer holds guard-suppressed conflicts (§14.7).

### Scorecard

The §17.4 block, rendered with every number linked to its evidence. Comparability grade and cache reuse are adjacent to the speedup, never in a footnote.

### Chaos control

Buttons are **armed then fired (two-step), with the blast radius shown before firing** `[SOURCE]`. The armed state displays exactly what will be affected. Firing calls `POST /runs/{id}/faults` and records the fault into the effective scenario (§26.1) so the session stays reproducible.

### Timeline

Scrubber over virtual time with fault markers, finding markers and the ghost-baseline marker. Keyboard: `←/→` step, `shift` ×10, `j/k` between findings `[SOURCE]`, `space` play/pause.

## 28.4 Data loading and live updates

1. `GET /runs/{id}` → render the verdict pill and top bar immediately.
2. In parallel: `/waterfall`, `/findings`, `/graph` → the three panels.
3. Open the WebSocket if the run is `running`; otherwise skip it entirely (a completed run needs no socket).
4. Events stream in the background for the state table and the event inspector; the UI is fully usable before they finish.

Loading order is deliberate: the verdict is the product `[SOURCE]`, so it must not wait behind 4 000 events.

## 28.5 Performance strategy

| Technique | Applies to |
|---|---|
| Virtualised rendering (windowed by virtual time) | Waterfall beyond 5 000 spans `[SOURCE]` |
| Canvas fallback | Waterfall beyond 20 000 spans (SVG node count becomes the bottleneck) |
| Memoised selectors | All derived data |
| `requestAnimationFrame` batching | Scrub updates — one render per frame regardless of event rate |
| Web Worker | State reconstruction folds for large runs |
| Deterministic layout caching | Graph positions cached per `graph_hash` |

---

# 29. UI / UX

## 29.1 Design tokens `[SOURCE]`

Base palette (as supplied):

| Token | Hex | Role |
|---|---|---|
| `--navy-900` | `#0A2947` | Primary surface / canvas |
| `--cream` | `#F3E4C9` | Primary text, active nodes |
| `--sage` | `#D3D4C0` | Secondary text, healthy state |
| `--clay` | `#8B5E3C` | Accent, heat, warning |

The palette contains no status colours, so they are derived inside the same family rather than bolted on as generic red/amber/green `[SOURCE]`:

| Derived token | Hex | Role |
|---|---|---|
| `--navy-950` | `#061B30` | Recessed panels, wells |
| `--navy-800` | `#123659` | Raised cards |
| `--navy-700` | `#1D4A73` | Borders, grid lines |
| `--sage-dim` | `#8A9384` | Muted / inactive |
| `--ok` | `#9DB88A` | Healthy — sage pushed green |
| `--warn` | `#C98F4B` | Degraded — clay pushed amber |
| `--crit` | `#A8443A` | Failed — clay pushed red, still earthy |
| `--fault` | `#E0A458` | Injected-fault marker (the one high-energy colour) |

**Latency heat ramp** (graph edges and waterfall bars) `[SOURCE]`:

```
fast ──────────────────────────────────────────▶ slow
#9DB88A → #D3D4C0 → #F3E4C9 → #C98F4B → #8B5E3C → #A8443A
 sage-green   sage     cream     amber     clay    clay-red
```

**Contrast requirement** `[SOURCE]`: cream on navy-900 ≈ 12:1. Do **not** use `--sage-dim` for body text on navy — it lands near 4:1. Restrict it to non-essential labels, and run every text/background pair through a contrast check before shipping (automated in CI, §33.13).

Additional tokens `[DETAIL]`: spacing scale `4/8/12/16/24/32/48`; radii `2/4/8`; `--focus-ring: 2px solid var(--cream)` with a 2px offset; elevation by `--navy-800/900/950` layering rather than shadows (shadows read poorly on a dark earthy palette).

## 29.2 Typography `[SOURCE]`

| Role | Face | Notes |
|---|---|---|
| Display / headings | **Geist** or **Inter Tight**, tight tracking, weight 600 | Engineering-plain, not decorative |
| Body / UI | **Inter**, 14px base | — |
| Data / IDs / logs / timings | **IBM Plex Mono** or **Geist Mono**, tabular numerals | Non-negotiable for scannable timing columns |

Deliberately avoid a serif display face on a cream background — that combination is the current AI-generated-design house style and will read as templated on a systems tool. `[SOURCE]`

## 29.3 Layout `[SOURCE]`

```
┌──────────────────────────────────────────────────────────────────────────┐
│  AgentDX   run #f2a91 · chaos · seed 42        ⏵ replay   [ Scorecard ]   │
├──────────────────────────────┬───────────────────────────────────────────┤
│                              │  CHAOS CONTROL                            │
│    AGENT DEPENDENCY GRAPH    │  ┌─────────────────────────────────────┐  │
│    (React Flow)              │  │ ⏱ Inject 500ms latency  planner→coder│ │
│                              │  │ ☠ Kill agent            reviewer     │ │
│    nodes = agents            │  │ ✂ Drop messages 20%     coder→tester │ │
│    edge width = volume       │  │ 🎭 Byzantine output     reviewer     │ │
│    edge colour = latency     │  └─────────────────────────────────────┘  │
│    pulse = live message      ├───────────────────────────────────────────┤
│    ring = fault active       │  FINDINGS                                 │
│                              │  ● write-write race  draft.module_a       │
│                              │    coder@t2418 ∥ reviewer@t2431           │
│                              │  ▲ redundant retrieval ×2                 │
│                              │  ▲ handoff = 61% of critical path         │
├──────────────────────────────┴───────────────────────────────────────────┤
│  COORDINATION WATERFALL          ◀━━━━━━━━━━●━━━━━━━━━▶  t = 2.41s        │
│  planner  ████▓▓                                                          │
│  coder        ░░░░████████▓▓▓                                             │
│  reviewer             ░░░░░░░░░████                                       │
│  tester                            ░░░░░░░████                            │
│  ╌╌╌ single-agent baseline ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ 0.83× ╌╌╌╌╌╌╌╌╌╌╌       │
│  █ work   ░ blocking wait   ▓ handoff   ▒ retry                           │
└──────────────────────────────────────────────────────────────────────────┘
```

Responsive rule `[DETAIL]`: below 1280px the graph collapses to a tab beside findings; below 900px the layout becomes a single column ordered **verdict → findings → waterfall → graph → chaos**, matching the information priority below.

## 29.4 Signature element `[SOURCE]`

> **The ghost baseline in the waterfall.** A dashed line marks where the single-agent baseline finished. Every bar extending past it is overhead made visible — you don't read the speedup number, you *see* it. One image, whole thesis. This is the frame that goes in the README GIF. Spend the design boldness here. Keep the rest of the interface quiet and dense.

Implementation requirements: the line renders even before the baseline finishes (as a pending ghost with a spinner), is labelled with the speedup and the comparability grade, and is the element the README GIF is framed on.

## 29.5 Information priority

The UI prioritises, in order: **1. Verdict · 2. Findings · 3. Critical path · 4. Waterfall · 5. Graph · 6. Chaos controls.**

This ordering governs load order (§28.4), single-column stacking, focus order and the export report's layout. **Do not turn AgentDX into a generic observability dashboard**: there is no "all spans" table on the main view, no log search bar, no metric-explorer panel. Anything that would make the interface look like an APM product is a scope violation (§4.4).

## 29.6 Status and severity states

| State | Colour | Shape/motion |
|---|---|---|
| Agent idle | `--sage-dim` | flat |
| Agent running | `--cream` | subtle pulse (motion-gated) |
| Agent blocked | `--warn` | dashed border |
| Agent crashed | `--crit` | solid ring + strike |
| Fault active | `--fault` | ring, always visible even under reduced motion |
| Finding critical / high / medium / low / info | `--crit` / `--crit` at 70% / `--warn` / `--sage` / `--sage-dim` | filled dot / filled dot / triangle / triangle / dot |

Severity is never conveyed by colour alone (§29.9) — shape and text label always accompany it.

## 29.7 Interactions and motion `[SOURCE]`

- Message pulses travel along edges during live runs; **disabled entirely under `prefers-reduced-motion`** and in replay-scrub mode (they would fight the scrubber).
- Clicking a finding highlights the two conflicting spans in the waterfall and the two nodes in the graph simultaneously. Cross-panel linking is the whole reason for three panels.
- Chaos buttons are *armed then fired* (two-step), with the blast radius shown before firing.
- Scrubbing is keyboard-driven too: `←/→` step event, `shift` for 10, `j/k` between findings.

Motion budget `[DETAIL]`: no transition longer than 200 ms; no looping animation except the live-message pulse; every animation gated behind `prefers-reduced-motion: no-preference`.

## 29.8 Copy rules `[SOURCE]`

Findings state what happened and what to do, in the interface's voice, never apologetically:

- ✅ "Lost update on `draft.module_a`. `coder` and `reviewer` wrote concurrently; `reviewer`'s value won. Add a lock or route both through the planner."
- ❌ "Uh oh! It looks like there might have been a potential state issue 😬"

Empty states point at the next action: "No runs yet. `agentdx run fixtures/code_pipeline` to record your first."

## 29.9 Accessibility

| Requirement | Specification |
|---|---|
| Contrast | WCAG AA on all text `[SOURCE]`; automated contrast test over the token matrix in CI |
| Keyboard | Every action reachable; visible focus rings `[SOURCE]`; documented shortcut list at `?` |
| Colour independence | Severity and status always carry a shape and a text label as well as a colour |
| Screen readers | Panels are landmarks; the waterfall exposes a data table alternative; findings are a list with `aria-describedby` pointing at the evidence |
| Motion | Full `prefers-reduced-motion` support, including disabling the pulse and the scrub animation |
| Zoom | Usable at 200% browser zoom without horizontal scrolling in single-column mode |

---

# 30. OpenTelemetry Interoperability  `P1`

> Emit OpenTelemetry GenAI-convention spans alongside the native event log. Result: AgentDX runs show up in Langfuse/Phoenix, and the project reads as *complementary to* the observability ecosystem rather than a naive competitor. Cheap to build, disproportionately strong in review. `[SOURCE]`

## 30.1 What is emitted

| AgentDX concept | OTel span | Notes |
|---|---|---|
| Run | Root span `agentdx.run` | Attributes: run id, seed, mode, scenario, verdict class |
| Agent step | `agentdx.agent` (kind INTERNAL) | Child of the run span |
| LLM call | `gen_ai.<operation>` per GenAI semantic conventions | `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| Tool call | `gen_ai.execute_tool` | `gen_ai.tool.name` |
| Message send/recv | Span links between producer and consumer spans | Links, not parent/child, because a fan-in has several producers |
| Fault injected | Span event `agentdx.fault` on the affected span | Type, target, params |
| Finding | Span event `agentdx.finding` on the run span | Type, severity, evidence seqs |

## 30.2 Attribute mapping

| Attribute | Value |
|---|---|
| `agentdx.run_id`, `agentdx.seq`, `agentdx.span_id` | Correlation back to the event log |
| `agentdx.virtual_ts_ms`, `agentdx.wall_ts_ms` | Both, always — virtual time is not a valid OTel timestamp |
| `agentdx.agent_id`, `agentdx.agent_role` | |
| `agentdx.bucket` | Overhead bucket, so a Langfuse user can group by it |
| `agentdx.on_critical_path` | boolean |
| `agentdx.fault_id` | when tainted |

**Timestamp rule** `[DETAIL]`: OTel spans carry **wall** timestamps (OTel is a wall-clock system) with virtual time as an attribute. Emitting virtual time as the OTel timestamp would make traces incoherent in any consumer. This is stated in the docs because it is the first question an observability engineer will ask.

## 30.3 Correlation

`trace_id` = deterministic derivation from `run_id`; `span_id` = the AgentDX span id padded to 16 hex. A user can therefore paste a `run_id` into Langfuse and find the trace, and paste a trace id into AgentDX and find the run.

## 30.4 Compatibility

| Platform | Status |
|---|---|
| Langfuse | OTLP/HTTP endpoint; GenAI conventions consumed natively |
| Arize Phoenix | OTLP; GenAI conventions |
| Any OTLP collector | Standard exporter; endpoint configured by `OTEL_EXPORTER_OTLP_ENDPOINT` |

Off by default (local-first, no egress); enabled with `--otel` or `[otel] enabled = true`.

## 30.5 Why this makes AgentDX complementary, not competitive

An observability platform answers *what happened* over a long window, sampled, in production. AgentDX answers *whether the topology is worth it* over one bounded, deterministic, reproducible experiment. By emitting OTel spans, an AgentDX run becomes another trace in the platform the team already runs — and the AgentDX-specific attributes (`bucket`, `on_critical_path`, `fault_id`) enrich that trace with information the platform cannot compute for itself. The correct claim is: **AgentDX produces data for your observability stack; it does not ask you to replace it.**

---

# 31. Security & Privacy

## 31.1 Threat model

Local-first, single-user, no authentication (§26). The assets to protect are: the user's prompts and responses, their API keys, their filesystem, and their production systems. The adversaries are: accidental exposure through sharing, a malicious or buggy agent in the graph under test, and a malicious `.agentdx` bundle received from someone else.

## 31.2 Prompt and response privacy

| Rule | Implementation |
|---|---|
| **Never write user prompt/response bodies to the event log by default — store hashes; body capture is opt-in** `[SOURCE]` | `capture_bodies=False` default; hashes in event payloads (§8.11) |
| The cache must store bodies (replay requires it) | Separate `cache.db`, `0600` permissions, excluded from bundles by default |
| Redaction | `redact_patterns` applied before hashing and storage; defaults cover common key formats |
| UI | With bodies off, the state table and span inspector show hashes, sizes and writers — sufficient for coordination analysis |

## 31.3 Sharing and bundles

`agentdx export` excludes cache bodies by default; `--include-cache-bodies` prints an explicit warning naming what will be included; `--sanitise` substitutes deterministic synthetic text of matching token length so timing and structure replay without content (§11.9).

## 31.4 Secrets and API keys

- Read from the environment only; never persisted, never logged, never included in bundles or event payloads.
- `run_start` records the provider **host**, not the key or the full URL with credentials.
- Error messages redact anything matching the key patterns before display.
- `agentdx doctor` warns if an API key appears in `agentdx.toml` or in a committed file.

## 31.5 Sandboxing

As specified in §13.2: registered tools only; filesystem confined to the run data dir plus a read-only fixture dir; network egress only to the configured provider base URL, and **none at all in replay mode**.

## 31.6 Destructive operations

Tools marked `destructive: true` are stubbed during fault runs (§13.8). Unregistered callables raise in `strict` mode. `state_corrupt` cannot target `protected` keys. Chaos against a user graph requires a committed `chaos_opt_in` plus a non-empty blast radius (§13.3).

## 31.7 Malicious agent behaviour

An agent under test is untrusted code the user chose to run; AgentDX does not sandbox Python itself (that would require a container or subprocess isolation, which is out of scope for v1 and is stated). What AgentDX does guarantee is that **an agent cannot escalate through AgentDX**: it cannot inject faults, cannot widen a blast radius, cannot write to the event log directly (the writer stamps and validates every event), and cannot disable guards. Running an untrusted graph should be done in a container, and the docs say so.

## 31.8 Fault injection authorisation

Summarised in §13.10. The key property: **opting a user graph into chaos requires editing a committed file**, so the decision is reviewable, not a flag someone types once in a terminal.

## 31.9 Bundle trust

An imported bundle is untrusted input. Import: validates the manifest schema; verifies the event hash chain (§9.7) and the canonical log hash; refuses path traversal in the archive; **never executes** anything from the bundle (a bundle contains data, not code — the graph is referenced by hash, not shipped). `--verify` re-executes only against a *local* graph whose hash matches.

## 31.10 Network posture

Bind to `127.0.0.1` by default; `--host` requires an explicit flag and prints an unauthenticated-server warning; no telemetry, no phone-home, no update check by default.

---

# 32. Non-Functional Requirements

Source NFRs `[SOURCE]` preserved verbatim as NFR-1…7 and extended with measurable thresholds and verification methods.

| # | Requirement | Threshold | Verified by |
|---|---|---|---|
| **NFR-1** | Instrumentation overhead in passthrough mode | **< 10%** wall clock; benchmark and publish it `[SOURCE]` | §34.1 benchmark in CI, regression-gated at 12% |
| **NFR-2** | Replay determinism | Replay of a recorded run must be **byte-identical on the canonical event log** (§10.7) — asserted in CI `[SOURCE]` | §33.3, 100 replays → 100 identical hashes |
| **NFR-3** | UI performance | **60 fps up to 50 agents / 5 000 spans**; virtualise the waterfall beyond that `[SOURCE]` | Playwright + trace timings, §33.12 |
| **NFR-4** | Offline operation | **Full demo runs offline from cache — zero API keys required** `[SOURCE]` | CI job with network disabled |
| **NFR-5** | Startup | **`docker compose up` → working demo in under 3 minutes on Apple silicon** `[SOURCE]` | Timed CI job on macOS runner + manual gate |
| **NFR-6** | Privacy | **Never write prompt/response bodies to the event log by default; opt-in only** `[SOURCE]` | Automated scan of the DB for plaintext after a default run |
| **NFR-7** | Accessibility | **Keyboard accessible, visible focus rings, WCAG AA on all text** `[SOURCE]` | axe-core in CI + token contrast matrix test |
| NFR-8 `[DETAIL]` | Analysis latency | Full analysis of a 5 000-event run < 2 s; 50 000-event run < 15 s | Benchmark |
| NFR-9 `[DETAIL]` | Runtime memory | < 500 MB RSS for a 50 000-event run | Benchmark with `tracemalloc` + RSS sampling |
| NFR-10 `[DETAIL]` | Event ingestion | ≥ 20 000 events/s sustained write throughput | Benchmark |
| NFR-11 `[DETAIL]` | Scalability | Correct (not necessarily fast) up to 50 agents, 200 000 events, 500 state keys | Synthetic-log tests |
| NFR-12 `[DETAIL]` | Portability | macOS (Apple silicon + Intel) and Linux x86_64/arm64; Python 3.12+ | CI matrix |
| NFR-13 `[DETAIL]` | Reliability | A crashed or aborted run leaves a readable, analysable partial log 100% of the time | Kill-test suite |
| NFR-14 `[DETAIL]` | Determinism of analysis | The same log analysed 100× yields byte-identical findings and verdict | §33.10 |
| NFR-15 `[DETAIL]` | Cold start | `agentdx run fixtures/code_pipeline` completes in < 20 s wall on a warm cache | Benchmark |
| NFR-16 `[DETAIL]` | Install | `uv pip install agentdx` in < 60 s on a warm cache; no compiler required | CI |

**Windows is not supported in v1** `[DETAIL]` and this is stated in the README rather than discovered. The runtime's signal handling, path assumptions and the Docker demo target Unix; adding Windows is a P2 item with its own test matrix.

---

# 33. Testing Strategy

Testing philosophy: **the analysis layer is pure, so most of the product is testable without running an agent at all.** The majority of tests feed hand-authored or generated event logs to analysers and assert exact outputs. This is what makes a 10-week build testable to a high standard.

| Layer | Approach | Count target |
|---|---|---|
| Unit | Pure functions, hand-computed expectations | ~250 |
| Golden | Synthetic event logs → exact findings/verdict JSON | ~40 |
| Integration | Real fixture runs end to end | ~15 |
| Determinism | Repeated replay equality | 3 suites |
| Property | Hypothesis-generated logs and schedules | ~15 |
| API | Contract tests against OpenAPI | ~30 |
| Frontend | Component + Playwright E2E | ~40 |
| Performance | Benchmarks with regression gates | ~10 |
| Security | Privacy and sandbox assertions | ~10 |

## 33.1 Unit tests

Per module: vector-clock operations; happens-before comparison; canonical serialisation; cache key construction and stability; virtual clock arithmetic; scheduler choice determinism; fault trigger evaluation; bucket classification precedence; every verdict rule; every resilience formula; every scenario validation rule.

## 33.2 Integration tests

Each fixture, end to end, offline: run → analyse → assert golden findings → export bundle → import → verify. Plus: a user-graph smoke test with the generic decorator API; a LangGraph subgraph test; an aborted-guard test asserting the partial log is analysable.

## 33.3 Deterministic replay tests — **the CI test that proves the thesis**

```python
# tests/determinism/test_replay_equality.py
@pytest.mark.parametrize("fixture", ["code_pipeline", "support_triage", "research_fanout"])
def test_100_replays_produce_identical_logs(fixture):
    original = run_fixture(fixture, seed=42, mode="replay")
    reference = canonical_log_hash(original.events)
    hashes = set()
    for i in range(100):
        r = replay(original.run_id)                    # fresh process every 10th iteration
        hashes.add(canonical_log_hash(r.events))
        assert r.schedule == original.schedule, f"schedule diverged at iteration {i}"
    assert hashes == {reference}, f"{len(hashes)} distinct logs across 100 replays"
```

Requirements: runs in CI from **week 3** `[SOURCE]`; at least 10 of the 100 replays execute in a **fresh process** (to catch process-state leaks such as hash randomisation or module-level caches); on failure, the first divergent event is printed side by side and the bundle is uploaded as an artifact.

`[SOURCE]` This is the "100 replays → 100 identical event logs" gate, and it is the single most important test in the repository.

## 33.4 Scheduler tests

Same seed → same schedule (×100); different seeds → ≥2 distinct schedules on a concurrent fixture; deadlock detection fires on a deadlocking fixture; starvation is reported; step budget terminates a non-yielding task; delay schedules reproduce exact interleavings.

## 33.5 Virtual-clock tests

Monotonicity; advance only when nothing is runnable; a 30 s injected-latency scenario completes in <1 s wall `[SOURCE]`; calibration profile application; drift warning fires above 10%; integer-only arithmetic (a property test asserting no float ever enters the clock).

## 33.6 Fault injection tests

One test per fault type asserting: the expected `fault_injected` event, the expected effect, the taint propagation, determinism under the same seed, and blast-radius refusal. Plus a property test: *for all scenarios and seeds, no fault ever affects a target outside the blast radius.*

## 33.7 Vector-clock and state-reconstruction tests

Send/recv merge correctness on a 4-agent synthetic log with hand-computed clocks; transitivity; concurrency detection; sparse-map handling of dynamically created agents; snapshot-accelerated state reconstruction equals a from-scratch fold at 50 random timestamps.

## 33.8 Race detector tests (true positives)

Minimum 12 hand-authored logs: write-write concurrent divergent (must report); write-write ordered by message (must not); write-write concurrent identical values (must suppress as idempotent); read-write concurrent (report as stale read); write-read concurrent (report as dirty read); three-agent conflict; same-agent-two-subtasks conflict; conflict across a lock (must suppress); conflict on a reducer channel (must suppress); torn read across a transaction (report, elevated); conflict with unavailable value hashes (report, confidence low, severity capped); conflict after an agent crash.

## 33.9 False-positive tests — **mandatory**

> **A system that reports problems on every run is not trustworthy.**

| Test | Requirement |
|---|---|
| Research fan-out fixture | **Zero** findings above `info` `[SOURCE]` |
| Reducer channel with 4 concurrent writers | Zero conflicts (guard G3) |
| Lock-protected concurrent writes | Zero conflicts (guard G4) |
| Sequential pipeline, no concurrency | Zero conflicts |
| Identical concurrent writes | Zero conflicts (guard G2) |
| Retry of the same tool call | **Not** reported as redundancy |
| Genuinely parallel fan-out with ≥3 average parallelism | Not reported as fake fan-out |
| Single-agent run | No coordination findings; verdict `SINGLE_AGENT` |

Each has a golden empty-findings file; any new finding fails CI. This suite is the trust contract of the product and may not be skipped or marked xfail.

## 33.10 Analysis determinism tests

The same event log analysed 100 times yields byte-identical findings JSON, an identical critical path (including tie-breaks), and an identical verdict. Guards against `set` iteration, dict ordering and floating-point accumulation leaking into analysis output.

## 33.11 Critical path, baseline and verdict tests

Critical path on hand-constructed DAGs with analytically known answers (including fan-in, retries and ties); bucket summation invariant as a property test over generated logs; redundancy grouping; baseline comparability grading at reuse 0.9/0.6/0.3; speedup formula tests; attribution normalisation sums to the gap; **one firing test and one non-firing test per verdict recommendation rule** (§18.6).

## 33.12 API and frontend tests

API: contract tests generated from the OpenAPI schema; pagination stability during a live run; WebSocket backlog-then-live ordering with no duplicates across a forced reconnect; error envelope shape for every documented code.
Frontend: component tests for waterfall bucket rendering and the ghost baseline; store selector tests for cross-panel linking; Playwright E2E covering Journey A and Journey B end to end; a 60 fps assertion at 50 agents / 5 000 spans (NFR-3); axe-core accessibility scan; a `prefers-reduced-motion` test asserting no animation runs.

## 33.13 Performance, security and regression tests

Performance: instrumentation overhead (§34.1) with a 12% gate; analysis latency gates (NFR-8); memory ceiling (NFR-9); ingestion throughput (NFR-10).
Security: no plaintext prompt substring in the event DB after a default run; API keys never appear in any artifact; bundle import rejects path traversal and a tampered hash chain; the API refuses non-loopback binding without `--host`.
Fixture regression: golden findings per fixture, diffed in CI — the mechanism by which fixtures act as the regression suite (§23).

## 33.14 Test data strategy

- **Synthetic event logs** are built by `tests/factories.py` (a small DSL: `log().agent("a").writes("k", "v1").concurrent_with(...)`), which makes the 40 golden tests readable and maintainable.
- **Recorded fixture caches** are committed, so the entire suite runs offline.
- **No network access in the test suite at all** — enforced by a `pytest` fixture that patches sockets and fails any attempt.

---

# 34. Benchmarking & Evaluation

**Rule E1 applies** (§2.12): the project publishes only numbers it measures itself on the shipped fixtures, with the harness in `bench/` and results in `bench/results/`. No external statistics.

## 34.1 Instrumentation overhead

**Method.** For each fixture, run N=30 iterations in three configurations: (a) uninstrumented, on the real event loop, against a local mock provider with fixed latency; (b) instrumented in `passthrough` mode; (c) instrumented in `replay` mode. Report median and p90 of wall duration; overhead = `median(b) / median(a) − 1`. The mock provider removes provider variance, which would otherwise dominate.

**Gate.** <10% `[SOURCE]`; CI fails above 12%.
**Published as:** a table of per-fixture overhead with N, median, p90 and the machine spec.

## 34.2 Replay determinism

**Method.** 100 replays per fixture, ≥10 in fresh processes; count distinct canonical log hashes.
**Gate.** Exactly 1 distinct hash per fixture. **Published as:** "100/100 identical" per fixture, with the hash.

## 34.3 Race detection accuracy

**Method.** A labelled corpus of 40 synthetic event logs: 20 containing a genuine race (by construction), 20 free of races but containing near-miss patterns (reducer channels, locks, ordered writes, identical values, retries). Compute precision, recall and F1.
**Gate.** Recall = 1.0 on the seeded set; **precision = 1.0 on the negative set** — a single false positive fails the benchmark, because trust is binary here.
**Published as:** a confusion matrix with the corpus committed so the claim is auditable.

## 34.4 Exploration cost and schedule coverage

**Method.** For each fixture, run exploration at `k ∈ {0,1,2,3}` and record: schedules executed, schedules reduced away, wall time, and the smallest `k` at which each seeded defect is first found.
**Published as:** a table showing where each defect is found — this is what substantiates the `k=2` default with the project's own data rather than a borrowed claim.

## 34.5 Speedup accuracy

**Method.** Two validations, because there is no external ground truth:
1. **Analytic fixtures.** Synthetic graphs with known durations where the correct speedup is derivable by hand; assert the computed value matches to ±1%.
2. **Virtual-versus-wall calibration.** For a live-recorded run, compare virtual makespan to measured wall makespan; report the divergence distribution.
**Published as:** "computed speedup matches the analytic value within X% on N synthetic graphs; virtual/wall divergence median Y%."

## 34.6 Resilience scoring

**Method.** A fault matrix (4 fault types × 3 fixtures × 3 seeds) with expected qualitative outcomes documented per cell; assert the computed classification matches the documented expectation, and that scores are stable across seeds within ±3 points where the outcome class is identical.

## 34.7 UI performance

**Method.** Playwright with tracing; synthetic runs at 10/50 agents and 500/5 000/20 000 spans; measure frame times during a scripted scrub, initial render time, and time-to-verdict-visible.
**Gate.** 60 fps at 50 agents / 5 000 spans `[SOURCE]`; time-to-verdict-visible <1 s.

## 34.8 Benchmark methodology rules

1. Fixed machine spec recorded with every result (CPU, RAM, OS, Python version).
2. N ≥ 30 for timing measurements; report median and p90, never the mean alone.
3. Warm-up iterations discarded.
4. The harness is committed and runnable by anyone: `uv run python -m bench.all`.
5. Results are committed as JSON alongside the rendered table, so a reviewer can recompute.
6. **No claim appears in the README that the harness cannot reproduce.**

---

# 35. Observability of AgentDX Itself

AgentDX must be debuggable when it is the thing that is wrong. Metrics are exposed at `GET /api/metrics` (Prometheus text) and written to `~/.agentdx/metrics.jsonl` per run.

| Metric | Type | Purpose | Alert threshold (local warning) |
|---|---|---|---|
| `agentdx_scheduler_step_duration_us` | histogram | Scheduler overhead per decision | p99 > 500 µs |
| `agentdx_scheduler_steps_total` | counter | Schedule length | — |
| `agentdx_event_ingest_rate` | gauge | Events/s written | < 5 000/s |
| `agentdx_event_queue_depth` | gauge | Writer backpressure | > 8 000 (near the 10 000 bound) |
| `agentdx_event_store_bytes` | gauge | Store size per run and total | run > 200 MB |
| `agentdx_cache_hit_ratio` | gauge | Replay health | < 1.0 in replay mode is an error, not a warning |
| `agentdx_replay_duration_ms` | histogram | Replay cost | — |
| `agentdx_analysis_duration_ms{analyser}` | histogram | Per-analyser cost | total > 2 s at 5 000 events |
| `agentdx_exploration_schedules_total` | counter | Exploration cost | — |
| `agentdx_exploration_reduced_total` | counter | Reduction effectiveness | ratio < 0.2 suggests weak reduction |
| `agentdx_memory_rss_mb` | gauge | Memory | > 500 MB |
| `agentdx_ui_frame_time_ms` | histogram | Client-reported, opt-in | p95 > 16.7 ms |
| `agentdx_nondeterminism_warnings_total` | counter | Determinism health | any value > 0 is investigated |

`agentdx doctor --verbose` prints the last run's metrics with interpretation, which is the first thing to attach to a bug report.

---

# 36. Error Handling

Every error has: a stable code, a detection point, a handling strategy, a log entry, a user-facing message, and a recovery path. Codes are namespaced by subsystem and are part of the public contract (they appear in CI output and are linked from docs).

| Code | Condition | Detection | Handling | User message | Recovery |
|---|---|---|---|---|---|
| `E-INSTR-001` | No AgentDX spans recorded | Analysis sees 0 spans | Abort analysis, exit 2 | "No AgentDX spans recorded. Did you wrap the graph with `agentdx.instrument()`?" | Link to instrumentation docs; `agentdx instrument --dry-run` |
| `E-INSTR-002` | Unsupported framework construct | SDK adapter | Emit `instrumentation_gap`, continue | Warning in the run summary: "3 constructs not instrumented; analysis may be incomplete" | `agentdx doctor` lists them |
| `E-EVENT-001` | Malformed event (structural) | Writer validation | **Raise immediately** — never write | "Internal error: invalid event (field `vclock` missing)" | Bug report with the run id |
| `E-EVENT-002` | Referential violation (`causal_parents` ≥ `seq`) | Writer validation | Raise | Same | Bug report |
| `E-CACHE-001` | Cache miss in replay | Cache lookup | Hard error, exit 3 `[SOURCE]` | Names agent, model, key prefix, nearest cached prompt, and the exact `--record` command | Re-record |
| `E-CACHE-002` | Cache DB corrupt | SQLite error | Abort | "Cache database unreadable" | `agentdx cache verify`, then re-record |
| `E-SCHED-002` | Deadlock | No runnable task, no timer | End run `FAILED`, dump wait reasons | "Deadlock at t=4200ms: `reviewer` awaiting message from `coder`; `coder` awaiting state key `plan`" | **This is often a real finding**, so the partial log is retained and analysed |
| `E-SCHED-003` | Livelock / step budget | 10 000 steps without clock advance | Abort | "Scheduler made no progress over 10 000 steps" | Inspect the log; raise the budget if legitimate |
| `E-SCHED-004` | Determinism leak | Leak detector | `strict`: abort; else warn | "`time.time()` called outside AgentDX in `coder`" | Wrap the call, or disable strict mode knowingly |
| `E-FAULT-001` | Fault target not found | Scenario validation | Abort before execution | "Fault targets `revewer`; valid: planner, coder, reviewer, tester" | Fix the typo |
| `E-CHAOS-001` | Fault outside blast radius | Runtime re-check | Abort run | "Fault `tool_failure(deploy)` outside declared blast radius" | Widen the radius deliberately |
| `E-SCEN-001…009` | Scenario schema errors | Load-time validation | Abort, exit 2 | Path, line, and the offending key | Fix the YAML |
| `E-GUARD-001` | Abort guard tripped | Guard monitor | Seal log, `ABORTED_GUARD` | "Aborted: token budget 200 000 exceeded at t=38 200ms" | Raise the guard or fix the retry storm |
| `E-ANLZ-002` | Cycle in the timing DAG | DAG construction | Abort analysis for that pass | "Cycle detected: span A → B → A. This indicates an instrumentation defect" | Bug report with the bundle |
| `E-ANLZ-003` | Critical path > makespan | Invariant check | Abort analysis | Same as above | Bug report |
| `E-ANLZ-004` | Residual > 2% | Invariant check | **Continue**, flag | "0.7% of elapsed time unattributed" or, above threshold, "12% unattributed — run `agentdx doctor`" | Improve instrumentation |
| `E-BASE-002` | Baseline not constructible | Baseline generator | Skip baseline, continue | "Baseline skipped: task requires multi-agent tool access. Critical path and overhead are still reported" | Supply `--baseline-prompt` |
| `E-REPLAY-001` | Replay divergence | Verification | Fail, exit 6 | First divergent event side by side, field named | Bug report — this is an AgentDX defect |
| `E-REPLAY-002` | Graph hash mismatch | Replay preflight | Refuse | "Graph changed since recording; replay would be meaningless" | Re-record |
| `E-BUNDLE-001` | Corrupt/tampered bundle | Hash chain verification | Refuse import | "Bundle integrity check failed at event 1043" | Re-export |
| `E-BUNDLE-002` | Incompatible schema | Manifest check | Attempt migration, else refuse | Both versions named | Upgrade AgentDX |
| `E-LLM-001` | Provider error in record mode | Provider shim | Retry per policy, then abort | Provider status and body | Retry; partial cache is retained |
| `E-API-4xx/5xx` | API errors | FastAPI handlers | Error envelope (§26) | Code + message + docs link | — |
| `E-WS-002` | WebSocket protocol error | WS handler | Close with a code | — | Client reconnects from `last_seq + 1` |
| `E-UI-001` | Frontend render error | React error boundary | Boundary per panel | "This panel failed to render. Other panels are unaffected." + copy-diagnostics button | Reload the panel only — **one broken panel must never take down the Control Tower** |

**Cross-cutting rules.**
1. **Never fail silently.** Anything that degrades analysis quality produces a visible warning attached to the run.
2. **Partial results beat no results.** A crashed run, an aborted run and a run with instrumentation gaps are all still analysed, with their limitations stated.
3. **Errors name the fix.** Every user-facing message contains either the command to run or the file and key to change.
4. **Internal errors are distinguishable from user errors.** Exit code 5 and the phrase "Internal error" are reserved for AgentDX defects, and those messages ask for a bundle.

---

# 37. CLI Specification

Built with Typer. Global options apply to every command: `--data-dir`, `--config`, `--verbose/-v`, `--quiet/-q`, `--json`, `--no-color`, `--seed`, `--strict/--no-strict`.

## 37.1 Commands

### `agentdx instrument`

```
agentdx instrument [PATH] [--framework langgraph|generic] [--dry-run] [--write]
```
Static analysis of a Python module or package: reports which nodes, tools and providers would be captured, and what would be missed. **Writes nothing unless `--write`.** Example:
```
$ agentdx instrument app.py --framework langgraph
  ✓ graph `app:graph` — 4 nodes will be captured: planner, coder, reviewer, tester
  ✓ 3 tools registered via @tool
  ⚠ direct `openai.OpenAI()` client at app.py:22 — wrap with agentdx provider shim
  → add: graph = agentdx.instrument(graph)
```
Exit: 0 ok · 2 nothing instrumentable found.

### `agentdx run`

```
agentdx run TARGET [--task FILE] [--scenario FILE] [--seed N]
                   [--record | --replay | --perturb | --passthrough]
                   [--baseline/--no-baseline] [--explore [--k N] [--max-schedules N]]
                   [--faults SPEC ...] [--ci] [--format junit|json|github] [--out DIR]
                   [--jobs N] [--calibrate] [--yes]
```
`TARGET` is a fixture name, an import path (`./app.py:graph`), a scenario file, or a directory of scenarios.
Examples:
```
agentdx run fixtures/code_pipeline
agentdx run ./app.py:graph --task tasks/refactor.md --record --calibrate
agentdx run scenarios/ --ci --format junit --out ci-out/
agentdx run fixtures/code_pipeline --explore --k 2
```
Exit: per §37.2.

### `agentdx replay`

```
agentdx replay RUN_ID|BUNDLE [--verify] [--at-virtual-ts MS] [--print-state] [--times N]
```
Re-executes from the log/bundle. `--verify` asserts canonical-log equality; `--times 100` is the determinism check. Exit: 0 · 6 divergence · 3 cache miss.

### `agentdx analyze`

```
agentdx analyze RUN_ID [--only race|timing|overhead|baseline|resilience|verdict]
                       [--explain] [--show-baseline-prompt] [--json]
```
Re-runs analysis over a sealed log (useful after an analyser upgrade — creates a new `analysis_version`, never mutating events). `--explain` prints the verdict rules, thresholds and every formula used.

### `agentdx compare`

```
agentdx compare RUN_A RUN_B [--json] [--tolerance-file FILE] [--force]
```
Metric deltas, findings added/removed, verdict change. Refuses when scenario hashes differ unless `--force`. Exit: 0 no regression · 1 regression beyond tolerance.

### `agentdx scenario`

```
agentdx scenario validate PATH        # schema + semantic validation, resolved output
agentdx scenario list [--json]
agentdx scenario new NAME [--from RUN_ID]     # generate a scenario from an existing run
agentdx scenario expand PATH          # print the matrix expansion
```

### `agentdx export` / `agentdx import`

```
agentdx export RUN_ID -o FILE.agentdx [--include-cache-bodies] [--sanitise]
agentdx import FILE.agentdx [--verify]
```

### `agentdx doctor`

```
agentdx doctor [--verbose]
```
Checks: Python version; `PYTHONHASHSEED`; data dir writability; SQLite version and WAL support; DuckDB availability; port availability; cache integrity; last run's determinism quality, instrumentation gaps and residual; whether an API key is present in a committed file. Prints a fix for every failure. Exit: 0 all pass · 1 warnings · 2 failures.

### Additional commands `[DETAIL]`

```
agentdx ui [--port 8420] [--host 127.0.0.1] [--open]     # serve the Control Tower
agentdx cache {stats|verify|prune|migrate|export}
agentdx baseline update [--scenarios DIR]                 # refresh CI regression baselines
agentdx bench [--suite all|overhead|determinism|race|explore|ui]
agentdx version [--json]
```

## 37.2 Exit codes (authoritative)

| Code | Meaning |
|---|---|
| 0 | Success; all assertions passed |
| 1 | Assertion failure / regression detected |
| 2 | Usage, configuration or validation error |
| 3 | LLM cache miss in replay mode |
| 4 | Guard aborted a run |
| 5 | Internal error (AgentDX defect) |
| 6 | Determinism verification failed |
| 7 | No scenarios or runs found |

Stable across releases; a change here is a breaking change.

## 37.3 Output conventions

- Human output: colour when a TTY, quiet when piped; the scorecard block (§17.4) is the terminal's headline output.
- `--json` on any command emits a machine-readable object to stdout with all human output suppressed to stderr, so every command is scriptable.
- Progress: a single-line status during a run (`agents 4 · events 1 204 · t=2.4s virtual`), replaced by the scorecard at the end.

---

# 38. Developer Experience

## 38.1 The first five minutes

The target: **clone → install → run fixture → open Control Tower → see a real defect → replay it → understand the verdict**, with **no API keys**. `[SOURCE]`

```
$ git clone https://github.com/<user>/agentdx && cd agentdx     #  0:00
$ uv sync                                                        #  0:20  (no compiler needed)
$ uv run agentdx run fixtures/code_pipeline                      #  0:45

  AgentDX 0.4.0 · fixture code_pipeline · seed 42 · mode replay (offline)
  agents 4 · events 4 183 · virtual 6.01s · wall 1.9s

  Coordination Efficiency: 0.83×  ⚠  slower than single-agent
  ─────────────────────────────────────────────────────────────
  Ideal parallel speedup     2.40×   (total work 14.4s / critical path 6.0s)
  Achieved speedup           0.83×   (baseline 5.0s / multi-agent 6.0s)
  Overhead cost             -1.57×
    handoff latency          -0.71×   3.66s on critical path   [seq 1039→1051]
    blocking wait            -0.52×   2.68s                    [seq 1102→1140]
    redundant tool calls     -0.24×   1.24s, 3 400 tokens      [seq 880, 884]
    orchestration            -0.10×   0.52s                    [seq 210→240]
  Token cost multiplier      3.1×     Comparability B (cache reuse 62%)

  FINDINGS (2)
  ● critical  LOST UPDATE  draft.module_a   coder@t2418 ∥ reviewer@t2431
  ▲ high      COORDINATION BOTTLENECK  coder→reviewer = 61% of critical path

  Verdict: STATE_CONFLICT_RISK (score 61, confidence high)
           merge `reviewer` into `coder`; the handoff on that edge accounts
           for 61% of critical-path time.

  → agentdx ui        open the Control Tower at http://127.0.0.1:8420/runs/r_f2a91

$ uv run agentdx ui --open                                       #  1:10
```

In the browser: the ghost baseline in the waterfall; clicking the lost-update finding highlights both spans and both nodes; the scrubber seeks to t=2418; "Generate reproduction" writes `scenarios/repro_f_0117.yaml`; running it re-triggers the finding. **Total elapsed: under five minutes, no credentials, no network.**

## 38.2 Why this works with no API keys

The fixture caches are committed (§23), replay mode is the default (§21.4), and a replay-mode cache miss is a hard error rather than a live call (§11.2). The demo cannot accidentally cost money or require credentials — a property worth stating in the README because it is unusual.

## 38.3 Documentation set

| Doc | Audience | Content |
|---|---|---|
| `README.md` | P6 reviewer | Thesis in one screen, the ghost-baseline GIF, the 4-command quickstart, the benchmark table, honest limitations |
| `docs/instrumentation.md` | P1 | LangGraph and generic paths; the one-line integration; what is and is not captured |
| `docs/event-schema.md` | All engineers | §9 as reference, with the JSON Schema |
| `docs/determinism.md` | P4, P6 | §10, including **what cannot be guaranteed** (§10.6) |
| `docs/limits.md` | P6 | Bounded exploration; comparability; the absence-of-proof statement |
| `docs/scenarios.md` | P3 | Schema, faults, safety rails, examples |
| `docs/ci.md` | P4 | Exit codes, artifacts, the GitHub Action |
| `docs/architecture.md` | New contributors | §24–§27 condensed |
| `bench/README.md` | P2, P6 | Methodology and how to reproduce every published number |

## 38.4 Contributor experience

`uv sync --all-extras` installs everything; `just test` / `just bench` / `just ui` wrap the common loops; pre-commit runs ruff, mypy (strict on `analysis/` and `events/`), and the import-linter rule of §24.3. A first-time contributor can add a verdict rule by editing `verdict_rules.toml` plus two tests, and the docs say so.

---

# 39. Deployment

## 39.1 Local development

```
uv sync                     # Python 3.12+, no compiler required
uv run agentdx run fixtures/code_pipeline
uv run agentdx ui --open
cd frontend && npm ci && npm run dev     # frontend hot reload against the local API
```
Data lives in `~/.agentdx/` (`runs.db`, `cache.db`, `runs/<id>/`, `metrics.jsonl`).

## 39.2 Docker Compose (the one-command demo)

```yaml
services:
  agentdx:
    build: .
    ports: ["8420:8420"]
    volumes: ["./.agentdx-data:/data"]
    environment:
      AGENTDX_DATA_DIR: /data
      AGENTDX_MODE: replay          # offline by default
      PYTHONHASHSEED: "0"
    command: >
      sh -c "agentdx run fixtures/code_pipeline &&
             agentdx run fixtures/support_triage &&
             agentdx run fixtures/research_fanout &&
             agentdx ui --host 0.0.0.0"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8420/api/health"]
      interval: 5s
      timeout: 3s
      retries: 20
```

**Gate: `docker compose up` → working demo in under 3 minutes on Apple silicon.** `[SOURCE]` Achieved by a multi-stage build with the frontend built at image-build time, the fixture caches baked in, and no model calls at runtime. A CI job times this on every release.

Note the `--host 0.0.0.0` inside the container is safe because the port binding is controlled by Compose; the warning in §31.10 applies to bare-metal use.

## 39.3 Platform support

| Platform | Status |
|---|---|
| macOS Apple silicon | **Primary target** `[SOURCE]` — CI + manual gate |
| macOS Intel | Supported, CI |
| Linux x86_64 / arm64 | Supported, CI (the Docker target) |
| Windows | **Not supported in v1** (§32), stated in the README |

## 39.4 Build and packaging

- **Backend:** `uv build` → wheel + sdist; entry point `agentdx`; no compiled extensions, so no wheels-per-platform problem.
- **Frontend:** `vite build` → static assets, copied into `src/agentdx/api/static/` and shipped inside the wheel. `agentdx ui` therefore serves a fully self-contained app with no Node requirement for end users.
- **Fixture caches:** committed as compressed SQLite files under `fixtures/*/cache/`, included as package data.
- **Image:** multi-stage (node build → python runtime), non-root user, target < 500 MB.

## 39.5 Serving

FastAPI serves both the API (`/api`, `/ws`) and the built frontend (`/` with SPA fallback) from one process on port 8420, bound to loopback by default. One process is a deliberate simplification: there is no reverse proxy, no CORS configuration and no second port to explain.

## 39.6 Release process

1. Conventional commits → version bump.
2. CI must be green, including the determinism suite and the benchmark gates.
3. `bench/results/` regenerated and committed if any published number changed.
4. Tag → GitHub Actions builds the wheel and the image, publishes to PyPI and GHCR, attaches the fixture bundles to the release.
5. `CHANGELOG.md` records event-schema and exit-code changes in a dedicated **Breaking** section — these are the two contracts external users depend on.

---

# 40. Project Roadmap

The source 10-week roadmap `[SOURCE]` is preserved exactly in its deliverables and gates, and expanded with objectives, dependencies and demo milestones.

## 40.1 Week-by-week

| Wk | Engineering objectives | Deliverables | Depends on | Acceptance gate `[SOURCE]` | Demo milestone |
|---|---|---|---|---|---|
| **1** | Lock the event contract; stand up storage and capture | Event schema + validators + canonical form; SQLite store with append-only triggers; decorator SDK; LangGraph adapter v0; code-pipeline fixture | — | **Events land for a real LangGraph run** | `agentdx run` prints an event count and an agent list |
| **2** | Make execution deterministic | Cooperative scheduler; virtual clock; seeded RNG; determinism patches + leak detector; scenario schema v1 (FR-11a, per §4.5) | Wk 1 | **Same seed → same interleaving, 100×** | Two runs, same seed, identical schedule printed |
| **3** | Make runs free and offline | LLM record/replay cache; provider shim; cache key construction; **replay-equality assertion wired into CI** `[SOURCE]` | Wk 2 | **Full run replays offline, byte-identical canonical log** | Unplug the network; the fixture still runs |
| **4** | Break things safely | Fault injector + interception points; MVP faults (latency, crash, drop, tool failure); blast radius, hypothesis, guards | Wk 2, 3 | **Kill an agent mid-run; the log shows the cascade** | Terminal shows the crash and the retry cascade |
| **5** | Make time legible | Timing DAG; critical path; six-bucket decomposition; redundancy detector; parallelism metrics | Wk 1, 4 | **Time buckets sum to wall clock ±2%** | Decomposition table in the terminal |
| **6** | Answer the product question | Baseline generator; comparability grading; speedup formulas; attribution; verdict engine v1 | Wk 3, 5 | **Terminal prints the FR-8 scorecard** | The scorecard block, end to end |
| **7** | Find the invisible bug | Vector clocks in events; race detector; the four false-positive guards; minimal repro generator; research fan-out fixture | Wk 1, 2 | **Finds the seeded race; clean on the healthy fixture** | Finding printed with both spans and a repro command |
| **8** | Make it visible | Control Tower shell; API (runs, waterfall, findings, scorecard); waterfall panel with the ghost baseline; scorecard panel | Wk 5, 6 | **Ghost-baseline visualisation working** | Browser screenshot for the README |
| **9** | Make it explorable | Graph panel; chaos panel (arm→fire, blast radius shown); findings cross-linking; timeline scrubber; WebSocket live stream | Wk 4, 7, 8 | **End-to-end demo** | The full three-panel demo, click-through |
| **10** | Make it credible and shippable | Bounded exploration (FR-6); CI mode (FR-11b); support-triage fixture; benchmark suite + published results; README + GIF; Docker demo; OTel export if time allows | All | **Public repo** | `docker compose up` in <3 min; README GIF |

## 40.2 Dependency-critical path of the project itself

```
Wk1 events ──▶ Wk2 scheduler ──▶ Wk3 cache ──▶ Wk4 faults
                     │                 │            │
                     └────────▶ Wk7 race ◀──────────┘
                                       │
     Wk5 critical path ──▶ Wk6 baseline/verdict ──▶ Wk8 UI ──▶ Wk9 UI ──▶ Wk10 ship
```

The event schema (week 1) and the scheduler (week 2) are on the project's own critical path. **Slippage there costs a week downstream; slippage in weeks 8–10 costs only polish.** Spend the buffer early.

## 40.3 Fallback scope-cut order `[SOURCE]`

> **If the schedule slips, drop in this order:** bounded exploration (FR-6) → CI mode (FR-11b) → resilience scoring (FR-9) → the dependency graph panel (the waterfall carries the demo alone).

Extended with two further cuts and the hard floor `[DETAIL]`:

| Order | Cut | Cost of cutting |
|---|---|---|
| 1 | FR-6 bounded exploration | Loses a strong depth signal; the seeded race is still found in one run |
| 2 | FR-11b CI mode | Loses the P4 persona; scenarios still work |
| 3 | FR-9 resilience scoring | Loses the P3 persona's number; faults still demonstrate cascades |
| 4 | Graph panel | The waterfall carries the demo alone `[SOURCE]` |
| 5 | OTel export | Loses a credibility signal, not a capability |
| 6 | Support-triage fixture | Loses the redundancy demo; keep code-pipeline and research fan-out — **the false-positive control is never cut** |
| — | **Hard floor** | FR-1, FR-2, FR-3, FR-5, FR-7, FR-8, FR-12 (code-pipeline + research fan-out), waterfall + scorecard. Below this the thesis is not demonstrable and the project should ship late rather than incomplete |

---

# 41. Team Responsibilities

The source targets a solo build `[SOURCE]`; ownership is nonetheless defined by area so the architecture supports a team later, and so a solo builder knows which hat they are wearing.

| Area | Owns | Interfaces exposed | Cannot change without agreement |
|---|---|---|---|
| **Runtime** | Scheduler, virtual clock, determinism, fault injector, cache | `Scheduler`, `VirtualClock`, `FaultInjector`, `Cache` | The event contract; yield-point semantics |
| **SDK** | Decorators, LangGraph adapter, provider shims, sync primitives | `instrument()`, `@agent`, `@tool` | Event field meanings; the "one line" integration promise |
| **Event & storage** | Schema, validation, canonical form, SQLite/DuckDB, bundles, migrations | `EventWriter`, `Store` | Schema version policy; append-only invariants |
| **Analysis** | Causality, race, timing, overhead, baseline, resilience, verdict | Pure functions over logs | Verdict thresholds without a version bump; the evidence contract |
| **Frontend** | Control Tower, panels, store, design tokens | Consumes the OpenAPI client | Information priority (§29.5) |
| **API** | FastAPI routes, WebSocket protocol, OpenAPI schema | HTTP/WS contract | Endpoint shapes without a version note |
| **Testing/QA** | All suites, golden files, false-positive suite, fixtures | `just test` | Skipping the false-positive suite (§33.9) — never permitted |
| **DevOps** | uv, Docker, CI workflows, release, benchmarks harness | `just`, workflows | Exit codes; the 3-minute demo gate |
| **Documentation** | README, docs set, published limitations | — | Adding a statistic without a reproducible measurement (Rule E1) |

**Solo-build sequencing:** weeks 1–3 are Runtime + Event/Storage; weeks 4–7 are Runtime + Analysis; weeks 8–9 are Frontend + API; week 10 is DevOps + Documentation. Testing is continuous and is never a separate phase — the determinism suite must exist by week 3 `[SOURCE]`.

---

# 42. Risks & Mitigations

The source risk table `[SOURCE]` is preserved and expanded with probability, detection and fallback for each.

## 42.1 Determinism leaks

| Field | Detail |
|---|---|
| Risk | Unseeded RNG, wall-clock reads, unordered dict/set iteration, concurrent tool I/O `[SOURCE]` |
| Probability | **High** — this is the default state of Python code |
| Impact | **Critical** — the entire product claim collapses |
| Detection | Replay-equality assertion in CI **from week 3** `[SOURCE]`; the leak detector (§10.6); a lint rule banning direct `time.time()`/`random` in instrumented paths `[SOURCE]`; ≥10 of 100 replays in fresh processes |
| Mitigation | Trap ambient non-determinism at the runtime boundary (§10.5); `PYTHONHASHSEED=0`; integer-only virtual clock; `strict` mode aborts on a leak |
| Fallback | If a leak proves untrappable in user code, narrow the guarantee explicitly to *"determinism of the coordination structure given wrapped tools"*, document it, and report per-run **determinism quality** — never claim more than is measured |

## 42.2 Scope explosion

| Field | Detail |
|---|---|
| Risk | Every feature in the source research sounds essential `[SOURCE]` |
| Probability | **High** |
| Impact | High — nothing ships |
| Detection | Weekly gate slippage; any milestone with an unplanned deliverable |
| Mitigation | The ⭐ rule: FR-2, FR-3, FR-5, FR-7, FR-8 are the product; everything else is optional. Cut RL, cut anomaly detection, cut hosted mode `[SOURCE]`. §4.6's three scope rules |
| Fallback | The §40.3 cut order, down to the hard floor |

## 42.3 Race detector false positives

| Field | Detail |
|---|---|
| Risk | False positives destroy trust `[SOURCE]` |
| Probability | **High without guard G3** — LangGraph reducer channels are designed for concurrent writes and would each look like a race |
| Impact | High — a tool that cries wolf is abandoned |
| Detection | The false-positive suite (§33.9); the research fan-out golden empty file; the §34.3 precision benchmark (precision must be 1.0) |
| Mitigation | Require concurrency **and** value divergence `[SOURCE]`, plus declared-merge (G3) and explicit-synchronisation (G4) guards; show suppressed conflicts in an auditable drawer |
| Fallback | Raise the reporting threshold to `write_write` with divergence only, demote `read_write`/`write_read` to `info`, and say so in the docs |

## 42.4 Virtual-clock divergence

| Field | Detail |
|---|---|
| Risk | The virtual clock diverges from reality, making numbers meaningless `[SOURCE]` |
| Probability | Medium |
| Impact | Medium-high — speedup numbers lose meaning |
| Detection | Post-run drift check (>10% raises `clock_drift`); the §34.5 virtual-vs-wall benchmark |
| Mitigation | Calibration pass against real timings; **always show measured wall clock next to virtual** `[SOURCE]` |
| Fallback | Report speedup in both virtual and measured wall time, and mark virtual-only results as indicative |

## 42.5 Model / cache limitations

| Field | Detail |
|---|---|
| Risk | Groq free-tier limits during recording `[SOURCE]`; model deprecation; volatile prompts causing permanent cache misses |
| Probability | Low for rate limits (one recording pass, then cache forever `[SOURCE]`); **Medium** for deprecation over a year |
| Impact | Medium |
| Detection | Cache hit-rate metric; the prompt-volatility diagnostic (§11.4) |
| Mitigation | Rate-limit the recorder; one recording pass then cache forever; the OpenAI-compatible shim (§8.5) so the provider is swappable; **an existing cache never needs a provider again** |
| Fallback | Ship committed fixture caches so the demo is immune to any provider change |

## 42.6 Scheduler correctness

| Field | Detail |
|---|---|
| Risk | A subtly wrong scheduler produces plausible but incorrect interleavings, invalidating race findings and timings |
| Probability | Medium |
| Impact | **Critical** — wrong findings are worse than no findings |
| Detection | Hand-computed golden schedules; deadlock/livelock tests; the invariant `critical_path ≤ makespan`; property tests over generated task sets |
| Mitigation | Keep the scheduler small and side-effect free at the decision point; the single decision function (§10.2) is the only source of ordering |
| Fallback | Restrict yield points to a smaller, fully enumerated set and document the reduced concurrency model |

## 42.7 Performance overhead

| Field | Detail |
|---|---|
| Risk | Instrumentation exceeds the 10% budget `[SOURCE]`, making adoption unattractive |
| Probability | Medium |
| Impact | Medium |
| Detection | §34.1 benchmark, gated at 12% in CI |
| Mitigation | Off-hot-path serialisation, batched writes, `blake2b`, copy-on-write clocks, no analysis during execution |
| Fallback | A `lightweight` capture mode that drops `state_read` events (losing `read_write` detection) — documented as a trade-off, never a silent default |

## 42.8 UI complexity

| Field | Detail |
|---|---|
| Risk | Three panels are a lot of frontend for one person `[SOURCE]` |
| Probability | Medium |
| Impact | Medium |
| Detection | Week 8–9 gates |
| Mitigation | Build waterfall → scorecard → graph in that order. The waterfall alone tells the story `[SOURCE]` |
| Fallback | Ship waterfall + scorecard + findings; the graph becomes P1 |

## 42.9 Framework compatibility

| Field | Detail |
|---|---|
| Risk | LangGraph internals change and the adapter breaks |
| Probability | Medium (a fast-moving library) |
| Impact | Medium |
| Detection | A pinned-version CI matrix plus a nightly job against LangGraph `latest`; `instrumentation_gap` events surface partial breakage in the field |
| Mitigation | Proxy-based interception rather than monkey-patching internals (§8.3); fail loudly, never silently; the generic decorator API as the escape hatch |
| Fallback | Pin a supported range in `pyproject.toml` and document it; the fixtures pin the same range so the demo never breaks |

## 42.10 Competitive positioning

| Field | Detail |
|---|---|
| Risk | The "this is just LangSmith" objection `[SOURCE]` |
| Probability | Medium-high in any serious review |
| Impact | Medium — a credibility failure, not a technical one |
| Detection | Review feedback; README bounce |
| Mitigation | §1.7 framing + OTel interop + **lead with the speedup verdict, never with the trace view** `[SOURCE]`; the honest competitive table (§1.7) rather than a "no competition" claim |
| Fallback | Reposition explicitly as "a testing harness that complements your observability stack", which is true and defensible in any room |

## 42.11 Additional risks `[DETAIL]`

| Risk | Prob | Impact | Detection | Mitigation | Fallback |
|---|---|---|---|---|---|
| Baseline is not fairly comparable | Medium | High | Cache reuse rate; comparability grade | Grade every comparison; de-emphasise grade C; publish the baseline prompt | Report overhead decomposition without a speedup ratio |
| Bounded exploration is misread as exhaustive | Medium | Medium | Review feedback | The mandatory coverage statement in CLI, API and UI (§15.6) | Rename the feature "schedule sampling" |
| Event-store growth on long runs | Low | Medium | `event_store_bytes` metric | Batched writes, Parquet export, explicit prune | Sampling mode for `state_read` events |
| Solo-builder bus factor | — | High | — | Documentation-first; this PRD; typed interfaces; §41 ownership map | — |

---

# 43. Open Questions & Engineering Decisions

Genuine decisions are **not** silently resolved here. Each carries a recommended default so work is never blocked, and an owner and a deadline so a decision is actually made.

## 43.1 Must decide before implementation

| # | Question | Recommendation | Decide by |
|---|---|---|---|
| 43.1.1 | **Naming** — AgentDX vs AgentOrchestrator vs Orchestration Lab. "DX" reads as developer-experience, which slightly undersells the reliability angle. Renaming after the repo is public costs stars and links. `[SOURCE]` | **Keep AgentDX.** It is shortest and clearest `[SOURCE]`, the package name is available, and the tagline carries the reliability framing. Decide before the repo goes public | Week 1 |
| 43.1.2 | **Scope of `--ci`** — is regression-gating a v1 feature, or does it dilute the pre-deployment story? `[SOURCE]` | **Split** (§4.5): scenario YAML is P0 (FR-4 depends on it); CI mode is P1 and *extends* rather than dilutes the story | Week 1 |
| 43.1.3 | **CrewAI adapter** — broader reach, or a distraction from doing LangGraph properly? `[SOURCE]` | **Defer to P2.** The generic decorator API covers non-LangGraph systems. Doing one framework properly is the stronger signal; revisit only if a real user asks | Week 2 |
| 43.1.4 | **Byzantine faults** — is "confidently wrong output" measurable without an eval layer, or does it need a judge model (and therefore non-determinism)? `[SOURCE]` | **Measurable without a judge** (§11.8): source wrong outputs from a curated fixture pool, and measure the *system's response* (did any agent detect it? did the run claim success?) rather than judging text | Week 4 |
| 43.1.5 | Event schema changes of §9.1 (`causal_parents` array, `span_id`, `sched_step`, `schema_version`, hash chain) | **Adopt all.** The schema is frozen at the end of week 1; changing it later invalidates recorded runs | Week 1 |
| 43.1.6 | Determinism guarantee wording — canonical projection vs literal byte equality (§10.1) | **Adopt the canonical projection.** Literal equality including `wall_ts_ms` is impossible and would make the CI gate untestable | Week 2 |

## 43.2 Can decide during implementation

| # | Question | Recommended default |
|---|---|---|
| 43.2.1 | LangGraph version range to pin; whether to support the functional `@entrypoint` API | Pin a tested minor range; graph API only in v1; add functional API if a fixture needs it |
| 43.2.2 | SQLite→DuckDB threshold | 20 000 events (§27.1); tune from benchmarks |
| 43.2.3 | Calibration defaults when no profile exists | LLM 800 ms, tool 200 ms, agent_step 50 ms (§10.4) |
| 43.2.4 | Sleep sets / full DPOR in exploration | Independence-based reduction in v1; upgrade only if redundancy >40% (§15.4) |
| 43.2.5 | Waterfall SVG→Canvas switchover | 20 000 spans (§28.5) |
| 43.2.6 | Verdict weights in `verdict_rules.toml` | §18.2 defaults; tune against the fixtures, version any change |
| 43.2.7 | Whether `state_read` capture is sampled in a lightweight mode | Full capture by default; lightweight mode only as the §42.7 fallback |
| 43.2.8 | Bundle format zip vs tar.zst | zip (§20.7), for cross-platform tooling and random access |

## 43.3 Deliberately deferred

| # | Decision | Why deferred |
|---|---|---|
| 43.3.1 | Hosted mode, auth, multi-tenancy, billing | Out of scope by product decision (§4.4). Revisit only with real multi-user demand, and as a separate tier |
| 43.3.2 | **Semantic redundancy detection** (embedding similarity of tool args) — should the redundancy detector attempt it, or stay on exact-hash matching for v1? `[SOURCE]` | **Recommend exact hash** `[SOURCE]`. Semantic dedup introduces false positives and a model dependency into a deterministic pipeline. Revisit only with a labelled false-positive study |
| 43.3.3 | LLM-as-judge for semantic correctness | Contradicts the determinism thesis (§4.4); the pluggable assertion hook covers the need |
| 43.3.4 | Distributed / multi-process agent execution | Requires a distributed virtual clock; the event model accommodates it, the runtime does not (§4.4) |
| 43.3.5 | Automatic topology rewriting | AgentDX recommends; it does not refactor (§4.4) |
| 43.3.6 | Windows support | Test-matrix cost with no MVP value (§32) |
| 43.3.7 | Fine-tuning / RL for coordination optimisation | Cut in the source: no time, no eval signal `[SOURCE]` |

---

# 44. Acceptance Criteria

This section is the definition of done for the MVP. Every gate is **binary, observable, and executable by someone who has never seen the codebase**. A gate is met only when its command exits 0 in CI on a clean checkout — not when the behaviour has been seen working locally.

## 44.1 Global MVP acceptance gates

| # | Gate `[SOURCE]` | Binary criterion | Verification command | Blocking |
|---|---|---|---|---|
| **G1** | The seeded race is detected | `code_pipeline` run yields ≥1 finding of type `lost_update` on `draft.module_a`, severity `critical`, naming both `coder` and `reviewer` writes with their event sequences | `agentdx run fixtures/code_pipeline --assert findings.race >= 1` | Yes |
| **G2** | The healthy fixture yields **zero** false positives | `research_fanout` produces an empty race-findings set across all 100 determinism replays **and** across the k=2 exploration frontier | `pytest tests/false_positives/ -q` | Yes |
| **G3** | Deterministic replay, 100 out of 100 | 100 runs at seed 42, ≥10 in fresh processes, all canonical projections byte-identical (§10.1) | `pytest tests/determinism/test_replay_equality.py` | Yes |
| **G4** | Fault injection reproduces a failure | A scenario killing `reviewer` at t=3000 produces the same failure classification and the same cascade shape on every repeat | `agentdx scenario run scenarios/kill_reviewer.yaml --repeat 20` | Yes |
| **G5** | Critical path is validated against reality | Σ(six overhead buckets) + critical path = makespan within **±2%** on all three fixtures | `pytest tests/analysis/test_decomposition_invariant.py` | Yes |
| **G6** | Baseline comparison works | A single-agent baseline is generated for all three fixtures with a comparability grade, and its token/latency figures are reported with evidence | `agentdx compare <run_id> --baseline` | Yes |
| **G7** | The speedup verdict works | The FR-8 scorecard prints achieved speedup, ideal speedup, and the signed six-bucket attribution summing to the delta | `agentdx analyze <run_id> --scorecard` | Yes |
| **G8** | Control Tower renders the complete workflow | Waterfall with ghost baseline, scorecard, graph, findings, and scrubber all render for a stored run; clicking a finding cross-highlights span, node and timeline | `npm run test:e2e` (Playwright, 3 fixtures) | Yes |
| **G9** | The demo works offline | Full three-fixture demo completes with the network disabled and **no API keys present in the environment** | `just demo-offline` (runs with `--network none`) | Yes |
| **G10** | Docker demo in under 3 minutes on Apple silicon | `docker compose up` reaches a healthy `/api/health` and a populated run list in < 180 s, cold cache, arm64 | `just bench-docker-cold` | Yes |

## 44.2 Per-requirement acceptance summary

Each FR carries its own criteria in §7; this is the roll-up used at the weekly gate.

| FR | Ships in | Met when | Cut-safe |
|---|---|---|---|
| FR-1 Instrumentation | Wk 1 | LangGraph fixture instrumented in one line; overhead < 10%; `instrumentation_gap` raised on adapter breakage | No |
| FR-2 Deterministic execution | Wk 2 | G3 | No |
| FR-3 LLM record/replay | Wk 3 | G9; replay-mode miss is a hard error, never a silent live call | No |
| FR-4 Fault injection | Wk 4 | G4; every fault has a declared blast radius and abort guard | No |
| FR-5 Race detection | Wk 7 | G1 **and** G2 | No |
| FR-6 Bounded exploration | Wk 10 | k=2/N=200 completes on `code_pipeline` under the time budget; the coverage statement is printed verbatim | Yes (cut 1) |
| FR-7 Performance analysis | Wk 5 | G5 | No |
| FR-8 Baseline & verdict | Wk 6 | G6, G7; every verdict carries ≥1 evidence reference | No |
| FR-9 Resilience scoring | Wk 9 | Score computed per fault and aggregate; silent failure caps the score at 49 | Yes (cut 3) |
| FR-10 Replay & time travel | Wk 9 | Scrubbing to any `sched_step` reconstructs state in < 200 ms p95 | No |
| FR-11a Scenario YAML | Wk 2 | Schema validates; invalid scenarios fail with a line number | No |
| FR-11b CI mode | Wk 10 | Exit codes per §37; JUnit XML consumed by GitHub Actions | Yes (cut 2) |
| FR-12 Fixtures | Wk 1/7/10 | All three fixtures reproduce their expected findings exactly | Partly (cut 6, never the healthy control) |
| FR-13 OTel export | Wk 10 | Spans validate against the OTLP schema and import into a standard viewer | Yes (cut 5) |

## 44.3 Quality gates that are never waived

These are **not** feature gates; they are correctness gates. A release that fails any of them is not released, regardless of schedule pressure.

1. **G2 (zero false positives on the healthy fixture).** A tool that reports races in correct code is worse than no tool. `[SOURCE]`
2. **G3 (100/100 determinism).** The entire product claim rests on it.
3. **Race precision = 1.0** on the labelled benchmark set (§34.3). Recall may be < 1.0 and is reported honestly; precision may not.
4. **The bounded-exploration coverage statement** (§15.6) appears verbatim in CLI output, API responses and the UI. Removing it is a release blocker.
5. **Rule E1** — no statistic ships without a reproducible measurement in `bench/results/`.
6. **Every verdict carries evidence.** A verdict with an empty evidence array fails schema validation and cannot be rendered.

## 44.4 Explicit non-goals for acceptance

The MVP is **not** accepted against: exhaustive concurrency verification, semantic correctness judging, distributed execution, hosted multi-user operation, Windows support, or framework coverage beyond LangGraph plus the generic API. Claiming any of these would violate §4.4 and §15.6.

---

# 45. End-to-End Technical Walkthrough

One complete execution, traced through every layer, using the `code_pipeline` fixture and the seeded lost-update defect. Sequence numbers and timings match the demo output in §38.1. This section exists so that an engineer can read a single narrative and understand how all forty-odd components fit together.

## 45.1 The system under test

```
                 ┌──────────┐
   task ────────▶│ planner  │
                 └────┬─────┘
              ┌───────┴────────┐
              ▼                ▼
        ┌──────────┐     ┌──────────┐
        │  coder   │     │ reviewer │      both write state["draft"]
        └────┬─────┘     └────┬─────┘      no reducer declared  ← the defect
             └───────┬────────┘
                     ▼
               ┌──────────┐
               │ packager │
               └──────────┘
```
`draft` is a plain dict channel. `coder` writes `draft.module_a` after generating code; `reviewer` writes `draft.module_a` after revising it. LangGraph's default last-write-wins channel silently discards one of them depending on completion order. The bug is invisible in a trace viewer: both nodes succeed, the run reports success, and the output is merely *sometimes* wrong.

## 45.2 Step 1 — Instrumentation (FR-1, §8)

```python
from agentdx import instrument
graph = build_pipeline_graph()
app = instrument(graph, run_config="agentdx.toml")
result = app.invoke({"task": "build module A"})
```

`instrument()` walks the compiled graph and installs five bindings (§8.3): node entry/exit, channel read/write, tool invocation, LLM client call, and edge traversal. Each binding is a proxy, not a monkey-patch, so an unrecognised LangGraph internal produces an `instrumentation_gap` event rather than a silent hole. The reducer registry is populated here: `draft` is recorded as **no declared reducer**, which is what later permits guard G3 to *not* suppress the finding.

## 45.3 Step 2 — Scheduler takes control (FR-2, §10)

`instrument()` replaces the default asyncio event loop driver with the cooperative scheduler. Execution now advances only at declared yield points: LLM call boundaries, tool boundaries, channel reads and writes, and node entry/exit. At every yield the scheduler calls the single decision function:

```python
def choose(ready: list[Task], step: int, rng: Random) -> Task:
    ready.sort(key=lambda t: (t.priority, t.agent_id, t.local_seq))   # total order
    return ready[rng.randrange(len(ready))] if explore else ready[0]
```

Because `ready` is totally ordered before any random draw, and the RNG is seeded from `run_seed` and consumed only here, the interleaving is a pure function of `(seed, program)`. `sched_step` increments once per decision and is stamped on every event — this integer, not the wall clock, is the run's logical time.

## 45.4 Step 3 — Agent execution and event generation (§9)

`planner` runs first. Its node entry emits:

```json
{ "seq": 1, "schema_version": "1.0", "sched_step": 1,
  "type": "agent_start", "agent_id": "planner", "span_id": "s_0001",
  "vclock": {"planner": 1},
  "causal_parents": [], "virtual_ts_us": 0, "wall_ts_ms": 1754654201118,
  "fault_tainted": false, "payload": {"node": "planner"} }
```

Events are appended to an in-memory ring and flushed in batches off the hot path, keeping capture overhead inside the 10% budget (NFR-1). The append-only trigger on `events` rejects any UPDATE or DELETE, and each row carries `prev_hash`/`hash` forming the blake2b chain used for bundle tamper detection (§9.7).

## 45.5 Step 4 — LLM call served from cache (FR-3, §11)

`planner` calls the model. The provider shim intercepts at the OpenAI-compatible boundary and constructs the cache key from the canonicalised request: model, messages, temperature, tools, and the ordinal of this call within the agent. Key found in `fixtures/code_pipeline/cache/cache.db`:

```
llm_start   seq 12   virtual_ts 0.000s   cache=HIT   key=b2:9f41…
llm_end     seq 13   virtual_ts 0.812s   tokens_in=412 tokens_out=189
```

No network. In `replay` mode a miss would raise `E_CACHE_MISS` and abort — never a silent live call, which is what makes the offline guarantee (G9) real rather than aspirational. **The cache decides *what* the model returns; the scheduler decides *when* the reply is delivered.** Keeping those two concerns separate is why replay and exploration compose.

## 45.6 Step 5 — Virtual clock advances (§10.3)

The recorded latency (812 ms) is applied to the virtual clock, not slept on. The clock is an integer microsecond counter advanced only by the scheduler:

```
advance(agent="planner", delta_us=812_000)   →  virtual_now = 812_000
```

Wall time for this step was 1.4 ms. The run therefore models 6.01 s of virtual execution in 1.9 s of wall clock, and both numbers are reported side by side (§42.4) so the reader can judge the model rather than trust it.

## 45.7 Step 6 — Fan-out, and the fault that is *not* injected

`planner` completes; the scheduler makes `coder` and `reviewer` ready simultaneously. This baseline run injects no faults, so the fault injector's `should_fire()` returns false at each interception point and no event is tainted. (§45.16 shows the same run under a kill fault.)

Vector clocks fork at the fan-out:

```
coder    inherits {planner:4}  →  {planner:4, coder:1}
reviewer inherits {planner:4}  →  {planner:4, reviewer:1}
```

Neither vector dominates the other from here on: the two agents are formally concurrent, which is exactly the precondition the race detector needs.

## 45.8 Step 7 — The concurrent writes

```
seq 2418  state_write  coder     draft.module_a  vclock{planner:4,coder:31}  value_hash b2:71c…
seq 2431  state_write  reviewer  draft.module_a  vclock{planner:4,reviewer:27} value_hash b2:e08…
```

Both writes succeed. The channel keeps the later one. `packager` reads a `draft.module_a` that contains the coder's un-reviewed output, and the run reports success. **Nothing in the trace looks wrong** — which is precisely the failure class F3 (§2) that motivates the product.

## 45.9 Step 8 — Event store (§27)

4 183 events land in SQLite (WAL mode, batched writes, one writer). At the 20 000-event threshold analytics would shift to DuckDB over the same rows; below it, SQLite serves both. The run row is finalised with makespan, seed, schema version and the terminal hash of the chain. **From this moment the log is the single source of truth: every number in the rest of this walkthrough is derived from it, and nothing is derived from live process state.**

## 45.10 Step 9 — Causality graph and race detection (FR-5, §14)

Analysis begins post-hoc. Two distinct graphs are built (§14.1): the **causality graph** (happens-before, from vector clocks) and the **timing DAG** (durations, for critical path). Conflating them is the classic error; keeping them separate is why a long span and a concurrent span are never confused.

The detector walks accesses per state key:

```
for key, accesses in by_key.items():
    for a, b in pairs(accesses):
        if dominates(a.vclock, b.vclock) or dominates(b.vclock, a.vclock):
            continue                                  # ordered → not a race
        if a.kind == b.kind == "read":  continue      # G1
        if a.value_hash == b.value_hash: continue     # G2 no divergence
        if reducer_declared(key):        continue     # G3 designed concurrency
        if sync_between(a, b):           continue     # G4 explicit barrier
        emit_finding(...)
```

For `draft.module_a`: vectors are incomparable, both are writes, hashes differ, **no reducer is declared**, no barrier exists. A `lost_update` finding is emitted at severity `critical`, citing seq 2418 and 2431, both vector clocks, both value hashes, and the read at seq 2502 that consumed the survivor.

On the `research_fanout` fixture the same pass emits nothing: its shared channel *does* declare a reducer, so G3 suppresses it and the suppression is recorded in the auditable drawer rather than hidden. That contrast is the false-positive control (G2).

The minimal-repro generator then derives a scenario that forces the losing interleaving with a two-delay schedule, and writes `scenarios/repro_f_0117.yaml`.

## 45.11 Step 10 — Timing DAG and critical path (FR-7, §16)

Longest-path DP over the timing DAG, with deterministic tie-breaks on `(agent_id, seq)`:

```
critical path = 6.01s  :  planner ▸ coder ▸ [handoff 3.66s] ▸ reviewer ▸ packager
total work    = 14.42s
ideal speedup = 14.42 / 6.01 = 2.40×          (§17: total work ÷ critical path)
```

The six-bucket decomposition attributes the gap between ideal and achieved, and the invariant holds:

```
critical path 6.01 + handoff 3.66 + blocking 2.68 + redundant 1.24 + orchestration 0.52 … 
Σ buckets + critical path = makespan ± 2%      ✓ (G5)
```

The `coder → reviewer` edge alone carries 3.66 s — **61% of the critical path** — which becomes the second finding, `coordination_bottleneck`.

## 45.12 Step 11 — Single-agent baseline (§17)

The baseline generator constructs an equivalent single-agent program from the same task and the same tool set, and runs it against the same cache slice. Cache reuse is 62%, so the comparison is graded **B** — good enough to report a ratio, with the grade shown next to it rather than buried in a footnote.

```
baseline makespan 5.00s   |  multi-agent 6.01s
achieved speedup  5.00 / 6.01 = 0.83×
token multiplier  3.1×
```

## 45.13 Step 12 — Verdict engine (FR-8, §18)

Inputs: achieved 0.83×, ideal 2.40×, a critical race finding, a bottleneck edge at 61%, comparability B. Rule precedence puts state-correctness above performance, so:

```
verdict      STATE_CONFLICT_RISK
score        61
confidence   high      (grade B, deterministic replay confirmed, ≥2 evidence refs)
evidence     [ev:2418, ev:2431, ev:2502, edge:coder→reviewer, run:r_f2a91]
recommendation  merge `reviewer` into `coder` — the handoff on that edge
                accounts for 61% of critical-path time
```

The recommendation comes from the deterministic rule table, not from a model. Every clause traces to a row in the event log.

## 45.14 Step 13 — API and WebSocket (§26)

`GET /api/runs/r_f2a91/scorecard` returns the block above; `GET .../waterfall?from=0&to=6010` returns spans plus the ghost-baseline overlay; `GET .../findings` returns both findings with their evidence arrays. During a live run the same payloads stream over `/ws` as `event_batch`, `finding`, and `verdict` frames, so the UI does not poll.

## 45.15 Step 14 — Control Tower and the moment of recognition (§28, §29)

The developer opens the waterfall. The ghost baseline sits behind the multi-agent spans as a single pale bar ending at 5.0 s — **the multi-agent run visibly finishes later than the single agent it was supposed to beat.** That one image is the product's thesis rendered in a glance.

The findings rail shows the critical race first. Clicking it:

- highlights spans 2418 and 2431 in the waterfall,
- highlights `coder` and `reviewer` in the graph panel with the contested channel edge in `--crit`,
- seeks the scrubber to `sched_step` 2418 and shows the state panel with both pending values side by side.

The developer sees two different `module_a` bodies about to collapse into one. Nothing about this required reading the code.

## 45.16 Step 15 — Replay, and confirming the diagnosis

```
$ agentdx scenario run scenarios/repro_f_0117.yaml --repeat 20
  20/20 runs reproduce lost_update on draft.module_a   (delays: coder@yield3, reviewer@yield2)
```

The developer also runs the chaos variant to confirm the blast radius is understood:

```
$ agentdx scenario run scenarios/kill_reviewer.yaml
  fault: process_kill reviewer @ t=3000  (blast radius: agents=[reviewer])
  steady-state hypothesis violated: run_completes=false
  resilience 42/100 — silent failure detected (packager consumed a partial draft)
```

The score is capped at 49 by the silent-failure rule (§19), because a system that fails without saying so is not resilient regardless of its recovery time.

## 45.17 Step 16 — The fix, and the regression gate

The developer declares a reducer on the channel:

```python
draft: Annotated[dict, merge_module_writes]
```

Re-running: guard G3 now suppresses the conflict as *designed* concurrency, the finding disappears, and the suppression is visible in the drawer — the developer can see that AgentDX noticed the concurrent write and deliberately accepted it. The handoff bottleneck remains, so the verdict downgrades to `COORDINATION_OVERHEAD` with the same merge recommendation.

The repro scenario is committed and wired into CI:

```yaml
# .github/workflows/agentdx.yml
- run: agentdx scenario run scenarios/repro_f_0117.yaml --ci
```
```
assertions:
  - findings.race == 0
  - speedup.achieved >= 0.95
```

Exit 0 today; exit 2 the day someone removes the reducer. **The bug that was invisible in production is now a failing test in a pull request** — which is the entire arc the product exists to produce.

## 45.18 Layer summary of the walkthrough

| Layer | Component | What it contributed above |
|---|---|---|
| Capture | SDK bindings, reducer registry | Events, and the fact that `draft` had no reducer |
| Runtime | Scheduler, virtual clock, RNG | A reproducible interleaving; 6.01 s modelled in 1.9 s |
| Runtime | LLM cache, provider shim | Offline, free, identical model outputs |
| Runtime | Fault injector | The kill scenario and its blast radius |
| Storage | SQLite WAL, hash chain | The single source of truth |
| Analysis | Vector clocks, race detector, G1–G4 | The critical finding and the suppressed non-finding |
| Analysis | Timing DAG, decomposition | 2.40× ideal, the 61% edge, the ±2% invariant |
| Analysis | Baseline, verdict | 0.83× achieved, `STATE_CONFLICT_RISK`, the recommendation |
| Delivery | FastAPI, WebSocket | The payloads |
| Delivery | Control Tower, ghost baseline | The moment of recognition |
| Loop | Repro generator, CI mode | The regression test |

---

# 46. Final Architecture Summary

## 46.1 The product in one paragraph

AgentDX is a **local-first coordination debugger and chaos-testing harness for multi-agent LLM systems**. It executes an instrumented agent graph under a deterministic scheduler and a virtual clock, with all model calls served from a record/replay cache, so that any run is exactly reproducible and free. It records every coordination act to an append-only event log, then analyses that log post-hoc to answer one question: **does the multi-agent topology actually provide a benefit over a single-agent system, and if not, exactly where is the coordination overhead or reliability failure occurring?**

## 46.2 Definitive architecture

```
┌──────────────────────────── CONTROL TOWER (React) ────────────────────────────┐
│  Waterfall + ghost baseline  │  Scorecard/verdict  │  Graph  │  Chaos  │ Rail │
└──────────────────────────────────┬────────────────────────────────────────────┘
                        REST + WebSocket (FastAPI, :8420, loopback)
┌──────────────────────────────────┴────────────────────────────────────────────┐
│ ANALYSIS  (pure functions over the log — no live process state)               │
│  causality ▸ race (G1–G4) │ timing DAG ▸ critical path ▸ 6-bucket overhead    │
│  baseline ▸ comparability │ verdict ▸ evidence │ resilience │ exploration k=2  │
└──────────────────────────────────┬────────────────────────────────────────────┘
┌──────────────────────────────────┴────────────────────────────────────────────┐
│ STORAGE   SQLite (WAL, append-only triggers, hash chain)  ·  DuckDB >20k ev    │
│           llm_cache.db  ·  ~/.agentdx/runs/<id>/  ·  .agentdx bundles          │
└──────────────────────────────────┬────────────────────────────────────────────┘
┌──────────────────────────────────┴────────────────────────────────────────────┐
│ RUNTIME   cooperative scheduler (single decision fn) · virtual clock (int µs)  │
│           seeded RNG · determinism traps · fault injector · safety guards      │
└──────────────────────────────────┬────────────────────────────────────────────┘
┌──────────────────────────────────┴────────────────────────────────────────────┐
│ SDK       instrument() · LangGraph adapter (5 bindings, reducer-aware)         │
│           @agent/@tool generic API · OpenAI-compatible provider shim           │
└───────────────────────────────────────────────────────────────────────────────┘
```

**The one-way rule:** every arrow points downward for control and upward for data. Analysis never calls the runtime; the UI never reads the database directly; the SDK never knows an analysis exists. This is enforced mechanically by the import-linter contract in §24.3.

## 46.3 Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12, asyncio | Where agent frameworks live; cooperative scheduling is natural |
| Framework | LangGraph primary + generic decorator API | Depth on one framework beats breadth on four |
| Model | Groq Llama 3.1 8B via an OpenAI-compatible shim | Free tier for recording; provider-swappable; cache makes it irrelevant afterwards |
| Store | SQLite (WAL) + DuckDB analytics | One file, no server; columnar scans when logs grow |
| Cache | SQLite `llm_cache` | Same operational story; offline by construction |
| API | FastAPI + WebSockets | One process serves API, WS and static frontend |
| UI | React 18, Vite, TS, React Flow, Zustand, Tailwind, visx | React Flow for topology, visx for the waterfall |
| Tooling | uv, Docker Compose, ruff, mypy, import-linter | `uv sync` then one command to a working demo |
| Cost | **$0** | No hosted services, no credentials, no per-run spend |

## 46.4 Execution model, restated

1. Instrumentation wraps the graph; nothing about the agent logic changes.
2. The scheduler owns all ordering; `sched_step`, not wall time, is logical time.
3. The virtual clock owns all duration; recorded latencies are applied, never slept.
4. The cache owns all model output; replay-mode misses are hard errors.
5. The fault injector fires only at declared interception points, inside a declared blast radius, under abort guards.
6. The event log is append-only and hash-chained, and is the only input to analysis.
7. Analysis is a set of pure, deterministic functions; the same log always yields the same verdict.
8. Every reported number carries evidence pointing back into the log.

## 46.5 MVP scope, restated

**In:** FR-1 instrumentation, FR-2 determinism, FR-3 record/replay, FR-4 fault injection, FR-5 race detection, FR-7 performance analysis, FR-8 baseline and verdict, FR-10 replay, FR-11a scenarios, FR-12 three fixtures, and the waterfall + scorecard + findings UI.

**Stretch:** FR-6 bounded exploration, FR-9 resilience scoring, FR-11b CI mode, FR-13 OTel export, graph and chaos panels.

**Out:** hosted mode, auth, billing, multi-tenancy, LLM-as-judge, distributed execution, automatic refactoring, Windows, generic observability. `[SOURCE]`

## 46.6 Success criteria, restated

The build succeeds when the ten gates of §44.1 pass, and when a reviewer who has never seen the project can, in under five minutes and without credentials, watch AgentDX find a real concurrency defect that a trace viewer cannot see, replay it deterministically, understand from the ghost baseline that the topology is slower than a single agent, read *which* coordination edge caused it, and convert the finding into a failing CI test.

## 46.7 What AgentDX deliberately does not claim

- It does **not** exhaustively verify concurrency. Bounded exploration at k=2 samples a schedule frontier; **absence of a finding is not proof of correctness**, and this statement ships verbatim in the CLI, the API and the UI. `[SOURCE]`
- It does **not** judge semantic correctness of agent output.
- It does **not** guarantee determinism of arbitrary user code — only of the coordination structure, given wrapped tools and trapped ambient sources (§10.6).
- It does **not** replace observability. It is a testing harness that complements LangSmith, Langfuse and OpenTelemetry, and it exports to them rather than competing with them.
- It does **not** publish any statistic it has not measured itself and cannot reproduce from `bench/results/` (Rule E1).

---

# Appendix A — Source Synthesis

Preserved from PRD v1.0 `[SOURCE]`, with a final row recording what v2.0 adds.

| Source | Contributed |
|---|---|
| **Perplexity Research** (Multi-Agent System Reliability Validator) | The name AgentDX; failure-mode injection catalogue (latency, resource starvation, byzantine agents); task-decomposition analysis; the speedup calculator concept |
| **GPT 5.2 Comparison** (Orchestration Lab) | Trace-based behaviour modelling; replay as a first-class feature; throughput-vs-correctness framing; SLAs for agents; fallback to single-agent baseline |
| **Opus Comparison** (AgentOrchestrator) | Message-passing graph analysis; causal tracing of decision cascades; the "distributed systems debugger" framing; three-panel Control Tower |
| **Claude Opus 4.5 Thinking** | Foundational coordination-debugger concept; anomaly detection on communication patterns; latency heatmaps; replay for specific decisions |
| **Gemini screenshot** (Multi-Agent Chaos Engineering Debugger) | The chaos-engineering framing — active injection over passive monitoring; the $0 stack (Python/LangGraph/Groq/React Flow); "Chaos Control" panel |
| **PRD v1.0 adds** | Deterministic scheduler + virtual clock; LLM record/replay cache; vector-clock happens-before race detection; delay-bounded exploration; overhead decomposition; the corrected competitive positioning; safety rails; the ghost-baseline signature visual |
| **PRD v2.0 (this document) adds** | Full engineering specification of every subsystem: canonical event schema v1 with hash chain; the canonical-projection determinism definition; the reducer-aware false-positive guards G1–G4; the two-graph separation rule; the ±2% decomposition invariant; comparability grading; the evidence contract for verdicts; complete API, storage, CLI and error-code contracts; the mandatory false-positive test suite; benchmark methodology under Rule E1; and the corrections listed in Appendix C |

---

# Appendix B — Glossary

Preserved from PRD v1.0 `[SOURCE]`, extended with terms introduced in v2.0 `[DETAIL]`.

**From v1.0**

- **Happens-before** — Lamport's partial order on distributed events. If neither of two events happens-before the other, they are *concurrent*.
- **Vector clock** — per-agent logical timestamp vector; enables detecting concurrency without a global clock.
- **Critical path** — longest weighted path through the span DAG; the floor on wall-clock duration.
- **Delay bounding** — schedule exploration technique that bounds the number of preemptions/delays, finding most concurrency bugs at small *k*.
- **Byzantine agent** — an agent that returns plausible but wrong output rather than failing cleanly.
- **Blast radius** — the set of components a chaos experiment is permitted to affect.
- **Steady-state hypothesis** — the measurable property asserted to hold before a fault is injected.

**Added in v2.0**

- **Canonical projection** — the subset of event fields that must be byte-identical across replays; excludes volatile fields such as `wall_ts_ms`. The determinism guarantee is defined over this projection (§10.1).
- **Causality graph** — the happens-before graph derived from vector clocks. Distinct from the timing DAG; conflating them is the classic analysis error (§14.1).
- **Timing DAG** — the duration-weighted graph used for critical-path and overhead analysis.
- **Schedule** — a concrete sequence of scheduler decisions; identified by `(seed, delay schedule)`.
- **Delay schedule** — the ordered set of forced delays that distinguishes one explored schedule from the baseline.
- **Cache slice** — the subset of cached model responses reachable by a given run; the unit of fairness in baseline comparison.
- **Comparability grade** — A/B/C rating of how fairly a baseline can be compared to a multi-agent run, driven mainly by cache reuse (§17.5).
- **Coordination efficiency** — achieved speedup expressed against the single-agent baseline; the headline number of the scorecard.
- **Ghost baseline** — the pale single-agent bar rendered behind the multi-agent waterfall; the product's signature visual.
- **Guards G1–G4** — the four false-positive suppressions in race detection: read/read, no value divergence, declared reducer, explicit synchronisation (§14.6).
- **Evidence contract** — the rule that every finding, verdict and published number references specific event sequences in the log (§18.4).
- **`sched_step`** — the monotonic counter of scheduler decisions; the run's logical time, and the unit the replay scrubber seeks over.
- **Rule E1** — no statistic ships without a reproducible measurement committed to `bench/results/` (§2.12).
- **Instrumentation gap** — an event emitted when an adapter binding fails to attach, so partial coverage is loud rather than silent.

---

# Appendix C — Corrections to PRD v1.0

Forward-referenced from §10.1 and §17.3. These are the only places where v2.0 states that v1.0 was **incorrect** rather than merely incomplete. Both were found while writing the acceptance criteria — each would have produced a gate that could never pass.

## C.1 Ideal parallel speedup formula

| | |
|---|---|
| **v1.0 stated** | ideal parallel speedup = critical path ÷ total work |
| **Problem** | The critical path is by definition ≤ total work, so this ratio is always ≤ 1. It cannot produce the **2.40×** shown in v1.0's own scorecard example, and it inverts the meaning of "speedup" |
| **v2.0 states** | **ideal parallel speedup = total work ÷ critical path** (§16, §17.3) |
| **Check** | 14.42 s ÷ 6.01 s = 2.40× — matches v1.0's intended figure exactly |
| **Impact** | Formula only. Every intended number, the scorecard layout and the product claim are unchanged |

## C.2 The determinism guarantee ("byte-identical")

| | |
|---|---|
| **v1.0 stated** | NFR-2: replaying a run produces a **byte-identical** event log |
| **Problem** | Event rows include `wall_ts_ms` and other genuinely volatile fields. Literal byte equality is physically impossible, so the CI assertion could never pass and the guarantee would have to be quietly weakened at implementation time |
| **v2.0 states** | Byte-identical **on the canonical projection** — the ordered sequence of `(sched_step, type, agent_id, span_id, causal_parents, vclock, virtual_ts_us, payload_hash)` — with volatile fields excluded by definition (§10.1) |
| **Impact** | The guarantee is now precisely testable and is enforced by gate G3 (100/100 replays, ≥10 in fresh processes). The strength of the claim is unchanged: everything that determines behaviour is covered; only fields that cannot determine behaviour are excluded |

## C.3 Changes that are *not* corrections

For clarity, the other v1.0 departures recorded in this document are **improvements or refinements**, not errors, and each is argued in place with its trade-off: `causal_parent` → `causal_parents[]` (§9.1), the added `span_id` / `sched_step` / `schema_version` fields (§9.1), the append-only hash chain (§9.7), the FR-11 split into FR-11a/FR-11b (§4.5), the OpenAI-compatible provider shim (§8.5), and the addition of guards G3 and G4 to race detection (§14.6).

---

*End of document. AgentDX PRD v2.0 — supersedes v1.0 Draft for build.*
