# Known limitations

This document exists because AGENTS.md §6 requires it: "Where the system cannot know
something, it says so." It is the honest counterpart to the README and the rest of `docs/` —
every claim here is a real, currently-true boundary of what AgentDX proves, not a roadmap
item. Where a limitation is scheduled to close, the owning prompt is named; where it is not
scheduled, that is stated too. See `CONTEXT.md` §5 (build state), §6 (gate status) and §9
(deviations) for the full, currently-changing account this file summarizes rather than
duplicates.

## Determinism boundaries (I1, PRD §10)

- **Determinism is canonical-projection equality, not byte equality** (locked decision,
  `CONTEXT.md` §3, PRD §10.1/§43.1.6). Fields marked volatile in `events/schema.py`
  (`wall_ts_ms`, `payload.duration_wall_ms`, the `run_start` provenance fields) are excluded
  from the equality check by design — they are expected to differ across replays and are not
  a determinism defect when they do.
- **The guarantee requires `PYTHONHASHSEED=0` and single-OS-thread scheduler execution**
  (`CONTEXT.md` §3, AGENTS.md §4.1). Any environment that cannot hold both — a different
  interpreter build, a thread the scheduler doesn't own — is outside what I1 covers.
- **Golden fixture logs are provisional**, not yet regenerated against a real
  `runtime.scheduler.Scheduler` run (`RunHost` still does not exist as of the 2026-08-27
  P18/19 session — `CONTEXT.md` §5 rows 6/9/12/17, ADR-001). They were built by
  `fixtures/_harness.py`, whose own docstring states it "adds no causal edges of its own."
  Every test asserting against `tests/golden/*.jsonl` is therefore asserting against a
  hand-shaped approximation of what the real scheduler emits, not the real scheduler's own
  output — a gap the P12 OP-2 audit found was not cosmetic (a real bug, the vclock/
  `causal_parents` fallback confusion, was invisible until someone checked what the
  scheduler actually stamps).
- **The 100-replay, >=10-fresh-process check (G3, PRD §33.3/§34.2) requires a real,
  independent Python interpreter per fresh replica** — not a shared venv reached via an
  environment variable. A sandbox whose fresh subprocesses cannot independently resolve
  the project's dependencies (this was true of the cloud sandbox the P18/19 QA-acceptance
  session ran in — see `tests/acceptance/__init__.py`) cannot exercise this check for real
  locally; it needs the actual CI job.

## Bounded-search caveat (I10, FR-6, PRD §15.6)

The exploration engine (`explore/`, P13) enumerates a **bounded** set of schedules
(`k` transpositions of a fixture's own concurrent-write emission order — `CONTEXT.md` C-26)
and applies independence-based reduction (locked decision, `CONTEXT.md` §3). It does not,
and by design cannot, enumerate every possible interleaving. The required coverage statement
— **"Bounded search: absence of findings is not proof of absence."** — is the honest summary
of this, and I10 requires it verbatim in CLI output, API responses and the UI; removing it
anywhere is a release blocker, not a wording choice.

Separately, as of P13 (`CONTEXT.md` §5 row 13, D-55): the exploration engine's own
DEFINITION OF DONE was demonstrated only against a hand-built synthetic `Scheduler` harness
shaped like `code_pipeline`/`research_fanout` — **not the literal fixtures themselves**,
because `sdk/langgraph.py` does not yet route LangGraph dispatch through `Scheduler`. Do not
read P13's self-reported DoD as evidence that exploration has been exercised against a real
fixture; it has not.

## Comparability rules (G6, PRD §17.5)

Baseline comparison grades a single-agent run's comparability to the multi-agent run on a
letter scale (A/B/C, at reuse ratios of roughly 0.9/0.6/0.3 per PRD §33.11's test spec) —
a run built from a very different task decomposition than the multi-agent one is graded
lower-confidence, not silently compared as if equivalent. The grading logic was audited once
(independent OP-2, FAIL on test-quality grounds against the underlying arithmetic — not the
grading design itself — repaired same day; `CONTEXT.md` §6 row G6). It has not been
re-audited since, and the literal `agentdx compare <run_id> --baseline` CLI command remains
an explicit stub (exit 2) as of P17 — the grading logic is real and tested in isolation, but
nothing today lets a user reach it through the CLI.

## Race detection: the honest recall statement (I5, P12, PRD §34.3)

**Precision is 1.0 by design and is never traded for recall** (I5, PRD §44.3 item 3) — a
single false positive on the labelled benchmark set fails the acceptance gate outright, and
this project's four suppression guards (G1-G4 in `analysis/race.py`, not to be confused with
the PRD gates of the same letter) exist specifically to protect that number. Recall is
reported honestly, whatever it is, per the same rule:

- **Guard G3 (`_has_declared_reducer`) suppresses on reducer *presence*, not reducer
  *correctness*, by design.** A channel with a declared-but-destructive reducer (for example
  a last-write-wins reducer under a non-null key, silently discarding real updates) is
  suppressed the same as a channel with a correct one. This is a recall gap, not a precision
  gap — it only ever suppresses a real finding, never fabricates one — but it means "AgentDX
  found no reducer-channel race" is not the same claim as "this reducer is correct."
- **The formal PRD §34.3 labelled benchmark corpus (40 synthetic logs, 20 seeded races / 20
  near-miss) has not been built.** `analysis.race`'s precision/recall claims to date rest on
  the 12-row true-positive matrix (§33.8) and the `tests/false_positives/` suite (§33.9,
  gate G2), not on the dedicated §34.3 benchmark. Recall against the formal corpus is
  therefore **unmeasured, not merely unpublished** — there is no number to report yet, honest
  or otherwise, and none should be inferred from the matrix/false-positive results, which
  test different (real, and directed) properties.
- **`minimal_repro` (PRD §14.8) implements step 4 of 4 only** — it does not yet emit a
  `delay_schedule` (D-53), blocked on a `scenario.schema` field P12 had no authority to add.
- A conflict whose `value_hash` is unavailable is reported at capped, lower confidence rather
  than suppressed (the case is real; only the *code path proving it end to end* was not
  separately exercised as of the P12 build — see `docs/race-detection.md`).

## What this session (P18/19, 2026-08-27) additionally found and did not fix

Out of scope for this prompt by its own instruction ("no new features... REPORT IT — do not
implement the feature here"). Recorded here because a limitations doc that omits the
limitations found while writing it would defeat its own purpose:

- **G1's literal CLI command fails at argument parsing** (`agentdx run ... --assert
  findings.race >= 1` → `No such option: --assert`) — earlier and more basic than the
  previously-documented `sdk/`-spawn deadlock; the deadlock is real too, but a user hits the
  missing flag first.
- **`analysis/overhead.py`'s decomposition invariant test cannot distinguish a correctly
  computed zero residual from a residual forced to zero by a bug**, on the current three
  golden fixtures specifically — because the real residual is already exactly 0ms on all
  three (`code_pipeline`, `research_fanout`, `support_triage`; confirmed by direct
  measurement, this session). A mutation that added a genuine +100ms discrepancy was caught
  immediately (4/4 tests failed, via the function's own `E-OVHD-001` self-check) — so the
  invariant check is not broken in general, but nothing in the current suite exercises the
  "residual should be nonzero and we report it correctly" branch. A fourth, synthetic fixture
  with deliberate critical-path slack would close this.
- **`docs/cli.md` carries five Rule-E1-unmarked numerals** (regression-tolerance percentages
  and test-seeded drop values in its CI-mode tolerance table) — flagged by
  `scripts/check_bench_markers.py`, exit 2. These read as configured thresholds and
  test-seeded values rather than measured claims, which is arguably outside Rule E1's intent,
  but the checker does not distinguish the two and neither did this session's own re-reading
  of AGENTS.md §6, so they are reported rather than silently reclassified.
