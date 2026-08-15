"""Gate G3 — PRD §10.1's central claim, tested directly: same seed in, same log out.

DEFINITION OF DONE, verbatim from the P06 mission:

- 100 runs at seed 42, >= 10 of them in fresh OS processes, byte-identical canonical
  projections (`events.canonical.canonical_log_hash`).
- Two same-seed runs print an identical `schedule_decision` sequence.
- Different seeds produce different interleavings.

All three are asserted below against the fixed scenario in `_harness.py` — four fake
agents, no LLM, no graph, no fixture (design constraint 6), but with a real spread of
yields, sleeps, and stamped payload events so the canonical projection compared has actual
content to diverge on if determinism ever breaks.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentdx.events.canonical import canonical_log_hash
from agentdx.events.schema import EventType
from tests.determinism._harness import run_scenario_async

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_IN_PROCESS_REPLAYS = 90
_SUBPROCESS_REPLAYS = 10
_SEED = 42


# ---------------------------------------------------------------------------------------
# 100 runs at seed 42, byte-identical canonical projections
# ---------------------------------------------------------------------------------------


@pytest.mark.determinism
@pytest.mark.asyncio
async def test_100_runs_at_seed_42_are_byte_identical_10_of_them_in_fresh_processes() -> None:
    """The gate G3 replay-equality test. Paste this test's own output into the report."""
    hashes: list[str] = []

    # 90 in-process replays — each run gets its own fresh Scheduler/VirtualClock/sink
    # (build_scenario_scheduler is called fresh inside run_scenario_async every time), so
    # this is 90 independent runs within one interpreter, not one run inspected 90 times.
    for _ in range(_IN_PROCESS_REPLAYS):
        events = await run_scenario_async(_SEED)
        hashes.append(canonical_log_hash(events))

    # 10 replays in genuinely fresh OS subprocesses — a new interpreter, new memory space,
    # new PYTHONHASHSEED application, new everything except the seed and the scenario code.
    env = {"PYTHONHASHSEED": "0", "PATH": _subprocess_path()}
    for _ in range(_SUBPROCESS_REPLAYS):
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell, trusted interpreter
            [sys.executable, "-m", "tests.determinism._subprocess_runner", str(_SEED)],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        hashes.append(result.stdout.strip())

    assert len(hashes) == _IN_PROCESS_REPLAYS + _SUBPROCESS_REPLAYS
    distinct = set(hashes)
    assert len(distinct) == 1, (
        f"gate G3 FAILED: {len(distinct)} distinct canonical hashes across "
        f"{len(hashes)} runs at seed {_SEED}: {sorted(distinct)}"
    )
    # Not a degenerate/empty log — a real scenario actually ran.
    assert hashes[0] != canonical_log_hash(())


def _subprocess_path() -> str:
    """Return a minimal PATH for the child so `sys.executable` and its shared libs resolve."""
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


# ---------------------------------------------------------------------------------------
# Two same-seed runs print an identical schedule_decision sequence
# ---------------------------------------------------------------------------------------


@pytest.mark.determinism
@pytest.mark.asyncio
async def test_two_same_seed_runs_print_an_identical_schedule_decision_sequence() -> None:
    first = await run_scenario_async(_SEED)
    second = await run_scenario_async(_SEED)

    def decisions(events: tuple[object, ...]) -> list[tuple[str, int]]:
        return [
            (str(e.payload["chosen_task_id"]), e.virtual_ts_ms)  # type: ignore[attr-defined]
            for e in events
            if e.type is EventType.SCHEDULE_DECISION  # type: ignore[attr-defined]
        ]

    first_decisions = decisions(first)
    second_decisions = decisions(second)

    # DEFINITION OF DONE: "two same-seed runs print an identical schedule_decision
    # sequence" — printed here (captured with `pytest -s`) so the sequence itself, not just
    # the pass/fail assertion below, is the pasteable evidence.
    print(f"seed={_SEED} schedule_decision sequence ({len(first_decisions)} decisions):")  # noqa: T201
    for chosen_task_id, virtual_ts_ms in first_decisions:
        print(f"  virtual_ts_ms={virtual_ts_ms} chosen_task_id={chosen_task_id}")  # noqa: T201

    assert first_decisions == second_decisions
    assert len(first_decisions) > 1  # a schedule with real choices, not a degenerate one


# ---------------------------------------------------------------------------------------
# Different seeds produce different interleavings
# ---------------------------------------------------------------------------------------


@pytest.mark.determinism
@pytest.mark.asyncio
async def test_different_seeds_produce_different_interleavings() -> None:
    async def decision_sequence(seed: int) -> list[str]:
        events = await run_scenario_async(seed)
        return [
            str(e.payload["chosen_task_id"])
            for e in events
            if e.type is EventType.SCHEDULE_DECISION
        ]

    sequences = {seed: await decision_sequence(seed) for seed in range(1, 9)}

    # Every sequence must be non-degenerate (more than one real choice was made).
    for seed, seq in sequences.items():
        assert len(seq) > 1, f"seed {seed} produced a degenerate single-decision schedule"

    # Across eight distinct seeds, seeing only one distinct interleaving would mean the
    # seed is not actually reaching `_choose` at all — assert genuine divergence exists.
    distinct_sequences = {tuple(seq) for seq in sequences.values()}
    assert len(distinct_sequences) > 1, (
        "gate G3 FAILED: all 8 seeds produced the identical interleaving — "
        "the seed does not appear to influence scheduling at all"
    )

    # And the canonical hash — not just the raw decision list — must differ too, since
    # that is the artifact gate G3 actually compares.
    hashes = {seed: canonical_log_hash(await run_scenario_async(seed)) for seed in (1, 2)}
    assert hashes[1] != hashes[2]
