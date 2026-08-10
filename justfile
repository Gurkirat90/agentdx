# AgentDX task runner — CONTEXT.md §3 locks `just` as the task runner.
# Every recipe here is the *only* sanctioned way to run that check, so CI and a
# developer's laptop cannot drift apart: .github/workflows/ci.yml calls these
# recipes and nothing else.

set shell := ["bash", "-uc"]

# PYTHONHASHSEED=0 is required project-wide (AGENTS.md §4.1) and `agentdx doctor`
# checks it. It must be in the environment before the interpreter starts, so it
# is exported here rather than set in code.
export PYTHONHASHSEED := "0"

_default:
    @just --list

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

# Sync the Python env (PRD §39.1)
sync:
    uv sync --all-groups

# Install the frontend toolchain (PRD §39.1)
sync-frontend:
    cd frontend && npm ci

# Install the local pre-commit hooks (AGENTS.md §10)
hooks:
    uv run pre-commit install

# ---------------------------------------------------------------------------
# The CI spine — `just ci` is exactly what .github/workflows/ci.yml runs
# ---------------------------------------------------------------------------

# Everything CI runs, in CI's order
ci: lint typecheck test check-imports check-determinism check-ledger check-bench

# ruff lint + format check (AGENTS.md §4)
lint:
    uv run ruff check .
    uv run ruff format --check .

# mypy --strict on the package (AGENTS.md §4)
typecheck:
    uv run mypy --strict src/agentdx

# pytest — the P01 exit-5 shim was removed at P02, which is when tests first exist (D-05)
test *ARGS:
    uv run pytest {{ARGS}}

# import-linter — the mechanical form of the CONTEXT.md §4 layer contract (I3, I13)
check-imports:
    uv run lint-imports --config .importlinter

# Determinism hygiene gate (AGENTS.md §4.1, tripwire 2)
check-determinism:
    uv run python scripts/check_determinism_hygiene.py

# Ledger append-only + size + ADR-reference integrity (AGENTS.md §10, tripwire 13)
check-ledger:
    uv run python scripts/check_ledger.py

# Rule E1: every published number carries a resolvable [bench:<file>] marker (I9, tripwire 7)
check-bench:
    uv run python scripts/check_bench_markers.py

# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

# TypeScript + eslint gates (AGENTS.md §4)
lint-frontend:
    cd frontend && npm run typecheck && npm run lint

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

# Apply formatting and autofixes
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# ---------------------------------------------------------------------------
# Demo (PRD §38.1) — the fixtures land at P05; these recipes fail loudly until then
# ---------------------------------------------------------------------------

# Run the three reference fixtures and open the Control Tower
demo:
    uv run agentdx run fixtures/code_pipeline
    uv run agentdx run fixtures/support_triage
    uv run agentdx run fixtures/research_fanout
    uv run agentdx ui --open

# Gate G9 / NFR-4 / I7 — the same demo with the network disabled and no API keys
demo-offline:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "${GROQ_API_KEY:-}${OPENAI_API_KEY:-}${ANTHROPIC_API_KEY:-}" ]; then
      echo "G9: an API key is present in the environment; the offline demo must run without one" >&2
      exit 2
    fi
    env -u GROQ_API_KEY -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
      AGENTDX_MODE=replay uv run agentdx run fixtures/code_pipeline
    env -u GROQ_API_KEY -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
      AGENTDX_MODE=replay uv run agentdx run fixtures/support_triage
    env -u GROQ_API_KEY -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
      AGENTDX_MODE=replay uv run agentdx run fixtures/research_fanout

# Serve the Control Tower (PRD §37.1)
ui:
    uv run agentdx ui

# ---------------------------------------------------------------------------
# Benchmarks (PRD §34) — the only source of published numbers (I9)
# ---------------------------------------------------------------------------

# Run the benchmark suite; results are written to bench/results/
bench SUITE="all":
    uv run agentdx bench --suite {{SUITE}}

# Gate G10 — cold `docker compose up` to a healthy /api/health in < 180 s
bench-docker-cold:
    uv run python bench/harness/docker_cold_start.py
