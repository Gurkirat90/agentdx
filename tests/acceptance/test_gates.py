"""One executable test per PRD §44.1 global MVP acceptance gate (G1-G10).

Every test literally runs the verification command from PRD §44.1's table, in the repo
root, and asserts exit code 0 — the CONTEXT.md definition of "gate met." No test here
reaches into the product's internals; the whole point is that these are indistinguishable
from what a human or CI runs by hand. See `tests/acceptance/__init__.py` for the honest,
as-of-2026-08-27 account of which gates are expected to fail and why.

These tests are deliberately excluded from the default `just test` / `pytest` collection
(pytest.ini's `testpaths = ["tests"]` still picks them up unless a caller passes
`--ignore=tests/acceptance`, which `just test` does NOT do — see the new `acceptance`
justfile recipe, which runs *only* this directory). They are slow (several spawn
subprocesses, one drives Playwright, one drives Docker) and several are meant to fail
until their underlying gap (tracked in CONTEXT.md §5/§6) closes — mixing them into the
routine `just ci`/`just test` loop would make routine development red for reasons no
single commit can fix, which is its own kind of alarm fatigue this project's culture
(AGENTS.md §8) argues against.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Where a gate's own results (exit code, stdout/stderr tail) are written, one JSON file per
# gate, so `just acceptance`'s table-printing step (scripts/print_acceptance_table.py) has a
# single source of truth that does not depend on parsing pytest's own terminal output.
RESULTS_DIR = REPO_ROOT / "tests" / "acceptance" / ".results"


def _run_gate(
    gate_id: str, command: Sequence[str], *, timeout_s: float = 300, cwd: Path = REPO_ROOT
) -> None:
    """Run `command` from the repo root and assert it exits 0.

    Guarantees: writes `.results/<gate_id>.json` (command, exit code, truncated
    stdout/stderr, and whether the binary needed to run the command was even found)
    unconditionally — including when the assertion below fails — so `just acceptance`'s
    table reflects a real attempt, not a silent skip. Never suppresses or reinterprets a
    non-zero exit; a stub, a missing subcommand and a deadlock all show up as the same
    honest FAIL a human would see running the command by hand.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    binary = command[0]
    if shutil.which(binary) is None and not (cwd / binary).exists():
        result = {
            "gate": gate_id,
            "command": " ".join(command),
            "found_binary": False,
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": f"'{binary}' not found on PATH — command never ran.",
        }
        (RESULTS_DIR / f"{gate_id}.json").write_text(json.dumps(result, indent=2))
        pytest.fail(f"{gate_id}: '{binary}' not found on PATH — command never ran.")

    try:
        proc = subprocess.run(  # noqa: S603
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        returncode: int | None = proc.returncode
        stdout_tail = proc.stdout[-4000:]
        stderr_tail = proc.stderr[-4000:]
    except subprocess.TimeoutExpired as exc:
        returncode = None
        stdout_tail = (exc.stdout or b"").decode(errors="replace")[-4000:] if isinstance(exc.stdout, bytes) else str(exc.stdout or "")[-4000:]
        stderr_tail = f"TIMEOUT after {timeout_s}s"

    result = {
        "gate": gate_id,
        "command": " ".join(command),
        "found_binary": True,
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }
    (RESULTS_DIR / f"{gate_id}.json").write_text(json.dumps(result, indent=2))

    assert returncode == 0, (
        f"{gate_id}: '{' '.join(command)}' exited {returncode}, not 0.\n"
        f"--- stderr tail ---\n{stderr_tail}\n--- stdout tail ---\n{stdout_tail}"
    )


@pytest.mark.acceptance
def test_g1_seeded_race_is_detected() -> None:
    """PRD §44.1 G1: `code_pipeline` yields >=1 `lost_update` race finding.

    Known-red as of 2026-08-27 (CONTEXT.md §7, P17 row): `agentdx run` deadlocks on
    `code_pipeline` because nothing in `sdk/` calls `Scheduler.spawn()` for LangGraph's
    parallel fan-out (exit 5, not the assertion failure this command is meant to probe).
    The underlying race-detection algorithm this gate is really about is independently
    covered, off the CLI path, by `tests/analysis/race/test_gate_g1.py` against the
    golden `code_pipeline` log — that is a different, narrower claim than this gate's
    literal command, and this test does not substitute one for the other.
    """
    _run_gate(
        "G1",
        ["agentdx", "run", "fixtures/code_pipeline", "--assert", "findings.race >= 1"],
    )


@pytest.mark.acceptance
def test_g2_zero_false_positives_on_healthy_fixture() -> None:
    """PRD §44.1 G2: zero false positives on `research_fanout` (100 replays + k=2 frontier).

    Never waived (PRD §44.3 item 1, CONTEXT.md §6). `tests/false_positives/` is the
    mandatory §33.9 suite; no skip markers, no xfail, per AGENTS.md and the suite's own
    module docstrings.
    """
    _run_gate("G2", ["pytest", "tests/false_positives/", "-q"])


@pytest.mark.acceptance
def test_g3_deterministic_replay_100_of_100() -> None:
    """PRD §44.1 G3: 100/100 deterministic replay at seed 42, >=10 fresh processes.

    Never waived (PRD §44.3 item 2, CONTEXT.md §6): "the entire product claim rests on
    it." Known-red in *this specific sandbox* only (not a CI claim): the fresh-process
    replicas re-exec `sys.executable` with a near-empty environment, and this sandbox has
    no system-wide Python >=3.11, so the child hits `ModuleNotFoundError: No module named
    'tomllib'` before it can even attempt the replay — an environment artifact confirmed
    by inspecting the captured subprocess stderr, not a product defect. Expected to pass
    in the real CI job, which runs Python 3.12 project-wide.
    """
    _run_gate("G3", ["pytest", "tests/determinism/test_replay_equality.py"])


@pytest.mark.acceptance
def test_g4_fault_injection_reproduces_failure() -> None:
    """PRD §44.1 G4: killing `reviewer` at t=3000 reproduces the same cascade, 20/20.

    Known-red as of 2026-08-27: the literal command's `scenario run ... --repeat 20`
    invocation does not exist as a CLI surface. P17's real `scenario` subcommands are
    `validate`/`list`/`expand`/`new` (CONTEXT.md §7); no `--repeat` flag exists in
    §37.1/§22.1's grammar. The same underlying cascade-reproduction property is
    demonstrated, off the CLI path, by `tests/integration/faults/test_gate_g4.py` against
    a hand-authored `Scheduler` harness — again, a narrower claim than this literal gate.
    """
    _run_gate(
        "G4",
        ["agentdx", "scenario", "run", "scenarios/kill_reviewer.yaml", "--repeat", "20"],
    )


@pytest.mark.acceptance
def test_g5_critical_path_decomposition_invariant() -> None:
    """PRD §44.1 G5: sum(six overhead buckets) + critical path = makespan, within +-2%."""
    _run_gate("G5", ["pytest", "tests/analysis/test_decomposition_invariant.py"])


@pytest.mark.acceptance
def test_g6_baseline_comparison_works() -> None:
    """PRD §44.1 G6: a single-agent baseline is generated with a comparability grade.

    Known-red as of 2026-08-27: `agentdx compare` is an explicit P17 stub that exits 2
    ("not yet implemented"), correctly out of P17's declared scope (CONTEXT.md §6 row G6).
    `<run_id>` is a placeholder per PRD §44.1's own literal command text; no run_id can
    make a stub succeed, so no attempt is made to resolve a real one here.
    """
    _run_gate("G6", ["agentdx", "compare", "<run_id>", "--baseline"])


@pytest.mark.acceptance
def test_g7_speedup_verdict_scorecard() -> None:
    """PRD §44.1 G7: the FR-8 scorecard prints achieved/ideal speedup + attribution.

    Known-red as of 2026-08-27: `agentdx analyze` is an explicit P17 stub (exit 2),
    correctly out of P17's declared scope (CONTEXT.md §6 row G7). See G6's docstring for
    why `<run_id>` is left as the literal placeholder.
    """
    _run_gate("G7", ["agentdx", "analyze", "<run_id>", "--scorecard"])


@pytest.mark.acceptance
def test_g8_control_tower_renders_and_cross_highlights() -> None:
    """PRD §44.1 G8: Control Tower renders the full workflow, Playwright e2e (3 fixtures).

    Requires `frontend/`'s npm toolchain (`npm ci` under `frontend/`) and a browser
    Playwright can drive; neither is guaranteed present outside CI. `found_binary` in
    `.results/G8.json` will be `False` if `npm` itself is missing, which is a setup gap,
    not this gate's own signal.
    """
    _run_gate(
        "G8",
        ["npm", "run", "test:e2e"],
        timeout_s=600,
        cwd=REPO_ROOT / "frontend",
    )


@pytest.mark.acceptance
def test_g9_offline_demo() -> None:
    """PRD §44.1 G9: the full three-fixture demo works offline, no API keys present.

    Never waived (PRD §44.3 maps this to I7). Known-red as of 2026-08-27: `just
    demo-offline` calls `agentdx run fixtures/...` directly, so it inherits G1's `sdk/`
    fan-out deadlock the instant a real (non-golden-log) run is attempted.
    """
    _run_gate("G9", ["just", "demo-offline"], timeout_s=180)


@pytest.mark.acceptance
def test_g10_docker_demo_under_180s_cold() -> None:
    """PRD §44.1 G10: `docker compose up` reaches healthy `/api/health` in <180s, cold, arm64.

    Known-red as of 2026-08-27: `bench/harness/docker_cold_start.py`, the script `just
    bench-docker-cold` calls, does not exist. No Docker build has been attempted by this
    prompt — implementing it would be a new feature, out of this prompt's scope; the gap
    is reported, per the prompt's own OUT OF SCOPE instruction, not silently filled.
    """
    _run_gate("G10", ["just", "bench-docker-cold"], timeout_s=200)
