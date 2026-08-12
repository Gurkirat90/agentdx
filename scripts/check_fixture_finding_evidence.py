#!/usr/bin/env python3
"""Mechanise drift tripwire 4 for fixture data.

CONTEXT.md §11 tripwire 4: "A verdict, finding or scorecard is produced without event `seq`
references in its evidence array." Invariant I6 states the general rule the tripwire
mechanises for the live analysis engine: "every finding/verdict/scorecard carries evidence =
concrete event `seq` refs; an empty evidence array is a schema failure, not a softened
finding."

`fixtures/*/golden_findings.json` are the reference corpora every future analyser is graded
against (PRD §23, gate FR-12) — an evidence-empty entry sitting in one of their `findings`
arrays would either fail I6 the moment a real detector tried to reproduce it, or train
whoever writes that detector to treat an empty evidence array as an acceptable output. This
script is the grep that keeps that from happening again.

Origin: an OP-2 audit (2026-08-12) found exactly this defect in all three `golden_findings.json`
files — entries with `"evidence": {"seq": []}` alongside a self-invented
`"computable_from_this_log": false` escape hatch. Repaired under OP-3 the same day by moving
every evidence-empty entry out of `findings` into `expected_not_yet_built` or
`expected_not_yet_measurable`, which make no evidence claim and are exempt from this check by
construction — this script only ever reads `findings`. This script is task #11 of that
repair's prevention step, so the defect gets a mechanical gate, not just a one-time fix.

Checked, per fixture's `golden_findings.json`:

1. **`findings` is present and is a list.** A missing or malformed key is a schema violation
   in its own right, not silently skipped.
2. **Every entry in `findings` has a non-empty `evidence.seq` list of integers.** This is the
   literal condition tripwire 4 names.
3. **No finding-shaped array lives under an unauthorised key.** ADR-011 fixes the schema at
   exactly `findings` / `expected_not_yet_built` / `expected_not_yet_measurable` — "a fourth
   array... reintroduces exactly the defect this ADR closes." An independent OP-2 audit
   (2026-08-13) found `research_fanout/golden_findings.json` had drifted to a fourth key,
   `not_built`, in the same session ADR-011 was written, and this script's first revision did
   not catch it because it only ever read `findings` by name. This check closes that hole: any
   top-level list whose entries look like findings (each a dict with a `finding_id` key) but
   whose key is not one of the three sanctioned names is flagged, regardless of what it's
   called.

`expected_not_yet_built` and `expected_not_yet_measurable` are deliberately not scanned: they
exist precisely so a real gap can be recorded honestly without a finding claim, and requiring
evidence from them would just re-create the escape-hatch pattern this script exists to
prevent, one field name later. ADR-011 fixes the schema at exactly these two arrays plus
`findings` — a fourth array (an earlier revision of `research_fanout/golden_findings.json`
briefly used `not_built`) is schema drift in its own right, not a third exempt category; this
script does not special-case one, on purpose.

Exit codes: 0 clean, 2 violation, 1 usage/environment error (e.g. no fixtures found, invalid
JSON — distinct from a real evidence violation so CI failure output is not ambiguous about
which kind of problem it is).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"
EXIT_VIOLATION = 2
EXIT_ERROR = 1

# ADR-011: the schema is exactly these three top-level arrays and no others.
SANCTIONED_FINDING_ARRAYS = frozenset(
    {"findings", "expected_not_yet_built", "expected_not_yet_measurable"}
)


def _iter_golden_findings_files() -> list[Path]:
    """Return every `fixtures/*/golden_findings.json`, in a stable order."""
    if not FIXTURES_DIR.is_dir():
        return []
    return sorted(FIXTURES_DIR.glob("*/golden_findings.json"))


def _check_file(path: Path) -> list[str]:
    """Return one violation message per evidence-empty (or malformed) entry in `findings`."""
    problems: list[str] = []
    fixture = path.parent.name

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{fixture}: {path.relative_to(REPO_ROOT)} is not valid JSON: {exc}"]

    if "findings" not in data:
        return [f"{fixture}: {path.relative_to(REPO_ROOT)} has no top-level 'findings' key"]

    findings = data["findings"]
    if not isinstance(findings, list):
        return [
            f"{fixture}: {path.relative_to(REPO_ROOT)}'s 'findings' is not a list "
            f"(found {type(findings).__name__})"
        ]

    for index, entry in enumerate(findings):
        finding_id = entry.get("finding_id", f"<findings[{index}], no finding_id>")
        evidence = entry.get("evidence")
        if not isinstance(evidence, dict):
            problems.append(
                f"{fixture}: {finding_id!r} has no 'evidence' object (tripwire 4 / invariant I6)"
            )
            continue
        seq = evidence.get("seq")
        if not isinstance(seq, list) or len(seq) == 0:
            problems.append(
                f"{fixture}: {finding_id!r} has an empty or missing 'evidence.seq' "
                f"(tripwire 4 / invariant I6) — move it to expected_not_yet_built or "
                f"expected_not_yet_measurable instead of leaving it in findings"
            )
            continue
        if not all(isinstance(s, int) for s in seq):
            problems.append(
                f"{fixture}: {finding_id!r}'s 'evidence.seq' contains a non-integer entry: {seq!r}"
            )

    for key, value in data.items():
        if key in SANCTIONED_FINDING_ARRAYS or not isinstance(value, list):
            continue
        if any(isinstance(item, dict) and "finding_id" in item for item in value):
            sanctioned = sorted(SANCTIONED_FINDING_ARRAYS)
            problems.append(
                f"{fixture}: top-level key {key!r} holds a finding-shaped array (entries "
                f"with 'finding_id') but is not one of {sanctioned} "
                f"(ADR-011 — the schema is exactly these three arrays, no others)"
            )

    return problems


def main() -> int:
    """Run the evidence check over every fixture's golden_findings.json and report violations."""
    files = _iter_golden_findings_files()
    if not files:
        print(
            f"check-fixture-finding-evidence: no fixtures/*/golden_findings.json found under "
            f"{FIXTURES_DIR.relative_to(REPO_ROOT)}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    problems: list[str] = []
    for path in files:
        problems += _check_file(path)

    if problems:
        print(
            "check-fixture-finding-evidence: FAILED (tripwire 4, invariant I6)",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        return EXIT_VIOLATION

    print(
        f"check-fixture-finding-evidence: OK — {len(files)} golden_findings.json file(s) "
        f"scanned, every finding has non-empty seq evidence"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
