"""tests/golden/fixtures_runner.py — the one-command comparison harness `just fixtures-check` runs.

Runs each of the three P05 fixtures through `fixtures._harness.FixtureRunHost` and compares
the result's **canonical log hash** (`agentdx.events.canonical.canonical_log_hash` — the same
function gate G3 uses) against the committed golden log in this directory. Canonical hashing
already excludes every `Volatility.VOLATILE` field (`wall_ts_ms`, `host`, `pid`,
`started_at_utc`, `env`, ...) per the schema's own marks (PRD §10.7), so this comparison is
exactly "did the *meaningful* log change," never "did the wall clock differ."

Usage:

    python -m tests.golden.fixtures_runner check         # what `just fixtures-check` runs
    python -m tests.golden.fixtures_runner regenerate     # AGENTS.md §5 standing exception,
                                                            # ADR-001 consequence 2 — see
                                                            # tests/golden/README.md
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from fixtures._harness import FixtureRunHost

import agentdx
from agentdx.events.canonical import canonical_log_hash, decode_event
from agentdx.events.schema import Event

GOLDEN_DIR = Path(__file__).parent
FIXTURE_NAMES = ("code_pipeline", "support_triage", "research_fanout")
DEFAULT_SEED = 42


class FixtureRun(NamedTuple):
    """One executed fixture: its result, its events, and the host that ran it."""

    status: str
    events: list[Event]
    host: FixtureRunHost
    run_id: str


def golden_path(name: str) -> Path:
    """Return the committed golden log path for a fixture."""
    return GOLDEN_DIR / f"{name}.jsonl"


def load_golden(name: str) -> list[Event]:
    """Return the committed golden events for a fixture, decoded."""
    lines = golden_path(name).read_text(encoding="utf-8").splitlines()
    return [decode_event(line) for line in lines if line]


async def run_fixture(name: str, *, seed: int = DEFAULT_SEED) -> FixtureRun:
    """Execute one fixture fresh, through the real SDK + this repo's provisional harness."""
    graph_module = importlib.import_module(f"fixtures.{name}.graph")
    checks_module = importlib.import_module(f"fixtures.{name}.checks")

    with tempfile.TemporaryDirectory() as scratch:
        host = FixtureRunHost(name, work_dir=Path(scratch), checks_module=checks_module)
        previous_host = agentdx.install_runtime(host)
        try:
            instrumented = graph_module.build_graph()
            result = await agentdx.run(
                instrumented,
                task=graph_module.TASK,
                graph_input={"task": graph_module.TASK},
                seed=seed,
            )
        finally:
            agentdx.install_runtime(previous_host)

        out = Path(scratch) / f"{name}.jsonl"
        host.export_jsonl(result.run_id, out)
        events = _decode_file(out)
        return FixtureRun(status=result.status, events=events, host=host, run_id=result.run_id)


def _decode_file(path: Path) -> list[Event]:
    """Decode a canonical JSONL event log from an arbitrary path (not necessarily golden/)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [decode_event(line) for line in lines if line]


async def check_fixture(name: str, *, seed: int = DEFAULT_SEED) -> tuple[bool, str]:
    """Return `(ok, detail)` comparing a fresh run of `name` against its golden log."""
    run = await run_fixture(name, seed=seed)
    golden = load_golden(name)
    fresh_hash = canonical_log_hash(run.events)
    golden_hash = canonical_log_hash(golden)
    if fresh_hash != golden_hash:
        detail = (
            f"{name}: canonical log hash mismatch — fresh={fresh_hash} "
            f"golden={golden_hash} (fresh has {len(run.events)} events, "
            f"golden has {len(golden)})"
        )
        return False, detail
    return True, f"{name}: canonical log hash matches golden ({len(golden)} events)"


async def check_all() -> bool:
    """Check every fixture against its golden log; print a line per fixture; return overall ok."""
    all_ok = True
    for name in FIXTURE_NAMES:
        ok, detail = await check_fixture(name)
        print(("OK  " if ok else "FAIL") + " " + detail)
        all_ok = all_ok and ok
    return all_ok


async def regenerate_all() -> None:
    """Regenerate every fixture's golden log. AGENTS.md §5 standing exception (ADR-001)."""
    for name in FIXTURE_NAMES:
        run = await run_fixture(name)
        destination = golden_path(name)
        with destination.open("w", encoding="utf-8") as fh:
            for event in run.events:
                from agentdx.events.canonical import encode_event

                fh.write(encode_event(event))
                fh.write("\n")
        print(f"regenerated {destination} ({len(run.events)} events, run_id={run.run_id})")


def main() -> int:
    """CLI entry point: `check` (default) or `regenerate`."""
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "regenerate":
        asyncio.run(regenerate_all())
        return 0
    if mode == "check":
        ok = asyncio.run(check_all())
        return 0 if ok else 1
    print(f"unknown mode {mode!r}: use 'check' or 'regenerate'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
