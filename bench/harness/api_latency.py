#!/usr/bin/env python3
"""PRD §26.3: API endpoint latency. Writes `bench/results/api-latency.json`.

**What is measured, stated precisely, because §26.3's table names a p95 target and this
harness cannot honestly produce a p95 from a handful of samples run once on one machine.**
Every configuration below is run `repeats` times against a real `TestClient` (real FastAPI
routing, dependency injection, and Pydantic response validation — not a bypassed handler
function) over a real SQLite-backed `Store`, and the **slowest** sample is reported, matching
this project's other two benchmarks (`sdk-overhead.json`, `store-write-throughput.json`).
"Slowest of a few repeats" is a coarser statistic than "p95 under load" — reported as such,
not relabelled to match the PRD's own column header.

**Fixture size.** No committed fixture under `tests/golden/` or `fixtures/` reaches the
"5 000 events"/"5 000 spans" load assumptions §26.3 states — the real fixtures are 40-64
events (`tests/golden/*.jsonl`), sized for readable golden-file diffs, not load testing. This
harness therefore generates synthetic logs at the PRD's own stated scale, the same choice
`store-write-throughput.json` already made for NFR-10's 100 000-event target: `flat_log` (a
`build_log_of_length`-style multi-agent log, ≥6 000 events, real `state_write`s across
several keys) for `GET /runs/{id}`, `/events`, `/findings`, `/state`, and the WS backlog case
(§26.3's own "5 000 events" line item); `waterfall_log` (5 000 independent `llm_call`-kind
leaf spans, each a real `span_start`/`llm_call`/`span_end` triple, round-robined over 3
agents — 15 002 raw events, because a real leaf span costs 3, not 1) for
`GET /runs/{id}/waterfall`, matching §26.3's "5 000 spans" line item on its own axis rather
than reusing the events-scaled fixture for a target stated in spans.

**`POST /runs` is measured on the refusal path, not a real launch.** This build's default
`agentdx ui` wiring has no `RunLauncher` (see `docs/api.md` "What this build does not yet
do" — the P06/P17 `RunHost` gap, left unresolved by owner instruction) — every `POST /runs`
in this codebase's own default configuration returns `503 E-RUN-503` before touching a
launcher at all. That refusal still exercises real FastAPI routing, Pydantic body validation
and dependency resolution, so it is timed and reported, but it is **not** a measurement of
"returns before execution" (§26.3's own load assumption for this row) — there is no
execution path in this build to return before. Reported honestly as `not_measured`, not
silently presented as if it met the row.

Rule E1 (AGENTS.md §6): the JSON this writes is the file every published API latency number
must cite with a `[bench:api-latency.json]` marker.

Usage: `python bench/harness/api_latency.py [--repeats N]`
Exit codes: 0 always (informational — §26.3 is not gated the way NFR-1/NFR-10 are; see
`gate_status` in the output for which rows this run met).
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from tests.analysis._events import llm_call, run_end, run_start, span_end, span_start  # noqa: E402
from tests.unit.store.conftest import chain  # noqa: E402
from tests.unit.store.factories import build_log_of_length, run_record_for  # noqa: E402

from agentdx.api.app import create_app  # noqa: E402
from agentdx.config import AgentDXConfig  # noqa: E402
from agentdx.events.schema import Event  # noqa: E402
from agentdx.store.sqlite import FindingRecord, RunRecord, Store  # noqa: E402

DEFAULT_REPEATS = 5
FLAT_LOG_TARGET_EVENTS = 6_000
WATERFALL_SPANS = 5_000
# The WS backlog case reuses `flat_events` (>= FLAT_LOG_TARGET_EVENTS) rather than a
# separate constant — see `run_benchmark`'s `ws_run_id` comment for why.

TARGETS_MS: dict[str, float] = {
    "get_run_detail": 50.0,
    "get_events_1000": 100.0,
    "get_waterfall_5000_spans": 150.0,
    "get_findings": 50.0,
    "get_state": 100.0,
    "post_runs": 200.0,
    "ws_backlog_5000_events": 1000.0,
}
"""§26.3's target p95, by this harness's own measurement name. Not gates (see module
docstring) — thresholds a slowest-of-`repeats` sample is compared against for the report."""


def _build_waterfall_log(num_spans: int) -> tuple[Event, ...]:
    """Return a sealed log of `num_spans` independent `llm_call`-kind leaf spans.

    Each span is its own `span_start`(kind=`llm_call`)/`llm_call`/`span_end` triple — a real
    leaf per `analysis.timing._LEAF_KINDS`, not a bare event on a parent span (see
    `docs/api.md`'s waterfall declared-gap note on why that distinction matters). Round-
    robined over 3 agents so `GET .../waterfall` returns multiple lanes, the shape a real
    multi-agent run produces.
    """
    agents = ("planner", "coder", "reviewer")
    events: list[Event] = []
    vclock: dict[str, int] = {}
    virtual_ts = 0
    seq = 0

    def advance(ms: int) -> int:
        nonlocal virtual_ts
        virtual_ts += ms
        return virtual_ts

    events.append(run_start(seq=seq, virtual_ts_ms=advance(0)))
    seq += 1
    for index in range(num_spans):
        agent = agents[index % len(agents)]
        span_id = f"span_{index:06d}"
        parent = seq - 1
        vclock[agent] = vclock.get(agent, 0) + 1
        events.append(
            span_start(
                seq=seq,
                virtual_ts_ms=advance(10),
                vclock=dict(vclock),
                causal_parents=[parent],
                agent_id=agent,
                span_id=span_id,
                kind="llm_call",
                name="llm",
            )
        )
        seq += 1
        vclock[agent] += 1
        events.append(
            llm_call(
                seq=seq,
                virtual_ts_ms=advance(10),
                vclock=dict(vclock),
                causal_parents=[seq - 1],
                agent_id=agent,
                span_id=span_id,
                prompt_tokens=10,
                completion_tokens=5,
            )
        )
        seq += 1
        vclock[agent] += 1
        events.append(
            span_end(
                seq=seq,
                virtual_ts_ms=advance(10),
                vclock=dict(vclock),
                causal_parents=[seq - 1],
                agent_id=agent,
                span_id=span_id,
                duration_virtual_ms=20,
            )
        )
        seq += 1
    events.append(
        run_end(
            seq=seq,
            virtual_ts_ms=advance(10),
            vclock=dict(vclock),
            causal_parents=[seq - 1],
            virtual_makespan_ms=virtual_ts,
            event_count=seq + 1,
        )
    )
    return tuple(events)


def _populate(store: Store, events: tuple[Event, ...]) -> str:
    record = RunRecord(**run_record_for(events))  # type: ignore[arg-type]
    store.create_run(record)
    batch = chain(events)
    size = store.config.append_batch_size
    for start in range(0, len(batch), size):
        store.append(batch[start : start + size])
    store.seal(record.run_id, batch[-1].this_hash)
    return record.run_id


def _timed(fn: Callable[[], object], *, repeats: int) -> tuple[float, float]:
    """Run `fn()` `repeats` times; return `(best_seconds, worst_seconds)`."""
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return min(samples), max(samples)


def run_benchmark(*, repeats: int) -> dict[str, Any]:
    """Populate synthetic fixtures, time every §26.3 row against a real app, return the report."""
    tmp = Path(tempfile.mkdtemp(prefix="agentdx-bench-"))
    db_path = tmp / "agentdx.db"
    scenarios_dir = tmp / "scenarios"
    scenarios_dir.mkdir()
    empty_toml = tmp / "empty.toml"
    empty_toml.write_text("", encoding="utf-8")
    config = AgentDXConfig.load(config_path=empty_toml, api={"scenarios_dir": scenarios_dir})

    store = Store.open(db_path, config=config.store)
    try:
        flat_events = build_log_of_length(FLAT_LOG_TARGET_EVENTS)
        flat_run_id = _populate(store, flat_events)
        store.upsert_finding(
            FindingRecord(
                finding_id="fnd_bench",
                run_id=flat_run_id,
                type="lost_update",
                severity="critical",
                title="bench finding",
                description="bench finding for GET /findings timing",
                evidence={"event_seqs": [1, 2]},
                analysis_version="1",
            )
        )
        waterfall_events = _build_waterfall_log(WATERFALL_SPANS)
        waterfall_run_id = _populate(store, waterfall_events)
        # `flat_events` (>= FLAT_LOG_TARGET_EVENTS, close to §26.3's literal "5 000 events")
        # is the WS backlog fixture, not `waterfall_events` (15 002 events — 5 000 *spans*,
        # a different axis, needs 3 events each to be real leaf spans): reusing the bigger
        # log would time a 3x-larger backlog than the PRD's own stated load assumption.
        ws_run_id = flat_run_id
    finally:
        store.close()

    app = create_app(config=config, store_path=db_path)
    client = TestClient(app)

    measurements: dict[str, dict[str, float]] = {}

    def measure(name: str, fn: Callable[[], object]) -> None:
        best, worst = _timed(fn, repeats=repeats)
        measurements[name] = {
            "best_ms": round(best * 1000, 3),
            "worst_ms": round(worst * 1000, 3),
            "target_ms": TARGETS_MS[name],
            "met_on_worst": worst * 1000 < TARGETS_MS[name],
        }

    measure("get_run_detail", lambda: client.get(f"/api/runs/{flat_run_id}").raise_for_status())
    measure(
        "get_events_1000",
        lambda: client.get(
            f"/api/runs/{flat_run_id}/events", params={"limit": 1000}
        ).raise_for_status(),
    )
    measure(
        "get_waterfall_5000_spans",
        lambda: client.get(f"/api/runs/{waterfall_run_id}/waterfall").raise_for_status(),
    )
    measure(
        "get_findings", lambda: client.get(f"/api/runs/{flat_run_id}/findings").raise_for_status()
    )
    at_ts = flat_events[-1].virtual_ts_ms
    measure(
        "get_state",
        lambda: client.get(
            f"/api/runs/{flat_run_id}/state", params={"at_virtual_ts": at_ts}
        ).raise_for_status(),
    )

    # POST /runs: refusal path only (see module docstring) — no ValueError on 503, this IS
    # the response this build's default wiring gives, and raise_for_status would turn the
    # thing being measured into a benchmark failure.
    scenario_body = {"scenario_id": "bench-does-not-exist"}
    measure("post_runs", lambda: client.post("/api/runs", json=scenario_body))

    def drain_ws_backlog() -> None:
        # A real client acks as it consumes: PRD §26.2's flow control pauses the server
        # after `ws_flow_control_max_unacked` (default 5 000) unacked events, and
        # `waterfall_run_id`'s backlog (15 002 events: run_start + 5 000 * 3-event spans +
        # run_end) is 3x that — a drain loop that never acks would stall on flow control
        # and eventually get closed by the idle-heartbeat timeout, timing that stall
        # instead of the backlog send itself.
        with client.websocket_connect(f"/ws/runs/{ws_run_id}") as ws:
            ws.receive_json()  # hello
            ws.send_json({"type": "subscribe", "from_seq": 0})
            seen = 0
            total = len(flat_events)
            while seen < total:
                msg = ws.receive_json()
                if msg["type"] == "events":
                    seen += len(msg["events"])
                    last_seq = msg["events"][-1]["seq"]
                    ws.send_json({"type": "ack", "through_seq": last_seq})
            ws.send_json({"type": "unsubscribe"})

    measure("ws_backlog_5000_events", drain_ws_backlog)

    return {
        "benchmark": "api-latency",
        "requirement": "PRD §26.3",
        "requirement_text": "Endpoint p95 latency targets under the stated load assumptions",
        "method": (
            "Each row is run `repeats` times against a real `fastapi.testclient.TestClient` "
            "(real routing, dependency injection, Pydantic response validation) over a real "
            "SQLite-backed Store; the SLOWEST sample is reported. This is a coarser statistic "
            "than the PRD's own 'p95' column — see 'not_measured'."
        ),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "flat_log_events": len(flat_events),
            "waterfall_log_spans": WATERFALL_SPANS,
            "repeats": repeats,
        },
        "measurements": measurements,
        "gate_status": (
            "Informational, not a hard CI gate (§26.3 is a performance expectation, not an "
            "NFR with its own pass/fail requirement elsewhere in the PRD). See each row's "
            "'met_on_worst' for whether the slowest observed sample beat the stated target "
            "on this run's machine."
        ),
        "not_measured": [
            "p95 under concurrent load — every row is `repeats` sequential single-connection "
            "TestClient calls on one process, not a load-test percentile.",
            "post_runs measures this build's 503-refusal path (no RunLauncher wired into "
            "the default `agentdx ui` — see docs/api.md). §26.3's load assumption for this "
            "row, 'returns before execution', does not apply: there is no execution path in "
            "this build to return before.",
            "No committed fixture reaches the PRD's stated 5 000-event/span load assumption "
            "— flat_log and waterfall_log are synthetic logs generated at that scale by this "
            "harness, the same choice store-write-throughput.json made for NFR-10.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark, write `bench/results/api-latency.json`, print a one-line summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    args = parser.parse_args(argv)

    result = run_benchmark(repeats=args.repeats)
    out_path = REPO_ROOT / "bench" / "results" / "api-latency.json"
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for name, row in sorted(result["measurements"].items()):
        flag = "OK" if row["met_on_worst"] else "OVER"
        sys.stderr.write(
            f"{name}: worst={row['worst_ms']}ms target={row['target_ms']}ms [{flag}]\n"
        )
    sys.stderr.write(f"wrote {out_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
