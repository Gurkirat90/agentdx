# Changelog

All notable changes to AgentDX are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the versioning rules are stated in
full below rather than deferred to a link, because two of them are unusual.

**Nothing has been released.** There is no published wheel, no image, and no tag. The
`Unreleased` section below is the whole of this file's content. PRD §39.6 step 2 — "CI must be
green, including the determinism suite and the benchmark gates" — is **half** satisfied as of
2026-09-01: the CI spine (`just ci`) passes end to end for the first time, determinism suite
included, but the §44.1 acceptance gates do not. See [Release readiness](#release-readiness).

---

## Version policy

AgentDX is versioned `MAJOR.MINOR.PATCH`. The version in `pyproject.toml` is authoritative and
`agentdx version` reads it; nothing else holds a copy.

### The two contracts external users depend on

Most projects can describe "breaking" loosely. This one cannot, because two artefacts outlive
the process that produced them and are consumed by things this repository does not control:

| Contract | Where it lives | Why it is load-bearing |
|---|---|---|
| **The event schema** | `events/schema.py`, `SCHEMA_VERSION` (currently `2`) | A committed event log, a golden fixture corpus, and an exported `.agentdx` bundle are all read back later — potentially by a different version than wrote them |
| **CLI exit codes** | PRD §37.2, codes `0`–`7` | A CI pipeline branches on them. A silently renumbered exit code turns a green build red, or worse, a red build green |

**Any change to either is a MAJOR change**, regardless of how small the diff looks, and it gets
its own entry in a dedicated `### Breaking` section in this file naming the old and new
behaviour. This is PRD §39.6 step 5, and it is the reason this file exists at all.

### The rules

- **MAJOR** — an event-schema change, an exit-code change, or the removal of a public API in
  `agentdx.__init__`. Schema changes additionally require a `SCHEMA_VERSION` bump, an ADR in
  `CONTEXT.md` §8, and a migrate-on-read path (see `migrate_v1_to_v2` for the precedent set by
  D-49): a stored log must never become unreadable because a newer version exists.
- **MINOR** — new commands, new flags, new analysers, new fault types, new API endpoints. A new
  optional field in an event payload is MINOR only if `decode_event` reads an older log
  unchanged.
- **PATCH** — bug fixes, documentation, performance, and internal refactoring with no observable
  contract change.

### Two rules that are specific to this project

1. **A verdict-threshold change is documented even though it is not an API change.** Thresholds
   and weights live in `analysis/verdict_rules.toml`, versioned and printable via
   `agentdx analyze --explain`. Changing one changes what the tool *says about your system*
   without changing any signature, so it is recorded here under `Changed` with the old and new
   value. A user comparing two runs across versions has no other way to know.
2. **A published number changing is a release-note event.** Invariant I9 (Rule E1) requires every
   statistic to trace to a committed file in `bench/results/`. If a release changes one, PRD
   §39.6 step 3 requires the results file be regenerated and committed in the same release, and
   this file records which number moved.

### Pre-1.0

While the major version is `0`, MINOR carries breaking changes and PATCH carries everything
else — the standard 0.x convention. The two contracts above are treated as stable anyway: they
are the ones users cannot defend themselves against, and "we were pre-1.0" is not much comfort
to a CI pipeline that started passing for the wrong reason.

---

## Release readiness

A release requires PRD §39.6's five steps, of which step 2 (green CI, including the determinism
suite and the benchmark gates) is the binding one. PRD §44.3 names six quality gates that are
**never waived regardless of schedule pressure** — a release failing any of them is not released.

Step 2 has two halves, and they are now in different states. **The CI spine is green**: as of
2026-09-01 `just ci` passes all eight steps — `ruff check`, `ruff format --check`,
`mypy --strict` (98 source files), the full suite (2205 passed), import-linter (10 contracts,
149 files, 730 dependencies), determinism hygiene, ledger integrity, Rule E1 markers, and
fixture evidence. That is the first end-to-end green run in the project's history; `just lint`
had been failing since commit `d12b763` on a single formatting drift, and because `just ci`
aborts at its first step, the five checks behind it had never executed in one invocation.

**The acceptance gates are not green**, and they are what actually blocks a release.

As of the most recent mechanised run (`just acceptance`, owner-confirmed on real hardware —
`CONTEXT.md` §6), **four of the ten PRD §44.1 acceptance gates pass**. The blocking ones are
tracked in `CONTEXT.md` §6, which is authoritative and current; this file does not duplicate that
table, because a second copy is a copy that will drift.

The single item behind the largest group of failures is deviation **D-62**: nothing in `sdk/`
calls `runtime.scheduler.Scheduler.spawn()`, so no reference fixture completes a run. Until that
is closed, the demo cannot produce a run to demonstrate, and packaging work cannot move the
gates that depend on one.

---

## Unreleased

### Added

- `Dockerfile` — multi-stage image (Node build stage, Python runtime stage), non-root user,
  fixture data and scenarios copied in because they are not package data (PRD §39.4).
- `docker-compose.yml` — the one-command demo (PRD §39.2), with the three-fixture seeding step
  split into its own service so a seeding failure is distinguishable from a server failure.
- `bench/harness/docker_cold_start.py` — the gate G10 measurement harness, which did not
  previously exist. Times a genuinely cold `docker compose up` to *both* a healthy `/api/health`
  and a non-empty run list, and reports which of the two halves failed. Exits with a distinct
  code when the environment cannot run the measurement at all, so "no Docker installed" is never
  reported as "the demo is too slow".
- `docs/architecture.md` — PRD §24–§27 condensed for new contributors, completing the PRD §38.3
  documentation set.
- `CHANGELOG.md` — this file, including the version policy above (PRD §39.6 step 5).

### Fixed

- **`agentdx run` and `agentdx instrument` resolved the wrong target for a fixture path
  (D-77).** `_is_scenario_path` claimed every existing directory as a directory of scenarios, so
  `agentdx run fixtures/code_pipeline` — PRD §38.1's own quickstart, and the command
  `docker-compose.yml`'s `seed` service and `just demo-offline` each issue three times — was
  globbed for `*.yaml`, found none, and exited 7 before the fixture branch was ever reached.
  This is the confirmed cause of gate G9's exit 7. The predicate now defers to
  `is_fixture_name`, which in turn was corrected to require that a path *be* the fixture
  directory rather than merely share its basename, and to resolve a path-shaped target against
  its own checkout rather than the caller's working directory. Both of those were defects in the
  first repair, caught by review before release; see `CONTEXT.md` §9 D-77 for the precedence rule
  this settles, which PRD §37.1 leaves open. **Behaviour change:** a directory whose name matches
  a fixture (`my_scenarios/code_pipeline/`) is now read as a directory of scenarios, not as the
  fixture. Write the fixture name bare to mean the fixture. Fixing this does not make G9 or G10
  pass — it moves both from "the target never resolved" to "the run deadlocks" (D-62).
- **Rule E1 / invariant I9 violations in published documentation.** `scripts/check_bench_markers.py`
  was failing on eight unmarked numeric claims — six in `docs/cli.md` (regression tolerances and
  test-fixture magnitudes) and two in `docs/limitations.md` (mutation-test magnitudes). None was
  a genuine unmeasured performance claim; all were configured thresholds or test-fixture values
  that happened to carry a Rule E1 unit. Each was reworded to state the same fact without
  presenting a bare unit-bearing figure, rather than being suppressed — the checker has no
  suppression mechanism by design. `just check-bench` now passes, which it did not before.

### Known gaps in what was added

Stated here rather than in a commit message, because the commit message is not what a reader of
this file will find:

- **The `Dockerfile` and `docker-compose.yml` have been built and run once, warm, on
  Darwin/arm64, 2026-08-29** — see `bench/results/docker-cold-start.json`, which carries the
  `seed` service's real `E-SCHED-003` traceback and `seed_exit_code: 5`. That run confirms the
  image builds and the compose graph wires up; it does **not** confirm a cold build (it was
  taken with `--no-prune`, so `cold_cache` is `false` and no G10-conformant number exists yet),
  and it does not confirm the §39.4 image-size target, which has never been measured.
- **`docker-compose.yml`'s bind mount is unexercised on Linux.** `./.agentdx-data:/data` is
  written by uid 10001 inside the container; the build-time `chown` is shadowed by the mount at
  runtime. Docker Desktop on macOS remaps ownership and hides this, and the one run above was on
  Darwin. On Linux — a `CONTEXT.md` §3 supported platform — this may fail with EACCES.
- **The image bakes the built frontend into `src/agentdx/api/static/`, but nothing serves it.**
  `api/app.py` has no static-file mount, so PRD §39.4's "self-contained app with no Node
  requirement for end users" is not yet true. Adding the mount is an `api/` change.
- **PRD §39.2's compose block sets `AGENTDX_MODE` and `AGENTDX_DATA_DIR`, neither of which
  exists.** The real environment contract is `AGENTDX_<SECTION>_<KEY>` (`config.py`), so both of
  the PRD's names are silently ignored — they do not even error, because the unknown-key check
  only fires for a name matching a real section prefix. The shipped compose file uses the working
  names. `just demo-offline` still carries the inert `AGENTDX_MODE=replay`; harmless only because
  `replay` is already the default.

### Not done

- No release workflow, no tag, no publish step. PRD §39.6 step 2 requires green CI, and CI is not
  green. Writing the automation that would publish over a red gate is the specific thing PRD
  §44.3 forbids.
- No OpenTelemetry export (PRD §30, FR-13). It is scope-cut #5 and conditional on G1–G10 being
  green.
- No `README.md` rewrite and no ghost-baseline GIF. The GIF is a recording of the demo, and the
  demo does not run (D-62). A screenshot of a state the product cannot currently reach would be
  the most misleading artefact in the repository.
