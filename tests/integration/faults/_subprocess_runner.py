"""Fresh-process runner for `test_determinism_with_faults.py`'s subprocess replays.

Same shape and rationale as `tests/determinism/_subprocess_runner.py` — invoked as
``python -m tests.integration.faults._subprocess_runner <seed>`` with ``PYTHONHASHSEED=0`` set
in the child's environment before the interpreter starts. Prints exactly one line: the
``blake2b:...``-prefixed canonical log hash of the `kill_reviewer`-shaped, faults-enabled
scenario, so the parent test can capture stdout directly as the comparison value.
"""

from __future__ import annotations

import sys

from agentdx.events.canonical import canonical_log_hash
from tests.integration.faults._harness import run_scenario


def main() -> int:
    """Run the fixed faults-enabled scenario at the seed given on argv[1]; print its hash."""
    if len(sys.argv) != 2:
        print("usage: _subprocess_runner.py <seed>", file=sys.stderr)  # noqa: T201
        return 2
    seed = int(sys.argv[1])
    events = run_scenario(seed)
    print(canonical_log_hash(events))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
