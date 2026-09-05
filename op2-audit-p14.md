# OP-2 INDEPENDENT AUDIT — P14 `api/` (REST + WebSocket + OpenAPI)

**Scope.** `src/agentdx/api/{app,deps,errors,models,ws}.py`, `src/agentdx/api/routes/
{analysis,findings,runs,scenarios,scorecard,system}.py`, `src/agentdx/api/__init__.py`
(~3,500 lines); `tests/api/{conftest,test_analysis,test_app_serve,test_findings_scorecard,
test_runs,test_scenarios,test_system,test_ws}.py`; `docs/api.md`. This is P14's first **full**
OP-2 sign-off. A prior, explicitly *partial* review (2026-08-24, `docs/journal/2026-33.md`)
found and fixed one real I12 fail-open bug in `inject_fault`, declared two further findings
open, and explicitly stated "P14's full OP-2 sign-off against its complete `DELIVERABLES` and
gates was not performed here and remains owed." That debt is what this audit discharges.

**Method.** Read `CONTEXT.md` in full (§0, §2 — especially I12 — §4's layer table, §5 row 14,
§11 tripwire 18), `AGENTS.md`, PRD §26/§26.1/§26.2/§26.3, §13.3–§13.10, §33.12, `docs/api.md`,
and the full text of every scope file end to end. **Environment constraint, confirmed
independently, not merely trusted**: `python3 --version` in this sandbox is 3.10.12;
`ast.parse()` on `models.py` fails at line 20 (`type JsonValue = ...`, PEP 695) with
`SyntaxError: invalid syntax`; separately, `agentdx/__init__.py` unconditionally imports
`agentdx.config`, which does `import tomllib` (3.11+ stdlib), so **no `agentdx.*` module is
importable at all** in this sandbox, not just `api/` — confirmed by direct attempt
(`ModuleNotFoundError: No module named 'tomllib'`). The repo's own `.venv` is a dead symlink to
a macOS Anaconda interpreter that does not exist here. This matches the disclosed D-66 gap and
is not reported as a new finding. Consequently, every finding below that needed live execution
was demonstrated by copying the exact, cited lines of the real source **verbatim** into a
throwaway script (`/tmp/op2audit/*.py`, never committed, repo never edited) and running them
standalone — never by asserting "this looks wrong" without running anything. Three such scripts
were run; each is described inline. Existing test files were read as text (not executed) and
checked for what they actually assert vs. what they claim to guard.

---

## VERDICT: **FAIL**

The one previously-fixed defect (I12 fail-open on an unresolvable scenario) holds up under
adversarial re-test. But this audit found **5 new findings**, including one **CRITICAL** defect
that reopens I12 through a path the prior repair explicitly assumed was closed off, and one
**HIGH** defect in the same authorization function that was never audited at all. `api/` is
broad, honestly self-documented (its own `docs/api.md` "declared gaps" section is unusually
candid and mostly checks out), and its error-envelope/WebSocket-cursor engineering is genuinely
careful — but the chaos-authorization surface, the one place this module enforces a numbered
invariant against real safety consequences, has real, demonstrated holes.

1. **(CRITICAL)** I12 is completely bypassable via `POST /api/import`. `store/bundle.py`'s
   integrity verification hashes `events.jsonl` only — `run.json`'s own fields (`status`,
   `scenario_id`, `mode`, `seed`, ...) are never hash-checked against anything. A hand-edited
   bundle (`run.json.status = "running"`, `scenario_id` omitted) imports cleanly and produces a
   `RunRecord(status="running", scenario_id=None)`. `inject_fault`'s entire I12 block is gated
   behind `if record.scenario_id is not None:` — `scenario_id is None` skips authorization
   entirely and falls straight to arming. The prior repair (2026-08-24) explicitly reasoned this
   state was "unreachable through the shipped API today" because `POST /api/runs` always sets
   `scenario_id`; that reasoning did not consider `POST /api/import`, a second, real, shipped
   endpoint that can produce exactly this state from attacker-controlled input.
2. **(HIGH)** `_check_chaos_authorization`'s blast-radius check flattens `agents`/`tools`/
   `edges`/`state_keys`/`providers` into one undifferentiated list and checks only "is this
   target string present somewhere," never which category it was declared under or which
   category the fault being armed actually operates in. Declaring a blast radius for one
   category (e.g. `tools: [external_api]`) silently authorizes any P0 fault whose target string
   collides with that name, including fault types (`latency`, `message_drop`) whose targets are
   never existence-checked against a real roster at all.
3. **(MEDIUM)** The one regression test guarding I10's verbatim-string requirement on
   `GET /runs/{id}/exploration`'s error message (`test_get_exploration_always_409_in_this_build`)
   checks only loose substring keywords ("coverage" / "bounded"), not the actual mandated
   sentence. A mutation that deletes the real I10 sentence entirely still passes the test,
   demonstrated live.
4. **(MEDIUM)** The two findings the partial 2026-08-24 review flagged and deliberately left
   open are both still open, confirmed by rereading the live files: `.importlinter`'s
   `api-never-imports-runtime` contract still does not forbid `agentdx.scenario` even though
   `CONTEXT.md` §4's prose table lists only `store, analysis` as `api/`'s permitted imports; and
   the ledger-citation problem is not just in stale session narrative — it is now baked into a
   **live, shipped** `pyproject.toml` comment that cites "ADR-014" as authorizing the
   `python-multipart` dependency, while the real ADR-014 (`CONTEXT.md` §8) is an unrelated
   determinism-exception clause. No ADR anywhere authorizes this dependency, in violation of
   `AGENTS.md` §2.
5. **(LOW-MEDIUM, coverage only)** `ws.py`'s flow-control/backpressure-sampling machinery
   (`_await_flow_control`, `_handle_ack`, `_maybe_update_sampling`) — a documented, load-bearing
   part of PRD §26.2 — has zero test coverage in `tests/api/test_ws.py`. No bug demonstrated;
   flagged as an honest gap, not asserted as broken.

---

## FINDING #1 (CRITICAL) — I12 is fully bypassable by importing a bundle with a hand-edited `run.json`

**Where:** `src/agentdx/store/bundle.py:493-554` (`_verify_contents`, never references
`contents.run_payload`) and `:981-1010` (`_run_record_from_payload`, builds `status`/
`scenario_id` straight from that unverified payload); `src/agentdx/api/routes/runs.py:582-596`
(`inject_fault`'s I12 block, gated by `if record.scenario_id is not None:`).

```python
# bundle.py:995-1010 — status and scenario_id come straight from the caller-supplied
# run.json, with ZERO cross-check against the actually-verified event log content:
return RunRecord(
    run_id=manifest.run_id,
    scenario_id=_opt_str(run.get("scenario_id")),
    ...
    status=str(run.get("status", "complete")),
    ...
)
```

```python
# runs.py:582-596 — the entire I12 authorization block lives inside this guard:
if record.scenario_id is not None:
    scenario = store.get_scenario(record.scenario_id)
    if scenario is None:
        raise ScenarioUnresolvableForChaosError(run_id, record.scenario_id)
    ...
    _check_chaos_authorization(resolved, body.target)
# scenario_id is None -> falls straight through to state.fault_controller.arm(...)
```

`_verify_contents` (bundle.py:493-554) checks `manifest.canonical_log_hash`,
`manifest.chain_head`, `manifest.event_count` and the per-event hash chain — all computed **only
from `events.jsonl`**. It never reads, hashes, or validates `contents.run_payload` (the
`run.json` member) at all. Nothing in the `.agentdx` format binds `run.json`'s `status`/
`scenario_id` fields to the verified log.

**Why this is reachable, not contrived.** `POST /api/import` is a real, shipped, fully-
implemented endpoint (`routes/runs.py::import_run`), not a declared gap — `docs/api.md` lists it
among the live endpoints with no caveat. Anyone who can export *any* legitimate run (including
one they made from a fixture, requiring no privilege at all — `GET /runs/{id}/export` needs
nothing but a valid `run_id`) can unzip the resulting `.agentdx` file, edit the plain-JSON
`run.json` member to set `"status": "running"` and delete/null its `"scenario_id"` key, re-zip,
and `POST` it to `/api/import`. The event-log hash/chain checks all still pass (the events file
was never touched), so `_verify_contents` reports `ok=True` and `import_bundle` proceeds. The
resulting run is live in the store with `status="running"`, `scenario_id=None`, and a real,
non-empty agent roster (from the untouched event log) — a normal-looking target for
`POST /runs/{id}/faults`. The prior repair's own docstring (`runs.py:26-32`) frames the
`scenario_id is None` state as "not reachable through the shipped API today" and treats closing
it as a deferred product decision rather than a live gap — that framing is incorrect because it
did not consider `POST /api/import` as a second run-creation path.

**Demonstrated** (static evidence quoted above, verbatim from the real source, plus a live,
standalone run reproducing the actual end state — `/tmp/op2audit/audit_i12_import_bypass.py`,
which copies `_opt_str` and the two field extractions from `_run_record_from_payload` verbatim,
plus `inject_fault`'s I12-gating shape verbatim, then feeds it an attacker-shaped `run.json`):

```
Step 2: import_bundle() -> _run_record_from_payload(manifest, run_payload) builds:
        RunRecord(status='running', scenario_id=None)

Step 3: POST /api/runs/{run_id}/faults on this imported, now-'running' run:
        -> I12_SKIPPED_ARMED
        <-- I12 (CONTEXT.md invariant, chaos-safety) COMPLETELY BYPASSED
```

The real `agentdx.*` package could not be imported end-to-end in this sandbox (`tomllib`/
PEP-695 blockers, see Method) — the `RunRecord`/field-extraction/gating logic above is copied
character-for-character from the cited line ranges, not reimplemented from memory or
paraphrased, and the two call sites were re-read against the live files a second time
immediately before writing this finding to confirm no intervening logic was omitted.

**Honest calibration of current real-world impact.** `agentdx ui`'s default wiring configures no
`FaultController` (`docs/api.md`: "`agentdx ui`'s default wiring supplies neither"), so today,
out of the box, `inject_fault` still reaches `if state.fault_controller is None: raise
FaultControlUnavailableError()` (`503`) — but that check runs **after** the I12 block, not
before, so the authorization bypass itself is live and provable independent of whether a
controller is wired in. The moment any `FaultController` is configured (the protocol is already
"fully tested against fakes ... wiring a real implementation in later is a matter of passing it
to `create_app(...)`" per `deps.py`'s own docstring, and `tests/api/conftest.py::
FakeFaultController` already exercises the happy path), this bypass arms a real fault with zero
authorization, on the very next prompt (P17-adjacent) that wires one in.

**Confirmed not already covered by any existing test.** `grep -rln "import_bundle\|/api/import"
tests/api/` finds only `test_runs.py`, whose one import-related test
(`test_export_then_import_round_trips`) imports a `sealed_run` (i.e. an already-`complete` run)
and never re-targets the imported run with a fault. No test anywhere constructs an imported run
with `status="running"`/`scenario_id=None` and no test anywhere calls
`POST /runs/{id}/faults` against an imported run. `tests/unit/store/test_bundle_safety.py`
(`grep -n "status\|scenario_id\|run\.json"` — zero matches) and
`tests/integration/store/test_bundle_roundtrip.py` likewise never test `run.json` metadata
tampering independent of the event log.

**Severity:** CRITICAL. This is the exact defect class tripwire 18 was written to generalize
("a function enforcing a numbered invariant has a call site where ... a missing-row check
causes the function to be skipped rather than the operation refused"), on the same invariant
(I12) the tripwire's own origin story is about, through a path the prior repair session did not
consider existed.

**Suggested fix direction (not mandatory).** The cleanest fix follows the same shape as the
already-fixed sibling bug: treat `scenario_id is None` on a `status="running"` run the same way
`scenario_id` set-but-unresolvable is now treated — refuse (`ScenarioUnresolvableForChaosError`
or a new sibling code) rather than skip. Separately, and independently worth considering:
`import_bundle` accepting a caller-supplied `status="running"` at all is questionable regardless
of I12 — an imported bundle is by definition a *completed* run's evidence; nothing in this
codebase can resume executing an imported "running" run, so accepting that status may deserve
its own validation at the bundle layer, not only a downstream authorization patch.

---

## FINDING #2 (HIGH) — I12's blast-radius check ignores category, authorizing faults across unrelated blast-radius declarations

**Where:** `src/agentdx/api/routes/runs.py:502-521` (`_check_chaos_authorization`).

```python
blast_radius = resolved.get("blast_radius")
declared: list[str] = []
if isinstance(blast_radius, dict):
    for value in blast_radius.values():          # <- every category, flattened together
        if isinstance(value, list):
            declared.extend(str(v) for v in value)
if not declared:
    raise ChaosAuthorizationError.blast_radius_empty(blast_radius)
if target not in declared:
    raise ChaosAuthorizationError.target_outside_blast_radius(target, blast_radius)
```

PRD §13.4's blast radius is explicitly five distinct categories (`agents`, `tools`, `edges`,
`state_keys`, `providers`) — "who may be affected" is a different question from "which tools may
be failed." `_check_chaos_authorization` discards that distinction: it flattens every category's
values into one bag of strings and only ever asks "is this target string present somewhere,"
never "was this target declared under the category this specific fault type actually operates
in."

**Why this is reachable, not contrived.** `FAULT_CATALOGUE`'s P0 tier includes `latency`
(`target_kinds=(EDGE, AGENT)`) and `message_drop`/`tool_failure` (single-kind, but still never
existence-checked — see `inject_fault`'s own docstring: "only checked when the fault's
`target_kinds` is unambiguously `(agent,)`"). A scenario author who writes
`blast_radius: {tools: [external_api]}`, intending to authorize only a `tool_failure` on the
tool named `external_api`, and who deliberately leaves `agents:`/`edges:` empty (believing —
correctly, per the PRD's own category semantics — that this means no agent or edge is in scope),
has in practice also authorized a `latency` fault targeting anything named `external_api`
interpreted as an agent or edge, and a `tool_failure` never gets checked against a real tool
roster at all, so any string collision anywhere in `blast_radius`'s five lists authorizes any P0
fault whose own target existence-check is skipped.

**Demonstrated** — real, verbatim-copied `_is_graph_target`/`_check_chaos_authorization` from
`routes/runs.py:159-162,502-521`, run standalone (`/tmp/op2audit/audit_i12_crosscheck.py`,
`ChaosAuthorizationError`'s three classmethods stubbed with the identical shape):

```
Case A: tool_failure on declared tool 'external_api' (expect AUTHORIZED)
  -> AUTHORIZED (correct)
Case B: a latency fault (target_kinds = EDGE|AGENT) whose target string is
        'external_api' interpreted as an AGENT id -- never declared under
        blast_radius.agents, only under blast_radius.tools:
  -> AUTHORIZED  <-- cross-category leak: agent fault authorized via a tools: entry
Case C: sanity check - agent name never mentioned anywhere is refused:
  -> REFUSED (expected) -> target 'totally_unrelated_agent' outside blast radius
```

Case C proves the function is not simply broken end to end (an unrelated name is correctly
refused) — the leak is specifically the cross-category collision, not a wholesale absence of
checking.

**Confirmed not already covered by any existing test.** Every I12 test in
`tests/api/test_runs.py` (`test_inject_fault_graph_target_without_opt_in_403`,
`..._empty_blast_radius_403`, `..._outside_blast_radius_403`, `..._inside_blast_radius_202`) uses
`agent_crash` exclusively (the one fault type whose target *is* existence-checked and whose
`target_kinds` is exactly `(AGENT,)`) and declares/checks only the `agents:` category. No test
anywhere declares a multi-category `blast_radius` or arms a fault whose `target_kinds` spans more
than one category.

**Severity:** HIGH — this is a real gap in the one function this module has that enforces I12
against real chaos-safety consequences, though (same caveat as Finding #1) currently unreachable
in practice only because no `FaultController` is wired into `agentdx ui`'s default configuration
yet.

**Suggested fix direction (not mandatory).** `_check_chaos_authorization` needs the fault's own
`target_kinds` (already available at the call site via `spec.target_kinds`) to know which
`blast_radius` categories are even relevant, and should check the target against only those
categories' declared values — e.g. an `agent`-kind fault checks `blast_radius.get("agents",
[])`, never `blast_radius.get("tools", [])`.

---

## FINDING #3 (MEDIUM) — I10's regression test for `GET /runs/{id}/exploration` has no discriminating power for the mandated verbatim sentence

**Where:** `tests/api/test_analysis.py:206`; the message it is meant to guard is
`src/agentdx/api/routes/analysis.py:294-300`.

```python
# tests/api/test_analysis.py:206 — the entire assertion:
assert "coverage" in body["error"]["message"].lower() or "bounded" in body["error"]["message"]
```

I10 (`CONTEXT.md` §2) requires: *"The bounded-exploration coverage statement appears **verbatim**
— 'Bounded search: absence of findings is not proof of absence.' ... Removing it is a release
blocker."* The route's real message does currently contain that exact sentence. The test,
however, only checks for the loose, disjunctive presence of the words "coverage" or "bounded"
(and the second branch isn't even case-folded), not the sentence itself.

**Why this is reachable, not contrived.** This is the *only* place in `tests/api/` that touches
the I10 string at all (`grep -rn "Bounded search|coverage_statement" tests/api/` — zero matches
anywhere, including this test). If a future edit to `analysis.py`'s error message (a copy tweak,
a rephrase, an accidental truncation) drops the mandated sentence while retaining any loose
mention of "coverage" or "bounded," this is the one test that exists specifically to catch that
and it would not.

**Demonstrated** — the exact assertion from line 206, copied verbatim, run against a message with
the real I10 sentence deleted (`/tmp/op2audit/audit_i10_test_weakness.py`):

```
I10 sentence present in mutated message: False
Mutated message -> True (test still PASSES even though the I10-mandated verbatim sentence is entirely gone)
```

**Confirmed not already covered by any existing test.** No other test in `tests/api/` or
elsewhere under `tests/` checks this specific route's message content, and `ExplorationResponse.
coverage_statement` (the schema field) is never populated in practice — the endpoint always
`409`s in this build — so the schema-level field offers no independent backstop either.

**Severity:** MEDIUM — I10 is an invariant with a release-blocker consequence attached in its own
definition; the guard for it on this specific route is real but toothless.

**Suggested fix direction (not mandatory).** Assert the literal sentence
(`"Bounded search: absence of findings is not proof of absence." in body["error"]["message"]`),
the same verbatim-string discipline `AGENTS.md`/I10 already demand of the shipped code.

---

## FINDING #4 (MEDIUM) — Both items flagged open by the 2026-08-24 partial review are still open, and the ledger-citation problem is now live in shipped source

**Where:** `.importlinter` (repo root); `pyproject.toml:35`; `CONTEXT.md` §8 (ADR-014's real
entry).

**(a) `.importlinter`'s `api-never-imports-runtime` contract still does not name `scenario`.**
Re-read directly: `forbidden_modules` for that contract is `runtime`, `sdk`, `explore`, `cli` —
no `scenario`. `CONTEXT.md` §4's prose table lists `api/`'s "May import" column as exactly
`store`, `analysis` — `scenario` is absent from that list too, on either side. The code
demonstrably imports it regardless: `routes/runs.py` (`from agentdx.scenario import loader,
validate` / `from agentdx.scenario.schema import FAULT_CATALOGUE, ...`) and `routes/scenarios.py`
both do, for real, load-bearing reasons (scenario resolution for `POST /api/runs`, I12
authorization itself). No new ADR ratifying this (the way ADR-013 ratified the analogous
`runtime/` → `scenario` import) exists anywhere in `CONTEXT.md` §8 as of this audit.

**(b) The ledger-citation problem is not confined to session narrative — it is in the shipped
source.** `pyproject.toml:35`, in the dependency list, directly under the comment "Nothing may be
added here without an ADR in CONTEXT.md §8 (AGENTS.md §2)":

```toml
  # `python-multipart` — ADR-014. FastAPI's own multipart/form parser (Starlette calls into
  # it for `UploadFile`); required by `POST /api/import`, which PRD §26.1 specifies as a
  # multipart upload. Not one of ADR-004's enumerated distributions — flagged for owner
  # ratification in P14's closing blocks, following AGENTS.md §2's "ADR before any new
  # dependency" rule.
  "python-multipart>=0.0.12,<1.0",
```

The real `ADR-014` in `CONTEXT.md` §8 (read in full) is: *"A 5th sanctioned `AGENTS.md` §4.1
determinism exception: `AbortGuardMonitor`'s `max_wall_duration_s` guard evaluation may read
`agentdx.wall_time()`..."* — entirely unrelated to `python-multipart` or to `api/`. The same
comment block that cites "ADR-014" as the authority also, three lines later, admits the
dependency was only ever "flagged for owner ratification" — i.e. the citation and the admission
of non-ratification sit in the same six lines, contradicting each other. `grep -n
"python-multipart|ApiConfig|\[api\] section" CONTEXT.md` returns nothing: no ADR or D-row in the
actual ledger of record authorizes this dependency, `ApiConfig`, or the `[api]` `agentdx.toml`
section P14 also added beyond its named `DELIVERABLES`, today.

**Confirmed not already covered by any existing test/check.** `scripts/check_ledger.py` only
verifies that every `ADR-NNN` *referenced inside `CONTEXT.md` itself* has a §8 row — it does not
scan `pyproject.toml` or any other source file, so a wrong citation embedded in a shipped source
comment is invisible to CI. No test asserts `.importlinter`'s `api-never-imports-runtime`
contract forbids `scenario`.

**Severity:** MEDIUM — process/governance rather than a runtime defect, but a live,
standing violation of `AGENTS.md` §2 ("ask first, always" / "an ADR is required before any new
dependency... enters `pyproject.toml`"), unresolved for 12+ days across two audit passes now.

**Suggested fix direction (not mandatory).** (a) Either add `scenario` to
`api-never-imports-runtime`'s `forbidden_modules` and build the (larger) refactor that would
require, or write the ADR that ratifies `api/` → `scenario` the same way ADR-013 ratified
`runtime/` → `scenario`, and fix `CONTEXT.md` §4's prose table to match. (b) Write the actual
ADR for `python-multipart`/`ApiConfig`/`[api]` in `CONTEXT.md` §8, using a real, currently-unused
ADR number, and fix the `pyproject.toml:35` comment to cite it instead of the wrong one.

---

## FINDING #5 (LOW-MEDIUM, coverage gap, no bug demonstrated) — WS flow-control/backpressure logic is entirely untested

**Where:** `src/agentdx/api/ws.py:225-255` (`_await_flow_control`, `_handle_ack`,
`_maybe_update_sampling`); `tests/api/test_ws.py` in full.

PRD §26.2 documents flow control ("server pauses after 5,000 unacked events; client `ack`
resumes") and backpressure sampling ("switches to sampled mode... sends a `status` sampling
frame") as load-bearing protocol behavior, and `docs/api.md` describes both as implemented. They
are: `_handle_ack`/`_await_flow_control`/`_maybe_update_sampling` are real, reasoned
implementations (read closely — the `ack_event.clear()`-then-`await ...wait()` pattern is a
textbook-correct wakeup, not a missed-wakeup race, since no `await` point separates the two
statements the receiver task could interleave through).

**Confirmed not already covered by any existing test.** `grep -n "E-WS-003|ack|sampling|
flow_control|through_seq" tests/api/test_ws.py` matches only comment prose and unrelated words
("ack accordingly," "backlog") — zero tests send an `{"type": "ack", ...}` message, zero tests
drive the connection past `ws_flow_control_max_unacked` to observe a pause, and zero tests drive
three consecutive pauses to observe the `{"type": "status", "sampling": N}` escalation frame.
Malformed-`subscribe` `E-WS-003` paths (`from_seq` non-int, `filters.types` wrong shape) are also
untested.

**Severity:** LOW-MEDIUM. No defect is claimed here — this is a pure coverage gap in a protocol
surface the project's own `AGENTS.md` §5 testing standard ("every bug fixed gets a regression
test," tests written same-prompt as code) would ordinarily expect covered, and PRD §33.12 names
"WebSocket backlog-then-live ordering... across a forced reconnect" as a required test but is
silent on flow control specifically — so this is a gap relative to the protocol's own documented
behavior, not a named, unmet PRD test requirement.

**Suggested fix direction (not mandatory).** Add a test that subscribes, artificially lowers
`ws_flow_control_max_unacked` (the same override pattern `test_too_many_connections_closes_4013`/
`test_heartbeat_timeout_closes_1000` already use via `dataclasses.replace`), observes the sender
pause, sends an `ack`, and observes it resume; a second test driving three consecutive pauses to
observe the `sampling` status frame.

---

## Re-verification of the 2026-08-24 I12 fail-open fix — HOLDS

Re-read `ScenarioUnresolvableForChaosError` (`errors.py:177-201`) and its two call sites in
`inject_fault` (`runs.py:588-595`: missing scenario row, and unparseable scenario text), plus
their regression tests (`tests/api/test_runs.py::test_inject_fault_scenario_row_missing_
refuses_409` and `::test_inject_fault_scenario_unparseable_refuses_409`). Both paths correctly
raise `409 E-CHAOS-004` rather than falling through to `_check_chaos_authorization({}, target)`
reading as "not a graph target." Adversarial attempts to route around it: (1) a scenario row that
exists but whose `content` is valid YAML yet fails `validate.validate` — not applicable here,
since `_check_chaos_authorization` runs on `loader.resolve_defaults(parsed.data)`, and
`inject_fault`'s scenario-resolution path only calls `parse_scenario_text`/`resolve_defaults`,
never `validate.validate` (unlike `_resolve_and_validate`, used by `create_run`) — so a
scenario that parses but wouldn't pass full validation still flows through
`_check_chaos_authorization` correctly, it just isn't independently re-validated, which is
consistent with the function's own contract (I12 authorization, not full scenario validation).
(2) A `target` scenario dict with `target` present but not a dict at all (e.g. `target: null`) —
`_is_graph_target` correctly returns `False` via `isinstance(target, dict)`, treated as
fixture-safe, which is correct: a scenario failing basic shape (already caught by
`_resolve_and_validate`/`validate.validate` at `create_run` time) cannot reach `inject_fault`
with that shape once a run exists, since `create_run` would have refused it first. No route
around the fixed defect was found. **This part of the module holds.**

---

## What was checked and holds up

- **The error envelope** (`errors.py`) is genuinely comprehensive: every documented `E-XXX-NNN`
  code maps to a real raise site, `install_exception_handlers` registers a disjoint handler per
  exception family (verified by reading all nine handlers), and the catch-all `Exception` handler
  logs the real traceback server-side while returning only the reserved-language "Internal error"
  message — no path found that leaks a stack trace or returns a bare framework 500.
- **Design Constraint 3** (loopback-only default, explicit `--host` + printed warning for
  non-loopback binding) is correctly implemented in `app.py::serve`/`_is_loopback`, and the
  warning text is captured verbatim in a real test
  (`test_app_serve.py::test_serve_non_loopback_with_allow_non_local_prints_the_warning_verbatim`,
  confirmed by reading it).
- **Event pagination (Design Constraint 6)** — `get_events`/`ws.py`'s `_read_batch` both stream
  through `store.read_events` and cap materialization at the page/batch size; the `next_from_seq`
  bookkeeping (seq immediately after the last event *scanned*, matched or not) is correct and
  consistent between the REST and WS paths, which is exactly what makes the reconnect-with-
  `from_seq` guarantee provable rather than merely intended.
- **The WS backlog/live cursor** (`_send_backlog` → `_send_live`, one shared `from_seq` handoff)
  is a single continuous read, not two independent code paths — `test_reconnect_with_from_seq_
  has_no_duplicate_and_no_gap` and `test_live_tail_sees_events_appended_after_subscribe` both
  genuinely exercise this against a real `TestClient`/store, not a mock, and their assertions
  (disjoint seq sets, exact ordered ranges) are real discriminating checks, not loose ones.
- **`ack_event`'s clear-then-wait pattern** in `_await_flow_control` is not a missed-wakeup race:
  no `await` point separates the `while` condition check, `clear()`, and the start of `wait()`,
  so the receiver task cannot interleave a `set()` into the gap.
- **Scenario mutual-exclusivity** (`target.fixture` xor `target.graph`, enforced by
  `scenario/validate.py`'s `_check_target`) means `_is_graph_target`'s simpler `"graph" in
  target` check is safe by construction for any scenario that has passed validation — confirmed
  by reading `validate.py:461-485` directly.
- **The I12 fail-open fix from 2026-08-24** (see dedicated section above) — holds under this
  audit's adversarial re-test.
- `docs/api.md`'s "What this build does not yet do" section is unusually honest and, spot-checked
  against the code for several items (`/exploration` always 409s, `/scorecard` 409s until a
  scorecard row exists, `determinism.unwrapped_tools` hardcoded `0`, `timing.critical_path_ms`
  hardcoded `null`), accurately describes real, declared gaps rather than silently shipping them.

---

## NOT DONE / RISKS

- **No live pytest run, no live HTTP/WebSocket request against the real FastAPI app anywhere in
  this audit.** Every finding involving `api/` logic was demonstrated by copying the exact cited
  source lines verbatim into a standalone script and running *that* — a faithful, but not
  identical, substitute for exercising the real, wired-together app end to end (real dependency
  injection, real Pydantic validation, real routing). This is the disclosed D-66/Python-3.10 gap,
  not a choice.
- **`models.py`'s Pydantic schemas were read, not round-tripped.** I checked several routes'
  return shapes against their `response_model` by hand (e.g. `ScorecardResponse`'s deliberate
  `extra="allow"` pass-through, `EventOut`'s field list against `_project_event`) but did not
  verify every one of the ~30 models against every route's actual return value — a full,
  systematic schema-vs-route audit of all 19 REST paths was not completed given the volume, and
  is a real, named gap in this sign-off, not an implicit "checked and clean."
- **`docs/openapi.json` was not diffed against a freshly regenerated schema** — `app.openapi()`
  cannot be called in this sandbox (blocked by the same import-chain constraint), so whether the
  committed file is current was not independently confirmed, only read as text.
- **PRD §33.12's "contract tests generated from the OpenAPI schema"** — no such generated-contract
  test suite exists in `tests/api/`; `test_app_serve.py` checks path presence only. Noted as a
  PRD-requirement gap, not separately written up as a full finding given time budget, but real.
- **`bench/results/api-latency.json`'s three missed §26.3 targets** (`get_events_1000`,
  `get_waterfall_5000_spans`, `get_state`) were read and are honestly reported by `docs/api.md`
  itself — not independently re-measured or re-litigated here; taken as accurately self-reported
  since the file's own `not_measured`/methodology caveats were legible and consistent with the
  numbers.
- **`FaultTargetNotFoundError`'s declared partial-coverage gap** (only agent-kind targets are
  existence-checked) was read and is consistent with its own docstring's honest framing; not
  independently re-derived beyond confirming the `target_kinds == (TargetKind.AGENT,)` exact-tuple
  condition really does exclude `latency` (multi-kind) from any existence check, which Finding #2
  already relies on and re-confirms.
- **`routes/scorecard.py`/`routes/findings.py`/`routes/scenarios.py`/`routes/system.py`** were
  read in full and no additional findings were produced against them beyond what's listed above —
  they are comparatively thin and this audit is reasonably confident in that read, but they did
  not receive the same adversarial script-based testing as `runs.py`'s I12 surface, since nothing
  in them enforces a numbered invariant.
- **Concurrency/thread-safety of `ApiState.concurrency`/`WsConnectionGuard`** (plain `set`/`dict`
  mutation from a sync route handler running in FastAPI's threadpool alongside async WS handlers
  on the event loop) was read but not stress-tested; `ConcurrencyGuard`'s own docstring already
  concedes the store's `status='running'` count, not this guard, is the actual source of truth, so
  a race here looks self-limiting, but this was not proven under load.
- Scope consciously not pursued: `store/`'s own bundle-safety test suite beyond the specific
  `run.json`/status grep already reported; a full re-derivation of every `E-STORE-*` → HTTP status
  mapping in `_status_for_store_code`; the frontend-facing half of PRD §33.12 (out of `api/`'s own
  scope).
