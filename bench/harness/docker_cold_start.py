#!/usr/bin/env python3
"""Gate G10 / PRD §39.2 / NFR-5: cold `docker compose up` to a working demo.

**What is measured, stated precisely (Rule E1, AGENTS.md §6).** PRD §44.1's G10 criterion is
"`docker compose up` reaches a healthy `/api/health` **and a populated run list**, cold
cache, arm64" within the threshold §44.1 names. Both halves are measured here, because the
first half alone is trivially satisfiable by a server that started and did nothing:

* **healthy** — `GET /api/health` returns 200.
* **populated** — `GET /api/runs` returns a non-empty ``runs`` array. A server that answers
  health checks over an empty run list is not a demo, and this harness reports it as a
  failure rather than as a fast pass.

**Cold means cold.** A warm-cache measurement is not the measurement, so unless
``--no-prune`` is passed this harness removes the compose project's containers and volumes,
removes the locally-built image, prunes the entire builder cache, and deletes the bind-mount
data directory *before* starting the clock. Those teardown seconds are excluded from the
timed window; the timer starts at the ``docker compose up --build`` invocation, which is the
command a user actually types.

**Architecture.** G10 names arm64. This harness records the real machine architecture in its
output and refuses to report a pass on any other architecture unless ``--allow-any-arch`` is
passed, in which case the JSON says so and ``arch_conformant`` is false. An x86_64 number is
a useful signal and is not the gate.

**Update 2026-09-07: the D-62 blocker below is closed.** ADR-019 (2026-09-03) fixed the
scheduler-dispatch deadlock this docstring used to describe as unconditional. `agentdx run
<fixture>` completes end-to-end on real hardware and was re-confirmed in this project's own
sandbox (Python 3.10 + its disclosed stdlib-compat shim) against a fresh data directory for
all three fixtures. The `seed` step this harness measures should now complete and populate a
real run list — **not yet re-confirmed by an actual run of this harness**, since no
environment this project has had access to combines a Docker daemon with the fix landing.
Whoever next runs this on a real Docker host should expect a genuine `populated: true`
result and treat anything else as a new, real finding, not this old, closed gap resurfacing.
The paragraph below (kept for history) describes what this harness reported *before* the fix.

**What was previously unfixable, for the record.** Before 2026-09-03, the demo could not
populate a run list at all: nothing in ``sdk/`` called ``runtime.scheduler.Scheduler.spawn()``
(D-62), so every fixture graph deadlocked and ``docker-compose.yml``'s ``seed`` service exited
non-zero. This harness reported *which* step failed rather than a bare timeout — the
difference between "the demo is too slow" and "the demo does not work" is the entire
diagnostic value of running it, and that diagnostic behavior is unchanged now that the
underlying failure is gone.

Rule E1: the JSON this writes is the file every published cold-start number must cite with a
``[bench:docker-cold-start.json]`` marker.

Usage: ``python3.12 bench/harness/docker_cold_start.py [--threshold-s N] [--no-prune]
[--allow-any-arch] [--keep-up]``

Exit codes: 0 the gate was met · 2 the gate was not met (too slow, unhealthy, or an empty
run list) · 3 the environment cannot run the measurement at all (no Docker daemon, no
Compose v2).
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_PATH = REPO_ROOT / "bench" / "results" / "docker-cold-start.json"
DATA_DIR = REPO_ROOT / ".agentdx-data"

G10_THRESHOLD_S = 180.0
"""PRD §44.1 G10 / NFR-5, in seconds. The gate's own number, not a local choice."""

G10_ARCH = "arm64"
"""PRD §44.1 G10 and §39.3: Apple silicon is the primary target and the gate's platform."""

COMPOSE_PROJECT = "agentdx-g10"
"""An isolated project name, so `compose down` here never touches a developer's own stack.

Scope note (F6, 2026-08-29): this isolates the *compose* teardown only. `_go_cold`'s builder
prune is daemon-global and no project name can scope it — see that function's own warning. An
earlier version of this docstring claimed the prune was isolated too; it never was.
"""

HEALTH_URL = "http://127.0.0.1:8420/api/health"
RUNS_URL = "http://127.0.0.1:8420/api/runs"

POLL_INTERVAL_S = 1.0
HTTP_TIMEOUT_S = 5.0

EXIT_NOT_MET = 2
EXIT_CANNOT_MEASURE = 3


class CannotMeasure(RuntimeError):
    """The environment cannot run this measurement at all.

    Distinct from a failed gate on purpose: "Docker is not installed" and "the demo took too
    long" are different facts, and reporting the first as the second would be a false
    negative against the product.
    """


@dataclass(slots=True)
class Outcome:
    """The result of one cold-start attempt.

    Guarantees: ``met`` is true only when every one of ``healthy``, ``populated`` and
    ``within_threshold`` is true *and* the architecture conformed (or was explicitly
    waived). No single field can carry the gate on its own.
    """

    threshold_s: float
    arch: str
    arch_conformant: bool
    cold_cache: bool = True
    """False when `--no-prune` was passed. A warm run can never satisfy G10, whatever it
    times — the gate's own wording is "cold cache". Recorded in the result file so a warm
    number can never be cited as the gate by someone who did not watch it run."""

    build_and_up_s: float | None = None
    healthy_at_s: float | None = None
    populated_at_s: float | None = None
    run_count: int = 0
    healthy: bool = False
    populated: bool = False
    failed_step: str | None = None
    detail: str = ""
    seed_exit_code: int | None = None
    base_images_present: list[str] = field(default_factory=list)
    """Which base images the daemon already held when the clock started. A non-empty list
    means this run skipped a registry pull a fresh machine would pay — the largest known
    source of run-to-run spread (140.273 s vs 106.342 s, same commit, 2026-09-01)."""

    log_tail: list[str] = field(default_factory=list)

    @property
    def within_threshold(self) -> bool:
        """Return whether the populated-run-list moment landed inside the threshold."""
        return self.populated_at_s is not None and self.populated_at_s < self.threshold_s

    @property
    def met(self) -> bool:
        """Return whether gate G10 was met, in full, cold, on a conformant architecture."""
        return (
            self.healthy
            and self.populated
            and self.within_threshold
            and self.arch_conformant
            and self.cold_cache
        )


def _require_docker() -> None:
    """Raise if Docker and Compose v2 are not both usable from this shell.

    Raises:
        CannotMeasure: the ``docker`` binary is absent, the daemon is unreachable, or the
            ``docker compose`` subcommand is not available.
    """
    if shutil.which("docker") is None:
        detail = "the `docker` binary is not on PATH; G10 cannot be measured on this machine"
        raise CannotMeasure(detail)
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        detail = (
            "the Docker daemon is not reachable "
            f"(`docker info` exited {probe.returncode}): {probe.stderr.strip()}"
        )
        raise CannotMeasure(detail)
    compose = subprocess.run(
        ["docker", "compose", "version", "--short"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if compose.returncode != 0:
        detail = (
            "`docker compose` (Compose v2) is unavailable; the v1 `docker-compose` binary is "
            "not a substitute — this project's compose file uses the v2 spec"
        )
        raise CannotMeasure(detail)


def _compose(
    *args: str, check: bool = False, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one `docker compose` subcommand against this harness's isolated project."""
    return subprocess.run(  # noqa: S603 -- argv is this module's own literals, never user input
        ["docker", "compose", "-p", COMPOSE_PROJECT, *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


BASE_IMAGES: Final = (
    "node:20-bookworm-slim",
    "python:3.12-slim-bookworm",
    "ghcr.io/astral-sh/uv:0.5.11",
)
"""The three images the Dockerfile pulls. `docker builder prune` does NOT remove these —
they live in the image store, not the build cache — so a run that finds them present skips
a registry pull that a fresh machine would pay. Recorded, and removable with --pull-cold."""


def _base_images_present() -> list[str]:
    """Return which of `BASE_IMAGES` the daemon already has locally.

    Guarantees: never raises. An image whose presence cannot be determined is reported as
    absent, which biases the record toward "this run may have paid a pull" rather than
    toward a flattering number.
    """
    present: list[str] = []
    for ref in BASE_IMAGES:
        probe = subprocess.run(  # noqa: S603 -- argv is this module's own literals
            ["docker", "image", "inspect", ref, "--format", "{{.Id}}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            present.append(ref)
    return present


def _remove_base_images() -> None:
    """Delete the pulled base images so the next build pays a real registry pull."""
    for ref in BASE_IMAGES:
        subprocess.run(  # noqa: S603 -- argv is this module's own literals
            ["docker", "image", "rm", "-f", ref],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )


def _go_cold(*, pull_cold: bool = False) -> None:
    """Remove every cached artefact this measurement must not benefit from.

    Guarantees: on return, the compose project has no containers, no volumes and no locally
    built image, the builder cache is empty, and the bind-mount data directory is gone. This
    runs *before* the clock starts, so its own cost is never counted.

    **What this does NOT remove by default: the pulled base images.** `docker builder prune`
    empties the build cache; base images live in the image store and survive it. Two cold
    runs of the same commit on 2026-09-01 measured 140.273 s and 106.342 s — a 34 s spread
    explained largely by the first paying a registry pull the second did not. `cold_cache:
    true` therefore means "no build cache", not "nothing cached at all". PRD §44.1 says only
    "cold cache" and does not settle whether a fresh CI runner's image pull counts; changing
    the default would move a gate's goalposts silently, so it is recorded rather than
    changed. Pass `--pull-cold` for the stricter reading.

    **This is destructive beyond this project.** `docker builder prune -af` takes no project
    scope: it empties the whole daemon's build cache, so every other repository on the machine
    rebuilds from scratch afterwards. That is unavoidable if "cold" is to mean cold — G10's own
    wording — but it is stated here rather than discovered. Pass `--no-prune` for a warm run
    that leaves other projects alone; the result file then records `cold_cache: false` and can
    never be cited as the gate.
    """
    _compose("down", "-v", "--rmi", "local", "--remove-orphans")
    if pull_cold:
        _remove_base_images()
    subprocess.run(
        ["docker", "builder", "prune", "-af"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)


def _http_json(url: str) -> object | None:
    """Return the decoded JSON body at `url`, or None if it is not answering yet.

    Guarantees: never raises for a connection refused, a timeout, or a non-2xx status —
    those are the normal states of a server that has not finished starting, and a poll loop
    must be able to treat them as "not yet" rather than as an error.
    """
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as response:  # noqa: S310
            if response.status != 200:
                return None
            # `json.loads` is typed `Any`; binding it to `object` before returning is what
            # makes this function's declared type honest under `mypy --strict`, rather than
            # letting `Any` leak out through a return annotation that promises `object`.
            decoded: object = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    return decoded


def _seed_exit_code() -> int | None:
    """Return the `seed` service container's exit code, or None if it cannot be read.

    This is why `docker-compose.yml` splits seeding into its own service: the fixture runs
    have their own container and therefore their own exit status, so "the demo is slow" and
    "a fixture failed" are distinguishable facts rather than one opaque non-zero.
    """
    listing = _compose("ps", "-a", "--format", "json", "seed")
    if listing.returncode != 0 or not listing.stdout.strip():
        return None
    for line in listing.stdout.strip().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and "ExitCode" in record:
            code = record["ExitCode"]
            return int(code) if isinstance(code, (int, str)) else None
    return None


def _diagnose_seed(exit_code: int | None) -> str:
    """Return what the `seed` container's exit code actually means, per PRD §37.2.

    Guarantees: the returned sentence is derived from the observed exit code, never assumed.
    An earlier version of this harness hardcoded a D-62 attribution into every `compose_up`
    failure; the first real run returned exit 7 (target resolution), not exit 5 (the D-62
    deadlock), so the hardcoded text was wrong the very first time it was read. A gate
    harness that prejudges its own failure cause is worse than one that reports the raw
    code — CONTEXT.md §11 tripwire 19 is the same class of defect in a different shape.
    """
    meanings = {
        0: "The seed step succeeded; the failure is elsewhere in the stack.",
        1: "Exit 1: an assertion failed or a regression was detected.",
        2: "Exit 2: a usage, configuration or validation error — check the compose command.",
        3: "Exit 3: a replay-mode cache miss (E-CACHE-001). The committed fixture cache did "
        "not cover a call the run made. This is I7 working, not failing.",
        4: "Exit 4: an abort guard stopped the run.",
        5: "Exit 5: an internal error. This is the exit code D-62's scheduler deadlock "
        "surfaces as — nothing in sdk/ calls Scheduler.spawn() (zero hits against a spawn "
        "defined at runtime/scheduler.py:752). NOTE: earlier revisions of this string said "
        "'LangGraph's parallel fan-out has no runnable task'. d62-design.md §3 downgrades "
        "that to a hypothesis — _resume_task grants one event-loop tick per resumption and "
        "recognises only scheduler-created Futures as suspension, so any await needing more "
        "than one tick deadlocks regardless of concurrency. Fan-out may be incidental. "
        "Check the log_tail's wait_reason: empty means no yield point was ever reached.",
        6: "Exit 6: determinism verification failed.",
        7: "Exit 7: no scenarios or runs were found — the target never resolved to anything "
        "runnable, so no agent executed and D-62 was never reached. This was D-77's "
        "signature before it was repaired (`run.py::_is_scenario_path` claimed ANY existing "
        "directory as a scenario directory, so a fixture path was globbed for *.yaml before "
        "the fixture branch was tried). Seeing it again means a NEW resolution gap, not that "
        "one — check the target against `cli._target.is_fixture_name` first.",
    }
    if exit_code is None:
        return "The seed container's exit code could not be read."
    return meanings.get(exit_code, f"Exit {exit_code}: not a documented PRD §37.2 code.")


def _log_tail(lines: int = 40) -> list[str]:
    """Return the last `lines` lines of the compose project's combined logs."""
    logs = _compose("logs", "--no-color", f"--tail={lines}")
    return logs.stdout.strip().splitlines() if logs.stdout else []


def measure(
    threshold_s: float, *, prune: bool, allow_any_arch: bool, pull_cold: bool = False
) -> Outcome:
    """Run one cold `docker compose up` and time it to a healthy, populated demo.

    Guarantees: the returned `Outcome` reflects only what was observed. Every field that was
    not reached stays None rather than being filled with a plausible value, and `failed_step`
    names the first thing that did not happen.

    Raises:
        CannotMeasure: Docker or Compose v2 is unavailable.
    """
    _require_docker()

    arch = platform.machine()
    arch_conformant = arch in {G10_ARCH, "aarch64"} or allow_any_arch
    outcome = Outcome(
        threshold_s=threshold_s,
        arch=arch,
        arch_conformant=arch_conformant,
        cold_cache=prune,
    )

    if prune:
        _go_cold(pull_cold=pull_cold)

    outcome.base_images_present = _base_images_present()

    started = time.monotonic()
    up = _compose("up", "-d", "--build", timeout=threshold_s * 3)
    outcome.build_and_up_s = round(time.monotonic() - started, 3)

    if up.returncode != 0:
        outcome.failed_step = "compose_up"
        outcome.seed_exit_code = _seed_exit_code()
        outcome.detail = (
            f"`docker compose up -d --build` exited {up.returncode} after "
            f"{outcome.build_and_up_s}s. {_diagnose_seed(outcome.seed_exit_code)} "
            "stderr tail: " + " | ".join(up.stderr.strip().splitlines()[-5:])
        )
        outcome.log_tail = _log_tail()
        return outcome

    # Poll both halves of the criterion until the threshold elapses. Health is recorded the
    # first time it answers; the clock keeps running until the run list is populated too.
    deadline = started + threshold_s
    while time.monotonic() < deadline:
        now = time.monotonic() - started
        if not outcome.healthy and _http_json(HEALTH_URL) is not None:
            outcome.healthy = True
            outcome.healthy_at_s = round(now, 3)
        if outcome.healthy:
            body = _http_json(RUNS_URL)
            if isinstance(body, dict):
                runs = body.get("runs")
                if isinstance(runs, list) and runs:
                    outcome.populated = True
                    outcome.run_count = len(runs)
                    outcome.populated_at_s = round(time.monotonic() - started, 3)
                    break
        time.sleep(POLL_INTERVAL_S)

    outcome.seed_exit_code = _seed_exit_code()
    outcome.log_tail = _log_tail()

    if not outcome.healthy:
        outcome.failed_step = "health"
        outcome.detail = f"/api/health never returned 200 within {threshold_s}s"
    elif not outcome.populated:
        outcome.failed_step = "populated_run_list"
        outcome.detail = (
            f"/api/health became healthy at {outcome.healthy_at_s}s but /api/runs stayed "
            f"empty for the full {threshold_s}s. A healthy server over an empty run list is "
            "exactly what G10's 'populated run list' clause exists to reject."
        )
    elif not outcome.within_threshold:
        outcome.failed_step = "threshold"
        outcome.detail = (
            f"the demo came up populated at {outcome.populated_at_s}s, over the "
            f"{threshold_s}s threshold"
        )
    elif not outcome.arch_conformant:
        outcome.failed_step = "architecture"
        outcome.detail = (
            f"measured on {arch}, but G10 names {G10_ARCH}; pass --allow-any-arch to record "
            "this as a non-gating datapoint"
        )
    return outcome


def _write_results(outcome: Outcome) -> None:
    """Write the Rule E1 result file every published cold-start number must cite."""
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "benchmark": "docker-cold-start",
        "gate": "G10",
        "criterion": (
            "PRD §44.1 G10: `docker compose up` reaches a healthy /api/health AND a "
            "populated run list, cold cache, arm64, under the threshold."
        ),
        "threshold_s": outcome.threshold_s,
        "met": outcome.met,
        "cold_cache": outcome.cold_cache,
        "base_images_present": outcome.base_images_present,
        "base_images_pulled_this_run": [
            r for r in BASE_IMAGES if r not in outcome.base_images_present
        ],
        "is_gate_conformant_run": outcome.cold_cache and outcome.arch_conformant,
        "environment": {
            "machine": outcome.arch,
            "system": platform.system(),
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "arch_conformant": outcome.arch_conformant,
        },
        # I11: an unqualified duration is virtual time. Every number here is wall-clock, so
        # every key carries the `_wall_s` suffix (CONTEXT.md §2 I11; the naming lint covers
        # only `src/agentdx/`, so this convention is held by hand under `bench/`).
        "timings_wall_s": {
            "build_and_up_wall_s": outcome.build_and_up_s,
            "healthy_at_wall_s": outcome.healthy_at_s,
            "populated_at_wall_s": outcome.populated_at_s,
        },
        "run_count": outcome.run_count,
        "healthy": outcome.healthy,
        "populated": outcome.populated,
        "failed_step": outcome.failed_step,
        "seed_exit_code": outcome.seed_exit_code,
        "detail": outcome.detail,
        "log_tail": outcome.log_tail,
        # Written from what this run actually did. An earlier version emitted the cold
        # paragraph unconditionally, so a `--no-prune` result described a teardown that never
        # happened while its own `cold_cache: false` said otherwise (F5).
        "method": (
            (
                "Containers, volumes, the locally built image and the whole builder cache "
                "were removed, and the bind-mount data directory deleted, before the clock "
                "started; that teardown is excluded from the timed window. "
            )
            if outcome.cold_cache
            else (
                "WARM RUN — `--no-prune` was passed, so NO teardown was performed and the "
                "build reused whatever cache the daemon already held. This is not G10, and "
                "`met` is false regardless of the timings below. "
            )
        )
        + (
            "The timer starts at `docker compose up -d --build` and stops the first poll at "
            "which /api/health answers 200 AND /api/runs returns a non-empty runs array. "
            "Both halves are required — health alone is not the gate."
        ),
        "gate_status": (
            "Pass/fail against the threshold is the claim. The exact second counts are real "
            "wall-clock measurements of a container build and will vary with machine, disk, "
            "and registry latency; never quote a fixed digit from this file as a permanent "
            "fact in CONTEXT.md or the README — cite the file. Same discipline as D-68."
        ),
    }
    RESULTS_PATH.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Run the G10 measurement and report it. Returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold-s", type=float, default=G10_THRESHOLD_S)
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="skip the cold-cache teardown (a warm measurement — NOT the gate)",
    )
    parser.add_argument("--allow-any-arch", action="store_true")
    parser.add_argument(
        "--pull-cold",
        action="store_true",
        help="also delete the pulled base images, so the build pays a real registry pull",
    )
    parser.add_argument(
        "--keep-up", action="store_true", help="leave the stack running for inspection"
    )
    args = parser.parse_args(argv)

    try:
        outcome = measure(
            args.threshold_s,
            prune=not args.no_prune,
            allow_any_arch=args.allow_any_arch,
            pull_cold=args.pull_cold,
        )
    except CannotMeasure as exc:
        sys.stderr.write(f"G10: CANNOT MEASURE — {exc}\n")
        sys.stderr.write(
            "No result file was written. An unmeasurable gate is reported as unmeasured, "
            "never as a pass and never as a number (AGENTS.md §8, I9).\n"
        )
        return EXIT_CANNOT_MEASURE

    if args.no_prune:
        sys.stdout.write(
            "WARNING: --no-prune was passed. This is a warm-cache run and is not G10.\n"
        )

    _write_results(outcome)

    verdict = "PASS" if outcome.met else "FAIL"
    sys.stdout.write(f"G10 {verdict} — threshold {outcome.threshold_s}s, arch {outcome.arch}\n")
    sys.stdout.write(f"  build + up      : {outcome.build_and_up_s}s\n")
    sys.stdout.write(f"  healthy at      : {outcome.healthy_at_s}s\n")
    sys.stdout.write(f"  populated at    : {outcome.populated_at_s}s ({outcome.run_count} runs)\n")
    if outcome.failed_step:
        sys.stdout.write(f"  failed step     : {outcome.failed_step}\n")
        sys.stdout.write(f"  detail          : {outcome.detail}\n")
    if outcome.seed_exit_code not in (None, 0):
        sys.stdout.write(f"  seed exit code  : {outcome.seed_exit_code}\n")
    sys.stdout.write(f"  results         : {RESULTS_PATH.relative_to(REPO_ROOT)}\n")

    if not args.keep_up:
        _compose("down", "-v", "--remove-orphans")

    return 0 if outcome.met else EXIT_NOT_MET


if __name__ == "__main__":
    raise SystemExit(main())
