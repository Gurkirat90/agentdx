#!/usr/bin/env python3
"""PRD §34.2: replay determinism, measured against the real fixtures and the real scheduler.

**What is measured, stated precisely (Rule E1, AGENTS.md §6).** For each of the three real
P05 fixtures (`code_pipeline`, `support_triage`, `research_fanout`), this harness executes
`cli.commands.run._run_direct_target` — the exact async function `agentdx run <fixture>`
itself calls — 100 times at the same seed, through `bench.harness._real_fixture_run.
run_fixture_fresh` (real `Scheduler`, real `Store`, real `EventWriter`; a fresh isolated data
directory per call so `run_id`'s D-80 reuse path never short-circuits a "replay" into a
cache hit — see that module's own docstring). At least 10 of the 100 run in a genuinely
fresh OS subprocess (`bench.harness._replay_subprocess_runner`), not just a fresh in-process
call, matching the PRD's own "≥10 in fresh processes" wording and this project's existing
gate-G3 precedent (`tests/determinism/test_replay_equality.py`). `events.canonical.
canonical_log_hash` is computed for every run; the distinct-hash count is the published
number.

**Why this harness, not `tests/determinism/test_replay_equality.py`.** That test already
proves gate G3 (100/100 identical) against a synthetic four-fake-agent scenario, deliberately
with no LLM, no graph, no fixture (its own module docstring, design constraint 6) — it is a
scheduler-level determinism proof, not a per-fixture PRD §34.2 measurement. §34.2 explicitly
asks for "100 replays *per fixture*" with results "published as '100/100 identical' per
fixture, with the hash" — a claim about the three real, shipped reference systems, which
this harness is the first to measure and publish.

**Relationship to D-89 (`CONTEXT.md` §9).** This session's own P18.3 investigation found
that the *committed golden fixture logs* (`tests/golden/*.jsonl`) are stamped by
`fixtures/_harness.py` under `ImmediateScheduler()`, never the real `Scheduler` — so this
benchmark's own numbers, run through the real production path, are not directly comparable
to those golden logs' shape (event count, `sched_step` structure) and make no claim about
them. This harness measures whether the real system is deterministic, independent of
whether the golden corpus has been regenerated against it (D-89's own open recommendation).

Usage: `python3.12 bench/harness/replay_determinism.py`
Exit codes: 0 — every fixture had exactly 1 distinct hash · 2 — at least one did not.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from agentdx.events.canonical import canonical_log_hash  # noqa: E402
from bench.harness._real_fixture_run import REAL_FIXTURE_NAMES, run_fixture_fresh  # noqa: E402

SEED = 42
IN_PROCESS_REPLAYS = 90
SUBPROCESS_REPLAYS = 10


def _in_process_hash(fixture_name: str, run_dir: Path) -> str:
    _run_id, events = run_fixture_fresh(fixture_name, seed=SEED, data_dir=run_dir)
    return canonical_log_hash(events)


def _subprocess_hash(fixture_name: str, run_dir: Path) -> str:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell, trusted interpreter
        [
            sys.executable,
            "-m",
            "bench.harness._replay_subprocess_runner",
            fixture_name,
            str(SEED),
            str(run_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def _measure_fixture(fixture_name: str, scratch_root: Path) -> dict[str, object]:
    hashes: list[str] = []
    for i in range(IN_PROCESS_REPLAYS):
        run_dir = scratch_root / fixture_name / f"inproc-{i}"
        hashes.append(_in_process_hash(fixture_name, run_dir))
    for i in range(SUBPROCESS_REPLAYS):
        run_dir = scratch_root / fixture_name / f"subproc-{i}"
        hashes.append(_subprocess_hash(fixture_name, run_dir))

    distinct = sorted(set(hashes))
    return {
        "fixture": fixture_name,
        "replays": len(hashes),
        "in_process_replays": IN_PROCESS_REPLAYS,
        "subprocess_replays": SUBPROCESS_REPLAYS,
        "distinct_hash_count": len(distinct),
        "distinct_hashes": distinct,
        "the_hash": distinct[0] if len(distinct) == 1 else None,
        "met": len(distinct) == 1,
    }


def main() -> int:
    """Measure replay determinism for all three real fixtures; gate, publish, report."""
    with tempfile.TemporaryDirectory(prefix="agentdx-bench-replay-") as scratch:
        scratch_root = Path(scratch)
        per_fixture = [_measure_fixture(name, scratch_root) for name in REAL_FIXTURE_NAMES]

    overall_met = all(row["met"] for row in per_fixture)

    result = {
        "benchmark": "replay-determinism",
        "requirement": "PRD §34.2",
        "requirement_text": (
            "Method: 100 replays per fixture, >=10 in fresh processes; count distinct "
            "canonical log hashes. Gate: exactly 1 distinct hash per fixture. Published as: "
            "'100/100 identical' per fixture, with the hash."
        ),
        "function_under_test": (
            "cli.commands.run._run_direct_target (the real production path 'agentdx run "
            "<fixture>' itself calls), via bench.harness._real_fixture_run.run_fixture_fresh"
        ),
        "seed": SEED,
        "fixtures": per_fixture,
        "met": overall_met,
        "method": (
            "For each real fixture, 90 in-process replays plus 10 fresh-OS-subprocess "
            "replays (100 total, >=10 in fresh processes per the PRD's own wording), each "
            "against an isolated, fresh data directory so run_id's D-80 cache-reuse path "
            "never turns a 'replay' into a cache hit. canonical_log_hash is computed per "
            "run; the published number is the count of distinct hashes across all 100."
        ),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
    }

    out = REPO_ROOT / "bench" / "results" / "replay-determinism.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    sys.stdout.write(f"replay-determinism (seed={SEED}):\n")
    for row in per_fixture:
        status = "MET" if row["met"] else "NOT MET"
        label = (
            f"{row['distinct_hash_count']}/{row['replays']} identical"
            if row["met"]
            else (f"{row['distinct_hash_count']} DISTINCT HASHES across {row['replays']} replays")
        )
        sys.stdout.write(f"  {row['fixture']}: {label} ({status})\n")
    sys.stdout.write(f"written to {out}\n")

    return 0 if overall_met else 2


if __name__ == "__main__":
    raise SystemExit(main())
