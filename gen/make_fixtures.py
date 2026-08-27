#!/usr/bin/env python3
"""Generate P16's frontend test fixtures from real backend output — never hand-typed JSON.

This is a **recreation** of the `gen/make_fixtures.py` that `frontend/tests/frontend/fixtures/
README.md` (P15) already cites by name for the three `*.waterfall.json` fixtures — that file
was not present in this session's cloud-sandbox transfer of the repository (the same class of
environment artifact CONTEXT.md already records for `op2-audit-p09.md`, §13's P12 OP-2 row: a
file real on the owner's machine, lost in a tarball transfer, not a fabricated citation). It is
rewritten here rather than guessed at from the README's description alone, and now also emits
the fixtures P16 needs: `*.graph.json`, `*.findings.json`, `*.state.json`.

Method, unchanged from P15's for the parts P15 already established (waterfall), extended the
same way for the new files: call the real, shipped FastAPI route **function** in-process
(never a mocked return value) against a temporary SQLite `Store` seeded with one of the three
committed P05 golden logs (`tests/golden/{fixture}.jsonl`), through the real `RunRecord` /
`build_chain` / `ChainedEvent` pipeline every other bench/test harness in this repo already
uses (`bench/harness/store_write_throughput.py`, `tests/unit/store/factories.py`).

`findings.json` cannot be produced by calling a route function, because no route in this build
computes real findings for a run (`GET /api/runs/{id}/findings` is precomputed — reads
`store.list_findings` — and no P17 orchestration pipeline exists yet that ever calls
`store.upsert_finding` for a real run; the same declared gap `scorecard.py`/`ws.py`'s own
docstrings name for the scorecard/verdict pushes). So this script does what a P17 pipeline
would: runs `analysis.race.find_conflicts` for real against the golden log, turns each
resulting `Finding` into a `FindingRecord` via `store.upsert_finding`, and *then* calls the
real `get_findings` route function to serialise it — the same "real code path, temporary
store" method as every other fixture here, just with one more real step because the pipeline
that would normally do it does not exist (P16 ruling **C-32**, `docs/api.md`).

`evidence` shape: PRD §26.1 names it `{event_seqs, span_ids, computation}` for `FindingOut`.
`analysis.race.Finding` does not carry `span_ids` (state ops, not spans) — this script resolves
each evidence `seq`'s span. `fault_id` is carried through so the Findings panel's fault taint
status is real data, not a placeholder (`None` on every golden fixture — none of the three
carries a fault, honestly reported).

**A real gap found while wiring this up, worked around here rather than silently left broken:**
`event.span_id` (the value `finding.evidence_seq`'s own events carry) is *not* always one of
the span ids `get_waterfall` actually renders. This build's golden logs nest spans — an outer
per-agent-turn span (e.g. `span_id=c0263d617570`, seq 4-18) wraps several inner tool-call spans
(`966640563638` seq7-9, `5f55856dac38` seq10-12, `bb943fc038ff` seq14-16) — and a `state_write`
event fired *between* two inner spans carries the **outer** span's id, which `get_waterfall`
does not include as a lane entry at all (confirmed by direct inspection: `code_pipeline`'s real
waterfall has 7 spans across 3 agents; the golden log has 9 span ids total, with 2 outer/
wrapper ones invisible to the waterfall). Using the raw `event.span_id` here would give the
Findings panel an evidence span id no waterfall span ever matches — cross-panel linking (PRD
§20.3, gate G8) would silently never highlight anything for a `state_conflict` finding.
`_nearest_leaf_span` below resolves each evidence seq to the *closest span `get_waterfall`
actually renders* for that seq's agent instead (containing range if one exists, else nearest by
seq distance, ties broken toward the preceding span) — a documented approximation of "which
visible unit of work this write belongs to," not a claim that the write literally occurred
inside that exact span's boundaries. Fixing this so evidence always resolves to a real waterfall
span is properly `get_waterfall`'s or `analysis.race`'s own job (a backend change, out of P16's
"no new analysis" scope) — flagged here, not fixed there.

Usage: `python gen/make_fixtures.py`
Writes into `frontend/tests/frontend/fixtures/`.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from tests.golden.fixtures_runner import load_golden  # noqa: E402
from tests.unit.store.factories import run_record_for  # noqa: E402

from agentdx.analysis.race import find_conflicts  # noqa: E402
from agentdx.api.routes.analysis import get_graph, get_state_at, get_waterfall  # noqa: E402
from agentdx.api.routes.findings import get_findings  # noqa: E402
from agentdx.config import StoreConfig  # noqa: E402
from agentdx.events.canonical import build_chain  # noqa: E402
from agentdx.events.writer import ChainedEvent  # noqa: E402
from agentdx.scenario import loader  # noqa: E402
from agentdx.store.sqlite import FindingRecord, RunRecord, Store  # noqa: E402

OUT_DIR = REPO_ROOT / "frontend" / "tests" / "frontend" / "fixtures"
FIXTURE_NAMES = ("code_pipeline", "research_fanout", "support_triage")


def _write_scenario_fixture(scenario_path: str, out_name: str) -> None:
    """Emit a `ScenarioDetail`-shaped fixture (PRD §26.1) for the Chaos panel's tests.

    Real output of the shipped `scenario.loader.resolve_defaults`, run against a real,
    committed scenario file — never a hand-typed blast-radius/fault shape. This is what
    `GET /api/scenarios/{id}` actually returns for this file today.
    """
    path = REPO_ROOT / scenario_path
    parsed = loader.load_scenario_file(path)
    resolved = loader.resolve_defaults(parsed.data or {})
    scenario_id = path.stem
    payload = {
        "scenario_id": scenario_id,
        "source": "file",
        "path": str(path),
        "content": path.read_text(encoding="utf-8"),
        "content_hash": None,
        "resolved": resolved,
    }
    (OUT_DIR / out_name).write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    sys.stdout.write(f"{out_name}: scenario {scenario_id!r}, {len(resolved.get('faults', []))} declared fault(s)\n")


def _seeded_store(fixture: str, directory: Path) -> tuple[Store, list]:
    events = load_golden(fixture)
    chained = [
        ChainedEvent(event=event, prev_hash=prev, this_hash=this)
        for event, (prev, this) in zip(events, build_chain(events), strict=True)
    ]
    store = Store.open(directory / f"{fixture}.db", config=StoreConfig())
    store.create_run(RunRecord(**run_record_for(events, status="sealed")))  # type: ignore[arg-type]
    store.append(chained)
    return store, events


def _leaf_spans_by_agent(waterfall) -> dict[str, list[tuple[int, int, str]]]:  # noqa: ANN001
    """`{agent_id: [(seq_start, seq_end, span_id), ...]}` for every span `get_waterfall` actually
    renders, in seq order — the real, visible span set `_nearest_leaf_span` resolves against."""
    out: dict[str, list[tuple[int, int, str]]] = {}
    for lane in waterfall.lanes:
        out[lane.agent] = sorted(
            (span.seq_start, span.seq_end, span.span_id) for span in lane.spans
        )
    return out


def _nearest_leaf_span(
    leaf_spans_by_agent: dict[str, list[tuple[int, int, str]]], agent_id: str | None, seq: int
) -> str | None:
    """The closest waterfall-rendered span for one evidence `seq` (see this module's docstring
    for why `event.span_id` itself is not always one of these). Containing range wins outright;
    otherwise nearest by seq distance, ties broken toward the *preceding* span (candidates are
    seq-ordered, so a strict `<` comparison naturally keeps the first/earliest on a tie)."""
    if agent_id is None:
        return None
    candidates = leaf_spans_by_agent.get(agent_id, [])
    best: str | None = None
    best_dist: int | None = None
    for seq_start, seq_end, span_id in candidates:
        if seq_start <= seq <= seq_end:
            return span_id
        dist = (seq - seq_end) if seq_end < seq else (seq_start - seq)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = span_id
    return best


def _findings_for(
    fixture: str,
    run_id: str,
    events: list,
    store: Store,
    leaf_spans_by_agent: dict[str, list[tuple[int, int, str]]],
) -> None:
    """Run the real race detector and persist every real `Finding` via `store.upsert_finding`.

    This is the one step a real P17 pipeline would perform and this build has no orchestration
    prompt to do yet (P16 ruling C-32) — everything downstream of this call (evidence shape,
    serialisation) is the real, shipped `agentdx.api.routes.findings` code.
    """
    agent_by_seq = {e.seq: e.agent_id for e in events}
    conflicts = find_conflicts(events)
    for conflict in conflicts:
        if conflict.suppressed_by is not None:
            continue
        finding = conflict.to_finding()
        span_ids = sorted(
            {
                span_id
                for s in finding.evidence_seq
                if (span_id := _nearest_leaf_span(leaf_spans_by_agent, agent_by_seq.get(s), s))
                is not None
            }
        )
        evidence = {
            "event_seqs": list(finding.evidence_seq),
            "span_ids": span_ids,
            "computation": (
                f"analysis.race.find_conflicts: {finding.subtype} on key {finding.key!r} "
                f"between {finding.agent_a!r} (seq {finding.seq_a}) and "
                f"{finding.agent_b!r} (seq {finding.seq_b})"
            ),
            "fault_id": finding.fault_id,
            "key": finding.key,
            "agent_a": finding.agent_a,
            "agent_b": finding.agent_b,
        }
        store.upsert_finding(
            FindingRecord(
                finding_id=finding.finding_id,
                run_id=run_id,
                type=finding.type,
                subtype=finding.subtype,
                severity=finding.severity,
                title=finding.title,
                description=finding.description,
                evidence=evidence,
                recommendation=finding.recommendation,
                analysis_version="race-v1",
            )
        )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        for fixture in FIXTURE_NAMES:
            store, events = _seeded_store(fixture, directory)
            try:
                run_id = events[0].run_id
                # `get_waterfall` must run before `_findings_for` — the finding evidence's
                # span-id resolution needs the *real, rendered* span set to resolve against
                # (this module's docstring; `_nearest_leaf_span`).
                waterfall = get_waterfall(run_id, store)
                _findings_for(fixture, run_id, events, store, _leaf_spans_by_agent(waterfall))

                graph = get_graph(run_id, store, at_virtual_ts=None)
                findings = get_findings(
                    run_id, store, severity=None, type=None, include_suppressed=True
                )
                # A representative mid-run scrub point: the midpoint of the virtual makespan.
                mid_ts = waterfall.virtual_makespan_ms // 2
                state = get_state_at(run_id, store, at_virtual_ts=mid_ts)

                payload = {
                    "run_id": run_id,
                    "fixture": fixture,
                    "waterfall": json.loads(waterfall.model_dump_json()),
                    "graph": json.loads(graph.model_dump_json(by_alias=True)),
                    "findings": json.loads(findings.model_dump_json()),
                    "state": json.loads(state.model_dump_json()),
                }
                (OUT_DIR / f"{fixture}.waterfall.json").write_text(
                    json.dumps({"run_id": run_id, "fixture": fixture, "waterfall": payload["waterfall"]}, indent=2, sort_keys=True) + "\n"
                )
                (OUT_DIR / f"{fixture}.graph.json").write_text(
                    json.dumps({"run_id": run_id, "fixture": fixture, "graph": payload["graph"]}, indent=2, sort_keys=True) + "\n"
                )
                (OUT_DIR / f"{fixture}.findings.json").write_text(
                    json.dumps({"run_id": run_id, "fixture": fixture, "findings": payload["findings"]}, indent=2, sort_keys=True) + "\n"
                )
                (OUT_DIR / f"{fixture}.state.json").write_text(
                    json.dumps({"run_id": run_id, "fixture": fixture, "state": payload["state"]}, indent=2, sort_keys=True) + "\n"
                )
                sys.stdout.write(
                    f"{fixture}: {len(graph.nodes)} nodes, {len(graph.edges)} edges, "
                    f"{len(findings.findings)} findings, {len(state.keys)} state keys "
                    f"@ virtual_ts={mid_ts}\n"
                )
            finally:
                store.close()

    _write_scenario_fixture("scenarios/kill_reviewer.yaml", "kill_reviewer.scenario.json")
    _write_scenario_fixture(
        "scenarios/reviewer_crash_midflight.yaml", "reviewer_crash_midflight.scenario.json"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
