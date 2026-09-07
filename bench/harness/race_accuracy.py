#!/usr/bin/env python3
"""PRD §34.3: race detection accuracy over a labelled 40-log corpus.

**What is measured, stated precisely (Rule E1, AGENTS.md §6).** `agentdx.analysis.race.
detect_conflicts` — the exact function every acceptance gate and every CLI `run`/`analyze`
call reaches for race findings — run against `race_accuracy_corpus.CORPUS`'s 40 labelled
logs (20 genuine races, 20 clean near-misses; that file documents each one and why it is
labelled the way it is). This harness classifies each log by whether `detect_conflicts`
returned anything at all, compares that classification against the corpus's own ground
truth, and reports precision/recall/F1 over the resulting confusion matrix.

**Gate (PRD §34.3, literal).** Recall = 1.0 on the race set (every genuine race is found —
a missed race is a false negative, and this project's own trust contract, §33.9, treats a
false negative as seriously as a false positive). Precision = 1.0 on the clean set — a single
false positive fails the benchmark, because "a system that reports problems on every run is
not trustworthy" (§33.9's own words, restated here because it applies to this gate exactly
as much as to that one).

Usage: `python3.12 bench/harness/race_accuracy.py`
Exit codes: 0 both thresholds were met · 2 either was not.
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from bench.harness.race_accuracy_corpus import CORPUS  # noqa: E402

from agentdx.analysis.race import detect_conflicts  # noqa: E402


def _classify(events: object, crdt_keys: frozenset[str]) -> str:
    """Return `"race"` if `detect_conflicts` reported anything, else `"clean"`.

    `events` is typed loosely here only to keep this function's own signature independent of
    `Event`'s import path; every real caller passes a `tuple[Event, ...]`.
    """
    findings = detect_conflicts(events, crdt_keys=crdt_keys)  # type: ignore[arg-type]
    return "race" if findings else "clean"


def main() -> int:
    """Classify every corpus case, build the confusion matrix, gate, and write the result."""
    per_case: list[dict[str, object]] = []
    tp = fp = tn = fn = 0

    for case in CORPUS:
        predicted = _classify(case.events, case.crdt_keys)
        correct = predicted == case.label
        if case.label == "race" and predicted == "race":
            tp += 1
        elif case.label == "race" and predicted == "clean":
            fn += 1
        elif case.label == "clean" and predicted == "race":
            fp += 1
        else:
            tn += 1
        per_case.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "label": case.label,
                "predicted": predicted,
                "correct": correct,
                "event_count": len(case.events),
            }
        )

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    recall_met = recall == 1.0
    precision_met = precision == 1.0
    overall_met = recall_met and precision_met

    false_negatives = [c["case_id"] for c in per_case if not c["correct"] and c["label"] == "race"]
    false_positives = [c["case_id"] for c in per_case if not c["correct"] and c["label"] == "clean"]

    result = {
        "benchmark": "race-accuracy",
        "requirement": "PRD §34.3",
        "requirement_text": (
            "Method: a labelled corpus of 40 synthetic event logs: 20 containing a genuine "
            "race (by construction), 20 free of races but containing near-miss patterns "
            "(reducer channels, locks, ordered writes, identical values, retries). Compute "
            "precision, recall and F1. Gate: recall = 1.0 on the seeded set; precision = 1.0 "
            "on the negative set."
        ),
        "function_under_test": "agentdx.analysis.race.detect_conflicts(events, *, crdt_keys)",
        "gate_status": (
            "This is a deterministic, seed-free classification benchmark (no wall-clock "
            "timing, no randomness) — the confusion matrix and the precision/recall/F1 "
            "figures below are exact and reproducible on every run, not a distribution like "
            "the timing benchmarks in this directory."
        ),
        "corpus_size": len(CORPUS),
        "race_count": sum(1 for c in CORPUS if c.label == "race"),
        "clean_count": sum(1 for c in CORPUS if c.label == "clean"),
        "confusion_matrix": {
            "true_positive": tp,
            "false_positive": fp,
            "true_negative": tn,
            "false_negative": fn,
        },
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "thresholds": {"recall": 1.0, "precision": 1.0},
        "met": overall_met,
        "recall_met": recall_met,
        "precision_met": precision_met,
        "false_negative_case_ids": false_negatives,
        "false_positive_case_ids": false_positives,
        "per_case": per_case,
        "method": (
            "Every log in bench/harness/race_accuracy_corpus.py is built through "
            "tests.analysis.race._causal_log.CausalLog, the same vector-clock/causal_parents "
            "construction the real scheduler performs (not hand-set placeholders) — see that "
            "corpus file's own module docstring for the full account of all 40 cases and the "
            "five near-miss categories PRD §34.3 names. A case is classified 'race' if "
            "detect_conflicts(events, crdt_keys=...) returns one or more findings, 'clean' "
            "otherwise; predicted label is compared against the corpus's own ground truth."
        ),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
    }

    out = REPO_ROOT / "bench" / "results" / "race-accuracy.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    sys.stdout.write(
        f"race-accuracy: {len(CORPUS)} cases ({result['race_count']} race, "
        f"{result['clean_count']} clean)\n"
        f"  confusion matrix: TP={tp} FP={fp} TN={tn} FN={fn}\n"
        f"  precision={precision:.4f} (need 1.0, {'MET' if precision_met else 'NOT MET'})\n"
        f"  recall={recall:.4f} (need 1.0, {'MET' if recall_met else 'NOT MET'})\n"
        f"  f1={f1:.4f}\n"
    )
    if false_negatives:
        sys.stdout.write(f"  missed races (false negatives): {false_negatives}\n")
    if false_positives:
        sys.stdout.write(f"  false alarms (false positives): {false_positives}\n")
    sys.stdout.write(f"written to {out}\n")

    return 0 if overall_met else 2


if __name__ == "__main__":
    raise SystemExit(main())
