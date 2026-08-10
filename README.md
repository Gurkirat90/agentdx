# AgentDX

**Multi-agent coordination debugger, deterministic replay runtime, and chaos harness.**

> Status: **P01 — repository scaffold, toolchain and CI spine.** There is no product here yet.
> The tree, the gates and the ledger exist first, on purpose. See `CONTEXT.md` §5 for what is built.

## The thesis

Teams reach for multi-agent architectures on the assumption that more agents means more
parallelism, and they have no way to check. AgentDX takes an existing agent graph — LangGraph, or
any Python multi-agent system wrapped with the decorator API — and executes it under a
deterministic cooperative scheduler driven by a virtual clock, serving every model call from a
record/replay cache and optionally injecting controlled faults. Every observable action lands in an
append-only event log, and pure analytical passes over that log answer one question: *is this
multi-agent system actually better than one agent doing the same work — and if not, exactly where
did the time, the tokens and the correctness go?* AgentDX is not an observability dashboard.
Observability watches production and shows you what happened; AgentDX runs the system under
controlled conditions before deployment and tells you *why*, reproducibly, offline, for free.

## Quickstart

*(The commands below are the target shape, from PRD §38.1. They land with the fixtures at P05.)*

```bash
uv sync
uv run agentdx run fixtures/code_pipeline
uv run agentdx ui --open
```

No API keys. No network. The fixture caches are committed and replay mode is the default, so the
demo cannot accidentally call a model or cost money — a cache miss in replay mode is a hard error,
never a silent live call.

## What is honest about this tool

- **Bounded search: absence of findings is not proof of absence.** Schedule exploration is bounded;
  a clean report means no defect was found in the schedules explored, not that none exists.
- **Race precision is held at the maximum and recall is reported as measured.** A tool that reports
  races in correct code is worse than no tool, so precision is never traded for recall.
- **Every finding carries evidence** — concrete event sequence references you can click through to
  the spans that produced it. A finding with an empty evidence array cannot be rendered.
- **Every published statistic carries a `[bench:<file>]` marker** resolving to a committed result in
  `bench/results/`, and CI fails the build if one does not. That is why this README currently
  contains no numbers.
- **Windows is not supported in v1** (PRD §32). macOS on Apple silicon is the primary target; macOS
  Intel and Linux x86_64/arm64 are supported.

## Repository map

The directory tree is the architecture, and it is enforced rather than documented: see PRD §25 for
the tree, `CONTEXT.md` §4 for the layer contract, and `.importlinter` for the machine-readable form
of that contract. In particular `agentdx.analysis.*` may not import `agentdx.runtime.*` or
`agentdx.sdk.*`, and CI fails if it ever does.

## Development

```bash
just sync          # Python environment
just hooks         # local pre-commit hooks
just ci            # everything CI runs: lint, typecheck, test, imports, determinism, ledger, bench markers
```

See `CONTRIBUTING.md` for the working loop and `AGENTS.md` for the standing engineering rules.

## Documentation

| Document | What it is |
|---|---|
| `docs/AgentDX-PRD-v2.md` | The spec of record. Read-only (ADR-000) |
| `CONTEXT.md` | The project ledger: invariants, locked decisions, build state, decision log |
| `AGENTS.md` | Standing rules for every agent and human working in this repository |
| `CONTRIBUTING.md` | The build loop and the checks that gate it |

## License

MIT — see `LICENSE`.
