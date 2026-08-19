"""Determinism regression: gate G3's own bar, re-checked with faults armed and firing.

Definition of Done: "a determinism regression proving G3 still passes 100/100 with faults
enabled." Gate G3 itself (`tests/determinism/test_replay_equality.py`) never arms a single
fault — its `_harness.py` has no `FaultInjectorHook` at all. This file re-runs the same
byte-identical-canonical-projection claim against `tests/integration/faults/_harness.py`'s
`kill_reviewer`-shaped scenario instead, where `CrashInjector` is wired in and a real fault
(`agent_crash` on `reviewer` at `t=3000`) fires on every single run. If arming
`FaultRandomStream`, `FaultTaintTracker`, or any fault-class module's own state introduced a
single non-deterministic read (wall-clock, unseeded random, dict/set iteration order), it would
show up here as a hash mismatch — the same mechanism gate G3 itself relies on, just pointed at
a run where faults are not merely present but actually fire.

Not a second, weaker restatement of gate G4: G4 checks the higher-level `CascadeShape` (which
agents crashed, taint *counts*); this checks the full canonical log, byte for byte, 100/100 —
strictly stronger, and the two are complementary regression pressure on the same claim.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentdx.events.canonical import canonical_log_hash
from tests.integration.faults._harness import SEED, run_scenario_async

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_IN_PROCESS_REPLAYS = 90
_SUBPROCESS_REPLAYS = 10


def _subprocess_path() -> str:
    import os

    return os.environ.get("PATH", "")


@pytest.mark.determinism
@pytest.mark.asyncio
async def test_100_runs_with_faults_enabled_are_byte_identical_10_of_them_in_fresh_processes() -> (
    None
):
    """Gate G3's own verification shape, faults-enabled. Paste this test's own output."""
    hashes: list[str] = []

    for _ in range(_IN_PROCESS_REPLAYS):
        events = await run_scenario_async(SEED)
        hashes.append(canonical_log_hash(events))

    env = {"PYTHONHASHSEED": "0", "PATH": _subprocess_path()}
    for _ in range(_SUBPROCESS_REPLAYS):
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell, trusted interpreter
            [sys.executable, "-m", "tests.integration.faults._subprocess_runner", str(SEED)],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        hashes.append(result.stdout.strip())

    distinct = set(hashes)
    print(  # noqa: T201
        f"\ndeterminism-with-faults: {len(hashes)}/{len(hashes)} runs at seed={SEED} "
        f"({_IN_PROCESS_REPLAYS} in-process + {_SUBPROCESS_REPLAYS} fresh-subprocess) "
        f"produced {len(distinct)} distinct canonical hash(es): {distinct}"
    )
    assert len(distinct) == 1, (
        f"determinism-with-faults FAILED: {len(distinct)} distinct canonical hashes across "
        f"{len(hashes)} runs at seed {SEED}"
    )


@pytest.mark.determinism
@pytest.mark.asyncio
async def test_two_same_seed_runs_with_faults_produce_an_identical_schedule_decision_sequence() -> (
    None
):
    """The same claim gate G3 checks at a human-legible level, not just a hash.

    The `schedule_decision` sequence itself — so a mismatch (if one ever occurred) is
    diagnosable.
    """
    first = await run_scenario_async(SEED)
    second = await run_scenario_async(SEED)

    def _decisions(events: object) -> list[tuple[int, str]]:
        from agentdx.events.schema import EventType

        return [
            (e.virtual_ts_ms, e.payload["chosen_task_id"])  # type: ignore[index]
            for e in events  # type: ignore[attr-defined]
            if e.type is EventType.SCHEDULE_DECISION
        ]

    first_decisions = _decisions(first)
    second_decisions = _decisions(second)
    print(  # noqa: T201
        f"\nseed={SEED} schedule_decision sequence ({len(first_decisions)} decisions), "
        "faults enabled:"
    )
    for virtual_ts_ms, chosen_task_id in first_decisions:
        print(f"  virtual_ts_ms={virtual_ts_ms} chosen_task_id={chosen_task_id}")  # noqa: T201

    assert first_decisions == second_decisions
