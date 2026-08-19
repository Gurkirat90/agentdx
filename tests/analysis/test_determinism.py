"""NFR-14: analysing the same log 100 times yields byte-identical output.

Covers the full P10 surface (`timing`, `overhead`, `redundancy`) in one pass, over a real
golden fixture — the kind of log this matters for in production, not a toy.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from agentdx.analysis.overhead import decompose_critical_path, decompose_total_work
from agentdx.analysis.redundancy import detect_redundancy
from agentdx.analysis.timing import build_timing_dag, critical_path, parallelism_metrics
from agentdx.events.canonical import decode_event
from agentdx.events.schema import Event

_GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"


def _analyse_once(events: list[Event]) -> str:
    dag = build_timing_dag(events)
    cp = critical_path(dag)
    pm = parallelism_metrics(dag, cp)
    groups = detect_redundancy(dag)
    cp_dec = decompose_critical_path(dag, cp, events, redundancy_groups=groups)
    tw_dec = decompose_total_work(dag, redundancy_groups=groups)
    return repr(dag) + repr(cp) + repr(pm) + repr(groups) + repr(cp_dec) + repr(tw_dec)


def test_hundred_analyses_are_byte_identical() -> None:
    path = _GOLDEN_DIR / "support_triage.jsonl"
    with path.open(encoding="utf-8") as f:
        events = [decode_event(line) for line in f]

    digests = {
        hashlib.sha256(_analyse_once(events).encode("utf-8")).hexdigest() for _ in range(100)
    }

    assert len(digests) == 1, f"analysis is not deterministic: {len(digests)} distinct outputs"
