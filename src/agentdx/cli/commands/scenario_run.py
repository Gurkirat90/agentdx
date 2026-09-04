"""`agentdx scenario run PATH --repeat N` — CLI-invented, PRD §44.1's G4 gate command.

**Not part of PRD §37.1's own `agentdx scenario` grammar** (`validate`/`list`/`new`/`expand`
only — `cli/commands/scenario.py`'s own module docstring: "none of them executes anything").
G4's literal verification command, `agentdx scenario run scenarios/kill_reviewer.yaml
--repeat 20`, is its only textual source; PRD §22.1's own worked examples (§22, "CI/CD
Integration") also use this exact shape (`agentdx scenario run scenarios/repro_f_0117.yaml
--repeat 20`), so this is at least a *consistently* PRD-used shape, just never given a formal
`## 37.1` entry — the same class of documented-but-ungrammared gap `--assert`
(`cli/commands/run.py::_parse_assert_expr`) and `--faults` (`_parse_fault_spec`) are.

**What "repeat" means here, and why each repeat needs its own isolated store.**
`scenarios/kill_reviewer.yaml`'s own header comment (added when this gap was first found)
already settles the semantics: `--repeat` is a rerun count at a **fixed seed** — the scenario's
own `seed: 42`, not varied per repeat — because "reproducing 20/20 identical outcomes is what
invariant I1 (determinism, gate G3) already guarantees for any fixed scenario + seed." This is
the same claim `tests/integration/faults/test_gate_g4.py`'s own harness already proves against
a synthetic `Scheduler` (20 fresh runs, same seed, identical cascade shape every time); this
command is that same claim's real-CLI, real-fixture, real-fault-hook path.

A fixed seed means every repeat computes the *identical* `run_id` (`make_run_id(seed,
scenario_hash, graph_hash)`, content-derived, PRD §6.1) — so repeats 2..N against the *same*
store would hit D-80/ADR-020's reuse-and-print path (`RunAlreadyExistsError`, re-scored from
the first repeat's own stored log) rather than genuinely re-executing. That would make a
"20/20 identical" claim vacuous: repeats 2-20 would trivially "agree" with repeat 1 because
they never ran anything, they only re-read it — exactly the "an exit code alone cannot
distinguish 'the property held' from 'nothing was actually checked'" trap
`tests/acceptance/test_gates.py::_run_gate`'s own docstring already names for a different
reason. So each repeat gets its own fresh, temporary `Store` — never the user's real
`~/.agentdx/agentdx.db` — forcing genuine, independent re-execution every time. This also
keeps 20 near-identical validation runs out of a user's real run history in the Control Tower.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from agentdx.cli._config import GlobalOptions, resolve_config
from agentdx.cli._exitcodes import ASSERTION_FAILURE, NOT_FOUND, OK, USAGE_ERROR
from agentdx.cli._output import Output
from agentdx.cli._scenario_io import ScenarioLoadError, ScenarioValidationError
from agentdx.cli._target import TargetError
from agentdx.cli.commands import run as run_cmd
from agentdx.config import AgentDXConfig
from agentdx.store.sqlite import Store

__all__ = ["scenario_run"]


def _reproducibility_signature(outcome: run_cmd._RunOutcome) -> tuple[object, ...]:
    """Reduce one repeat's outcome to a comparable signature.

    The "same failure classification, same cascade shape" comparison G4's own text asks for:
    the verdict class, the sorted set of (finding type,
    finding severity) pairs, how many faults actually fired, and every assertion's own
    (id, status) pair — sorted so this is a genuine set comparison, not order-sensitive.

    Deliberately reuses fields `_score_one`/`_score_reused` already computed rather than
    re-deriving a parallel "cascade shape" abstraction — `outcome.status`/`.exit_code` alone
    would miss a *different* failure that happens to carry the same exit code, and the fields
    read here are exactly what a human reading `--repeat`'s own printed report would compare
    by eye across repeats.
    """
    verdict_class = (
        outcome.analysis.verdict.verdict_class.value if outcome.analysis is not None else None
    )
    finding_shape = (
        tuple(sorted((f.type, f.severity) for f in outcome.summary.findings))
        if outcome.summary is not None
        else ()
    )
    faults_fired = outcome.summary.faults_fired if outcome.summary is not None else None
    assertion_shape = tuple(sorted((a.assertion_id, a.status) for a in outcome.assertions))
    return (outcome.status, verdict_class, finding_shape, faults_fired, assertion_shape)


def scenario_run(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="A scenario file (not a directory).")],
    repeat: Annotated[
        int,
        typer.Option(
            "--repeat",
            help=(
                "Execute the scenario this many times, at its own fixed seed, each in an "
                "isolated store — confirming I1 (determinism) holds for this exact scenario "
                "and fault, not just the synthetic harnesses that already cover it."
            ),
        ),
    ] = 1,
) -> None:
    """Run a scenario `--repeat` times and confirm the outcome reproduces identically."""
    out: Output = ctx.obj["output"]
    options: GlobalOptions = ctx.obj["options"]
    if repeat < 1:
        out.error(f"--repeat must be >= 1, got {repeat}")
        raise typer.Exit(code=USAGE_ERROR)
    if not path.is_file():
        out.error(f"{path}: no such scenario file")
        raise typer.Exit(code=NOT_FOUND)
    try:
        config = resolve_config(options)
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(code=USAGE_ERROR) from exc

    try:
        exit_code = asyncio.run(
            _async_scenario_run(path=path, repeat=repeat, config=config, out=out)
        )
    except TargetError as exc:
        out.error(str(exc))
        raise typer.Exit(code=USAGE_ERROR) from exc
    except (ScenarioLoadError, ScenarioValidationError) as exc:
        out.error(str(exc))
        raise typer.Exit(code=USAGE_ERROR) from exc
    raise typer.Exit(code=exit_code)


async def _async_scenario_run(
    *, path: Path, repeat: int, config: AgentDXConfig, out: Output
) -> int:
    outcomes: list[run_cmd._RunOutcome] = []
    signatures: list[tuple[object, ...]] = []
    with tempfile.TemporaryDirectory(prefix="agentdx-scenario-run-") as tmp_dir:
        for i in range(repeat):
            store = Store.open(Path(tmp_dir) / f"repeat_{i}.db", config=config.store)
            try:
                outcome = await run_cmd._run_scenario_file(
                    path,
                    seed_override=None,
                    fault_specs=[],
                    cache_mode=config.run.mode,
                    config=config,
                    store=store,
                    out=out,
                )
            finally:
                store.close()
            outcomes.append(outcome)
            signatures.append(_reproducibility_signature(outcome))

    # Counted via len() only, never iterated — set order is not in play here, and each
    # signature was itself produced deterministically (§4.1(4) cli/ progress output).
    distinct = set(signatures)  # determinism-exempt: §4.1(4) cli/ — count only, never iterated
    reproducible = len(distinct) == 1
    any_run_error = any(o.detail is not None for o in outcomes)
    scenario_name = outcomes[0].scenario_name if outcomes else str(path)

    if reproducible and not any_run_error:
        out.line(
            out.style(
                f"{scenario_name}: {repeat}/{repeat} repeats produced an identical outcome",
                color="green",
                bold=True,
            )
        )
    else:
        out.line(
            out.style(
                f"{scenario_name}: NOT reproducible across {repeat} repeats "
                f"({len(distinct)} distinct outcome(s))",
                color="red",
                bold=True,
            )
        )
    for i, (outcome, signature) in enumerate(zip(outcomes, signatures, strict=True)):
        marker = "✓" if signature == signatures[0] and outcome.detail is None else "✗"
        out.line(f"  [{i}] {marker} status={outcome.status} run_id={outcome.run_id}")
        if outcome.detail is not None:
            out.line(f"      {outcome.detail}")
    for assertion in outcomes[0].assertions if outcomes else ():
        mark = {"passed": "✓", "failed": "✗", "not_measurable": "·"}[assertion.status]
        out.line(f"  {mark} {assertion.assertion_id}: {assertion.detail}")
    out.coverage_statement()

    if not reproducible or any_run_error:
        return ASSERTION_FAILURE
    if any(o.status == "failed" for o in outcomes):
        return ASSERTION_FAILURE
    return OK
