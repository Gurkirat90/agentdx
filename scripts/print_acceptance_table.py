#!/usr/bin/env python3
"""Print the PRD §44.1 acceptance-gate pass/fail table from `tests/acceptance/.results/*.json`.

Guarantees: reads only what `tests/acceptance/test_gates.py` itself wrote (one JSON file per
gate, written unconditionally whether the gate's test passed or failed); never re-derives a
verdict from anything else, so this script and the pytest run it follows can never disagree
about a gate's status. Exits 1 if any gate is missing a results file (the run did not reach
it) or reported a non-zero/absent return code; exits 0 only if all ten gates report
`returncode == 0`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "tests" / "acceptance" / ".results"
GATE_IDS = [f"G{n}" for n in range(1, 11)]


def main() -> int:
    """Print the table to stdout; return the process exit code (0 = all ten pass)."""
    rows: list[tuple[str, str, str]] = []
    all_pass = True
    for gate_id in GATE_IDS:
        result_path = RESULTS_DIR / f"{gate_id}.json"
        if not result_path.exists():
            rows.append((gate_id, "NO RESULT", "just acceptance was not run, or crashed before this gate"))
            all_pass = False
            continue
        data = json.loads(result_path.read_text())
        rc = data.get("returncode")
        command = data.get("command", "")
        if rc == 0:
            rows.append((gate_id, "PASS", command))
        else:
            all_pass = False
            found = data.get("found_binary", True)
            detail = command if found else f"{command}  (binary not found)"
            rows.append((gate_id, f"FAIL (exit {rc})", detail))

    id_w = max(len(r[0]) for r in rows)
    status_w = max(len(r[1]) for r in rows)
    print(f"{'Gate':<{id_w}}  {'Status':<{status_w}}  Command")
    print(f"{'-' * id_w}  {'-' * status_w}  {'-' * 40}")
    for gate_id, status, command in rows:
        print(f"{gate_id:<{id_w}}  {status:<{status_w}}  {command}")

    passed = sum(1 for r in rows if r[1] == "PASS")
    print(f"\n{passed}/{len(rows)} gates PASS.")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
