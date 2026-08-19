# Scenario reference

The scenario YAML format: every field, its type, its default, and an example. This is the
authorisation surface for chaos (PRD §13.10, invariant I12) — nothing here executes a fault;
`src/agentdx/scenario/` only parses, defaults, validates and expands the declaration.
Fault *execution* is `runtime/faults/` (P09, not built). CI mode (`--ci`, P17/FR-11b) is P1
and also not built. This document only covers what `agentdx.scenario` implements today.

Every error this module raises carries a namespaced code (`E-SCEN-NNN`), the exact source
line, and a suggested fix. Anchors below (`#e-scen-001` etc.) match `ScenarioError.docs_url`.

## Minimal scenario

```yaml
scenario: reviewer_crash_midflight
task: fixtures/code_pipeline/refactor_module.md
seed: 42
hypothesis:
  task_success: ">= 0.9"
faults:
  - type: agent_crash
    agent: reviewer
    at_virtual_ts: 2400
guards:
  max_virtual_duration_ms: 120000
  max_tokens: 200000
assertions:
  - no_state_conflicts
  - speedup_vs_baseline: ">= 1.0"
```

This validates unchanged — no `version:` and no `target:` are required. `version` defaults
to `1`. `target` is inferred from `task`'s path when it looks like `fixtures/<name>/...`
(here, `fixture: code_pipeline`) — see [`target`](#target) below.

## Top-level fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `version` | int | `1` | See [Versioning](#versioning). |
| `scenario` | string | *(required)* | Non-empty slug. Becomes the scenario id. |
| `description` | string | `null` | Free text, no effect on execution. |
| `extends` | string (path) | `null` | See [`extends`](#extends-reuse-and-composition). |
| `matrix` | mapping | `null` | See [Matrix expansion](#matrix-expansion). |
| `target` | mapping | inferred from `task` | See [`target`](#target). |
| `task` | string | *(required)* | A `fixtures/<name>/...` path, or an inline task string. |
| `seed` | int | `0` | The determinism seed (I1). |
| `mode` | `replay` \| `record` \| `perturb` \| `passthrough` | `replay` | I7: offline by default. |
| `repeats` | int | `1` | In-process repeat count. (`--repeat N` on the CLI is separate — P1, out of scope here.) |
| `chaos_opt_in` | bool | `false` | Required (with a non-empty `blast_radius`) before any fault may target a user graph. See [Chaos safety](#chaos-safety-invariant-i12). |
| `hypothesis` | mapping | `{}` | See [`hypothesis`](#hypothesis). |
| `blast_radius` | mapping | all-empty | See [Chaos safety](#chaos-safety-invariant-i12). |
| `faults` | list | `[]` | See [`faults`](#faults). |
| `guards` | mapping | PRD §13.6 defaults | See [`guards`](#guards). |
| `baseline` | mapping | `{generate: true, prompt: null, allow_low_comparability: false}` | See [`baseline`](#baseline). |
| `exploration` | mapping | `{enabled: false, k: 2, max_schedules: 200}` | See [`exploration`](#exploration). |
| `success_check` | mapping | `{type: none, ref: null}` | See [`success_check`](#success_check-the-pluggable-hook). |
| `assertions` | list | `[]` | See [`assertions`](#assertions). |

An unknown top-level key, or an unknown key inside any of the mappings below, is
**`E-SCEN-002`** — an error, never a warning (Design Constraint 6). The suggestion names the
closest known key by fuzzy match when one is close enough.

### `target`

```yaml
target:
  fixture: code_pipeline     # OR
  graph: "./app.py:build_graph"
```

Exactly one of `fixture` (a name under `fixtures/`) or `graph` (a `path:attr` reference to a
user-owned LangGraph). Both present, or neither present and `task` cannot be used to infer
one, is **`E-SCEN-003`**. When `target` is a `graph:` reference, the scenario is chaos-testing
*your own code*, not a shipped fixture — this is exactly the case [chaos
safety](#chaos-safety-invariant-i12) below gates.

**Inference from `task:` only works for a real fixture directory (C-13, corrected).**
`target.fixture` is inferred from a `task:` path shaped like `fixtures/<name>/...`, but a
syntactic `fixtures/<name>/...` shape is not itself evidence `<name>` is a fixture —
`infer_fixture_from_task_path` checks that `fixtures/<name>/graph.py` actually exists on
disk before inferring, rather than maintaining a denylist of known non-fixture names by hand.
`fixtures/tasks/` (task descriptions shared across fixtures) and `fixtures/perturbations/`
(no `graph.py`) are both real, shipped examples of directories this correctly declines to
infer from; `task: fixtures/tasks/<file>.md` (PRD §21.2's own literal `[SOURCE]` example)
needs an explicit `target:`.

The first C-13 fix (2026-08-16, P08's OP-3 repair) denylisted only the single name `"tasks"`,
and a second independent OP-2 audit, the same day, proved the identical bug still live
against `fixtures/perturbations/` — the denylist's own claim ("any future shared, non-fixture
directory") did not match what the code actually did. The positive, filesystem-backed check
above (C-13's correction) is what closed it for good, along with **`E-SCEN-012`** (below):
`target.fixture` naming a fixture that doesn't exist — inferred *or* explicit — is now an
error at validation time, not a silent pass followed by a failure deep into a run (**C-15**).
`target.graph` is never checked this way, since a user-owned graph is legitimately resolved
best-effort, not required to exist in `fixtures/`.

### `hypothesis`

```yaml
hypothesis:
  task_success: ">= 0.9"
  p95_virtual_duration_ms: "<= 45000"
  max_token_spend: "<= 500000"
```

Each value is a comparison expression: an operator (`>=`, `<=`, `==`, `>`, `<`) followed by a
number, e.g. `">= 0.9"`. A value that doesn't parse this way is **`E-SCEN-011`**.

### `faults`

```yaml
faults:
  - type: agent_crash
    agent: reviewer
    at_virtual_ts: 2400
    recoverable: false
```

A list of fault declarations. Each entry needs:

- `type`: one of the [fault catalogue](#fault-catalogue) names. Unknown type: `E-SCEN-011`.
- **exactly one** target field for that fault type (e.g. `agent`, `edge`, `tool`,
  `state_key`, `provider` — see the catalogue). Zero or more than one: `E-SCEN-011`.
- **exactly one** trigger field for that fault type (e.g. `at_virtual_ts`, `at_span_n`,
  `after_n_messages`, `on_state_write`, `probability_permille`, `always`). Zero or more than
  one: `E-SCEN-011`.
- any fault-specific parameters the catalogue names for that `type` (e.g. `agent_crash`
  also accepts `recoverable`, `restart_after_ms`, `allow_total_failure`).

**Parameter and trigger *values* are checked, not just their names (OP-3 repair, 2026-08-16).**
Every trigger field has a type (`at_virtual_ts`/`at_span_n`: non-negative int;
`after_n_messages`: int ≥ 1; `probability_permille`: int in `[0, 1000]`; `on_state_write`:
non-empty string; `always`: bool), and every parameter PRD §12.2's "Safety" row bounds is
enforced: `message_reorder.window ≤ 16`, `message_duplicate.copies ≤ 5`, `agent_slow
.factor_milli ≤ 100_000` (`factor ≤ 100` under ADR-007). A value of the wrong type or outside
its bound is **`E-SCEN-011`**. Not enforced, and not silently guessed at: `agent_crash`'s
"cannot crash the last live agent unless `allow_total_failure: true`" bound depends on the
graph's live-agent count at fire time, which this static, pre-execution validator cannot
know — that check belongs to `runtime/faults/` (P09).

A target naming an agent/tool/edge not present in a *resolvable* graph is **`E-SCEN-005`**
(see [Graph identity resolution](#graph-identity-resolution)). A fault against a user graph
with no `chaos_opt_in`/`blast_radius`, or targeting something outside the declared
`blast_radius`, is **`E-SCEN-004`** — see [Chaos safety](#chaos-safety-invariant-i12).

Declaring a fault here does **not** execute it. `runtime/faults/` (P09) is what fires a fault
at run time; this module only validates that the declaration is well-formed and authorised.

#### Fault catalogue

Transcribed from PRD §12.2. STOP CONDITION 1's original cross-check against §21.1's worked
example covered `agent_crash` only ("no disagreement found" was true, but narrower than it
read); an OP-2 audit (2026-08-16) found `tool_failure`'s trigger vocabulary had never been
checked at all — see the table note below and **C-14**. Two parameters are integer-typed per
ADR-007 rather than the bare floats PRD §12.2 shows: `agent_slow.factor` → `factor_milli`
(int, milli-units), and every `probability` parameter → `probability_permille` (int, 0-1000).

| Fault type | Tier | Target(s) | Trigger(s) | Parameters |
|---|---|---|---|---|
| `latency` | P0 | edge, agent | `at_virtual_ts`, `after_n_messages`, `always` | `delay_ms`, `jitter_ms`, `pattern` |
| `message_drop` | P0 | edge | `at_virtual_ts`, `after_n_messages`, `always` | `probability_permille` |
| `message_reorder` | P1 | edge | `at_virtual_ts`, `after_n_messages`, `always` | `window` (≤ 16) |
| `message_duplicate` | P1 | edge | `at_virtual_ts`, `after_n_messages`, `always` | `probability_permille`, `copies` (≤ 5) |
| `agent_crash` | P0 | agent | `at_virtual_ts`, `at_span_n`, `on_state_write` | `recoverable`, `restart_after_ms`, `allow_total_failure` |
| `agent_slow` | P1 | agent | `at_virtual_ts`, `always` | `factor_milli` (≤ 100_000) |
| `tool_failure` | P0 | tool | `always`, `at_virtual_ts`, `probability_permille` | `mode`, `count` |
| `rate_limit` | P1 | provider | `always` | `rps_cap`, `retry_after_ms` |
| `byzantine` | P1 | agent | `at_span_n`, `always` | `mode`, `pool` |
| `state_corrupt` | P1 | state_key | `at_virtual_ts`, `on_state_write` | `mutation` |

**`tool_failure`'s trigger column (C-14).** PRD §12.2's literal text reads `always | first_n |
after_virtual_ts | probability`, but `first_n` has no case in §12.3's canonical `should_fire`
pseudocode at all, and `after_virtual_ts` is that pseudocode's `AtVirtualTs` case under a
different name. Before the OP-3 repair, the code substituted `after_n_messages` (a
Transport-class, edge-message concept with no clear meaning for a tool-targeted fault) for
both, without ever cross-checking §12.3 or logging a ruling. `at_virtual_ts` is now accepted
(the clean match for `after_virtual_ts`); `first_n` has no §12.3 equivalent and is not
supported.

"Tier" is which fault-execution milestone owns running it (`P0` = MVP fault set,
`P1` = the six later fault types) — every type validates identically regardless of tier; a
scenario may declare a `P1` fault today even though nothing executes it yet.

### `guards`

```yaml
guards:
  max_virtual_duration_ms: 120000
  max_tokens: 200000
  max_retries: 20
  max_wall_duration_s: 300
  max_events: 500000
  max_llm_calls: 500
```

Abort guards (PRD §13.6). Every key defaults to the value shown (the PRD's own defaults).
Each value must be a positive integer, and must not exceed a configured **hard ceiling** — a
value above the ceiling is **`E-SCEN-008`**. Ceilings are read from `agentdx.toml`'s
`[scenario]` section (keys `guard_ceiling_max_virtual_duration_ms`, etc.); if that section is
absent, the ceiling defaults to 10x the PRD default for each key. Ceilings exist so a scenario
cannot structurally request an unbounded run (P09 "cannot receive an unbounded fault
request" — Design Constraint 2) — they are a validation-time concept the PRD does not number,
distinct from the *guard values* the PRD does specify.

### `baseline`

```yaml
baseline:
  generate: true
  prompt: null
  allow_low_comparability: false
```

Whether a baseline run is generated for comparison (PRD §17.4/17.5). `generate` defaults to
`true`. `speedup_vs_baseline` (an assertion) is not measurable when `generate: false` —
`E-SCEN-007`, see [Assertions](#assertions).

### `exploration`

```yaml
exploration:
  enabled: false
  k: 2
  max_schedules: 200
```

Schedule-exploration knobs (out of scope for execution in this module; validated for shape
only).

### `success_check` (the pluggable hook)

```yaml
success_check:
  type: python
  ref: "fixtures.code_pipeline.checks:task_success"
```

- `type`: `python` (import `ref` as `module:function`), `shell` (run `ref` as a shell
  command, exit code 0 = pass), or `none` (default — no check configured; `task_success` is
  then not measurable).
- `ref`: required when `type` is `python` or `shell`.

**Both forms share one wall-clock timeout (PRD §21.6's default), read from `agentdx.toml`'s
`[scenario].success_check_timeout_s`, not hardcoded.** `type: shell` has always enforced this
via `subprocess.run(timeout=...)`. `type: python` did not, until a second independent OP-2
audit (2026-08-16) found a hung or looping `checks.py` function would block indefinitely —
`run_python_success_check` now enforces the same limit via `signal.alarm`, the simplest
mechanism that can interrupt an arbitrary in-process call without threads or subprocesses.
Declared limitation: `SIGALRM` only exists on Unix and only fires in the main thread (this
project's CI targets `ubuntu-latest`/`macos-14` only); called from a worker thread or where
`SIGALRM` is unavailable, the timeout silently does not apply and `fn` runs unbounded.

**Trust boundary (Design Constraint 5).** A `type: python` check is loaded by `importlib`
at validation time (`E-SCEN-009` if the import or attribute lookup fails) and again at
evaluation time (`assertions.load_success_check`). This is sanctioned for a fixture's own
`checks.py` (PRD §13.3's sandboxed fixture set) or a scenario's own committed `checks.py` —
**never** for a `ref` resolved from a path inside an extracted `.agentdx` bundle. A bundle's
`scenario.yaml` is data the bundle *exporter* wrote; loading a bundle-supplied `ref` would let
an imported bundle execute arbitrary code on import. Keeping bundle-derived refs away from
this loader is `store/bundle.py`'s and `cli/`'s responsibility (its "member allowlist, no
dynamic import" design) — this module only documents the boundary; it does not import bundles.

A `type: python` function's signature: `(final_state: dict, run: RunSummary) -> bool |
tuple[bool, str]`. A `type: shell` command's exit code is the result; stdout (last 500 bytes)
becomes the assertion detail.

### `assertions`

```yaml
assertions:
  - no_state_conflicts
  - no_silent_failures
  - deterministic_replay
  - task_success
  - speedup_vs_baseline: ">= 1.0"
  - resilience_score: ">= 70"
  - token_cost_multiplier: "<= 3.0"
  - max_findings: {severity: high, count: 0}
  - critical_path_share: {edge: "coder->reviewer", cmp: "<= 0.4"}
```

Each entry is either a bare name (no parameters) or a single-key mapping `{name: params}`.
See [Built-in assertions](#built-in-assertions) for what each one measures and when it is
*not measurable* (`E-SCEN-007`) rather than pass/fail.

## Chaos safety (invariant I12)

The scenario is the **authorisation surface** for chaos (PRD §13.10) — `runtime/faults/`
(P09) is not built to accept an unbounded fault request; blast radius, the steady-state
hypothesis, and the abort guards are declared *here*, not passed in at run time.

A fault targeting a shipped fixture (`target.fixture`) needs no extra authorisation — fixtures
are already a sandboxed, version-controlled set (PRD §13.3). A fault targeting a **user
graph** (`target.graph`) requires *both*:

1. `chaos_opt_in: true`, and
2. a non-empty `blast_radius` — at least one of `blast_radius.agents`, `.tools`, `.edges`,
   `.state_keys`, `.providers` must be non-empty.

Missing either is **`E-SCEN-004`**. Additionally, every fault's target must fall *inside* the
declared blast radius for its kind (an `agent_crash` targeting `worker` requires `worker` in
`blast_radius.agents`; a `state_corrupt` targeting `session.*` matches by glob against
`blast_radius.state_keys`) — a target outside the declared radius is also `E-SCEN-004`.

```yaml
target:
  graph: "./app.py:build_graph"
chaos_opt_in: true
blast_radius:
  agents: [worker]
faults:
  - type: agent_crash
    agent: worker
    at_virtual_ts: 1000
```

## Graph identity resolution

`E-SCEN-005` (an unrecognised fault target) and the `blast_radius` glob check need to know
what agents/tools/edges a graph actually has. This is resolved **statically**, by parsing the
target `graph.py` (or the fixture's `graph.py`) with Python's `ast` module and reading
`add_node(...)`/`add_edge(...)` calls and `@tool("name")`-decorated functions — the file is
never imported or executed. Two reasons: `scenario/` imports nothing else in the package (a
project-wide layering rule), and validation must be safe to run on scenario files pointing at
arbitrary user code, before any of it is trusted to execute. If the graph file cannot be found
or parsed, resolution silently returns "unknown" and the corresponding checks are skipped
(best-effort, never a crash) — this is a known limitation, not a defect.

`E-SCEN-005` covers `agent`, `tool`, and (since the OP-3 repair, 2026-08-16) `edge` targets —
`graph_identity.edges` was already collected and used by `critical_path_share` before this,
but not checked here until an OP-2 audit found a fabricated edge validated cleanly.
`state_key` and `provider` targets are never checked against graph identity: state keys are
not part of a graph's static shape (a static catalogue of them would have a real
false-negative rate, since `agentdx.state().write(...)` calls can be conditional/dynamic),
and providers are not graph elements at all.

## Versioning

`version:` defaults to `1` when absent — PRD §21.2's own `[SOURCE]` example has no
`version:` key and must still validate (this is the P08 prompt's ruling for the one place
§21.2 and §21.3's error table read as being in tension: §21.3 covers an explicit-and-wrong
version, not an absent one). An explicit `version` that isn't `1` (the only schema version
this build supports) is **`E-SCEN-001`**.

## `extends` — reuse and composition

```yaml
# child.yaml
extends: base.yaml
scenario: child
guards:
  max_tokens: 50000
```

`extends` names another scenario file (relative to the extending file, unless absolute). The
child is deep-merged onto the parent: a mapping value merges key-by-key (recursively); any
other value, **including a list**, replaces the parent's value wholesale — PRD §21.5 is
explicit that faults are not concatenated ("the classic 'inherited fault I did not intend'
hazard"), so a child with its own `faults:` entirely replaces the parent's.

A missing or unreadable `extends` target, or a chain that cycles back on itself, is
**`E-SCEN-006`**.

## Defaults and `scenario_hash`

All defaults are applied by exactly one function, `loader.resolve_defaults` (Design
Constraint 4) — never scattered across individual field checks. The **fully-resolved**
scenario (defaults applied, `extends` merged, `target` inferred) is what gets recorded into
run metadata as `scenario.yaml`, never the sparse file the author wrote — so a run is
reproducible from the recorded scenario alone, without needing the original file or its
`extends` chain. `scenario_hash` is a sha256 over the resolved document's canonical
(sorted-key) JSON form, so two files differing only in which optional key was spelled out
explicitly hash identically.

## Matrix expansion

```yaml
matrix:
  seed: [1, 2, 3]
  "faults[0].type": [agent_crash, tool_failure]
```

Expands one scenario document into the cross product of its `matrix:` entries — six derived
documents for the example above (3 seeds × 2 fault types), each with a deterministic id like
`kill_reviewer__faults[0].type-agent_crash__seed-1`.

A matrix key is either a **bare alias** (`seed`, `mode`, `repeats` — the three top-level
scalars unambiguous enough not to need a path) or a **dotted/bracketed path** into the
document, in the same shape `validate.py` reports errors against: `faults[0].type`,
`guards.max_tokens`. PRD §21.5's own illustrative example uses a bare `fault_type` key, which
does not correspond to any top-level field (a fault's type lives at `faults[i].type`, and
nothing in §21.1/§21.5 says which `i`) — this is a genuine PRD gap; under this scheme that
example must be written `"faults[0].type"`. A bare key that isn't one of the three aliases,
or a malformed path, is rejected with an error naming this convention explicitly.

**A caller must re-validate every expansion.** `expand_matrix` does not call
`validate.validate()` on its own output — it substitutes matrix values into an already-valid
base document, but a substitution can produce an invalid one (e.g. swapping `faults[0].type`
from `agent_crash` to `tool_failure` leaves `agent_crash`-only fields on the entry, which
`tool_failure` does not accept). Every `MatrixExpansion.document` must be passed back through
`validate.validate()` before it is used for anything, the same as any other scenario
document — `tests/unit/scenario/test_matrix.py::test_expansion_output_must_be_revalidated_by
_the_caller` demonstrates both that a substitution can break validity and that re-validating
catches it. This was undocumented before the P08 OP-3 repair (2026-08-16).

**Determinism (Design Constraint 3).** Matrix keys are sorted explicitly before the cross
product is built — never relied on as "mapping order happens to be stable enough" — so
expansion is byte-identical across repeated calls, processes, and platforms for the same
input document.

## Built-in assertions

PRD §21.7, transcribed. Every assertion's `status` is `passed`, `failed`, or
`not_measurable` — the third is not a failure. Several read scorecard/verdict metrics that
only exist once the analysis layer (P10/P11/P12, not built) computes them; this module
implements the full evaluation *interface* against those metrics (returning
`not_measurable` when a metric isn't available) but cannot itself produce a real metric —
that is out of scope here (P08 STOP CONDITION 2, resolved by the prompt's own OUT OF SCOPE
section).

| Assertion | Parameters | Not measurable when |
|---|---|---|
| `no_state_conflicts` | — | never (findings-based) |
| `no_silent_failures` | — | the scenario declares no `faults` |
| `speedup_vs_baseline` | comparison string | `baseline.generate` is `false`, or the metric isn't yet computed |
| `resilience_score` | comparison string | the scenario declares no `faults`, or the metric isn't yet computed |
| `token_cost_multiplier` | comparison string | the metric isn't yet computed |
| `max_findings` | `{severity, count}` | never (findings-based) |
| `critical_path_share` | `{edge, cmp}` | the metric isn't yet computed |
| `task_success` | — | no `success_check` is configured (`type: none`) |
| `deterministic_replay` | — | the replay-verification metric isn't yet computed |

An assertion referencing an edge not present in a resolvable graph, or requiring a
metric/precondition that structurally cannot be satisfied by the rest of the document (e.g.
`resilience_score` with no `faults:`), is **`E-SCEN-007`** — this is caught at validation
time, before any run, so a scenario author learns "this assertion can never pass" without
spending a run to find out.

## Error codes

| Code | Meaning |
|---|---|
| `E-SCEN-000` | Malformed YAML syntax, or the document root isn't a mapping. |
| `E-SCEN-001` | Unsupported (explicit) `version`. |
| `E-SCEN-002` | Unknown key (top-level or nested). |
| `E-SCEN-003` | `target` has both `fixture` and `graph`, or neither and none could be inferred. |
| `E-SCEN-004` | Chaos-safety gate: fault against a user graph missing `chaos_opt_in`/`blast_radius`, or a target outside the declared blast radius (invariant I12). |
| `E-SCEN-005` | A fault's target isn't present in the (resolvable) graph. |
| `E-SCEN-006` | `extends` target missing, unreadable, or cyclic. |
| `E-SCEN-007` | An assertion is structurally not measurable given the rest of the document. |
| `E-SCEN-008` | A `guards` value exceeds its configured hard ceiling. |
| `E-SCEN-009` | `success_check.ref` cannot be imported. |
| `E-SCEN-010` | A required top-level key (`scenario`, `task`) is missing. |
| `E-SCEN-011` | A value has the wrong shape/type for its field. |
| `E-SCEN-012` | `target.fixture` (inferred or explicit) does not name a real, shipped fixture (C-15). Never fires for `target.graph`. |

Every error carries the offending value's exact source line (from the position-preserving
parser in `loader.py`) and a suggested fix — never just "invalid scenario".
