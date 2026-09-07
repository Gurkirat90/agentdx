# AgentDX — multi-stage image (PRD §39.4).
#
# Stage 1 builds the Control Tower with Node; stage 2 is a Python runtime that carries no
# Node at all, so the shipped image has one language runtime rather than two. Non-root by
# construction. PRD §39.4's stated target is a sub-500-megabyte image.
#
# ---------------------------------------------------------------------------------------
# WHAT THIS IMAGE CANNOT DO TODAY — read before trusting it (AGENTS.md §2, §8)
# ---------------------------------------------------------------------------------------
# This file HAS been built and run, on Darwin/arm64, CPython 3.12.2, 2026-08-29 — later the
# same session it was authored. The evidence is `bench/results/docker-cold-start.json` (real
# container names, a real `E-SCHED-003` traceback from the `seed` service, `seed_exit_code: 5`)
# and the `./.agentdx-data/` bind-mount contents the containers wrote through it. An earlier
# revision of this header said the file had never been built; that was true when written and
# went stale about half an hour later, which is why this note now carries a date and an
# environment instead of a bare claim.
#
# What that run does NOT establish: it was taken with `--no-prune` (`cold_cache: false` in the
# result file), so it is a WARM measurement and is not a G10-conformant number. No cold build
# has been timed. The image size against PRD §39.4's sub-500MB target has never been measured.
#
# Two of the three gaps below are now CLOSED (2026-09-07 update). One remains.
#
#   1. CLOSED 2026-09-03 (ADR-019, closing D-62 task #25). `Scheduler.begin_call`'s candidate-β
#      fix means a fanned-out node body no longer deadlocks the single-task scheduler loop.
#      `agentdx run <fixture>` completes for real: ADR-019 validated all three reference
#      fixtures end-to-end on real Python 3.12 hardware, and this update re-confirmed it in
#      this sandbox (Python 3.10.12 + this session's disclosed stdlib-compat shim) against a
#      genuinely fresh, empty data directory — `code_pipeline`, `support_triage` and
#      `research_fanout` each exit 0 with a real verdict, no `(reused...)` line, a real
#      `agentdx.db`/`cache.db` written. The compose `seed` service below should now complete
#      and produce a populated run list, matching what G10 asks for — **not independently
#      re-confirmed inside an actual container**, since this sandbox has no Docker daemon; the
#      code path validated here is identical to what `seed`'s `sh -c` block runs, but a real
#      `docker compose up` on a machine with Docker is still the authoritative G10 check.
#      CORRECTION (2026-09-02, D-81) is still accurate and kept below: an earlier revision of
#      this header said "LangGraph's parallel fan-out therefore deadlocks the single-task
#      scheduler loop" as fact. That was measured and found WRONG.
#      `tests/integration/runtime/test_d62_suspension_contract.py` (P20, commit 5a17eb5): a
#      single-node, strictly sequential LangGraph graph with no fan-out at all deadlocks
#      identically, while two controls (a root that never suspends; a root awaiting an
#      already-resolved Future) both complete. Fan-out is incidental. The boundary actually
#      observed: the scheduler tolerates `await`, but not an `await` whose resolution requires
#      the event loop to run another task -- see `d62-design.md` §§2-3a for the corrected
#      mechanism and D-81 for the ledger record.
#
#   2. CLOSED 2026-09-07. `api/app.py` now mounts `StaticFiles` at `/` (after the API/WS
#      routers, so `/api/*` and `/ws/*` still resolve first), serving exactly the directory
#      this stage already copies the frontend build into
#      (`src/agentdx/api/static/`) — matching PRD §39.4. `/` now serves the Control Tower's
#      `index.html`, and any unmatched path under it falls back to `index.html` too (the
#      client-side router owns those). See `src/agentdx/api/app.py`.
#
#   3. STILL OPEN. Fixtures are not package data. `[tool.hatch.build.targets.wheel]` packages
#      `src/agentdx` only, and the committed fixture caches are `responses.json` files, not
#      the "compressed SQLite files ... included as package data" §39.4 describes. So a
#      `pip install agentdx` alone cannot run a fixture. This image copies the repository
#      tree for that reason, rather than installing the wheel and hoping. Out of scope for
#      this pass — a packaging change to `pyproject.toml`'s wheel target, not a Docker fix.

# ---------------------------------------------------------------------------------------
# Stage 1 — Control Tower (Vite build at image-build time, per PRD §39.2's own rationale)
# ---------------------------------------------------------------------------------------
FROM node:20-bookworm-slim AS frontend

WORKDIR /build/frontend

# Dependencies first, so a source-only edit does not re-resolve the tree. `npm ci` is the
# locked install — the same command `just sync-frontend` runs.
#
# THIS STEP DEPENDS ON `.dockerignore` EXCLUDING `**/node_modules/`. `COPY frontend/ ./`
# below runs *after* this install and would otherwise overwrite it with the host's macOS
# node_modules. That was live until 2026-09-01 and passed only by luck: the host tree
# carries all 23 @esbuild platform variants, linux-arm64 among them, so esbuild still
# resolved a working binary — while this `npm ci`'s entire output was discarded and the
# image shipped every platform's binaries plus all devDependencies. Delete `.dockerignore`
# and that returns silently.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# `frontend/src/api/schema.ts` is generated from this file (`npm run generate:api`) and is
# committed, so the build does not regenerate it — but the path is copied anyway so a future
# build step that does regenerate has it available without a second context change.
COPY docs/openapi.json /build/docs/openapi.json
COPY frontend/ ./

# `npm run build` is `tsc --noEmit && vite build`. The typecheck is deliberately part of the
# image build: an image that ships a frontend which does not typecheck is worse than a build
# that fails loudly here.
RUN npm run build

# ---------------------------------------------------------------------------------------
# Stage 2 — Python runtime
# ---------------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# `requires-python = ">=3.12,<3.13"` (pyproject) and `.python-version` pins 3.12; the base
# image tag is the single place that pin is expressed for the container target.

# curl is here for exactly one reason: PRD §39.2's healthcheck is literally
# `["CMD", "curl", "-f", "http://localhost:8420/api/health"]`. Keeping the PRD's literal
# healthcheck means the image must carry curl; substituting a Python one-liner would have
# been a silent deviation from a spec block that is quoted verbatim in docker-compose.yml.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

# PRD §39.4: non-root user. Created before the copy so ownership is set once.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agentdx

# uv is CONTEXT.md §3's locked installer; the pinned copy comes from the official image
# rather than a curl-to-shell, so the layer is content-addressed and reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/agentdx-venv \
    PATH="/opt/agentdx-venv/bin:${PATH}"

WORKDIR /app

# Dependency layer: lockfile + manifest only, so application edits do not re-resolve.
# `--no-install-project` installs the dependency closure without the project itself.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The application. `fixtures/` and `scenarios/` are copied because they are NOT package data
# (see gap 3 above) and `agentdx run fixtures/code_pipeline` resolves that argument as a
# repository-relative path via `cli._target.find_repo_root`.
COPY src/ ./src/
COPY fixtures/ ./fixtures/
COPY scenarios/ ./scenarios/
COPY agentdx.toml README.md LICENSE ./

RUN uv sync --frozen --no-dev

# PRD §39.4's intended destination for the built frontend. The mount that would serve it
# does not exist yet (gap 2) — this copy is what makes the asset present in the image, not
# a claim that it is reachable over HTTP.
COPY --from=frontend /build/frontend/dist/ ./src/agentdx/api/static/

# `AGENTDX_<SECTION>_<KEY>` is the real environment contract (`config.py`'s ENV_PREFIX and
# `_read_env_section`). PRD §39.2's compose block names `AGENTDX_MODE` and
# `AGENTDX_DATA_DIR`, neither of which matches a section prefix, so both are silently
# ignored by `AgentDXConfig.load()`. The correct names are used here and in
# docker-compose.yml, and the divergence is declared in CONTEXT.md §9.
ENV AGENTDX_RUN_MODE=replay \
    AGENTDX_RUN_DATA_DIR=/data \
    AGENTDX_STORE_DATA_DIR=/data \
    PYTHONHASHSEED=0 \
    PYTHONDONTWRITEBYTECODE=1

# PYTHONHASHSEED=0 is project-wide (AGENTS.md §4.1) and `agentdx doctor` checks it. It has
# to be in the environment before the interpreter starts, which is why it is an ENV here and
# not set anywhere in code.

RUN mkdir -p /data && chown -R agentdx:agentdx /data /app

USER agentdx

EXPOSE 8420

# The healthcheck is the image's own copy of the compose-level one, so `docker run` without
# compose still reports health. Interval and retries match PRD §39.2's block.
HEALTHCHECK --interval=5s --timeout=3s --retries=20 --start-period=10s \
    CMD curl -f http://localhost:8420/api/health || exit 1

# Default to serving only. The three-fixture seeding run lives in docker-compose.yml, so
# `docker run agentdx` gives a server and `docker compose up` gives the demo — one image,
# two honest entry points.
CMD ["agentdx", "ui", "--host", "0.0.0.0"]
