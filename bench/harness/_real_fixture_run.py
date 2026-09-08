"""bench/harness/_real_fixture_run.py — shared "run one real fixture, fresh" helper.

Every §34.2 (replay determinism) and §34.5 (speedup accuracy, virtual/wall calibration)
measurement needs the same primitive: execute one of the three real P05 fixtures end to end
through the **real production path** — `cli.commands.run._run_direct_target`, the exact
async function `agentdx run <fixture>` itself calls — and get back the sealed run's own
event log. This module is that one primitive, factored out so both harnesses call it
identically rather than each hand-rolling a slightly different composition (the same
"one obviously correct way" principle `bench/harness/race_accuracy_corpus.py`'s shared
builders already follow).

**Why the real `CliRunHost`/`Scheduler`, not `fixtures/_harness.py`.** D-89 (`CONTEXT.md`
§9, this session's own P18.3 finding) established that `fixtures/_harness.py` stamps every
event under `ImmediateScheduler()` — the explicit "no ordering, no determinism claim"
default — never the real `runtime.scheduler.Scheduler`. A benchmark that claims to measure
*the real system's* replay determinism or speedup accuracy has to run the real system, not
the provisional harness that produced the (separately, honestly, still-provisional) golden
fixtures. This module reuses exactly the composition `cli/commands/run.py::_run_direct_target`
already performs for a real `agentdx run <fixture>` invocation — no new business logic.

**Why a fresh isolated store per call, not the project's own `.agentdx-data/`.** `run_id`
is deterministic — `make_run_id(seed, scenario_hash, graph_hash)` — so two calls at the same
seed against the *same* store would hit D-80's reuse path on the second call (a legitimate
optimization for a real user, but exactly the wrong thing for a benchmark that needs N
independently-executed runs to measure). Each call here gets `GlobalOptions(data_dir=...)`
pointed at a directory the caller owns (a fresh `tempfile.mkdtemp()` by default), so every
call is a genuinely fresh `Store`/`agentdx.db`, never a cache hit.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from agentdx.cli._config import GlobalOptions, resolve_config
from agentdx.cli._output import Output
from agentdx.cli.commands.run import _run_direct_target
from agentdx.events.schema import Event
from agentdx.store.sqlite import Store

REAL_FIXTURE_NAMES: tuple[str, ...] = ("code_pipeline", "support_triage", "research_fanout")
"""The three PRD §23/§24/§25 reference fixtures P05 built — the same set §34.2/§34.5 name."""


class FixtureRunFailed(RuntimeError):
    """Raised when a fresh fixture run did not reach `status == "passed"`.

    A benchmark that silently accepted a failed run's (possibly partial) log would publish
    a number about something other than what it claims to measure.
    """


def run_fixture_fresh(
    fixture_name: str, *, seed: int, data_dir: Path | None = None
) -> tuple[str, tuple[Event, ...]]:
    """Execute `fixture_name` once, fresh, through the real CLI composition. Return its log.

    Args:
        fixture_name: One of `REAL_FIXTURE_NAMES`.
        seed: The run seed (PRD §10.10 — the only thing two "replays" have in common).
        data_dir: Where this run's isolated `agentdx.db`/cache live. Defaults to a fresh
            `tempfile.mkdtemp()`, deleted by the caller's own responsibility (or left for
            the OS temp-cleanup sweep — these are throwaway scratch stores, never read
            again after this function returns their decoded events).

    Returns:
        `(run_id, events)` — `events` is this run's own sealed, canonical-hashable log,
        read back from the store exactly as `store.read_events(run_id)` would for any
        real consumer.

    Raises:
        FixtureRunFailed: the run did not reach `status == "passed"`.
    """
    resolved_data_dir = data_dir if data_dir is not None else Path(tempfile.mkdtemp())
    resolved_data_dir.mkdir(parents=True, exist_ok=True)
    config = resolve_config(GlobalOptions(data_dir=resolved_data_dir))
    store_path = config.store.data_dir.expanduser() / "agentdx.db"
    store = Store.open(store_path, config=config.store)
    out = Output(quiet=True)
    try:
        outcome = asyncio.run(
            _run_direct_target(
                fixture_name,
                task=None,
                seed=seed,
                fault_specs=[],
                cache_mode="replay",
                config=config,
                store=store,
                out=out,
            )
        )
        if outcome.status != "passed" or outcome.run_id is None:
            detail = (
                f"{fixture_name} (seed={seed}): expected status 'passed' with a run_id, "
                f"got status={outcome.status!r} run_id={outcome.run_id!r} "
                f"detail={outcome.detail!r}"
            )
            raise FixtureRunFailed(detail)
        events = tuple(store.read_events(outcome.run_id))
        return outcome.run_id, events
    finally:
        store.close()
