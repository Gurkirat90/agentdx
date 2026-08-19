"""NFR-14 — `agentdx.analysis.race` determinism: 100 analyses, byte-identical, identical order.

`race.py`'s own module docstring commits to this exactly: "`detect_conflicts` and
`find_conflicts` iterate `events` once, in the order given, and every returned tuple is sorted
by an explicit, documented key ... never dict/set insertion order." Two independent things
need proving: (1) re-running the *same* analysis in the *same* process 100 times never drifts
(the in-process check), and (2) the result does not depend on `PYTHONHASHSEED` (Python
randomises `dict`/`set` iteration order per-process by default; a bare, un-sorted
`set(...)`/`dict(...)` iteration anywhere in the pipeline would make this fail across fresh
interpreters with different seeds, even though it might never fail in-process). Mirrors
`tests/determinism/test_replay_equality.py`'s established in-process + fresh-subprocess split
for the same underlying reason that module documents for gate G3.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentdx.analysis.race import detect_conflicts, find_conflicts
from agentdx.events.canonical import decode_event
from agentdx.events.schema import Event
from tests.analysis.race._events import state_write

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GOLDEN_DIR = _REPO_ROOT / "tests" / "golden"
_IN_PROCESS_RUNS = 100
_SUBPROCESS_RUNS = 8


def _load_golden(name: str) -> list[Event]:
    with (_GOLDEN_DIR / f"{name}.jsonl").open() as fh:
        return [decode_event(line) for line in fh]


# ---------------------------------------------------------------------------------------------
# 100 in-process analyses of one log: byte-identical (repr-equal) findings, identical order
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", ["code_pipeline", "research_fanout", "support_triage"])
def test_100_in_process_analyses_are_byte_identical(fixture_name: str) -> None:
    """NFR-14, pasteable evidence: 100 analyses of one log, byte-identical, identical order."""
    events = _load_golden(fixture_name)
    reprs: list[str] = []
    for _ in range(_IN_PROCESS_RUNS):
        findings = detect_conflicts(events)
        reprs.append(repr(findings))

    distinct = set(reprs)
    assert len(distinct) == 1, (
        f"NFR-14 FAILED: {len(distinct)} distinct results across {_IN_PROCESS_RUNS} in-process "
        f"analyses of {fixture_name}"
    )


def test_100_in_process_analyses_of_find_conflicts_are_byte_identical() -> None:
    """The lower-level `find_conflicts` (reported + suppressed) gets the identical guarantee."""
    events = _load_golden("code_pipeline")
    reprs = {repr(find_conflicts(events)) for _ in range(_IN_PROCESS_RUNS)}
    assert len(reprs) == 1


# ---------------------------------------------------------------------------------------------
# Fresh-process, varied-PYTHONHASHSEED analyses: still byte-identical
# ---------------------------------------------------------------------------------------------


@pytest.mark.determinism
def test_fresh_process_analyses_are_identical_across_varied_hash_seeds() -> None:
    """`code_pipeline` re-analysed in 8 fresh interpreters, each at a different hash seed.

    A bare `set`/`dict` iteration anywhere in the pipeline would show up here as divergent
    output even when the in-process test above (one hash seed, fixed for the whole pytest
    process) cannot see it at all.
    """
    hashes: list[str] = []
    for hash_seed in range(_SUBPROCESS_RUNS):
        env = {"PYTHONHASHSEED": str(hash_seed), "PATH": _subprocess_path()}
        result = subprocess.run(
            [sys.executable, "-m", "tests.analysis.race._subprocess_runner", "code_pipeline"],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        hashes.append(result.stdout.strip())

    assert len(hashes) == _SUBPROCESS_RUNS
    distinct = set(hashes)
    assert len(distinct) == 1, (
        f"NFR-14 FAILED: {len(distinct)} distinct findings-hashes across {_SUBPROCESS_RUNS} "
        f"fresh interpreters at varied PYTHONHASHSEED: {sorted(distinct)}"
    )
    assert hashes[0].startswith("blake2b:")


def _subprocess_path() -> str:
    """Return a minimal PATH for the child so `sys.executable` and its shared libs resolve."""
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


# ---------------------------------------------------------------------------------------------
# Order: findings come back sorted by the documented key, not discovery order
# ---------------------------------------------------------------------------------------------


def test_findings_are_sorted_by_key_then_subtype_then_slots_not_discovery_order() -> None:
    """`_dedupe`'s documented sort key - not the order conflicts happened to be appended in."""
    # Two independent races on two keys, deliberately appended out of alphabetical order.
    events = [
        state_write(
            seq=0,
            virtual_ts_ms=0,
            vclock={},
            causal_parents=(),
            agent_id="a",
            span_id="s",
            key="z_key",
            value="v1",
        ),
        state_write(
            seq=1,
            virtual_ts_ms=1,
            vclock={},
            causal_parents=(),
            agent_id="b",
            span_id="s",
            key="z_key",
            value="v2",
        ),
        state_write(
            seq=2,
            virtual_ts_ms=2,
            vclock={},
            causal_parents=(),
            agent_id="a",
            span_id="s",
            key="a_key",
            value="w1",
        ),
        state_write(
            seq=3,
            virtual_ts_ms=3,
            vclock={},
            causal_parents=(),
            agent_id="b",
            span_id="s",
            key="a_key",
            value="w2",
        ),
    ]
    findings = detect_conflicts(events)
    assert [f.key for f in findings] == ["a_key", "z_key"]
