# OP-3 REPAIR REPORT — `api/` (P14) first OP-2 audit (`op2-audit-p14.md`)

**Repaired same day as the audit, by the orchestrating session (not the audit agent — the audit
agent used only `Read`/`Grep`/`Bash`, no `Edit`/`Write`, and its first spawn was interrupted
mid-run by a session-level rate limit and retried fresh from an identical prompt).** Owner
authorized fixing all five findings in full (`AskUserQuestion`, "Fix all five now
(recommended)"), applying the audit's own suggested fix directions rather than novel designs.

**A standing constraint shapes every verification claim in this report and is stated once here
rather than repeated per finding**: this sandbox's Python 3.10.12 cannot import any `agentdx.*`
module at all. This is a *worse* form of the already-tracked **D-66** gap than previously
documented — D-66 was scoped to `api/models.py`'s PEP 695 `type` syntax specifically, but
`agentdx/__init__.py` unconditionally imports `agentdx.config`, which does `import tomllib`
(Python 3.11+ stdlib), so no `agentdx.*` module is importable here, not just `api/`. No
`pytest`/real HTTP/real WebSocket execution was possible for either the audit or this repair.
Both were verified instead by direct code reading, `ast.parse` syntax checks, config-file
parsing, and — for the one finding with genuinely new logic — a standalone throwaway script
running a verbatim copy of that logic, the same methodology the audit itself used and disclosed.
Every "Verified" subsection below says exactly which of these methods was used and nothing more.

## Finding #1 (CRITICAL) — `POST /api/import` could arm a fault with zero I12 authorization

**Repair.** `inject_fault`'s I12 block (`api/routes/runs.py`) was gated by
`if record.scenario_id is not None:` — a run with `scenario_id is None` skipped the entire
authorization check, arming any fault against it unconditionally. New `ScenarioMissingForChaosError`
(`api/errors.py`, `409 E-CHAOS-005`), sibling to the already-existing `ScenarioUnresolvableForChaosError`
(`E-CHAOS-004`, closed 2026-08-24 for the narrower "`scenario_id` set but unresolvable" case).
`inject_fault` now raises it outright when `record.scenario_id is None`, *before* ever reaching
scenario resolution — the audit's own suggested fix, applied verbatim: refuse, never treat as
fixture-safe by default, per PRD §36 rule 1 and I12 itself. `POST /api/runs` always sets
`scenario_id` (`RunCreateRequest.scenario_id: str`, no default — confirmed by reading
`api/models.py` directly), so the only way a stored run reaches `inject_fault` with
`scenario_id is None` is `POST /api/import` of a bundle whose `run.json` was hand-edited to omit
it; confirmed by reading `store/bundle.py` directly that its integrity check
(`verify_chain`/`canonical_log_hash`) covers `events.jsonl` only — `run.json`'s own fields,
including `scenario_id` (`_opt_str(run.get("scenario_id"))`, `bundle.py` line 997), are never
hashed or otherwise round-trip-verified. This is not a hypothetical reach; it is the exact path
the audit demonstrated via static reading of the import code.

**Verified — code reading only, no execution possible (see standing constraint above):**
- Re-read `api/routes/runs.py`'s new `inject_fault` block end to end: the `scenario_id is None`
  check is now the first thing done after the fault-catalogue/param/target checks and before any
  `store.get_scenario`/`loader.parse_scenario_text` call, so the raise is unconditional and
  cannot be bypassed by a scenario row that also happens to be missing or unparseable — those
  two cases still correctly fall through to `ScenarioUnresolvableForChaosError` immediately
  after, unchanged from the 2026-08-24 fix.
- Re-read `api/errors.py`'s new `ScenarioMissingForChaosError` class: `status_code =
  status.HTTP_409_CONFLICT`, code `E-CHAOS-005`, distinct from `E-CHAOS-004`; both are plain
  `ApiError` subclasses, so the existing generic `@app.exception_handler(ApiError)` (confirmed
  present, unmodified) dispatches it correctly with no new registration needed.
- Re-read `api/models.py::RunCreateRequest`: `scenario_id: str` (line 81, no default) — confirms
  the "always set on a live-created run" claim this fix's whole reasoning depends on.
- Re-read `store/bundle.py`'s hash-chain verification (`verify`, `_verify_chain`, lines ~460-580)
  and `run.json` deserialization (`_run_record_from_dict`, line ~997): confirmed no field of
  `run.json` — `scenario_id` included — participates in any hash the bundle checks on import.
- `ast.parse` on `api/errors.py` and `api/routes/runs.py`: both parse cleanly under this
  sandbox's Python 3.10 despite the file otherwise being import-blocked by D-66's PEP 695 gap
  elsewhere in the package — syntax-level confirmation only, not a semantic one.

## Finding #2 (HIGH) — `blast_radius` categories were flattened into one bag of strings

**Repair.** `_check_chaos_authorization` (`api/routes/runs.py`) previously collected every string
across all five `blast_radius.{agents,tools,edges,state_keys,providers}` lists into one `declared`
list and checked only membership — a `tools:` entry could authorize an unrelated agent- or
edge-kind fault whose target string happened to collide with it, defeating the category structure
PRD §13.4 itself specifies. New module-level `_BLAST_RADIUS_FIELD: Final[dict[TargetKind, str]]`
mirrors `scenario/validate.py`'s own private, pre-existing, already-correct mapping of the same
name and shape — duplicated locally rather than imported across the `api/`→`scenario` boundary,
following this project's established convention for a small, stable, another-layer-owned mapping
(`analysis/verdict.py`'s `Severity`, `analysis/baseline.py`'s `ComparabilityGrade` are the cited
precedents). `_check_chaos_authorization` now takes the armed fault's own `target_kinds`
(`FaultSpec.target_kinds`, already available at the call site as `spec.target_kinds`) and builds
`declared` only from the categories those `target_kinds` name — the audit's own suggested fix,
applied verbatim.

**Verified — code reading plus a standalone executable check (the one finding with genuinely new
branching logic, so the highest-value place to actually run something in this sandbox):**
- Re-read `scenario/validate.py` lines 80-85 directly: confirmed
  `_BLAST_RADIUS_FIELD: Final[dict[TargetKind, str]] = {AGENT: "agents", TOOL: "tools", EDGE:
  "edges", STATE_KEY: "state_keys", PROVIDER: "providers"}` is exactly what the new
  `api/routes/runs.py` copy mirrors, field-for-field.
- Built a throwaway script (`/tmp/op3verify/verify_finding2.py`) copying `TargetKind` verbatim
  from `scenario/schema.py` (substituting `Enum`+`str` mixin for `StrEnum`, since `StrEnum` is
  3.11+ and unavailable here — the enum's *values* are unaffected) and copying
  `_BLAST_RADIUS_FIELD`/`_check_chaos_authorization`'s post-fix body verbatim from
  `api/routes/runs.py` (raise statements swapped for local sentinel exceptions, since the real
  `ChaosAuthorizationError` classmethods pull in `fastapi`/`starlette`, out of scope for a
  pure-logic check). Ran it under this sandbox's real Python 3.10 interpreter — genuine
  execution, not a claim: 4 cases, all correct. (1) A `tools:` entry does **not** authorize an
  `AGENT`-kind fault targeting the same string — raises `BlastRadiusEmpty`, the fix's whole
  point. (2) The correct `agents:` entry **does** authorize the identical target string. (3) A
  multi-kind fault (`target_kinds=(EDGE, AGENT)`, `latency`'s real PRD §13.4 shape) is authorized
  via either of its relevant categories (`edges:`, tested). (4) Missing `chaos_opt_in` is still
  refused before blast radius is ever examined — ordering preserved from before the fix.
- Re-read the `inject_fault` call site: `_check_chaos_authorization(resolved, body.target,
  spec.target_kinds)` — confirms `spec.target_kinds` (the `FaultSpec` already resolved from
  `FAULT_CATALOGUE` earlier in the same function) is what's threaded through, not a re-derived or
  hand-written value.

## Finding #3 (MEDIUM) — I10's sentence was checked with a non-discriminating OR-substring test

**Repair.** `tests/api/test_analysis.py::test_get_exploration_always_409_in_this_build`'s
assertion `"coverage" in body["error"]["message"].lower() or "bounded" in
body["error"]["message"]` — which the audit demonstrated live passes even against a mutated
message with the real I10 sentence entirely removed, so long as either bare word survives —
replaced with an exact substring assertion of the full, verbatim I10 sentence: `"Bounded search:
absence of findings is not proof of absence." in body["error"]["message"]`, the audit's own
suggested fix, applied verbatim.

**Verified — code reading only, no execution possible:**
- Re-read `api/routes/analysis.py` lines 293-301 directly: the real `NotYetAvailableError`
  message (`E-EXPL-001`) ends `"...agentdx.explore directly (CONTEXT.md §4 layer contract).
  Bounded search: absence of findings is not proof of absence."` — confirmed the new assertion's
  string is an exact, verbatim substring of the real, current source message, not a guess at what
  it might say.
- `ast.parse` on `tests/api/test_analysis.py`: parses cleanly; confirmed by inspection that the
  new assertion is a plain `in` check against a Python string literal with no regex/formatting
  hazard that could silently pass on a near-miss.

## Finding #4 (MEDIUM, two parts) — undeclared `api/`→`scenario` import; mis-cited `python-multipart`

**Repair, part (a).** `api/` has imported `agentdx.scenario` since P14 shipped
(`routes/runs.py`'s `from agentdx.scenario import loader, validate`, `routes/scenarios.py`'s
equivalent) for real, load-bearing reasons — `POST /api/runs`'s scenario resolution and I12
chaos-authorization itself both need it — but this was never declared in CONTEXT.md §4's layer
table (which listed `api/`'s permitted imports as `store, analysis` only) or in
`.importlinter`'s `api-never-imports-runtime` contract comment. New **ADR-023** (CONTEXT.md §8)
ratifies the import, mirroring ADR-013's identical precedent for the same situation in
`runtime/`→`scenario`. `.importlinter`'s contract comment and CONTEXT.md §4's `api/` row both
updated to name `scenario` explicitly; `forbidden_modules` itself is unchanged, since `scenario`
was never listed there — nothing mechanical changes, only the documentation catches up to what
was already true.

**Repair, part (b).** `pyproject.toml`'s `python-multipart` dependency comment cited "ADR-014" as
its ratifying authority — a real ADR-014 exists in this ledger, but it is an unrelated
determinism-exception clause (`AbortGuardMonitor`'s wall-clock read), not any dependency
decision. New **ADR-024** (CONTEXT.md §8) formally ratifies `python-multipart` under its own
number, ties it to PRD §24.6 (the capability `api/` already cites) following ADR-004's own
"capability named, distribution enumerated by ADR" pattern, and corrects the `pyproject.toml`
comment to cite ADR-024 instead. The audit's second, non-firm question — whether `ApiConfig`/
`agentdx.toml`'s `[api]` table needs its own ADR — is ruled on rather than left open a second
time: no, since AGENTS.md §2's dependency rule is specifically about *dependencies* entering
`pyproject.toml`/`package.json`, and `ApiConfig` is ordinary implementation of already-cited PRD
§26 surface, the same class as every other module's own `config.py` section.

**Verified:**
- `python3 -c "import configparser; cp = configparser.ConfigParser(); cp.read('.importlinter')"`
  — parses cleanly, `api-never-imports-runtime` section present with the new comment.
- `python3` with `tomli` (installed via `pip install tomli --break-system-packages`, since this
  sandbox's Python 3.10 has no stdlib `tomllib`) loading `pyproject.toml` — parses cleanly,
  `python-multipart>=0.0.12,<1.0` present and unchanged, only its comment differs.
- `scripts/check_ledger.py` run for real against the working tree: **`check-ledger: OK — §8 and
  §9 append-only, length and ADR references all clean`** — confirms both new ADR-023/ADR-024
  §8 rows are correctly formatted, that no `ADR-NNN` reference anywhere in the file (including
  the new `.importlinter`/`pyproject.toml`/§4 citations) is dangling, and that the append-only
  invariant on §8/§9 against `origin/main` was not violated by these edits.
- CONTEXT.md's total line count re-confirmed at 495 lines (script's own count, not estimated) —
  under the 500-line cap after this cycle's §8 growth (+2 rows) and the accompanying 46th
  rollover (§13, detailed in CONTEXT.md's own commit).

## Finding #5 (LOW-MEDIUM) — `ws.py`'s flow-control/sampling machinery had zero test coverage

**Repair.** Two new tests added to `tests/api/test_ws.py`, following the audit's own suggested
fix direction and this file's existing `dataclasses.replace(api_config,
api=api_config.api.with_overrides(...))` override pattern (`test_too_many_connections_closes_4013`/
`test_heartbeat_timeout_closes_1000`'s own precedent).

`test_flow_control_pauses_and_resumes_on_ack` targets the backlog path: `ws_backlog_batch_size=5`/
`ws_flow_control_max_unacked=4` against a 30-event sealed run makes the second backlog batch
(seqs 5-9) provably block — `last_sent_seq(4) - acked_through(-1) = 5 > 4` — before any `ack` is
sent; the test receives the first batch, sends `ack through_seq=4`, and receives the second batch
only after that ack. No frame is ever left unread on the wire (each batch is received before the
next ack is sent), so the test makes no assumption about the transport's buffering depth.

`test_three_consecutive_pauses_escalate_to_sampled_mode` targets the live-tailing path
specifically, since `ws.py`'s own module docstring states a *backlog* pause never counts toward
sampling escalation ("backlog sending already blocks on flow control by design — a pause there is
not evidence of trouble"), only a pause while tailing *live* does. `ws_backlog_batch_size=1` makes
`_read_batch` return at most one event per read for both phases; all three live events are
written to the store in a single call *before* any of them is acked, so no `_send_live` iteration
can ever observe an empty read in between them (which would silently reset
`session.consecutive_pauses` to 0) purely from scheduling luck. The sequence is deterministic by
construction, not by timing: every wait blocks with no I/O in flight (the flow-control check runs
before `send_json`, never after), and every send is drained by exactly one `receive_json` before
the next `ack` is sent.

**Verified — code reading and manual trace only; these two tests could not be executed in this
sandbox (D-66) and this is disclosed rather than silently assumed passing:**
- Re-read `ws.py`'s `_send_backlog`/`_send_live`/`_await_flow_control`/`_handle_ack`/
  `_maybe_update_sampling` in full (lines 1-364) and traced both new tests' exact sequences
  against that code line by line — documented in full above and in each test's own docstring,
  including the specific seq/acked_through/last_sent_seq arithmetic at every step.
- Re-read `tests/api/conftest.py` and `tests/unit/store/factories.py`
  (`build_log`/`run_record_for`/`LogBuilder`) to confirm event counts, `chain()`'s per-event
  hash-pairing behavior, and that `RunRecord(**run_record_for(events))`/`store.append(chained[...])`
  is the exact same construction pattern `test_live_tail_sees_events_appended_after_subscribe`
  already uses successfully for a partial, unsealed log in this same file.
- `ast.parse` on `tests/api/test_ws.py`: parses cleanly (362 lines total, 2 new test functions).
- `awk` line-length check: zero lines over the project's 100-character `ruff` limit (one line was
  found over on a first pass, `RunRecord(**run_record_for(events, status="running"))  # type:
  ignore[arg-type]`, at 105 chars — fixed by dropping the redundant `status="running"` kwarg,
  since `run_record_for`'s own default is already `"running"`, matching the shorter two-line form
  `populate()`'s own helper already uses).
- Docstring convention check against this file's other tests (D205/D212 pydocstyle, `ruff`'s
  `google` convention, enforced project-wide per `pyproject.toml`): both new tests' docstrings
  were corrected to a single-line summary followed by a blank line, matching every existing
  docstring in this file — an early draft had a two-line wrapped summary, caught and fixed before
  finalizing.
- **Not done, disclosed rather than silently assumed**: neither new test has been run against a
  real `TestClient`/ASGI event loop. The design was chosen specifically to minimize the risk of a
  timing-dependent hang (store-content gating instead of real-time polling races, reasoned through
  in full above) but that reasoning has not been confirmed by actual execution. This is owed to
  the owner's next real-hardware `pytest tests/api/test_ws.py` run.

## What held, no repair needed

Per the audit's own "Re-verification of the 2026-08-24 I12 fail-open fix" and "What was checked
and holds up" sections: the narrower `scenario_id`-set-but-unresolvable I12 fix (now the sibling
of Finding #1's fix, both closed together), the REST error envelope, Design Constraint 3's
loopback-bind default, event pagination (Design Constraint 6, `_read_batch`'s per-call cap), WS
backlog/live cursor continuity across a reconnect, `_await_flow_control`'s clear-then-wait
race-freedom (confirmed independently by this session's own reading, no `await` point separates
the `while` check from `session.ack_event.clear()`/`wait()`), and scenario mutual-exclusivity —
none of these needed any change, and none were touched by this pass's fixes.

## Full verification, this pass

Given the standing constraint stated at the top of this report, "full verification" here means
something narrower and more honestly scoped than in a prior repair report with a working
interpreter — stated in full rather than glossed over:

- `ast.parse` — clean on every touched Python file: `src/agentdx/api/errors.py`,
  `src/agentdx/api/routes/runs.py`, `src/agentdx/api/ws.py` (read-only, unmodified — re-parsed to
  confirm the file this repair reasoned about matches what is on disk), `tests/api/test_analysis.py`,
  `tests/api/test_ws.py`, `tests/api/conftest.py` (read-only, unmodified).
- Line-length check (`awk`, 100-char `ruff` limit) — clean across all touched files after one
  fix (Finding #5, above); one pre-existing long line in `errors.py` (line 437, a `type:
  ignore[redundant-expr]` comment predating this session, untouched by any of these five fixes)
  is not this repair's to fix.
- Config-file parsing — `.importlinter` (`configparser`) and `pyproject.toml` (`tomli`) both
  parse cleanly with the expected content present.
- Cross-reference consistency — every new class/function/constant this repair introduced
  (`ScenarioMissingForChaosError`, `_BLAST_RADIUS_FIELD`, the rewritten
  `_check_chaos_authorization`) was grepped across the touched files to confirm single
  definitions, matching imports, and no orphaned references; `TargetKind`/`Final` were already
  imported in `routes/runs.py`, needing no new import lines.
- Standalone executable verification — one throwaway script (Finding #2), the single piece of
  genuinely new branching logic this repair introduced; 4/4 cases correct under this sandbox's
  real Python 3.10 interpreter.
- `scripts/check_ledger.py` — run for real (this script has no `agentdx.*` import dependency,
  so it is unaffected by D-66): **OK**, confirming §8/§9 append-only, the 500-line cap, and ADR
  reference integrity, all against the actual working tree.
- **Not done, and not claimed**: `pytest`, any real HTTP/ASGI `TestClient` run, `mypy --strict`,
  `ruff check` (as opposed to a manual line-length/`ast.parse` proxy for its syntax-level checks),
  `lint-imports`, `check_determinism_hygiene.py`. All of these require importing `agentdx.*`,
  which this sandbox cannot do at all (see standing constraint). Every one of them is owed to the
  owner's next real-hardware run, same as every module this session has repaired.

## Standing status

**Not `VERIFIED`** — a re-audit is owed, same standing pattern as every other module in this
ledger. Unlike every prior repair this session performed, this one carries an additional, more
severe caveat: **none of it has been confirmed by actually running the code**, only by reading it
carefully and, for the one piece of new branching logic, executing a faithful standalone copy of
it. This is stated plainly rather than softened, per this project's I9 norm ("report NOT DONE
rather than fabricate a result"). The newly-discovered, project-wide scope of the D-66 gap
(`agentdx/__init__.py`'s `import tomllib` blocking every `agentdx.*` import, not just
`api/models.py`'s PEP 695 syntax) is itself worth flagging to the owner as an update to D-66's own
CONTEXT.md §9 entry, since it changes what "run the tests in this sandbox" can mean for *any*
future module, not only `api/` — not actioned in this pass (out of this repair's own narrow
scope), but noted here for whoever picks up D-66 next. Also not done, matching the audit's own
"NOT DONE / RISKS" section where it still applies: no live pytest/HTTP/WS run of anything in
`tests/api/`, `models.py`'s ~30 Pydantic schemas not systematically re-verified against all 19
routes, `docs/openapi.json` not diffed against a freshly regenerated schema, PRD §33.12's
"contract tests generated from OpenAPI" gap not written up as its own finding, and
`ApiState.concurrency`/`WsConnectionGuard`'s thread-safety not stress-tested — none of these were
part of the five findings this repair closed, and none were newly investigated here.
