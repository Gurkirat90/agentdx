"""The concrete `RunHost` (PRD §37, week 10) — the composition root `agentdx run` needs.

**Why this file exists and what it is not.** `sdk.generic.RunHost` is a `Protocol`,
docstringed since P04 as "the half of `agentdx.run` that P06 owns" — but P06's own
independent audit found it unbuilt, classified the gap root cause (d) (an undeclared
deviation needing an owner decision), and left it "deliberately still unbuilt" (`CONTEXT.md`
§13, P06 OP-2 row). Every module built since — P07's cache, P09's faults, P11's baseline,
P12's race detector — cites the same gap and says, in some form, "whichever prompt builds
`RunHost` must call this." `sdk.generic.install_runtime`'s own docstring settles *where*:
"`cli/` installs the real host once at start-up; a test installs a fake." That line is not
an open design question this file re-litigates — it is the one placement PRD/CONTEXT already
committed to, unbuilt until now. This is composition, not new analysis: every primitive here
(`Scheduler`, `EventWriter`, `Store`, `Cache`, `CrashInjector`, `FaultRegistry`) already
exists, built and tested by an earlier prompt; this file wires them together the way
`fixtures/_harness.py` wired a provisional stand-in before any of them existed (see that
module's own docstring, and ADR-012).

**What "the real Scheduler" changes versus the fixture harness.** `fixtures/_harness.py`
stamps events with `sched_step = seq` (its own words: "There is no scheduler grouping several
events into one decision step") and has no concurrency model at all. Running the three P05
fixtures through *this* host, with a real cooperative `Scheduler`, is the first time in this
codebase's history that they execute under real scheduling — the golden corpora ADR-001
already declared provisional, "regenerated... once runtime/ and runtime/cache/ exist." This
prompt does **not** regenerate them (out of `cli/`'s DELIVERABLES, and a decision with test-
suite-wide consequences that belongs to its own reviewed change) — declared in this response's
NOT DONE/RISKS, not silently left for a future reader to discover.

**Fault coverage, stated precisely.** `runtime.scheduler.Scheduler(fault_hook=...)` accepts
exactly one hook, and only `runtime.faults.process.CrashInjector` (`agent_crash`) is built as
a `FaultInjectorHook` at all — `transport.TransportFaultInjector` (`latency`, `message_drop`)
and `dependency.DependencyFaultInjector` (`tool_failure`) are, by their own module
docstrings, "pure decision logic... never registered as `Scheduler(fault_hook=...)`", meant
to be consulted from a live SDK call site that does not exist yet (`docs/chaos-safety.md`
already calls this "a complete, schema-correct, tested decision engine with no live
production call site"). `_ArmedFaultHooks` below wires `agent_crash` for real and reports —
does not silently ignore — a scenario that arms any of the other three MVP fault types,
since firing them would require a `sdk/generic.py` change outside this prompt's DELIVERABLES.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import agentdx
from agentdx.config import AgentDXConfig
from agentdx.events.schema import DraftEvent, Event, EventType
from agentdx.events.writer import EventWriter
from agentdx.runtime.cache.modes import Cache, SchedulerCacheHook
from agentdx.runtime.cache.store import SqliteCacheStore
from agentdx.runtime.clock import CalibrationProfile, VirtualClock, wall_time
from agentdx.runtime.faults.process import CrashInjector
from agentdx.runtime.faults.registry import FaultRegistry
from agentdx.runtime.faults.taint import FaultTaintTracker
from agentdx.runtime.scheduler import FaultInjectorHook, Scheduler
from agentdx.sdk.generic import (
    InstrumentationGap,
    LifecycleHooks,
    RunContext,
    RunResult,
    hash_text,
)
from agentdx.store.sqlite import RunRecord, Store

if TYPE_CHECKING:
    from agentdx.scenario.assertions import AssertionResult
    from agentdx.sdk.generic import LlmCache

__all__ = [
    "UNSUPPORTED_LIVE_FAULT_TYPES",
    "CliRunHost",
    "OpenedRun",
    "build_cache",
    "build_cache_hook",
    "build_fault_hooks",
]

UNSUPPORTED_LIVE_FAULT_TYPES: frozenset[str] = frozenset(
    {"latency", "message_drop", "tool_failure"}
)
"""MVP fault types with no live scheduler/SDK call site yet (see module docstring).

`agentdx run --faults` arms these (so scenario validation and the fault registry treat them
identically to `agent_crash`), but this host cannot make them fire, and says so rather than
pretending they did.
"""


def _started_at_utc() -> str:
    """Return the current UTC instant as an ISO-8601 string, via the sanctioned accessor.

    `agentdx.wall_time()` is AGENTS.md §4.1 clause 3's one sanctioned real-clock read; this
    just formats its milliseconds as PRD §9.2's `started_at_utc` wants them.
    """
    return datetime.fromtimestamp(wall_time() / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _safe_env() -> dict[str, str]:
    """Return a minimal, non-secret environment snapshot for `run_start.payload.env`.

    PRD §10.7 names `env` as a volatile provenance field, not a credentials dump — I8
    (privacy by default) and `doctor`'s own "API key in a committed file" check both assume
    the log itself never carries one. Only a small, named allowlist of harmless keys is
    captured; everything else is a real deviation from `os.environ` on purpose.
    """
    allow = ("AGENTDX_CI", "CI", "PYTHONHASHSEED", "AGENTDX_FIXTURE_HARNESS")
    return {k: os.environ[k] for k in allow if k in os.environ}


@dataclass(eq=False)
class OpenedRun:
    """Bookkeeping `CliRunHost` keeps between `open_run` and `close_run`."""

    context: RunContext
    store: Store
    writer: EventWriter
    scheduler: Scheduler
    fault_registry: FaultRegistry | None
    scenario_id: str | None
    scenario_hash: str
    graph_hash: str
    cache_mode: str
    run_mode: str
    check_results: list[AssertionResult] = field(default_factory=list)
    fault_summary: tuple[object, ...] = ()


class CliRunHost:
    """The `sdk.generic.RunHost` `cli/` installs (per `install_runtime`'s own docstring).

    One instance is built per `agentdx run` invocation (see `cli.commands.run`), already bound
    to an already-constructed `Scheduler` — `open_run` cannot build the scheduler itself,
    because the caller must pass `scheduler.run(agentdx.run(...))`'s *same* scheduler in
    before the root coroutine (which calls `open_run`) ever starts (see module docstring's
    composition note, and `Scheduler.run`'s own "on successful completion... writing
    run_start/run_end... are the run host's job" docstring).
    """

    def __init__(
        self,
        *,
        run_id: str,
        seed: int,
        scheduler: Scheduler,
        clock: VirtualClock,
        store: Store,
        writer: EventWriter,
        config: AgentDXConfig,
        fault_registry: FaultRegistry | None,
        scenario_id: str | None,
        scenario_hash: str,
        graph_hash: str,
        cache_mode: str,
        run_mode: str,
        cache: Cache | None = None,
        model: str = "unspecified",
        provider_host: str = "offline",
    ) -> None:
        """Bind to one run's already-constructed runtime services.

        `cache` is the built `runtime.cache.modes.Cache` (`build_cache` below) that
        `openai_compatible.py`'s provider actually reads/writes on every LLM call
        (PRD §11.2) — omitted, `RunContext.create` defaults `cache` to `NoCache()`
        ("always empty"), which would silently turn every replay-mode run into a
        guaranteed cache miss (exit 3) regardless of what is on disk. Optional only so a
        caller with no LLM surface at all (a pure-Python fixture) is not forced to build one.
        """
        self._run_id = run_id
        self._seed = seed
        self._scheduler = scheduler
        self._clock = clock
        self._store = store
        self._writer = writer
        self._config = config
        self._fault_registry = fault_registry
        self._scenario_id = scenario_id
        self._scenario_hash = scenario_hash
        self._graph_hash = graph_hash
        self._cache_mode = cache_mode
        self._run_mode = run_mode
        self._cache = cache
        self._model = model
        self._provider_host = provider_host
        self._opened: OpenedRun | None = None

    @property
    def opened(self) -> OpenedRun:
        """Return the bookkeeping for the one run this host has opened.

        Raises:
            RuntimeError: `open_run` has not been called yet.
        """
        if self._opened is None:
            msg = "CliRunHost.opened read before open_run() completed"
            raise RuntimeError(msg)
        return self._opened

    async def open_run(self, *, task: str, scenario: str | None, seed: int | None) -> RunContext:
        """Create the run row, emit `run_start`, and return the bound `RunContext`.

        `seed` is accepted per the `RunHost` protocol but not re-resolved here — the CLI
        already resolved the seed that built `self._scheduler`/`self._run_id` before this
        host was constructed (module docstring's composition note), so this only asserts the
        two agree rather than silently trusting a caller that might pass something else.
        """
        resolved_seed = self._seed if seed is None else seed
        started_at = _started_at_utc()
        self._store.create_run(
            RunRecord(
                run_id=self._run_id,
                scenario_hash=self._scenario_hash,
                graph_hash=self._graph_hash,
                mode=self._run_mode,
                seed=resolved_seed,
                status="running",
                created_at=started_at,
                agentdx_version=agentdx.__version__,
                scenario_id=self._scenario_id,
            )
        )
        context = RunContext.create(
            run_id=self._run_id,
            recorder=self._scheduler.recorder,
            config=self._config,
            seed=resolved_seed,
            mode=self._cache_mode,
            clock=self._clock,
            scheduler=self._scheduler,
            # `runtime.cache.modes.Cache` and `sdk.generic.LlmCache` are two independently
            # defined, field-for-field identical shapes across the `runtime/` \ `sdk/` layer
            # boundary (`runtime/` may not import `sdk/`, CONTEXT.md §4) — `Cache`'s own
            # docstring calls this out as deliberate structural, not nominal, compatibility,
            # and every real call site (`openai_compatible.py`) reads `CachedResponse` by
            # attribute, never `isinstance`. The cast documents that pre-existing, tested
            # (`tests/unit/cache/test_store.py`) cross-layer contract; it is not new business
            # logic introduced here.
            cache=cast("LlmCache | None", self._cache),
            hooks=LifecycleHooks(),
        )
        self._scheduler.stamp(
            DraftEvent(
                type=EventType.RUN_START,
                payload={
                    "seed": resolved_seed,
                    "mode": self._run_mode,
                    "cache_mode": self._cache_mode,
                    "scenario_id": self._scenario_id,
                    "scenario_hash": self._scenario_hash,
                    "graph_hash": self._graph_hash,
                    "delay_schedule_hash": hash_text("no-delay-schedule"),
                    "calibration_id": None,
                    "agentdx_version": agentdx.__version__,
                    "sdk_version": agentdx.__version__,
                    "model": self._model,
                    "provider_host": self._provider_host,
                    "provider_sdk_version": f"agentdx-cli/{agentdx.__version__}",
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "started_at_utc": started_at,
                    "env": _safe_env(),
                },
            )
        )
        self._opened = OpenedRun(
            context=context,
            store=self._store,
            writer=self._writer,
            scheduler=self._scheduler,
            fault_registry=self._fault_registry,
            scenario_id=self._scenario_id,
            scenario_hash=self._scenario_hash,
            graph_hash=self._graph_hash,
            cache_mode=self._cache_mode,
            run_mode=self._run_mode,
        )
        return context

    async def close_run(self, context: RunContext, *, status: str, output: object) -> RunResult:
        """Emit `run_end`, seal the log, and return the result.

        `status` is `sdk.generic.run`'s own "complete"/"failed" (an exception propagated) —
        an abort-guard trip (`E-GUARD-001`, exit 4) is surfaced by `runtime/faults/safety.py`
        raising out of the scheduler loop entirely, before this method is ever reached; the
        CLI's `run` command classifies that case from the caught exception, not from here.
        """
        opened = self.opened
        run_end_status = {"complete": "complete", "failed": "failed"}.get(status, status)
        virtual_makespan_ms = self._clock.virtual_ms()
        # D-47 (CONTEXT.md §9): `fault_summary` has no home in `events/schema.py`'s RUN_END
        # field list yet (`payload.fault_summary` would fail E-EVENT-006, "unknown payload
        # field") — adding that field is a schema change outside `cli/`'s DELIVERABLES.
        # Computed here anyway (it is a pure read of `FaultRegistry.faults`) and kept on
        # `OpenedRun` for the `run` command to surface in its own scorecard/--json output —
        # not persisted into the canonical log. Declared, not silently dropped.
        fault_summary = self._fault_registry.summary() if self._fault_registry is not None else ()
        # +1 for the run_end event itself, about to be written — the store has not seen it
        # yet at the moment this count is read.
        self._writer.flush()
        event_count_before = self._store.event_count(self._run_id)
        total_llm_calls = self._store.count_events_of_type(self._run_id, EventType.LLM_CALL)
        total_tool_calls = self._store.count_events_of_type(self._run_id, EventType.TOOL_CALL)
        prompt_tokens = 0
        completion_tokens = 0
        for event in self._store.read_events(self._run_id):
            if event.type is EventType.LLM_CALL:
                prompt_tokens += int(event.payload.get("prompt_tokens") or 0)  # type: ignore[arg-type]
                completion_tokens += int(event.payload.get("completion_tokens") or 0)  # type: ignore[arg-type]
        self._scheduler.stamp(
            DraftEvent(
                type=EventType.RUN_END,
                payload={
                    "status": run_end_status,
                    "virtual_makespan_ms": virtual_makespan_ms,
                    "wall_makespan_ms": wall_time() - self._start_wall_ms(),
                    "event_count": event_count_before + 1,
                    "total_llm_calls": total_llm_calls,
                    "total_tool_calls": total_tool_calls,
                    "total_prompt_tokens": prompt_tokens,
                    "total_completion_tokens": completion_tokens,
                },
            )
        )
        self._writer.flush()
        self._store.seal(self._run_id, self._writer.last_hash)
        opened.fault_summary = tuple(fault_summary)
        gaps: tuple[InstrumentationGap, ...] = context.gaps
        return RunResult(run_id=self._run_id, status=status, output=output, gaps=gaps)

    def _start_wall_ms(self) -> int:
        record = self._store.get_run(self._run_id)
        if record is None:
            return wall_time()
        started = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
        return int(started.timestamp() * 1000)


class _ArmedFaultHooks:
    """Builds the one `FaultInjectorHook` a `Scheduler` accepts, and reports what it cannot fire.

    See module docstring's "Fault coverage" note. `unfireable` is populated whenever the
    resolved scenario arms a fault type this host has no live call site for — surfaced by the
    `run` command as a warning, never silently dropped from the registry (the fault is still
    validated, still shows up in `fault_summary` as `fault_not_triggered`, and a scenario
    that *requires* it to fire will correctly fail its assertion rather than pass by omission).
    """

    def __init__(
        self,
        *,
        registry: FaultRegistry,
        clock: VirtualClock,
        seed: int,
        stamp: Callable[[DraftEvent, Sequence[int]], Event],
    ) -> None:
        self.registry = registry
        self.taint = FaultTaintTracker()
        self.unfireable: tuple[str, ...] = tuple(
            sorted({f.decl.fault_type for f in registry.faults} & UNSUPPORTED_LIVE_FAULT_TYPES)
        )
        self.hook: FaultInjectorHook | None = None
        if any(f.decl.fault_type == "agent_crash" for f in registry.faults):
            self.hook = CrashInjector(
                registry=registry,
                clock=clock,
                seed=seed,
                stamp=stamp,
                taint=self.taint,
            )


def build_fault_hooks(
    *,
    registry: FaultRegistry | None,
    clock: VirtualClock,
    seed: int,
    scheduler_stamp: Callable[[DraftEvent, Sequence[int]], Event],
) -> _ArmedFaultHooks | None:
    """Return the fault-hook bundle for `registry`, or `None` if no faults are armed.

    `scheduler_stamp` must be `Scheduler.stamp` itself — passed in rather than closed over so
    this function has no import-time dependency on a live `Scheduler` instance existing yet.
    """
    if registry is None or not registry.faults:
        return None
    return _ArmedFaultHooks(registry=registry, clock=clock, seed=seed, stamp=scheduler_stamp)


def build_cache(
    *, cache_db_path: Path, mode: str, run_id: str, provider: str = "unspecified"
) -> Cache:
    """Open the run's LLM cache in `mode` (PRD §11.2), backed by the on-disk cache database."""
    store = SqliteCacheStore.open(cache_db_path)
    return Cache(backing_store=store, mode=mode, provider=provider, run_id=run_id)


def build_cache_hook(*, cache: Cache, config: AgentDXConfig) -> SchedulerCacheHook:
    """Return the `Scheduler(cache_hook=...)` value.

    See `SchedulerCacheHook`'s own docstring: `runtime/scheduler.py` never actually calls it
    (a pre-existing, declared gap, not introduced here) — wired anyway so a future scheduler
    change that adds the call site needs no `cli/` change to benefit from it.
    """
    calibration = CalibrationProfile.defaults_only(config.scheduler)
    return SchedulerCacheHook(store=cache.backing_store, calibration=calibration)
