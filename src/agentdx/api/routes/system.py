"""`GET /api/health` · `GET /api/version` · `GET /api/metrics` (PRD §26.1, §35).

Thin by construction: `health` and `version` return static/near-static facts; `metrics`
renders Prometheus text format from numbers this process can compute for real right now.
PRD §35's table names thirteen metrics that describe a *run in progress* — scheduler step
duration, cache hit ratio, per-analyser duration — none of which this build can source
honestly, because nothing in this codebase yet writes them anywhere the API can read
(`runtime/` has no metrics registry, and the API never imports `runtime` regardless —
`.importlinter` `api-never-imports-runtime`). Rather than fabricate a `0` for those (AGENTS.
md §2: "no placeholder implementations... mocked returns"), this endpoint emits Prometheus
`# HELP`/`# TYPE` metadata for the full PRD §35 metric set — so a scraper's config validates
against the real names and units — but a `# HELP`/`# TYPE` pair with no sample line is valid
Prometheus text format for "this metric exists but has nothing to report yet"; only the
metrics this process can compute from the store file and its own process state carry a
sample. See `docs/api.md` "What GET /api/metrics does not yet do".
"""

from __future__ import annotations

import os
import resource
from importlib.metadata import version as _installed_version
from typing import Annotated, Final

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from agentdx.api.deps import ApiState, get_state
from agentdx.api.models import HealthResponse, VersionResponse
from agentdx.events.schema import SCHEMA_VERSION

router = APIRouter(tags=["system"])

# `from agentdx import __version__` would import agentdx/__init__.py, which unconditionally
# pulls in `agentdx.runtime.clock`/`agentdx.runtime.determinism` and `agentdx.sdk.*` at module
# level — `lint-imports` confirmed this transitively breaks `api-never-imports-runtime` even
# though `api/` never names `runtime`/`sdk` itself. `importlib.metadata` reads the *installed
# package's* metadata (from `pyproject.toml`'s `[project] version`) without executing the
# package's own `__init__.py`, which is exactly the layer-safe accessor this needs — the same
# "pass it in / read it from outside, never source it from inside the constrained layer"
# pattern `store/bundle.py`'s `RunRecord.agentdx_version` already relies on.
_VERSION: Final[str] = _installed_version("agentdx")

# PRD §35's table, verbatim: (name, type, help). Every one gets HELP/TYPE lines; only the
# ones `_computed_samples` can source for real also get a sample (see module docstring).
_METRIC_DECLARATIONS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "agentdx_scheduler_step_duration_us",
        "histogram",
        "Scheduler overhead per decision (PRD Sec35, p99 alert threshold 500us)",
    ),
    ("agentdx_scheduler_steps_total", "counter", "Schedule length (PRD Sec35)"),
    (
        "agentdx_event_ingest_rate",
        "gauge",
        "Events per second written (PRD Sec35, alert threshold under 5000/s)",
    ),
    (
        "agentdx_event_queue_depth",
        "gauge",
        "Writer backpressure (PRD Sec35, alert threshold over 8000)",
    ),
    (
        "agentdx_event_store_bytes",
        "gauge",
        "Event store size in bytes, per run and total (PRD Sec35, alert threshold run over 200MB)",
    ),
    (
        "agentdx_cache_hit_ratio",
        "gauge",
        "Replay health (PRD Sec35; under 1.0 in replay mode is an error, not a warning)",
    ),
    ("agentdx_replay_duration_ms", "histogram", "Replay cost (PRD Sec35)"),
    (
        "agentdx_analysis_duration_ms",
        "histogram",
        "Per-analyser cost, labelled by analyser (PRD Sec35, threshold 2s at 5000 events)",
    ),
    ("agentdx_exploration_schedules_total", "counter", "Exploration cost (PRD Sec35)"),
    (
        "agentdx_exploration_reduced_total",
        "counter",
        "Reduction effectiveness (PRD Sec35, alert threshold ratio under 0.2)",
    ),
    (
        "agentdx_memory_rss_mb",
        "gauge",
        "Resident set size in MB (PRD Sec35, alert threshold over 500MB)",
    ),
    (
        "agentdx_ui_frame_time_ms",
        "histogram",
        "Client-reported UI frame time, opt-in (PRD Sec35, p95 alert threshold 16.7ms)",
    ),
    (
        "agentdx_nondeterminism_warnings_total",
        "counter",
        "Determinism health (PRD Sec35; any value over 0 is investigated)",
    ),
)


def _rss_mb() -> float:
    """Return this process's resident set size in MB.

    `ru_maxrss` is kibibytes on Linux, bytes on macOS/BSD — `os.uname().sysname` picks the
    right divisor, which is the same platform split PRD NFR-12's macOS/Linux support matrix
    already requires this codebase to reason about elsewhere.
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 if os.uname().sysname == "Linux" else 1024.0 * 1024.0
    return raw / divisor


def _computed_samples(state: ApiState) -> dict[str, float]:
    """Return the subset of `_METRIC_DECLARATIONS` this process can source for real."""
    samples: dict[str, float] = {"agentdx_memory_rss_mb": _rss_mb()}
    if state.store_path.is_file():
        samples["agentdx_event_store_bytes"] = float(state.store_path.stat().st_size)
    return samples


def render_prometheus_text(state: ApiState) -> str:
    """Render the PRD Sec35 metric set as Prometheus text format (PRD Sec26.1)."""
    samples = _computed_samples(state)
    lines: list[str] = []
    for name, kind, help_text in _METRIC_DECLARATIONS:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        if name in samples:
            lines.append(f"{name} {samples[name]!r}")
    return "\n".join(lines) + "\n"


@router.get("/health", response_model=HealthResponse, summary="Liveness probe (PRD Sec35)")
def get_health() -> HealthResponse:
    """Return `{"status": "ok"}` if the process can answer at all."""
    return HealthResponse()


@router.get(
    "/version",
    response_model=VersionResponse,
    summary="AgentDX version and event schema version (PRD Sec35)",
)
def get_version() -> VersionResponse:
    """Return the AgentDX package version and the event `schema_version` it writes."""
    return VersionResponse(version=_VERSION, schema_version=SCHEMA_VERSION)


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Self-observability counters, Prometheus text format (PRD Sec35)",
)
def get_metrics(state: Annotated[ApiState, Depends(get_state)]) -> PlainTextResponse:
    """Return the PRD Sec35 metric set as `text/plain; version=0.0.4` (Prometheus exposition)."""
    return PlainTextResponse(render_prometheus_text(state), media_type="text/plain; version=0.0.4")
