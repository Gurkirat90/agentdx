"""NFR-13: a crashed run leaves a readable, analysable partial log, 100% of the time.

Design constraint 2, and the one the prompt flags as easy to skip and expensive to discover
late. So it is tested the only way that proves anything: a real subprocess, writing through
a real `EventWriter` into a real store, killed with `SIGKILL` mid-run. `SIGKILL` cannot be
caught, so no cleanup handler runs and no buffer is flushed on the way out — what survives
is exactly what WAL plus one-transaction-per-batch provides.

"Readable" and "analysable" are asserted rather than asserted-about:

* the surviving events decode and pass `validate_log`;
* the hash chain verifies over the prefix, with no gap;
* the run row still says `running` with a null `sealed_at`, so nothing mistakes it for
  a complete run;
* §20.4 state reconstruction works, and its snapshots agree with a pure replay;
* the §27.4 `spans` view returns rows — an unterminated span at the tail simply does not
  appear, which is the honest answer rather than a truncated duration.

The test repeats the kill several times at different points, because "100% of the time" is
a claim about every kill point, not about one lucky one.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agentdx.config import StoreConfig
from agentdx.events.schema import EventType
from agentdx.events.validators import validate_log
from agentdx.store import duckdb as analytics
from agentdx.store.snapshots import rebuild_snapshots, state_at, state_by_replay, stored_snapshots
from agentdx.store.sqlite import Store

BATCH_SIZE = 8
TOTAL_EVENTS = 400
KILL_AFTER_FLUSHES = (1, 2, 4, 7)
"""Kill points, in flush boundaries. Several rather than one, because NFR-13 is a claim
about every crash point and a single sample would only prove it for that one."""

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="SIGKILL semantics are POSIX; Windows is unsupported in v1"
)


def _run_and_kill(db_path: Path, flushes: int) -> int:
    """Start the writer, wait for `flushes` flush boundaries, SIGKILL it, return the count.

    Returns the number of events the child reported having flushed before it died. That is
    a *lower bound* on what must survive — the store may legitimately hold more, never less.
    """
    script = Path(__file__).parent / "crash_writer.py"
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, str(script), str(db_path), str(TOTAL_EVENTS), str(BATCH_SIZE)],
        stdout=subprocess.PIPE,
        text=True,
    )
    seen = 0
    flushed = 0
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if not line.startswith("flushed "):
                continue
            flushed = int(line.split()[1])
            seen += 1
            if seen >= flushes:
                break
        os.kill(process.pid, signal.SIGKILL)
    finally:
        process.wait(timeout=30)
    assert process.returncode == -signal.SIGKILL, (
        f"the writer exited with {process.returncode} rather than being killed; the test "
        f"did not exercise a crash"
    )
    return flushed


@pytest.mark.parametrize("flushes", KILL_AFTER_FLUSHES)
def test_a_sigkilled_run_leaves_a_valid_partial_log(tmp_path: Path, flushes: int) -> None:
    """The surviving prefix decodes, validates, and verifies its own hash chain."""
    db_path = tmp_path / "agentdx.db"
    flushed = _run_and_kill(db_path, flushes)

    with Store.open(db_path, config=StoreConfig(snapshot_interval_events=5)) as store:
        runs = store.list_runs()
        assert len(runs) == 1
        run_id = runs[0].run_id

        events = tuple(store.read_events(run_id))
        assert len(events) >= flushed, "a committed batch did not survive the kill"
        assert [e.seq for e in events] == list(range(len(events))), "the prefix has a gap"

        validate_log(events)
        assert store.verify_chain(run_id) is None


@pytest.mark.parametrize("flushes", KILL_AFTER_FLUSHES)
def test_a_sigkilled_run_is_not_mistaken_for_a_complete_one(tmp_path: Path, flushes: int) -> None:
    """The run stays `running` with a null `sealed_at` and no canonical log hash.

    A partial log that presented itself as sealed would be far worse than a lost one: every
    downstream comparison would treat a truncated run as a finished one.
    """
    db_path = tmp_path / "agentdx.db"
    _run_and_kill(db_path, flushes)

    with Store.open(db_path) as store:
        record = store.list_runs()[0]
        assert record.sealed_at is None
        assert record.status == "running"
        assert record.canonical_log_hash is None
        events = tuple(store.read_events(record.run_id))
        assert all(e.type is not EventType.RUN_END for e in events)


@pytest.mark.parametrize("flushes", KILL_AFTER_FLUSHES)
def test_a_sigkilled_run_is_still_analysable(tmp_path: Path, flushes: int) -> None:
    """The partial log answers the §27.4 span view and §20.4 state reconstruction.

    This is the "analysable" half of NFR-13. An unterminated span at the tail contributes no
    row to `spans` — an inner join in the PRD's own SQL — which is the honest result: a span
    with no end has no duration.
    """
    db_path = tmp_path / "agentdx.db"
    _run_and_kill(db_path, flushes)

    with Store.open(db_path, config=StoreConfig(snapshot_interval_events=5)) as store:
        run_id = store.list_runs()[0].run_id
        events = tuple(store.read_events(run_id))

        spans = analytics.spans_via_sqlite(store, run_id)
        started = {e.span_id for e in events if e.type is EventType.SPAN_START}
        ended = {e.span_id for e in events if e.type is EventType.SPAN_END}
        assert {s.span_id for s in spans} == started & ended
        assert all(s.end_ms >= s.start_ms for s in spans)

        latest = events[-1].virtual_ts_ms
        assert state_at(store, run_id, latest) == state_by_replay(store, run_id, latest)


@pytest.mark.parametrize("flushes", KILL_AFTER_FLUSHES)
def test_snapshots_of_a_killed_run_never_describe_events_that_are_not_there(
    tmp_path: Path, flushes: int
) -> None:
    """Snapshots are written in the batch's own transaction, so they cannot outrun the log.

    The dangerous asymmetry would be a snapshot at seq 200 in a log that ends at seq 190 —
    reconstruction would then start from a state the log cannot account for. The reverse,
    events with no snapshot, is always fine and only costs replay time.
    """
    db_path = tmp_path / "agentdx.db"
    _run_and_kill(db_path, flushes)

    with Store.open(db_path, config=StoreConfig(snapshot_interval_events=5)) as store:
        run_id = store.list_runs()[0].run_id
        events = tuple(store.read_events(run_id))
        rows = stored_snapshots(store, run_id)

        assert all(seq <= events[-1].seq for seq in rows), "a snapshot outran the log"
        assert rows == rebuild_snapshots(events, store.config.snapshot_interval_events)
