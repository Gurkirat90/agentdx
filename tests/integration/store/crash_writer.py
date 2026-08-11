"""A writer subprocess for the NFR-13 kill test. Run by `test_crash_partial_log.py`.

Writes events through a real `EventWriter` into a real `SnapshottingStore`, printing each
flush boundary to stdout so the parent knows how far it got, then blocks forever. The
parent SIGKILLs it. Nothing here cleans up on exit — that is the point: `SIGKILL` cannot be
caught, so whatever survives is what the storage layer's durability actually provides,
not what a shutdown handler tidied up afterwards.

Usage: `python crash_writer.py <db path> <events to write> <batch size>`
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from agentdx.config import StoreConfig  # noqa: E402
from agentdx.events.writer import EventWriter  # noqa: E402
from agentdx.store.snapshots import SnapshottingStore  # noqa: E402
from agentdx.store.sqlite import RunRecord  # noqa: E402
from tests.unit.store.factories import build_log_of_length, run_record_for  # noqa: E402


def main() -> None:
    """Write events until killed, announcing each flush so the parent can time the kill."""
    db_path = Path(sys.argv[1])
    total = int(sys.argv[2])
    batch_size = int(sys.argv[3])

    events = build_log_of_length(total)
    store = SnapshottingStore.open(
        db_path, config=StoreConfig(snapshot_interval_events=5, append_batch_size=batch_size)
    )
    store.create_run(RunRecord(**run_record_for(events)))  # type: ignore[arg-type]

    writer = EventWriter(events[0].run_id, store, batch_size=batch_size)
    for event in events:
        if event.type.value == "run_end":
            break  # never seal: this process is going to be killed mid-run
        writer.write(event)
        if (event.seq + 1) % batch_size == 0:
            sys.stdout.write(f"flushed {event.seq + 1}\n")
            sys.stdout.flush()
    sys.stdout.write("done\n")
    sys.stdout.flush()
    while True:  # wait to be killed
        time.sleep(0.05)


if __name__ == "__main__":
    main()
