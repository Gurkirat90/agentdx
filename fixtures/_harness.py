"""fixtures/_harness.py — a PROVISIONAL, fixture-local `RunHost` + `Recorder`.

**This module is not part of `src/agentdx/`.** It exists only so the three P05 reference
fixtures can execute end to end, offline, before `runtime/` (P06, the scheduler) and
`runtime/cache/` (P07, the record/replay cache) exist. Both are `NOT STARTED` per
`CONTEXT.md` §5 at the time this module was written, and building either here would be a
scope violation of `AGENTS.md` §2 — this harness does only the minimum PRD §9.6 "stamping"
work needed to turn the SDK's `DraftEvent`s into a writable, validating `Event` log, using a
handful of the simplest rules that satisfy `events/validators.py`.

**Why this is legitimate rather than a guess.** `ADR-001` (`CONTEXT.md` §8) already rules
that the three fixtures are built in week 1, *before* the scheduler or cache exist, and that
their golden corpora are explicitly **provisional** — regenerated at P07 once both are real.
`docs/sdk.md` §1 documents exactly this seam: `Recorder`, `Clock`, `LlmCache` and `RunHost`
are `typing.Protocol`s precisely so the SDK can be exercised with no runtime in existence.
The precedent for "compute what a real component would compute, by script, rather than
hand-type it" is `tests/golden/build_event_log_40.py` (P02): events hand-specified, vector
clocks and fault taint *derived*. This module is the same idea one level up: fixtures
hand-specify agent behaviour (`graph.py`), and this module derives the stamping.

**What is honestly synthetic here, stated once rather than scattered as comments:**

* `sched_step` is set equal to `seq`. There is no scheduler grouping several events into one
  decision step, so giving every event its own step is honest rather than invented structure.
* `virtual_ts_ms` is a global counter incremented by 1 per event. It is monotonic (satisfies
  `E-EVENT-021`) and gives every event a distinct instant, but it does **not** model a real
  duration — there is no calibration profile (`CONTEXT.md` Q-43.2.3, still OPEN) for this
  harness to draw on. Anything computed from it (a critical-path share, a speedup ratio) is
  structural, not a measurement, until P06/P07 regenerate this corpus.
* `wall_ts_ms` is real elapsed wall-clock time since the run opened. This field is
  `Volatility.VOLATILE` — excluded from the canonical projection (PRD §10.7) — so reading a
  real clock here does not touch invariant I1. **Honest note on which exception this is (OP-3
  correction, 2026-08-13):** an earlier revision of this docstring claimed footing under
  `AGENTS.md` §4.1 clause 4 ("code executing outside a run context... a run is never in
  progress there"). That is not actually true of this module — `open_run`/`close_run` is
  this file's entire job, so a run is in progress for essentially all of it, which is the
  opposite of clause 4's condition. The real reason `agentdx doctor`'s lint rule does not flag
  this file is simpler and less flattering: the rule only scans `src/agentdx/`, and this
  module is deliberately outside that tree (see the top of this docstring). If this file's
  wall-clock read needed a substantive exception rather than a scanning boundary, the nearest
  analogy is clause 3 (the volatile-field writer, `agentdx.wall_time()`, PRD §9.2/§10.7) — this
  harness reads the same real clock for the same volatile, canonical-projection-excluded
  purpose, just from outside the tree that clause was written to govern. That is a judgment
  call, not a clean match to any of the four, and is recorded as one rather than asserted as
  settled.
* The vector clock is the textbook construction: on every event, start from the emitting
  slot's last known snapshot, merge in (elementwise max) every causal parent's own vclock,
  then increment the slot by one. This satisfies `E-EVENT-027` (a slot's own counter never
  regresses between two adjacent events that share it) and `E-EVENT-041` (no causal parent's
  vclock ever exceeds its child's, in any slot) by construction, not by testing after the
  fact — both are exercised in `tests/golden/test_fixtures_replay.py`.
* `causal_parents` is exactly what the SDK's own `emit(..., causes=...)` call sites already
  compute (a span's own `span_start`, a `message_recv`'s `message_send`, a tool call's own
  span). This harness adds no causal edges of its own.

Regenerating a golden log built through this module is the *standing exception* AGENTS.md §5
already carves out for the week-1 fixture corpora (ADR-001 consequence 2), not a violation of
"regenerate only on an explicit instruction."
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import agentdx
from agentdx.config import StoreConfig
from agentdx.events.canonical import encode_event
from agentdx.events.schema import (
    DraftEvent,
    Event,
    EventType,
    Stamp,
    VClock,
)
from agentdx.events.writer import EventWriter
from agentdx.sdk.generic import (
    InstrumentationGap,
    LifecycleHooks,
    RunContext,
    RunResult,
)
from agentdx.sdk.generic import emit as sdk_emit
from agentdx.store.sqlite import RunRecord, Store

FIXTURES_ROOT = Path(__file__).parent
RUN_MODE = "baseline"
"""`run_start.payload.mode` — PRD §6.1's run-mode enum (baseline/chaos/replay/explore),
**distinct** from the LLM cache mode (record/replay/perturb/passthrough; ruling C-9,
`docs/sdk.md` §10). None of the three P05 fixtures inject a fault or explore a schedule, so
every fixture run is `baseline`."""

CACHE_MODE = "replay"
"""Every committed fixture run is served from its own committed response pool — see
`load_responses` below — so the demo satisfies I7 (offline, no API keys) unconditionally."""


def deterministic_run_id(fixture_name: str, seed: int) -> str:
    """Return a `r_` + 5-hex run id, deterministic in `(fixture_name, seed)`.

    Mirrors the shape PRD §6.1 / `events/schema.py` document for `run_id` (`r_` + 5 hex of a
    content hash) without using `uuid4`, which `AGENTS.md` §4.1 bans outright. Two runs of the
    same fixture at the same seed always get the same id, which is what makes gate-style
    reproduction scenarios ("10/10") meaningful for a harness with no real scheduler.
    """
    material = f"{fixture_name}:{seed}".encode()
    return "r_" + hashlib.blake2b(material, digest_size=8).hexdigest()[:5]


def content_hash(*parts: str) -> str:
    """Return a `blake2b:` hash of the given strings, in the event log's hash format.

    Used as a stand-in for `scenario_hash` / `graph_hash` (PRD §9.5), which are real
    provenance hashes owned by `runtime/` (P06) and `scenario/` (P08) — neither exists yet.
    Provisional: recomputed, not compared, until those modules land.
    """
    h = hashlib.blake2b(digest_size=32)
    for part in parts:
        h.update(part.encode())
        h.update(b"\x00")
    return "blake2b:" + h.hexdigest()


# ---------------------------------------------------------------------------------------
# The vector clock (PRD §14.2's rules, applied rather than reinvented)
# ---------------------------------------------------------------------------------------


class VClockBuilder:
    """Builds a standard vector clock, one event at a time, in emission order.

    Guarantees: for any event E with causal parents P1..Pn, `E.vclock[s] >= Pi.vclock[s]`
    for every slot `s` and every parent `Pi` (this is what `E-EVENT-041` checks), and two
    adjacent events sharing a slot never regress that slot's counter (`E-EVENT-027`) — both
    are immediate consequences of "merge every parent, then increment," never checked after
    the fact.
    """

    def __init__(self) -> None:
        """Start with no slot ever having been observed."""
        self._last: dict[str, dict[str, int]] = {}

    def build(self, slot: str, causal_events: Sequence[Event]) -> VClock:
        """Return the next vclock for `slot`, having observed `causal_events` as parents."""
        base = dict(self._last.get(slot, {}))
        for parent in causal_events:
            for key, count in parent.vclock.items():
                if count > base.get(key, 0):
                    base[key] = count
        base[slot] = base.get(slot, 0) + 1
        self._last[slot] = dict(base)
        return {k: v for k, v in base.items() if v > 0}


# ---------------------------------------------------------------------------------------
# The stamping boundary — implements sdk.generic.Recorder structurally
# ---------------------------------------------------------------------------------------


@dataclass(eq=False)
class FixtureRecorder:
    """The `Recorder` this harness supplies: stamps, then hands the event to `EventWriter`.

    One instance per run. Structurally satisfies `agentdx.sdk.generic.Recorder` (a
    `runtime_checkable` Protocol with one method, `emit`); there is no base class to inherit.
    """

    run_id: str
    writer: EventWriter
    _next_seq: int = field(default=0, init=False)
    _virtual_ms: int = field(default=0, init=False)
    _clocks: VClockBuilder = field(default_factory=VClockBuilder, init=False)
    _by_seq: dict[int, Event] = field(default_factory=dict, init=False)
    _t0: float = field(default_factory=time.monotonic, init=False)
    llm_call_count: int = field(default=0, init=False)
    tool_call_count: int = field(default=0, init=False)

    @property
    def event_count(self) -> int:
        """Return how many events this recorder has stamped so far."""
        return self._next_seq

    def emit(self, draft: DraftEvent, causes: Sequence[int]) -> int:
        """Stamp `draft`, validate and buffer it through `EventWriter`, return its `seq`."""
        seq = self._next_seq
        self._next_seq += 1
        self._virtual_ms += 1

        slot = draft.clock_slot or draft.agent_id or "_run"
        causal_events = [self._by_seq[c] for c in causes]
        vclock = self._clocks.build(slot, causal_events)

        stamp = Stamp(
            seq=seq,
            sched_step=seq,
            virtual_ts_ms=self._virtual_ms,
            wall_ts_ms=int((time.monotonic() - self._t0) * 1000),
            vclock=vclock,
            causal_parents=tuple(sorted(set(causes))),
            fault_id=None,
        )
        event = Event.from_draft(draft, stamp, self.run_id)
        self.writer.write(event)
        self._by_seq[seq] = event

        if draft.type is EventType.LLM_CALL:
            self.llm_call_count += 1
        elif draft.type is EventType.TOOL_CALL:
            self.tool_call_count += 1
        return seq


# ---------------------------------------------------------------------------------------
# The run host — implements sdk.generic.RunHost structurally
# ---------------------------------------------------------------------------------------


@dataclass(eq=False)
class _OpenRun:
    """Bookkeeping for one run between `open_run` and `close_run`."""

    context: RunContext
    recorder: FixtureRecorder
    store: Store
    db_path: Path


class FixtureRunHost:
    """A provisional `RunHost`: one SQLite-backed store per run, sealed on `close_run`.

    Guarantees: every run is a real, validating `EventWriter` -> `Store` pipeline (P02 + P03,
    both `BUILT`) — nothing about persistence, validation or the hash chain is faked. Only
    the *stamping* (this module's `FixtureRecorder`) is provisional.
    """

    def __init__(
        self, fixture_name: str, *, work_dir: Path, checks_module: object | None = None
    ) -> None:
        """Bind this host to one fixture, writing scratch databases under `work_dir`.

        Args:
            fixture_name: Used to derive the deterministic run id and content hashes.
            work_dir: Scratch directory for the per-run SQLite file.
            checks_module: A fixture's `checks.py`, exposing `CHECKS`. When given, every
                check runs inside `close_run` — after the graph has produced its final state
                but *before* `run_end` is emitted — so every `assertion_result` lands in the
                same sealed log it is evidence for (PRD §21.6; see README "Why before
                run_end, not after").
        """
        self.fixture_name = fixture_name
        self.work_dir = work_dir
        self.checks_module = checks_module
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._open: dict[str, _OpenRun] = {}
        self.last_check_results: list[tuple[str, bool, str | None]] = []

    async def open_run(self, *, task: str, scenario: str | None, seed: int | None) -> RunContext:
        """Create the run row, open a store, and emit `run_start`."""
        resolved_seed = 42 if seed is None else seed
        run_id = deterministic_run_id(self.fixture_name, resolved_seed)
        db_path = self.work_dir / f"{run_id}.db"
        if db_path.exists():
            db_path.unlink()
        store = Store.open(db_path, config=StoreConfig())

        created_at = "1970-01-01T00:00:00Z"  # provisional: runtime/clock.py owns this (P06)
        store.create_run(
            RunRecord(
                run_id=run_id,
                scenario_hash=content_hash(self.fixture_name, scenario or "none"),
                graph_hash=content_hash(self.fixture_name, "graph"),
                mode=CACHE_MODE,
                seed=resolved_seed,
                status="running",
                created_at=created_at,
                agentdx_version=agentdx.__version__,
            )
        )
        writer = EventWriter(run_id, sink=store)
        recorder = FixtureRecorder(run_id=run_id, writer=writer)
        context = RunContext.create(
            run_id=run_id,
            recorder=recorder,
            seed=resolved_seed,
            mode=CACHE_MODE,
            hooks=LifecycleHooks(),
        )
        self._open[run_id] = _OpenRun(
            context=context, recorder=recorder, store=store, db_path=db_path
        )

        sdk_emit(
            context,
            EventType.RUN_START,
            {
                "seed": resolved_seed,
                "mode": RUN_MODE,
                "cache_mode": CACHE_MODE,
                "scenario_id": scenario,
                "scenario_hash": content_hash(self.fixture_name, scenario or "none"),
                "graph_hash": content_hash(self.fixture_name, "graph"),
                "delay_schedule_hash": content_hash("no-scheduler-yet"),
                "calibration_id": None,
                "agentdx_version": agentdx.__version__,
                "sdk_version": agentdx.__version__,
                "model": "fixture-local-deterministic",
                "provider_host": "offline",
                "provider_sdk_version": "fixtures/_harness.py",
                "host": "fixture-harness",
                "pid": 0,
                "started_at_utc": created_at,
                "env": {"AGENTDX_FIXTURE_HARNESS": "1"},
            },
        )
        return context

    async def close_run(self, context: RunContext, *, status: str, output: object) -> RunResult:
        """Emit `run_end`, seal the store, export the canonical JSONL log, return the result."""
        opened = self._open[context.run_id]
        recorder = opened.recorder

        final_state: dict[str, object] = {}
        if isinstance(output, Mapping):
            final_state.update(output)
        final_state.update(context.registry.values)  # explicit agentdx.state() wins (§8.2 item 3)

        self.last_check_results = []
        if self.checks_module is not None:
            summary = RunSummary(
                run_id=context.run_id,
                seed=context.seed,
                event_count=recorder.event_count,
                llm_call_count=recorder.llm_call_count,
                tool_call_count=recorder.tool_call_count,
            )
            self.last_check_results = run_checks(self.checks_module, final_state, summary)
            for assertion_id, passed, detail in self.last_check_results:
                emit_assertion_result(context, assertion_id, passed=passed, detail=detail)

        run_end_status = {"complete": "complete", "failed": "failed"}.get(status, "aborted")
        sdk_emit(
            context,
            EventType.RUN_END,
            {
                "status": run_end_status,
                "virtual_makespan_ms": recorder._virtual_ms,
                "wall_makespan_ms": int((time.monotonic() - recorder._t0) * 1000),
                "event_count": recorder.event_count + 1,
                "total_llm_calls": recorder.llm_call_count,
                "total_tool_calls": recorder.tool_call_count,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
            },
        )
        return RunResult(
            run_id=context.run_id,
            status=status,
            output=output,
            gaps=context.gaps,
        )

    def export_jsonl(self, run_id: str, destination: Path) -> int:
        """Write the sealed run's canonical event log as JSON Lines. Returns the event count."""
        opened = self._open[run_id]
        destination.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with destination.open("w", encoding="utf-8") as fh:
            for event in opened.store.read_events(run_id):
                fh.write(encode_event(event))
                fh.write("\n")
                count += 1
        return count

    def gaps(self, run_id: str) -> tuple[InstrumentationGap, ...]:
        """Return the instrumentation gaps recorded for a run."""
        return self._open[run_id].context.gaps


# ---------------------------------------------------------------------------------------
# The fixture-local response pool (stands in for a real record/replay cache — see README)
# ---------------------------------------------------------------------------------------


class ResponsePool:
    """A committed, deterministic JSON map of `(tool, args) -> canned result`.

    This is what each fixture's `cache/responses.json` is. **Design constraint 3, not an
    LLM cache**: none of the three PRD §23 fixture tables lists an LLM call for its agents —
    every one lists only tool names (`read_file`, `vector_search`, `web_search`, ...). So the
    thing that must "ship its own cache to run offline with no API keys" is a tool-response
    cache, not `runtime/cache/`'s record/replay LLM cache (P07, `NOT STARTED`). A miss is a
    hard error, matching I7's replay-mode contract in spirit: a fixture whose graph asks for
    a `(tool, args)` pair its committed pool does not have is a fixture that drifted from its
    own golden log.
    """

    def __init__(self, path: Path) -> None:
        """Load the pool from `path`. Raises FileNotFoundError if it does not exist."""
        self._path = path
        self._data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    def get(self, tool: str, key: str) -> object:
        """Return the canned result for `tool`/`key`.

        Raises:
            KeyError: no such entry — the fixture's graph and its committed pool disagree.
        """
        bucket = self._data.get(tool)
        if bucket is None or key not in bucket:
            msg = (
                f"fixture response pool {self._path} has no entry for tool={tool!r} "
                f"key={key!r}. This fixture is offline-only (I7): add the entry rather than "
                f"calling a live provider."
            )
            raise KeyError(msg)
        return bucket[key]


def load_pool(fixture_dir: Path) -> ResponsePool:
    """Return the `ResponsePool` for a fixture directory (its `cache/responses.json`)."""
    return ResponsePool(fixture_dir / "cache" / "responses.json")


# ---------------------------------------------------------------------------------------
# The §21.6 pluggable assertion hook, run before `run_end` (see README "Why before sealing")
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunSummary:
    """The minimal `RunSummary` a fixture's `checks.py` receives (PRD §21.6).

    A fixture-local stand-in: PRD §21.6's example signature names a `RunSummary` type that
    belongs to the not-yet-built `api`/`analysis` surface. This carries only what a check can
    honestly know from this harness: identity and counts, not analysis results (no verdict,
    no scorecard — those are P10/P11).
    """

    run_id: str
    seed: int
    event_count: int
    llm_call_count: int
    tool_call_count: int


def run_checks(
    checks_module: object, final_state: Mapping[str, object], summary: RunSummary
) -> list[tuple[str, bool, str | None]]:
    """Run every function in `checks_module.CHECKS` and return `(id, passed, detail)` triples.

    Each check is `(final_state, run) -> bool | tuple[bool, str]`, exactly PRD §21.6's
    signature. Does not emit events itself — the caller (each fixture's `run.py`) emits
    `assertion_result` once per outcome, inside the still-open run, before `run_end` (see
    `docs/fixtures.md` "Why checks run before run_end, not after").
    """
    results: list[tuple[str, bool, str | None]] = []
    for check in checks_module.CHECKS:
        outcome = check(final_state, summary)
        if isinstance(outcome, tuple):
            passed, detail = outcome
        else:
            passed, detail = outcome, None
        results.append((check.__name__, bool(passed), detail))
    return results


def emit_assertion_result(
    context: RunContext, assertion_id: str, *, passed: bool, detail: str | None
) -> int:
    """Emit one `assertion_result` event (PRD §21.6: "part of the log, part of the evidence")."""
    return sdk_emit(
        context,
        EventType.ASSERTION_RESULT,
        {
            "assertion_id": assertion_id,
            "kind": "assertion",
            "passed": passed,
            "expected": None,
            "actual": detail,
        },
    )


__all__ = [
    "CACHE_MODE",
    "RUN_MODE",
    "FixtureRecorder",
    "FixtureRunHost",
    "ResponsePool",
    "RunSummary",
    "VClockBuilder",
    "content_hash",
    "deterministic_run_id",
    "emit_assertion_result",
    "load_pool",
    "run_checks",
]
