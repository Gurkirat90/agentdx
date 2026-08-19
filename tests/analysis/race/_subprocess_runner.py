"""Fresh-process NFR-14 runner: analyse one golden log once and print a hash of its findings.

Invoked as ``python -m tests.analysis.race._subprocess_runner <fixture_name>`` by
``test_determinism.py``, with a *different* ``PYTHONHASHSEED`` set in each child's
environment before the interpreter starts (unlike ``tests/determinism/_subprocess_runner.py``,
which pins ``PYTHONHASHSEED=0`` for every replay - this runner's whole purpose is to *vary*
the hash seed across children, since that is what would expose a bare ``dict``/``set``
iteration order dependency `race.py`'s own NFR-14 docstring commits it does not have). Prints
exactly one line: a ``blake2b:``-prefixed hash of the repr of every returned `Finding`, in
returned order, so the parent can capture stdout directly as the comparison value.
"""

from __future__ import annotations

import sys
from hashlib import blake2b
from pathlib import Path

from agentdx.analysis.race import detect_conflicts
from agentdx.events.canonical import decode_event

_GOLDEN_DIR = Path(__file__).resolve().parents[3] / "tests" / "golden"


def main() -> int:
    """Analyse the fixture named on argv[1] and print a hash of its findings' repr sequence."""
    if len(sys.argv) != 2:
        print("usage: _subprocess_runner.py <fixture_name>", file=sys.stderr)  # noqa: T201
        return 2
    fixture_name = sys.argv[1]
    with (_GOLDEN_DIR / f"{fixture_name}.jsonl").open() as fh:
        events = [decode_event(line) for line in fh]
    findings = detect_conflicts(events)
    digest = blake2b(digest_size=32)
    for finding in findings:
        digest.update(repr(finding).encode("utf-8"))
        digest.update(b"\x00")
    # stdout IS the return value the parent test captures — a CLI-shaped script, matching the
    # T201 exemption `tests/determinism/_subprocess_runner.py` already documents for itself.
    print(f"blake2b:{digest.hexdigest()}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
