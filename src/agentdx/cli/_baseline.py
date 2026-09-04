"""`cli._baseline` — the concrete `analysis.baseline.BaselineExecutor` (PRD §24.3).

`analysis.baseline`'s own module docstring names the seam precisely: `BaselineExecutor` is
I3's one exception, "implemented by whatever module is allowed to import the runtime (`cli`,
per PRD §24.3 — not yet built, P17)". This is that implementation, built for gates G6
(`compare --baseline`) and G7 (`analyze --scorecard`) — both need a real `BaselineRun` to
compare against, not a fake one, or their scorecard's `achieved_speedup` (PRD §17.4/C5) is
unmeasurable and the gate stays red by construction.

**Why this composes `cli.commands.run._execute_one` rather than a new execution path.**
That function already is "build the real `Scheduler`/`VirtualClock`/`EventWriter`/`Cache`
services and drive `agentdx.run()` under `CliRunHost`" (its own docstring) — genuinely
generic over which `graph` it runs. Reusing it here means a baseline run is executed by the
exact same, already-tested machinery as any other `agentdx run` invocation; nothing about
scheduling, event stamping or store persistence is reimplemented (AGENTS.md §2/§4's "zero
business logic in cli/" convention, extended to "and none duplicated within it either").

**Why `code_pipeline` is the only target registered today.** None of the three P05 fixtures'
agents call a live model (`fixtures/_harness.py`'s `ResponsePool` docstring: "not an LLM
cache" — every agent is hardcoded Python over a tool-response pool). `code_pipeline` is the
one gate G6/G7 actually exercise (PRD §44.1's own literal commands, FR8-AC1/AC2), and its
fixture-specific single-agent counterpart is `fixtures/code_pipeline/graph.py::
build_baseline_graph` (see that module's docstring for the full design). A target with no
registered baseline graph fails honestly — `UnsupportedBaselineTargetError`, never a
fabricated result (AGENTS.md §2) — rather than silently producing a comparison for a graph
that was never actually run.

**Why `execute` is a plain sync method that calls `asyncio.run` internally, not `async def`.**
`analysis.baseline.BaselineExecutor` (the Protocol this implements) declares `execute` as a
sync method — I3's boundary means `analysis/` cannot depend on `asyncio` internals of a
particular event-loop shape, only on a plain callable. `compare`/`analyze` (the CLI commands
that call `generate_baseline`, which calls `executor.execute`) are themselves fully
synchronous top-level Typer commands with no `asyncio.run` of their own wrapping them — they
only need async execution for this one, optional step — so `execute`'s own `asyncio.run`
call here is never nested inside an already-running loop.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agentdx.analysis.baseline import (
    BaselineExecutionResult,
    BaselineOutcome,
    BaselineRunSpec,
)
from agentdx.cli._output import Output
from agentdx.cli.commands import run as run_cmd
from agentdx.config import AgentDXConfig
from agentdx.sdk.generic import hash_text
from agentdx.store.sqlite import Store

__all__ = ["CliBaselineExecutor", "UnsupportedBaselineTargetError", "baseline_available_for"]

#: Targets with a registered, scripted single-agent baseline graph (see module docstring).
_REGISTERED_TARGETS: frozenset[str] = frozenset({"code_pipeline"})


class UnsupportedBaselineTargetError(RuntimeError):
    """No concrete baseline graph is registered for a run's target.

    Raised rather than fabricating a result: `--baseline`'s whole point is a *real* single-
    agent execution (PRD §17.1), and a target with no scripted (or, one day, live-model)
    counterpart has nothing real to run.
    """

    def __init__(self, target_name: str | None) -> None:
        """Build the message naming what was asked for and what is actually registered."""
        self.target_name = target_name
        registered = ", ".join(sorted(_REGISTERED_TARGETS))
        msg = (
            f"no baseline executor is registered for target {target_name!r} "
            f"(registered: {registered or '(none)'}) — see cli/_baseline.py"
        )
        super().__init__(msg)


def baseline_available_for(target_name: str | None) -> bool:
    """Return whether `CliBaselineExecutor` has a real graph registered for `target_name`."""
    return target_name in _REGISTERED_TARGETS


def _build_baseline_graph(target_name: str) -> object:
    """Return the instrumented single-agent graph for `target_name`, or raise."""
    if target_name == "code_pipeline":
        from fixtures.code_pipeline.graph import build_baseline_graph

        return build_baseline_graph()
    raise UnsupportedBaselineTargetError(target_name)


@dataclass(eq=False)
class CliBaselineExecutor:
    """The `BaselineExecutor` `compare`/`analyze` inject into `analysis.baseline.generate_baseline`.

    One instance per invocation, bound to the target whose baseline is being generated (found
    via `Store.get_run(run_id).scenario_id` — populated for direct-target runs by `cli.
    commands.run._run_and_score`'s `direct_target_name`, see that function's docstring).
    """

    target_name: str
    config: AgentDXConfig
    out: Output

    def execute(self, spec: BaselineRunSpec) -> BaselineExecutionResult:
        """Run this target's registered single-agent graph and return its log and outcome.

        Every baseline run gets its own throwaway, temporary `Store` — never the user's real
        data directory — the same isolation `cli.commands.scenario_run` already uses for
        `--repeat`, for the same reason: this is a validation/comparison execution, not a run
        a user's history should retain.

        Raises:
            UnsupportedBaselineTargetError: no graph is registered for `self.target_name`.
        """
        graph = _build_baseline_graph(self.target_name)
        with tempfile.TemporaryDirectory(prefix="agentdx-baseline-") as tmp_dir:
            store = Store.open(Path(tmp_dir) / "baseline.db", config=self.config.store)
            try:
                _host, result, events = asyncio.run(
                    run_cmd._execute_one(
                        graph=graph,
                        task_text=spec.task,
                        scenario_id=f"{self.target_name}-baseline",
                        scenario_hash=hash_text(f"{self.target_name}-baseline-scenario"),
                        graph_hash=hash_text(f"{self.target_name}-baseline-graph"),
                        seed=spec.seed,
                        config=self.config,
                        store=store,
                        faults=[],
                        is_fixture_target=True,
                        cache_mode=spec.cache_mode,
                        run_mode="baseline",
                        out=self.out,
                    )
                )
            finally:
                store.close()
        outcome = (
            BaselineOutcome.COMPLETED if result.status == "complete" else BaselineOutcome.FAILED
        )
        return BaselineExecutionResult(events=events, outcome=outcome)


# `BaselineExecutor` is a `runtime_checkable` Protocol with one method, `execute(spec) ->
# BaselineExecutionResult` — `CliBaselineExecutor` satisfies it structurally, not nominally.
# `tests/unit/cli/test_baseline_executor.py` asserts `isinstance(executor, BaselineExecutor)`
# against a real instance, which is the real, importable check; unlike a nominal ABC, a bare
# module-level `issubclass` against a `Protocol` checks method *names* only, ignores their
# signatures, and would pass even if `execute` had drifted incompatibly — so it is not
# repeated here as a false sense of safety.
