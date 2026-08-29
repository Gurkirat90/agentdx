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
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Where a gate's own results (exit code, stdout/stderr tail) are written, one JSON file per
# gate, so `just acceptance`'s table-printing step (scripts/print_acceptance_table.py) has a
# single source of truth that does not depend on parsing pytest's own terminal output.
RESULTS_DIR = REPO_ROOT / "tests" / "acceptance" / ".results"


def _run_gate(
    gate_id: str,
    command: Sequence[str],
    *,
    timeout_s: float = 300,
    cwd: Path = REPO_ROOT,
    min_pytest_passed: int | None = None,
) -> None:
    """Run `command` from the repo root and assert it exits 0.

    Guarantees: writes `.results/<gate_id>.json` (command, exit code, truncated
    stdout/stderr, and whether the binary needed to run the command was even found)
    unconditionally — including when the assertion below fails — so `just acceptance`'s
    table reflects a real attempt, not a silent skip. Never suppresses or reinterprets a
    non-zero exit; a stub, a missing subcommand and a deadlock all show up as the same
    honest FAIL a human would see running the command by hand.

    `min_pytest_passed`, when given (G2/G3 — PRD §44.3's two never-waived gates — pass
    this), appends `--junit-xml=<results_dir>/<gate_id>-junit.xml` to `command` and reads
    the resulting report's `<testsuite tests=".." failures=".." errors=".." skipped="..">`
    attributes to require at least `min_pytest_passed` real passes. This closes a real gap
    found by independent review 2026-08-29: a subprocess that exits 0 because every test
    inside it was silently skipped (a broken fixture, an environment guard, anything short
    of zero tests collected — which is the only all-skip case pytest itself treats as
    non-zero, exit 5) would otherwise report this gate PASS with zero real assertions
    having run. An exit code alone cannot distinguish "the property held" from "nothing was
    actually checked."

    **Why JUnit XML and not the terminal summary text (corrected 2026-08-29, same day as
    the first version, D-71):** the first version of this parameter regex-matched pytest's
    human-readable `"N passed"` summary line out of captured stdout. Verified live against
    this project's own `tests/false_positives/` (real Python 3.12, real pytest 8.4.2, repo
    owner's machine): the subprocess exited 0, all 13 dots printed clean (`.............`
    `[100%]`, no `F`, no `s`), yet the captured stdout ended right there — no "N passed"
    line at all, confirmed by reading the raw `.results/G2.json` this function itself
    wrote, not by re-reading pytest's own truncated failure display. The cause was not
    root-caused (no `console_output_style` override in `pyproject.toml`; likely a
    conftest-level terminal-reporter customisation somewhere under `tests/`) — rather than
    guess a second regex, this switches to `--junit-xml`, a structured report pytest
    generates independently of whatever silences its terminal summary text.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    junit_path: Path | None = None
    if min_pytest_passed is not None:
        junit_path = RESULTS_DIR / f"{gate_id}-junit.xml"
        junit_path.unlink(missing_ok=True)  # stale file from a prior run must not be read as new
        command = [*command, f"--junit-xml={junit_path}"]

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

    # PYTHONHASHSEED=0 is required project-wide (AGENTS.md §4.1), not just inside a `just`
    # recipe -- `just`'s own `export PYTHONHASHSEED := "0"` (justfile line 11) is *a* way to
    # set it, not *the* requirement. Found 2026-08-27 by the repo owner running this suite
    # directly (`pytest tests/acceptance/...`, bypassing `just`): without it, G3's literal
    # command correctly, loudly refuses to run (`E-SCHED-004`, `DeterminismLeakError`) rather
    # than risk a false determinism claim -- exactly as designed. That is not a G3 defect;
    # it is this harness failing to reproduce the one environment guarantee every other gate
    # command already gets for free when launched via `just acceptance`. Setting it here
    # makes `pytest tests/acceptance/ -m acceptance` self-sufficient regardless of how it is
    # invoked, matching every other gate's real command exactly as a user would run it by
    # hand with the seed pinned (which the project requires unconditionally, not optionally).
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    try:
        proc = subprocess.run(  # noqa: S603
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
        returncode: int | None = proc.returncode
        stdout_tail = proc.stdout[-4000:]
        stderr_tail = proc.stderr[-4000:]
    except subprocess.TimeoutExpired as exc:
        returncode = None
        stdout_tail = (
            (exc.stdout or b"").decode(errors="replace")[-4000:]
            if isinstance(exc.stdout, bytes)
            else str(exc.stdout or "")[-4000:]
        )
        stderr_tail = f"TIMEOUT after {timeout_s}s"

    passed_count: int | None = None
    junit_parse_error: str | None = None
    if min_pytest_passed is not None:
        assert junit_path is not None  # set together, above
        if not junit_path.exists():
            # The binary crashed, was killed, or never got far enough to write a report --
            # 0 is the safe, honest reading: "nothing proven to have passed," not a parse
            # error to shrug off or treat as inconclusive.
            passed_count = 0
            junit_parse_error = f"{junit_path.name} was not written by the subprocess"
        else:
            try:
                # Suppression justified below: junit_path is written by the pytest
                # subprocess this same function just launched (a trusted local tool we
                # invoked), not attacker-supplied input -- the XXE/entity-expansion risk
                # stdlib `xml.etree` carries for untrusted data does not apply here.
                root = ET.parse(junit_path).getroot()  # noqa: S314
                # pytest emits either <testsuite> at the root or <testsuites><testsuite>.
                suite = root if root.tag == "testsuite" else root.find("testsuite")
                if suite is None:
                    passed_count = 0
                    junit_parse_error = f"no <testsuite> element in {junit_path.name}"
                else:
                    total = int(suite.get("tests", "0"))
                    failures = int(suite.get("failures", "0"))
                    errors = int(suite.get("errors", "0"))
                    skipped = int(suite.get("skipped", "0"))
                    passed_count = total - failures - errors - skipped
            except ET.ParseError as exc:
                passed_count = 0
                junit_parse_error = f"{junit_path.name} failed to parse: {exc}"

    result = {
        "gate": gate_id,
        "command": " ".join(command),
        "found_binary": True,
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "pytest_passed_count": passed_count,
        "pytest_junit_parse_error": junit_parse_error,
    }
    (RESULTS_DIR / f"{gate_id}.json").write_text(json.dumps(result, indent=2))

    assert returncode == 0, (
        f"{gate_id}: '{' '.join(command)}' exited {returncode}, not 0.\n"
        f"--- stderr tail ---\n{stderr_tail}\n--- stdout tail ---\n{stdout_tail}"
    )
    if min_pytest_passed is not None:
        assert passed_count is not None and passed_count >= min_pytest_passed, (
            f"{gate_id}: '{' '.join(command)}' exited 0, but only {passed_count} pytest "
            f"test(s) actually passed per {junit_path}'s <testsuite> counts (expected "
            f">= {min_pytest_passed}). A gate that exits 0 with nothing real behind it "
            f"(e.g. every test silently skipped) is a false PASS, not a real one."
            + (f"\n--- junit parse note ---\n{junit_parse_error}" if junit_parse_error else "")
            + f"\n--- stdout tail ---\n{stdout_tail}"
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
    module docstrings. `min_pytest_passed=1` (added 2026-08-29, see `_run_gate`'s own
    docstring) additionally requires at least one real pytest pass, so a silently-all-
    skipped run cannot report this never-waived gate PASS on exit code alone.
    """
    _run_gate("G2", ["pytest", "tests/false_positives/", "-q"], min_pytest_passed=1)


@pytest.mark.acceptance
def test_g3_deterministic_replay_100_of_100() -> None:
    """PRD §44.1 G3: 100/100 deterministic replay at seed 42, >=10 fresh processes.

    Never waived (PRD §44.3 item 2, CONTEXT.md §6): "the entire product claim rests on
    it." Known-red in *this specific sandbox* only (not a CI claim): the fresh-process
    replicas re-exec `sys.executable` with a near-empty environment, and this sandbox has
    no system-wide Python >=3.11, so the child hits `ModuleNotFoundError: No module named
    'tomllib'` before it can even attempt the replay — an environment artifact confirmed
    by inspecting the captured subprocess stderr, not a product defect. Expected to pass
    in the real CI job, which runs Python 3.12 project-wide. `min_pytest_passed=1` (added
    2026-08-29, see `_run_gate`'s own docstring) additionally requires at least one real
    pytest pass, so a silently-all-skipped run cannot report this never-waived gate PASS
    on exit code alone.
    """
    _run_gate("G3", ["pytest", "tests/determinism/test_replay_equality.py"], min_pytest_passed=1)


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

    Never waived (PRD §44.3 maps this to I7). Known-red as of 2026-08-27, corrected same
    day against a real run (Python 3.12, real `just`, repo owner's machine): `just
    demo-offline` fails at exit 7, `"no scenario files found under fixtures/code_pipeline"`
    -- a fixture/scenario-resolution gap in `agentdx run`'s target handling, reached before
    the previously-documented `sdk/`-spawn deadlock (G1) ever comes into play. This
    session's first guess (that G9 simply inherits G1's deadlock) was wrong; corrected here
    rather than left stale now that a real run exists to check it against.
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
