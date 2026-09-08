# OP-2 INDEPENDENT AUDIT — G10 Docker cold-start claim (`bench/harness/docker_cold_start.py`, D-93)

**Scope.** `bench/harness/docker_cold_start.py` (full file, all 593 lines); `bench/results/
docker-cold-start.json` (the committed result file the 2026-09-08 PASS cites); `Dockerfile`;
`docker-compose.yml`; the G10 row in `CONTEXT.md` §6 (row 192) and the D-93 ledger entry in §9;
the corresponding `CHANGELOG.md` "Release readiness"/"Added"/"Known gaps" passages. Cross-checked
but not itself re-audited: the rest of the acceptance-gate suite (G1–G9), `cli/host.py`'s D-80
reuse logic, and anything upstream of `agentdx run` (the D-62/ADR-019 scheduler-dispatch fix is
taken as given, per its own already-closed ledger status — not re-derived here).

**Method.** Read `CONTEXT.md` §0 (roles, OP-1/2/3 definitions), the relevant parts of §5/§6/§9/§11,
and `AGENTS.md`'s Rule E1 reference, before reading any code. Read the harness file end to end,
line by line, including every docstring (which this project uses as load-bearing documentation,
not decoration). Read `Dockerfile` and `docker-compose.yml` in full. Read the committed
`bench/results/docker-cold-start.json` field by field and traced each field back to the exact
line(s) of code that compute it. Read the existing `op2-audit-p13.md` as the format template this
report follows. Attempted a live Docker re-run (see below).

**Live re-execution: attempted, not possible.** `which docker` and `docker info` in this audit's
own sandbox both return "command not found" — there is no Docker binary at all, let alone a
running daemon. This audit is **code-review only**: every claim below is a static trace of the
harness's source against its own committed output, not a fresh execution of the gate. This matches
what the harness's own `CannotMeasure` path would report if run here, and it is exactly the
scope-boundary this project's own OP-2 protocol requires disclosing rather than glossing over.

---

## VERDICT: **PASS WITH NOTES**

The 2026-09-08 G10 PASS claim is **trustworthy at the level that determines `met`**. Tracing the
control flow precisely (not just the happy path): there is no code path found where `docker
compose up -d --build` fails or `/api/runs` stays empty and the harness still reports `met: true`
— `Outcome.met` is a conjunction of five independently-computed booleans, `healthy`/`populated`
come only from real polled HTTP responses (never from the seed container's exit code), and every
field in the committed JSON traces to a real computation, not a hardcoded default sitting on an
unreached branch. The harness explicitly avoids `CONTEXT.md` §11 tripwire 19's named failure mode
(trusting a subprocess exit code alone) — `seed_exit_code` is recorded for diagnostics only and
never feeds `populated` or `met`. `Dockerfile` and `docker-compose.yml` genuinely match what the
harness claims to be testing (an `agentdx` service plus a separate `seed` service running exactly
three fixtures), and `CONTEXT.md`'s D-93 entry and §6 row 192 do not overclaim anything found here
— if anything they under-claim, carrying forward honest caveats (a same-session flaky first
attempt, the still-unverified Linux bind-mount ownership question, the SPA route not being
container-verified) that a less careful ledger would have dropped once the number went green.

That said, this audit found one real, unexercised gap in the teardown's own verification (Finding
#1) and two smaller robustness gaps (Findings #2–#3) that a "cold means cold" gate should not
carry silently. None of them changes the verdict on the *specific, committed* 2026-09-08 result —
the run's own timing (67.308s for a multi-stage build, not the few seconds a warm-cache rebuild of
this image would take) is itself corroborating evidence the teardown actually worked — but they
are real gaps in the harness's own self-verification that the next person to trust this gate should
know about. That is why this is **PASS WITH NOTES**, not a bare PASS.

---

## FINDING #1 (MEDIUM) — the cold-teardown subprocess calls discard their own exit codes; a silently-failed `docker compose down` / `docker builder prune` would not be visible anywhere in the result file

**Where:** `bench/harness/docker_cold_start.py:194–205` (`_compose`, `check: bool = False` by
default); `:249–282` (`_go_cold`).

The exact teardown sequence, quoted in full:

```python
def _go_cold(*, pull_cold: bool = False) -> None:
    _compose("down", "-v", "--rmi", "local", "--remove-orphans")
    if pull_cold:
        _remove_base_images()
    subprocess.run(
        ["docker", "builder", "prune", "-af"],
        capture_output=True,
        text=True,
        check=False,
    )
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
```

This genuinely targets the right four things (`CONTEXT.md`'s and the docstring's own claim: "the
compose project has no containers, no volumes and no locally built image, the builder cache is
empty, and the bind-mount data directory is gone"), and the *bind-mount removal specifically* —
the piece that most directly matters for question 1's "could a stale populated run list survive"
concern — is **not** silently swallowed: `shutil.rmtree(DATA_DIR)` raises on failure, uncaught, and
`_write_results` is only reached after `measure()` returns normally, so a failed rmtree crashes the
script before any result (let alone a false `met: true`) is ever written.

But the other two calls — `docker compose down -v --rmi local --remove-orphans` (line 272) and
`docker builder prune -af` (lines 275–280) — both go through code paths with `check=False`, and
neither return code is inspected, logged, or recorded into `Outcome` or the JSON in any way. If
`compose down` silently failed (stale containers/networks left running — a real, observed Docker
failure mode, e.g. a network still attached to another project) or `builder prune` silently failed
(a permissions or daemon-state issue), the harness would proceed exactly as if teardown had
succeeded: `outcome.cold_cache = prune` (line 390) is set from the **boolean flag "was a cold run
requested,"** not from any verification that the teardown it requested actually completed. Nothing
downstream re-checks e.g. `docker compose ps -a` is empty, or that `docker builder du` reports zero
cache, before the timer starts.

**Why this doesn't overturn the specific 2026-09-08 result:** the committed JSON's
`build_and_up_wall_s: 67.308` is itself real, indirect evidence the teardown worked — a multi-stage
image (Node stage running `npm ci` + TypeScript build, Python stage running two `uv sync` passes)
rebuilding from a genuinely empty build cache is consistent with 67 seconds; a warm-cache rebuild of
an unchanged Dockerfile typically completes in single-digit seconds. This is corroboration, not
proof, and it is exactly the kind of signal a harness with real self-verification wouldn't need to
leave to a reader's inference.

**Suggested fix direction (not this audit's to make):** capture and record each teardown
subprocess's return code in the `Outcome`/JSON (e.g. `teardown_ok: bool`), and consider `check=True`
with a `CannotMeasure`-class failure (matching the existing pattern used for a missing Docker
daemon) rather than a silent pass-through, so "cold_cache: true" becomes a verified claim rather
than an attempted one.

---

## FINDING #2 (LOW) — an unhandled `subprocess.TimeoutExpired` on `docker compose up` would crash the harness with a raw traceback rather than a graceful FAIL

**Where:** `docker_cold_start.py:399` (`up = _compose("up", "-d", "--build", timeout=threshold_s *
3)`); no `except subprocess.TimeoutExpired` anywhere in `measure()` or `main()`.

`_compose` passes `timeout=` straight to `subprocess.run`, which raises `subprocess.TimeoutExpired`
if the command doesn't return within that window (540s at the default 180s threshold). `main()`
only catches `CannotMeasure` (line 558); a `TimeoutExpired` would propagate uncaught, crashing the
process with a Python traceback instead of writing a `failed_step: "threshold"`-shaped result. This
is not a false-PASS risk (the crash happens before `_write_results` is reached, so no result file —
let alone a false one — would be written), but it means an extremely slow/hung build reports as an
unhandled crash rather than the graceful, diagnosable FAIL the rest of this harness is built to
produce. Did not fire in the audited run (67.308s, far under the 540s ceiling).

---

## FINDING #3 (LOW, informational) — `_seed_exit_code()`'s line-by-line JSON parse of `docker compose ps` is sensitive to Compose CLI output-format variation, and a parse failure resolves silently to `None`

**Where:** `docker_cold_start.py:305–323` (`_seed_exit_code`).

```python
listing = _compose("ps", "-a", "--format", "json", "seed")
...
for line in listing.stdout.strip().splitlines():
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        continue
```

This assumes `docker compose ps --format json` emits one JSON object per line (NDJSON). That is
Compose v2's current behavior, and it worked correctly in the audited run (`seed_exit_code: 0` was
captured). Some Compose versions have emitted a single JSON array instead (which, if
pretty-printed, would fail every per-line `json.loads` and silently fall through to `return None`
with no warning). This field is diagnostic-only — it does not feed `healthy`, `populated`, or `met`
— so its severity is low, but it is worth flagging since `_diagnose_seed`'s whole point is to make
a `compose_up` failure legible, and a version-sensitive silent `None` would quietly defeat that on
a future run using a different Compose build.

---

## FINDING #4 (LOW, disclosure check — not a hidden defect) — "cold" by default excludes the three pulled base images, but this is already thoroughly disclosed and, in the audited run, was moot

**Where:** `docker_cold_start.py:208–247` (`BASE_IMAGES`, `_base_images_present`,
`_remove_base_images`); `:256–263` (`_go_cold`'s own docstring on this exact point); the JSON's
`base_images_present`/`base_images_pulled_this_run` fields; `CONTEXT.md` D-93.

By default (no `--pull-cold`), `_go_cold()` does not remove `node:20-bookworm-slim`,
`python:3.12-slim-bookworm`, or `ghcr.io/astral-sh/uv:0.5.11` — `docker builder prune` empties the
*build* cache, not the image store these live in. The docstring is explicit about this ("`cold_
cache: true` therefore means 'no build cache', not 'nothing cached at all'"), and the result JSON
records exactly which of the three were present before the run started
(`base_images_present`) versus pulled fresh this run (`base_images_pulled_this_run`). In the
committed 2026-09-08 result, **`base_images_present` is `[]`** — all three were pulled fresh, so
this run's "cold" claim was, in practice, fully cold in the strictest sense too, stricter than the
code's own baseline guarantee requires. Checked and confirmed correct; recorded here because it is
squarely inside audit item 1's question, not because anything is wrong with it.

---

## Positive controls — checked and genuinely correct, not just absence of finding

- **`met` is never derived from `seed_exit_code`.** Grepped every use of `seed_exit_code` in the
  file (lines 133, 404, 432, 497, 581) — all four are either the field's declaration, a diagnostic
  assignment, or output formatting. `Outcome.met` (lines 147–155) is `healthy and populated and
  within_threshold and arch_conformant and cold_cache` — none of those five is computed from
  `seed_exit_code`. This is exactly the discipline `CONTEXT.md` §11 tripwire 19 was written to
  demand, and the harness's own `_diagnose_seed` docstring (lines 326–335) names tripwire 19
  explicitly as the reason it exists, which checks out against the actual code, not just the prose.
- **`healthy` and `populated` are both real HTTP checks against real response bodies.** `_http_json`
  (lines 285–302) requires an actual `200` status and a JSON-decodable body; `populated`
  additionally requires `body["runs"]` to be a `list` with `len > 0` (lines 421–429) — matching the
  PRD criterion the docstring quotes ("`/api/runs` returns a non-empty `runs` array") exactly, not
  a looser proxy for it.
- **No path from a failed `docker compose up -d --build` to `met: true`.** If `up.returncode != 0`
  (line 402), the function records `failed_step = "compose_up"` and returns immediately — `healthy`
  and `populated` are dataclass fields that default to `False` (lines 129–130) and are never set
  before this early return, so `met` is `False` by construction, not by a downstream check that
  could be skipped.
- **Every field in the committed JSON traces to a real computation on a reached code path**,
  cross-checked one by one:
  - `cold_cache` ← `outcome.cold_cache = prune` (line 390), `prune = not args.no_prune` (line 554);
    `true` in the file because `--no-prune` was not passed.
  - `met` ← the `Outcome.met` property, all five components independently computed above.
  - `is_gate_conformant_run` ← `outcome.cold_cache and outcome.arch_conformant` (line 477),
    deliberately narrower than `met` (doesn't require the threshold/health/populated checks) — a
    real, distinct signal, not a duplicate of `met`.
  - `healthy` / `populated` / `run_count` ← the poll loop (lines 416–430), `run_count =
    len(runs)` from the actual decoded list.
  - `seed_exit_code` ← `_seed_exit_code()`, a real `docker compose ps -a --format json seed` call
    parsed for an `ExitCode` field (Finding #3 nonwithstanding, it worked here).
  - `timings_wall_s.*` ← `time.monotonic()` deltas recorded at the actual moments each condition
    was observed to hold, never a placeholder.
  - No field found sitting at a class-level default that a real branch could have skipped without
    setting.
- **`Dockerfile` and `docker-compose.yml` genuinely match the harness's claim of "an `agentdx`
  server plus a `seed` service running 3 fixtures."** `docker-compose.yml`'s `seed` service runs
  exactly `agentdx run fixtures/code_pipeline`, `fixtures/support_triage`, `fixtures/
  research_fanout` under `set -e` (lines 74–82), and `agentdx` depends on `seed` via
  `service_completed_successfully` (lines 98–100) — matching the log_tail's three "passed" lines
  and `run_count: 3` in the JSON exactly. `HEALTH_URL`/`RUNS_URL` (`127.0.0.1:8420/api/health`,
  `/api/runs`) match the compose file's `ports: ["8420:8420"]` and the healthcheck's own
  `curl -f http://localhost:8420/api/health`.
- **`CONTEXT.md`'s D-93 and §6 row 192 do not overclaim.** Both state plainly that `met: true` and
  `is_gate_conformant_run: true`, cite the exact seconds while explicitly deferring to the file's
  own "don't requote a fixed digit as permanent" caveat, and D-93 discloses — rather than hides — a
  same-session flaky first attempt (`compose up` exit 1 while a bare `build --no-cache` succeeded
  moments later) and three still-open items (an owed OP-2 audit — this one; the Linux bind-mount
  ownership question, never tested since every run has been Darwin/arm64; whether to fold G10 into
  the blocking CI gate set). `CHANGELOG.md` mirrors this precisely, including the honest note that
  the health check itself never probes `/` or a static asset path, so the SPA-fallback route is
  code-verified but not container-verified — a real, disclosed scope boundary, not something this
  audit found on its own.

---

## What was checked and holds up

- `DATA_DIR = REPO_ROOT / ".agentdx-data"` matches `docker-compose.yml`'s bind mount
  (`./.agentdx-data:/data`) exactly — the harness's Python-level teardown and the compose file's
  bind-mount path are the same directory, not a mismatch.
- `docker compose down -v` does **not** remove a host bind mount (only named/anonymous volumes
  declared under the compose file's own `volumes:` key) — confirmed this is standard Compose
  behavior, which is exactly why `_go_cold()` needs its own separate `shutil.rmtree(DATA_DIR)` call
  rather than relying on `-v` alone; the code already does this correctly.
  `COMPOSE_PROJECT`'s isolated project name genuinely scopes the *compose* teardown to this
  harness's own containers, as its own docstring claims — but `docker builder prune -af` is
  correctly and honestly disclosed as daemon-global, unscoped by project name (lines 264–270),
  matching the code's actual behavior (`docker builder prune` has no project-name concept at all).
- The architecture check (`arch in {"arm64", "aarch64"} or allow_any_arch`, line 385) matches
  `platform.machine()`'s real return value on Apple Silicon Darwin (`"arm64"`), consistent with the
  committed JSON's `"machine": "arm64"`.
- The exit-code diagnostic table in `_diagnose_seed` (lines 336–361) is derived, not assumed —
  its own docstring documents a prior version that hardcoded a wrong attribution and was caught
  when a real run returned a different exit code than expected; the current version reads the
  actual observed code and does not presuppose which failure occurred.

---

## NOT DONE / RISKS

- **No live Docker re-execution.** This audit's own sandbox has no `docker` binary at all
  (`which docker` → not found), so a real cold `docker compose up` could not be attempted here.
  Everything above is a static trace of the harness's source against its own already-committed
  output, not a fresh, independent run of the gate. The 2026-09-08 result was produced on the
  owner's own machine, which this audit did not have access to.
- **Finding #1's teardown-verification gap was not exercised** — i.e., this audit did not (and
  could not, without a Docker daemon) construct a scenario where `docker compose down` or `docker
  builder prune` actually fails, to confirm the predicted silent pass-through empirically. The
  code-level trace is confident; a live demonstration is not part of this audit's evidence.
- **Finding #3's Compose-version sensitivity was not tested against multiple Compose CLI
  versions** — flagged from reading the code and general knowledge of Compose v2's JSON output
  history, not from a reproduced failure.
- **D-93's own "flaky first attempt" (`compose up` exit 1, unexplained) was not independently
  investigated** — accepted as disclosed and unverifiable without a live re-run on the same
  machine.
- **The `Dockerfile`'s open item (sub-500MB image-size target, never measured) is out of this
  audit's scope** — it's a different, already-disclosed gap (`Dockerfile`'s own header, "Still
  open"), not part of G10's own criterion.
- **G8 and the rest of the acceptance-gate suite were not touched** — this audit is scoped to G10
  / the docker-cold-start harness only, per the assignment.
