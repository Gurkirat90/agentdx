"""Fresh-process runner for §34.2's replay-determinism benchmark.

Invoked as ``python -m bench.harness._replay_subprocess_runner <fixture_name> <seed>
<data_dir>`` by `replay_determinism.py`'s fresh-process replays, mirroring
`tests/determinism/_subprocess_runner.py`'s own pattern (a new interpreter, new memory
space, `PYTHONHASHSEED` set in the child's environment *before* the interpreter starts, so
it cannot be applied from inside this script). Prints exactly one line: the
``blake2b:...``-prefixed canonical log hash of the fresh run's own sealed log.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agentdx.events.canonical import canonical_log_hash
from bench.harness._real_fixture_run import run_fixture_fresh


def main() -> int:
    """Run one fixture fresh, at the given seed, in an isolated data dir; print its hash."""
    if len(sys.argv) != 4:
        sys.stderr.write("usage: _replay_subprocess_runner.py <fixture_name> <seed> <data_dir>\n")
        return 2
    fixture_name, seed_str, data_dir_str = sys.argv[1], sys.argv[2], sys.argv[3]
    _run_id, events = run_fixture_fresh(
        fixture_name, seed=int(seed_str), data_dir=Path(data_dir_str)
    )
    print(canonical_log_hash(events))  # noqa: T201 — stdout IS the parent's return value
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
