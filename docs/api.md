# AgentDX API (PRD §26)

P14 deliverable: the FastAPI application layer — every `§26.1` REST endpoint, the `§26.2`
WebSocket protocol, the `§26` error envelope, and `§35`'s system endpoints. This document is
the reference `errors.py::docs_url()` and several route docstrings point at
(`docs/api.md#<code>` anchors use the lower-cased `E-XXX-NNN` code as the anchor).

Out of scope for this prompt (unchanged from the P14 brief): no frontend, no new analysis,
no auth. Everything this build genuinely does not do yet is declared below, not silently
stubbed — AGENTS.md §2's "no placeholder implementations" rule applies to this layer too.

## Running it

```
agentdx ui                    # binds 127.0.0.1:8420 (or [api].host/.port from agentdx.toml)
agentdx ui --host 0.0.0.0     # explicit flag required off loopback; prints a no-auth warning
```

`agentdx.api.app.serve()` refuses a non-loopback `host` with a `ValueError` unless
`allow_non_local=True` — the CLI sets that flag exactly when `--host` was passed explicitly,
so passing `--host` *is* Design Constraint 3's "explicit flag". A real manual run of
`agentdx ui --host 0.0.0.0 --port 8420` printed:

```
WARNING: AgentDX is binding to 0.0.0.0:8420. This server has NO AUTHENTICATION. Anyone who
can reach this address can read every run's tool-call arguments and LLM payloads, inject
faults into live runs, and list every scenario on this machine. Only do this on a trusted
network.
```

(paste captured from `tests/api/test_app_serve.py::test_serve_non_loopback_with_allow_non_local_prints_the_warning_verbatim`,
which asserts the same three substrings against the real code path with `uvicorn.run` mocked
out so the test suite never actually opens a socket).

This server has **no authentication of any kind**. It binds loopback-only by default for
that reason.

## Layering (Invariant I3)

`api/` imports `events`, `store`, `analysis`, `scenario`, and `otel` — never `runtime` or
`sdk` (`.importlinter`'s `api-never-imports-runtime` contract, checked by `just
check-imports`). A run is launched or fault-injected through the small `RunLauncher` /
`FaultController` protocols in `agentdx/api/deps.py`, injected into `create_app(...,
run_launcher=..., fault_controller=...)`. Neither has a real implementation wired into
`agentdx ui` in this build (see "What this build does not yet do" below) — a server started
without them refuses the two routes that need one with `503`, rather than pretending to
launch a run or arm a fault it cannot actually execute.

## The error envelope (Design Constraint 2)

Every error response — ours, a `store`/`scenario`/`analysis` module's own coded error, a
FastAPI validation failure, or a genuine unhandled exception — is this shape, never a bare
framework 500 or a stack trace:

```json
{
  "error": {
    "code": "E-RUN-404",
    "message": "Run r_missing not found",
    "detail": {"run_id": "r_missing"},
    "docs": "docs/api.md#e-run-404"
  }
}
```

`agentdx/api/errors.py::install_exception_handlers` registers one handler per exception
family so this is the *only* shape this app ever emits (`tests/api/*` exercise the error
path of essentially every route). An unexpected exception is logged server-side with its
real traceback and reported to the client as `E-API-500` / "Internal error. Please file a
bug report." — no stack trace crosses the wire (PRD §36 rule 4, "reserved language").

### Error code reference

| Code | HTTP | Where | Meaning |
|---|---|---|---|
| `e-run-404` | 404 | most `/runs/{id}...` routes | Run does not exist. |
| `e-run-409` | 409 | `POST /runs`, `POST /runs/{id}/faults` | Concurrent-run limit exceeded, or the targeted run is not `running`. |
| `e-run-503` | 503 | `POST /runs` | No `RunLauncher` configured on this server. |
| `e-scen-404` | 404 | `GET /scenarios/{id}` | Scenario id names neither a persisted nor a discoverable scenario. |
| `e-fault-001` | 400 | `POST /runs/{id}/faults` | Fault target not found (agent targets only — see declared gap below). |
| `e-fault-002` | 400 | `POST /runs/{id}/faults` | Unknown or unshipped (non-P0) fault type. |
| `e-chaos-001` | 403 | `POST /runs/{id}/faults` | I12/§13.3: chaos opt-in missing, blast radius empty, or target outside the declared blast radius. |
| `e-chaos-503` | 503 | `POST /runs/{id}/faults` | No `FaultController` configured on this server. |
| `e-cmp-001` | 400 | `POST /runs/compare` | The two runs' scenario hashes differ and `force` was not set. |
| `e-expl-001` | 409 | `GET /runs/{id}/exploration` | No exploration report exists to serve (declared gap, see below). |
| `e-score-001` | 409 | `GET /runs/{id}/scorecard` | No scorecard has been computed for this run yet. |
| `e-anlz-0nn` | 409 | `GET /runs/{id}/graph`, `/waterfall` | The run's log is not yet in a shape this analysis can run over (e.g. an unterminated span on a still-running run). |
| `e-config-001` | 400 | any route | The server's own `agentdx.toml`/env config failed to load. |
| `e-store-*` | varies | any route touching the store | Passed through from `store/` unchanged — see `store/errors.py` for the full table; `api/` mints no second opinion of what a store failure means. |
| `e-api-422` | 422 | any route | Request validation failed (Pydantic) — `detail.errors` carries FastAPI's own per-field error list. |
| `e-api-4xx`/`e-api-5xx` | matches | any route | Generic fallback for an `HTTPException` this module didn't name a specific code for. |
| `e-ws-002` | — (WS error frame) | `WS /ws/runs/{id}` | Unrecognised client message `type`. |
| `e-ws-003` | — (WS error frame) | `WS /ws/runs/{id}` | Malformed `subscribe` payload (`from_seq`/`filters.types` wrong type). |

## REST endpoints (§26.1)

All under `/api`. Every endpoint below has passing tests in `tests/api/` covering both its
happy path and its declared error cases.

- `GET /api/health` — liveness probe, `{"status": "ok"}`.
- `GET /api/version` — AgentDX package version + `events.schema.SCHEMA_VERSION`.
- `GET /api/metrics` — Prometheus text format; see declared gap below.
- `GET /api/scenarios` — list scenarios discoverable under `[api].scenarios_dir`.
- `GET /api/scenarios/{scenario_id}` — fetch one; `404 E-SCEN-404` if unknown.
- `POST /api/scenarios/validate` — dry-run validate; body is exactly one of `text` or
  `scenario_id` (422 otherwise); a scenario that fails to parse/validate is still a `200`
  with `valid: false` and the structured error list, never a 400 — validating an invalid
  scenario is the whole point of the endpoint.
- `GET /api/runs` — list runs, newest first, cursor-paginated `(created_at desc, run_id)`;
  filters `status`, `fixture` (true = fixture-targeted, false = user-graph).
- `POST /api/runs` — start a run via the configured `RunLauncher`; `503 E-RUN-503` with none
  configured; `409 E-RUN-409` over the concurrent-run limit.
- `GET /api/runs/{run_id}` — metadata, real counts, verdict (when computed).
- `GET /api/runs/{run_id}/events` — paginated event log; see Design Constraint 6 below.
- `GET /api/runs/{run_id}/graph` — per-agent node/edge aggregates + critical path.
- `GET /api/runs/{run_id}/waterfall` — span waterfall, real leaf (`llm_call`/`tool_call`/
  `wait`) spans only; `from_ms`/`to_ms` window filter.
- `GET /api/runs/{run_id}/state` — state reconstruction as of `at_virtual_ts` (required query
  param, PRD §20.4).
- `GET /api/runs/{run_id}/exploration` — always `409 E-EXPL-001` in this build (declared gap).
- `GET /api/runs/{run_id}/findings` — findings, filterable by `severity`/`type`.
- `GET /api/runs/{run_id}/scorecard` — `409 E-SCORE-001` until a scorecard has been persisted.
- `POST /api/runs/{run_id}/faults` — arm a fault via the configured `FaultController`;
  `202 Accepted`; `503 E-CHAOS-503` with none configured.
- `POST /api/runs/compare` — diff two runs' real available metrics + content-matched findings.
- `GET /api/runs/{run_id}/export` — download a `.agentdx` bundle (zip).
- `POST /api/import` — upload a `.agentdx` bundle (multipart), register it, return its local
  `run_id`; `400` on a malformed bundle.

## WebSocket protocol (§26.2) — `WS /ws/runs/{run_id}`

Mounted at the app root, not under `/api` (verified end-to-end in
`tests/api/test_app_serve.py`, since this FastAPI version's OpenAPI schema and route
introspection don't surface WS routes at all).

**Connect.** `404`-equivalent close `4004` if `run_id` doesn't exist (checked before
`accept()`). `4013` if the run already has `[api].ws_max_connections_per_run` (default 8)
live connections. Otherwise the server sends `hello` first:

```json
{"type": "hello", "run_id": "...", "status": "running", "current_seq": 41}
```

**Subscribe.** The client sends exactly one `subscribe` (repeats are a no-op):

```json
{"type": "subscribe", "from_seq": 0, "filters": {"types": ["llm_call", "tool_call"]}}
```

The server then sends the entire backlog from `from_seq` as `events` batches (batch size
`[api].ws_backlog_batch_size`), seq-monotonic, before any live event — then transitions
seamlessly to live tailing, sent one at a time as `event`. The backlog/live boundary is a
single continuous cursor in `ws.py`'s implementation, not two separate code paths glued
together, which is what makes "no event ever delivered twice, no gap" provable rather than
merely intended (`tests/api/test_ws.py::test_reconnect_with_from_seq_has_no_duplicate_and_no_gap`,
`::test_live_tail_sees_events_appended_after_subscribe`).

**Flow control.** The server pauses sending past `[api].ws_max_unacked_events` (default
5000) unacked events until the client sends `{"type": "ack", "through_seq": N}`.

**Backpressure sampling** (declared heuristic — the PRD names the *existence* of this
behaviour, not its detection algorithm): after 3 consecutive live-tailing send-pauses caused
by flow control, the server switches to sending every Nth live event (`N` starting at 2,
doubling, capped at 64) and announces the change: `{"type": "status", "sampling": N}`. This
constant lives in `ws.py` (`_SAMPLING_PAUSE_THRESHOLD`, `_SAMPLING_MAX_N`), not
`config.py`, precisely because it is not a PRD-specified number — it is documented as a
heuristic in the module docstring, not represented as a tuned/validated constant.

**Heartbeat.** Client sends `{"type": "ping"}`, server replies `{"type": "pong"}`. If the
server hears nothing from the client for `[api].ws_heartbeat_timeout_s` (default derived
from the PRD's 45 seconds), it closes with code `1000` (proven with a short configured timeout in
`test_heartbeat_timeout_closes_1000`, not asserted only in prose).

**Reconnect.** A client that saw events up through `last_seen` reconnects and subscribes with
`from_seq = last_seen + 1`. Because backlog and live share one cursor and the store is the
single source of truth for "what happened", a reconnect after a mid-stream disconnect
resumes at exactly the right point — never repeating, never skipping (tested, not just
asserted by design).

**`finding` / `verdict` pushes.** `finding` pushes are real: `store/bundle.py::import_bundle`
calls `store.upsert_finding` for real bundle imports, and the live-tail loop polls
`store.list_findings` for new ids. `verdict` is protocol-complete (the server will send it
the moment a scorecard is persisted) but will not fire in *this* build's default wiring — no
P17 orchestrator in this codebase calls `store.upsert_scorecard` from a live run (grepped,
zero production call sites; see "What this build does not yet do").

**Polling, not push.** There is no writer-to-API push channel in this build — `store/` is a
plain SQLite file (PRD §27.3's "one writer, many readers"), so the live-tail loop polls the
store on an interval (`[api].ws_poll_interval_s`, default a tenth of a second — a server-side implementation
detail, not a PRD §26.2 client-visible number, documented in `config.py`). Short enough that
local WAL reads feel immediate; tunable if that assumption changes.

**Close codes.** `1000` normal (heartbeat timeout, or a clean `unsubscribe`); `4004` run not
found; `4013` too many connections for this run.

**Testing note:** Starlette's `TestClient` drives every WebSocket connection on one shared
background portal, and two genuinely concurrent connections opened against the *same*
`TestClient` corrupt that portal under `pytest` specifically (confirmed via multiple minimal
repros: the identical code works fine as a plain script, and works fine against a real
`uvicorn` server — this is a `TestClient` limitation, not a `ws.py` behaviour). The `4013`
test therefore pre-fills the connection-limit guard directly rather than opening two
concurrent `TestClient` connections — it still exercises the exact route-handler check
(`state.ws_connections.try_connect`), just without the portal-corrupting concurrency.

## Design Constraint 6 — event pagination never loads a full run into memory

`GET /runs/{id}/events` and the WS backlog sender both stream through
`store.read_events(run_id)` (a generator over the store's own paginated reads) and only ever
materialize one page/batch at a time — never the whole run. A 200,000-event run pages the
same way a 20-event run does. `GET /runs` (the *runs* list, not events) is the one exception,
and it is a deliberate, documented one: `store.list_runs()` has no `created_at`-ordered
index to page through (extending `store/` with one is outside this prompt's `DELIVERABLES`),
so the runs list re-sorts in Python — O(run count), not O(page size) — which is fine for a
`runs` table (hundreds to low thousands of rows in realistic local use) in a way the same
trade-off would not be for `events`.

## What this build does not yet do (declared gaps, not silent stubs)

- **`GET /api/metrics`** — PRD §35 names 13 metrics describing a run *in progress*
  (scheduler step duration, cache hit ratio, per-analyser duration, …). Nothing in this
  codebase writes those anywhere the API can read (`runtime/` has no metrics registry, and
  `api/` cannot import `runtime` regardless — I3). This endpoint emits real `# HELP`/`# TYPE`
  metadata for the full PRD §35 metric set (so a Prometheus scraper's config validates
  against the real names/units) but only attaches a sample line to the metrics this process
  can actually compute today (memory RSS, process uptime, and similar). A `# HELP`/`# TYPE`
  pair with no sample is valid Prometheus text format for "this metric exists, nothing to
  report yet" — not a fabricated `0`.
- **`GET /runs/{id}/exploration`** — always `409 E-EXPL-001`. No bounded schedule exploration
  report exists to serve; this is a P17/`explore` layering gap, not something P14's
  `DELIVERABLES` include building.
- **`GET /runs/{id}/scorecard`, `verdict` fields, `POST /runs/compare`'s `verdict_changed`** —
  `409 E-SCORE-001` / `null` / `False` until a scorecard has actually been persisted via
  `store.upsert_scorecard`. No P17 orchestrator in this build calls that. Findings *do* get
  persisted for real (bundle import calls `store.upsert_finding`), so `/findings` is a live
  endpoint even though `/scorecard` mostly isn't yet.
- **`GET /runs/{id}`'s `timing.critical_path_ms`** — reported `null`. Computing it means
  building the full timing DAG, the same cost `GET .../graph` already pays where the PRD's
  example payload actually needs the number; this route does not additionally pay that cost
  on every call for a field callers needing it can get from `/graph` directly.
- **`GET /runs/{id}`'s `determinism.unwrapped_tools`** — always `0`. No event or field
  anywhere in this codebase currently records this (grepped, not assumed) — a real, declared
  gap, not a fabricated zero dressed up as a real count.
- **`GET /runs/{id}/waterfall`'s per-span `bucket`** — always `null`. Overhead-bucket
  classification is not exposed per-span in this build.
- **`POST /runs`, `POST /runs/{id}/faults`** — `503` (`E-RUN-503` / `E-CHAOS-503`) unless a
  server is started with a real `RunLauncher`/`FaultController` wired in. `agentdx ui`'s
  default wiring supplies neither: this codebase's `runtime`/`cli` layers have no `agentdx
  run`/`RunHost` yet to launch (CONTEXT.md §7 — a P06/P17 gap, left unresolved by owner
  instruction), and `runtime/faults/`'s decision engines have "no live production call site"
  (CONTEXT.md §5). The protocols exist and are fully tested against fakes
  (`tests/api/conftest.py::FakeRunLauncher`/`FakeFaultController`) so wiring a real
  implementation in later is a matter of passing it to `create_app(...)`, not redesigning
  this layer.
- **`POST /runs/{id}/faults`'s target validation** — only agent targets are checked against
  the run's real roster (`store.list_agent_ids`). Edge/tool/state_key/provider targets are
  not verified to exist before the fault is armed — a declared gap: verifying them means
  resolving the run's graph, which `api/` cannot do without importing `runtime` (I3) or
  duplicating graph-resolution logic that belongs elsewhere.
- **`GET /runs` pagination ordering** — not backed by a `created_at`-indexed store read (see
  Design Constraint 6 above); acceptable at realistic local scale, called out explicitly so
  it isn't mistaken for `O(page size)`.
- **WS backpressure sampling's detection algorithm** — the PRD names the behaviour, not the
  algorithm. `ws.py`'s 3-consecutive-pauses / doubling-N / cap-64 heuristic is a declared,
  local implementation choice, not a number derived from the PRD.
- **`python-multipart` dependency** (`POST /import`'s multipart upload) — not one of ADR-004's
  enumerated distributions; flagged in P14's closing blocks for owner ratification (AGENTS.md
  §2: an ADR is required before any new dependency, and this one predates that ratification).

## OpenAPI schema

Committed at `docs/openapi.json` (generated via `app.openapi()` against a `create_app()`
built with an isolated temp config — regenerate the same way after changing any route
signature or Pydantic model). Confirmed to generate a complete TypeScript client with **zero
hand edits** via `npx openapi-typescript docs/openapi.json` (ad hoc — this is a verification
step, not a permanent frontend dependency; no `frontend/package.json` exists or was added).
19 REST paths; the WS route has no OpenAPI representation by FastAPI's own design (WS routes
are simply absent from `app.openapi()`'s schema — confirmed, not assumed, in
`test_app_serve.py`).

## Performance (§26.3)

`bench/harness/api_latency.py` measures every `§26.3` row against a real `TestClient` over a
real SQLite-backed store, on synthetic logs generated at the PRD's own stated scale (no
committed fixture reaches 5 000 events/spans — see `bench/results/api-latency.json`'s own
`not_measured` for exactly what this substitutes and why). One real, worst-of-5 run on this
machine (`bench/results/api-latency.json`):

| Endpoint | Target p95 | Worst-of-5 | Met |
|---|---|---|---|
| `GET /runs/{id}` | <50 ms | 40.7 ms [bench:api-latency.json] | yes |
| `GET /runs/{id}/events` (1 000) | <100 ms | 140.4 ms [bench:api-latency.json] | **no** |
| `GET /runs/{id}/waterfall` (5 000 spans) | <150 ms | 901.6 ms [bench:api-latency.json] | **no** |
| `GET /runs/{id}/findings` | <50 ms | 10.0 ms [bench:api-latency.json] | yes |
| `GET /runs/{id}/state` | <100 ms | 182.1 ms [bench:api-latency.json] | **no** |
| `POST /runs` (refusal path, see below) | <200 ms | 6.5 ms [bench:api-latency.json] | yes |
| WS backlog (5 000 events) | <1 s | 499.6 ms [bench:api-latency.json] | yes |

Reported honestly, not smoothed over: three rows miss their §26.3 target on this
measurement. `get_waterfall_5000_spans` is the largest miss — `analysis.timing.
build_timing_dag` + `critical_path` over 15 002 raw events is O(events), not optimized for
this endpoint's specific access pattern, and nothing in P14's `DELIVERABLES` scoped
optimizing `analysis/timing.py` itself (that module predates this prompt). `get_state` and
`get_events_1000` are both real-but-unindexed-for-this-shape store reads at this event
count. None of these are silently hidden — they are exactly what `just check-bench` requires
be true before a number is published, and CONTEXT.md's closing NOT DONE/RISKS block for P14
names all three as follow-up work, not a passed gate.

See `bench/results/api-latency.json` for the full method, environment, and every
`not_measured` caveat, and `bench/results/README.md` for the marker convention.
