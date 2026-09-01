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
# Three known gaps the image cannot paper over, all confirmed by that run:
#
#   1. `agentdx run <fixture>` cannot complete a run. Nothing in `sdk/` ever calls
#      `runtime.scheduler.Scheduler.spawn()` (D-62; confirmed independently this session by
#      `grep -rn '\.spawn(' src/agentdx/`, which returns zero hits against a `spawn` defined
#      at `runtime/scheduler.py:752`). LangGraph's parallel fan-out therefore deadlocks the
#      single-task scheduler loop. The compose command below will exit 5, not 0, and the run
#      list will be EMPTY. Gate G10 asks for a *populated* run list; this image cannot
#      produce one, and no amount of packaging work changes that.
#
#   2. `agentdx ui` serves the API only. `api/app.py` mounts `api_router` and `ws.router`
#      and nothing else — there is no `StaticFiles` mount and no `src/agentdx/api/static/`
#      directory, so PRD §39.4's "static assets copied into src/agentdx/api/static/ and
#      shipped inside the wheel" is not implemented. Stage 1 still builds the frontend and
#      stage 2 still places it where §39.4 says it belongs, so the asset is baked and ready
#      the moment that mount lands — but until it does, `/` returns 404 and the Control
#      Tower is reachable only through the Vite dev server. Adding the mount is an `api/`
#      change (P14), out of P19's DELIVERABLES.
#
#   3. Fixtures are not package data. `[tool.hatch.build.targets.wheel]` packages
#      `src/agentdx` only, and the committed fixture caches are `responses.json` files, not
#      the "compressed SQLite files ... included as package data" §39.4 describes. So a
#      `pip install agentdx` alone cannot run a fixture. This image copies the repository
#      tree for that reason, rather than installing the wheel and hoping.

# ---------------------------------------------------------------------------------------
# Stage 1 — Control Tower (Vite build at image-build time, per PRD §39.2's own rationale)
# ---------------------------------------------------------------------------------------
FROM node:20-bookworm-slim AS frontend

WORKDIR /build/frontend

# Dependencies first, so a source-only edit does not re-resolve the tree. `npm ci` is the
# locked install — the same command `just sync-frontend` runs.
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
