"""Fresh-process gate G3 runner: run the fixed scenario once and print its canonical hash.

Invoked as ``python -m tests.determinism._subprocess_runner <seed>`` by
``test_replay_equality.py``'s fresh-process replays, always with ``PYTHONHASHSEED=0`` set
in the child's environment *before* the interpreter starts — `PYTHONHASHSEED` can only be
read at process bring-up (`trap`'s own `hash_seed_is_pinned` check reads `os.environ`, but
the hash randomisation it is pinning happens earlier, at interpreter startup, so setting it
from inside this script would be too late). Prints exactly one line: the
``blake2b:...``-prefixed canonical log hash, nothing else, so the parent can capture stdout
directly as the comparison value.
"""

from __future__ import annotations

import sys

from agentdx.events.canonical import canonical_log_hash
from tests.determinism._harness import run_scenario


def main() -> int:
    """Run the fixed scenario at the seed given on argv[1] and print its canonical hash."""
    if len(sys.argv) != 2:
        print("usage: _subprocess_runner.py <seed>", file=sys.stderr)  # noqa: T201
        return 2
    seed = int(sys.argv[1])
    events = run_scenario(seed)
    # stdout IS the return value the parent test captures — this is a CLI-shaped script,
    # not library code, matching the T20 exemption already granted to scripts/* and
    # tests/golden/build_event_log_40.py for the identical reason.
    print(canonical_log_hash(events))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
