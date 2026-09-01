# AgentDX CLI (PRD §37, §22)

P17 deliverable: the `agentdx` console entry point — every PRD §37.1 command, the authoritative
§37.2 exit-code table, the §37.3 output conventions, `--ci` mode (§22.1-§22.7: machine-readable
output, regression comparison, the shipped GitHub Actions workflow), and `agentdx doctor`.

**The CLI composes; it does not implement.** Every number a command prints is read off an
`analysis/`, `scenario/`, or `runtime/` module this prompt did not write — `src/agentdx/cli/`
resolves a target, builds the real runtime services, drives them, and formats what they
return. Where a command needed a computation no module provides, that gap is named below
rather than faked (AGENTS.md §2).

## Running it

```
agentdx --help
agentdx <command> --help      # every command's own flags and PRD §37.2 semantics
```

Installed via the project's own console script (`pyproject.toml`'s `[project.scripts]`):
`uv run agentdx ...` from a checkout, or `agentdx ...` once installed.

## Global options (PRD §37, parsed once by `cli.main`'s `@app.callback()`)

| Flag | Meaning |
|---|---|
| `--data-dir PATH` | Override `[store]`/`[run]` `data_dir` (default `~/.agentdx`). |
| `--config PATH` | Path to `agentdx.toml` (default: discovered upward from cwd). |
| `-v`, `--verbose` | Print diagnostic detail. |
| `-q`, `--quiet` | Suppress progress/informational lines (never errors or the final result). |
| `--json` | Machine-readable output on stdout **only** — every human line moves to stderr. |
| `--no-color` | Disable ANSI colour (`FORCE_COLOR`/`NO_COLOR` env vars also apply, §37.3). |
| `--seed N` | Override `[run] seed`. |
| `--strict` / `--no-strict` | Override `[scheduler] strict_determinism`. |

Every command reads these from `ctx.obj` rather than re-parsing them — one place enforces the
§37.3 output contract ("`agentdx run fixtures/code_pipeline --json \| jq .verdict.class` must
never see a progress line mixed into its stdout").

## Exit codes (PRD §37.2 — authoritative; §22.2 restates the same table for `--ci`)

Defined exactly once, in `src/agentdx/cli/_exitcodes.py`; every command imports these names
rather than a bare integer. Changing one is a breaking change (CONTEXT.md §3).

| Code | Name | Meaning |
|---|---|---|
| 0 | `OK` | Success; all assertions passed. |
| 1 | `ASSERTION_FAILURE` | Assertion failure / regression detected. |
| 2 | `USAGE_ERROR` | Usage, configuration or validation error. |
| 3 | `CACHE_MISS` | LLM cache miss in replay mode (`E-CACHE-001`). |
| 4 | `GUARD_ABORTED` | A safety guard aborted the run (`E-GUARD-001`). |
| 5 | `INTERNAL_ERROR` | Internal error — an AgentDX defect, never a user error. |
| 6 | `DETERMINISM_FAILURE` | Determinism verification failed (`E-REPLAY-001`). |
| 7 | `NOT_FOUND` | No scenarios or runs found at the given path. |

Every code above is produced by a real, passing integration test asserting the process's
actual exit status — `tests/integration/cli/test_exit_codes.py`. Four of the eight
(3/4/6, and the exception branch feeding 5's non-deadlock cases) substitute the *cause* via a
monkeypatch rather than a completed real graph run; see "Known gap: no fixture graph can
complete yet" below for exactly why, and why that substitution is honest rather than a shortcut.

## Commands

### `agentdx instrument TARGET`

Static preview (`scenario.validate.resolve_graph_identity`'s AST scan — no import, no
execution) of the agents/tools/edges a `graph.py` declares. What a *run* actually misses
(state keys, provider calls) is a runtime fact, reported as `instrumentation_gap` events by
`agentdx run` itself, not approximated here.

```
$ agentdx instrument code_pipeline
static discovery of .../fixtures/code_pipeline/graph.py (no import, no execution):
  agents: coder, planner, reviewer, tester
  tools:  lint, read_file, run_tests, write_draft
  edges:  coder->tester, planner->coder, planner->reviewer, reviewer->tester
```

### `agentdx run TARGET [OPTIONS]`

The composition root. `TARGET` is a fixture name, an import path (`FILE.py:attribute`), a
scenario file, or a directory of scenario files (PRD §37.1). Builds the real `Scheduler` +
`VirtualClock` + `EventWriter` + `Cache` + fault hooks, installs `cli.host.CliRunHost` (the
`sdk.generic.RunHost` implementation `install_runtime`'s own docstring says belongs to
`cli/`), drives `agentdx.run()`, then composes the P10-P12 analysis pipeline and evaluates any
scenario assertions against the sealed log.

| Flag | Meaning |
|---|---|
| `--task TEXT` | Task text, or a path to one. Overrides a scenario's own `task:`. |
| `--seed N` | Override the run seed. |
| `--faults TYPE:AGENT:AT_VIRTUAL_MS` | Arm one fault ad hoc (direct-target mode; CLI-invented shorthand, not a PRD grammar — scenario files' own `faults:` block is the richer, PRD-given form). |
| `--cache-mode MODE` | `record\|replay\|perturb\|passthrough` (default `[run] mode`, `replay`). |
| `--baseline` | **Not yet implemented** — needs `analysis.baseline`'s `BaselineExecutor`. |
| `--ci` | Machine-readable mode (§22.1): no prose, writes JSON + JUnit to `--out`. |
| `--out DIR` | `--ci` artefact directory (default `.agentdx/ci`). |
| `--baseline-run PATH` | A previous `--ci` `summary.json` to regression-check against (§22.6). |
| `--format junit\|json\|github` | Which `--ci` artefact(s) to write. `github` is accepted but not yet a distinct GH-annotation renderer — falls back to writing both. |
| `--jobs N` | **Not yet implemented** — scenarios always run sequentially (already the §22.1 item 5 order). |
| `--fail-on SEVERITY` | **Not yet implemented** — a scenario's own `max_findings` assertion is the only severity gate this build enforces. |

`--ci` mode (§22.1): replay mode is not force-enabled by this build (declared reduction — see
below), output is one line per scenario with no spinners, scenarios run in sorted-path order,
and every artefact lands under `--out`.

### `agentdx doctor [--json]`

"Makes the first five minutes survivable" (Design Constraint 4). Six checks, each reading a
fact this module did not invent: the Python and `langgraph` version pins straight out of
`pyproject.toml`, `PYTHONHASHSEED=0` (AGENTS.md §4.1), the cache/store db paths from resolved
config, the store's own migration state (`store.migrations.current_version`/`latest_version`
— the same functions `Store.open` consults), and whether `[api].port` is already bound.

```
$ agentdx doctor
✓ python-version: 3.12.3 satisfies '>=3.12,<3.13'
✓ langgraph-version: 1.2.10 satisfies '>=1.2,<1.3'
✓ hash-seed: PYTHONHASHSEED=0
✓ cache-db: ~/.agentdx/cache.db does not exist yet — created on first `agentdx run` (not a
  failure; replay-mode runs against an empty cache will report E-CACHE-001)
✓ store-migration: ~/.agentdx/agentdx.db does not exist yet (fresh install)
✓ port: 127.0.0.1:8420 is free
```

Exits 0 when every check passes, 2 (usage/config error) otherwise. `tests/integration/cli/
test_doctor.py` deliberately breaks three setups and asserts `doctor` catches each one: a
missing/wrong `PYTHONHASHSEED`, a store db claiming a schema version this build cannot read,
and a port already bound by something else.

### `agentdx scenario validate|list|expand`

Thin wrappers over `cli._scenario_io`'s load → `extends`-resolve → validate → resolve-defaults
chain (already built and tested by `scenario/`) plus `scenario.matrix.expand_matrix`.
`validate PATH` accepts a file or a directory (validates every scenario found, sorted-path
order); `list [PATH]` prints each discovered scenario's id; `expand PATH` prints the full
matrix cross-product, or says plainly that the scenario has none.

`scenario new` (generate a scenario, optionally derived from a run) is deferred — see NOT
DONE below.

### `agentdx version`

Prints `agentdx.__version__` and `events.schema.SCHEMA_VERSION`.

### `agentdx ui [--host] [--port]`

Unchanged from P14: serves `agentdx.api.app`'s FastAPI app. See `docs/api.md`.

### Not yet implemented (exit 2, name themselves and why)

`replay`, `analyze`, `compare`, `export`, `import`, `scenario new`, every `cache` subcommand,
`baseline update`, and `bench` (P18's own command) all currently exit 2 with a message naming
the reason, rather than presenting a stub as working behaviour. See NOT DONE/RISKS in this
response for the reasoning behind deferring each — the short version: `run`/`doctor`/
`scenario {validate,list,expand}`/`instrument`/`version`, the §22 `--ci` machinery, and the
exit-code contract were prioritised (mission Design Constraint 7: "`--ci` is scope-cut #2 —
build P0 commands first").

## `--ci` mode: machine-readable output (PRD §22.4)

**JUnit XML** — one `<testsuite>` per scenario, one `<testcase>` per assertion; a run that
never reached assertion evaluation (an exception — cache miss, guard trip, scheduler
deadlock, ...) gets one synthetic `run` testcase carrying the exception text, rather than a
silently empty `<testsuite tests="0">`.

**JSON summary** — `{agentdx_version, schema_version, started_at, duration_wall_s, scenarios:
[...], totals: {...}}`, exactly PRD §22.4's own worked example, schema-versioned
(`JSON_SCHEMA_VERSION = 1`) and stable.

## Regression comparison (PRD §22.6)

```
agentdx run scenarios/ --ci --baseline-run .agentdx/baselines/main.json
```

Five tolerances, read from `cli.ci.REGRESSION_TOLERANCES` (not hardcoded per call site):

| Metric | Default tolerance | Fails when |
|---|---|---|
| `achieved_speedup` | 5 percent, relative, downward | New value below baseline minus 5 percent |
| `resilience_score` | −3 absolute | New score below baseline − 3 |
| `coordination_score` | −5 absolute | Below baseline − 5 |
| `token_cost_multiplier` | 10 percent, relative, upward | Above baseline plus 10 percent |
| `findings[high+]` | 0 new | Any new high/critical finding |

Baselines are refreshed only by an explicit `agentdx baseline update` (not yet implemented —
see NOT DONE) — never automatically. `tests/integration/cli/test_ci_mode.py` seeds a
25-percent speedup regression against a round-tripped baseline file and asserts
`check_regression` catches it (and a second test asserts a 1.5-percent drop, within
tolerance, does not).

## The shipped GitHub Actions workflow (PRD §22.5, `.github/workflows/agentdx.yml`)

```yaml
name: agentdx
on: [pull_request]
jobs:
  reliability:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: {enable-cache: true}
      - run: uv sync --frozen
      - name: Restore LLM cache
        uses: actions/cache@v4
        with:
          path: .agentdx/cache.db
          key: agentdx-cache-${{ hashFiles('fixtures/**', 'scenarios/**') }}
      - name: Validate scenarios
        run: uv run agentdx scenario validate scenarios/
      - name: Run reliability scenarios
        run: uv run agentdx run scenarios/ --ci --format junit --out ci-out/ --cache-mode replay
      - uses: actions/upload-artifact@v4
        if: always()
        with: {name: agentdx-results, path: ci-out/}
```

No secrets configured — every scenario runs `--cache-mode replay` against the committed
fixture cache (I7). Every step in this file was run manually against the real, shipped
`scenarios/` directory while building this response: `scenario validate` passes (2/2), the
`run --ci` step exits non-zero (see the gap below) and still writes a valid, informative
`junit.xml` to `ci-out/` — exactly the "if: always()" artifact-upload behaviour this workflow
depends on.

## Known gap: no fixture graph can complete a real run yet

Building `agentdx run` surfaced a gap outside this prompt's DELIVERABLES: **nothing in `sdk/`
calls `runtime.scheduler.Scheduler.spawn()`** — neither `sdk.langgraph.LangGraphAdapter` nor
`sdk.generic`'s `@agent`/`@tool` decorator path. The only call sites for `Scheduler.spawn()`
in this entire codebase, before this prompt, are hand-authored test harnesses
(`tests/integration/faults/_harness.py`) that drive the scheduler directly, never through the
real SDK. Every real agent therefore executes as one plain coroutine inside the scheduler's
single root task; the moment a graph's own executor suspends on anything that is not
`scheduler.yield_point()`/`scheduler.sleep()` — `fixtures/code_pipeline`'s parallel
`coder`/`reviewer` branch, in particular — the scheduler sees no runnable task and no pending
timer, and raises `DeadlockError` (`E-SCHED-003`).

This is the specific failure `cli/host.py`'s own module docstring predicted as a risk
("running the three P05 fixtures through this host, with a real cooperative `Scheduler`, is
the first time in this codebase's history that they execute under real scheduling"), now
confirmed and root-caused. It is an `sdk/` defect, not a `cli/` one — fixing it means deciding
how an agent step becomes a scheduler task (one task per node? per span? how does that
interact with LangGraph's own parallel supersteps?), a design question with test-suite-wide
consequences that belongs to its own reviewed change, not a same-prompt fix bundled into the
CLI. Every exit code this gap blocks from a genuine end-to-end run (1/3/4/6) is still verified
through the real Typer app with only `_execute_one`'s *return* substituted — see
`tests/integration/cli/test_exit_codes.py`'s own module docstring for the full reasoning.
