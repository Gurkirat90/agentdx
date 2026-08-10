# AGENTS.md — Standing rules for every agent working in this repository

Project: **AgentDX**. Spec of record: `AgentDX-PRD-v2.md`. State of record: `CONTEXT.md`.
These rules apply to every session, every model, every prompt. They are read before work begins.

*(Tool-specific instruction files — `CLAUDE.md`, `GEMINI.md`, `.cursorrules`, `.agents/rules/*` — may **add** tool-local guidance but may never override anything here. On conflict, this file wins, per `CONTEXT.md` §0.5. If a tool insists on its own root file, that file must contain a pointer to this one and nothing that contradicts it.)*

---

## 1. Session opening ritual — non-negotiable

Before writing a single line of code:

1. Read `CONTEXT.md` end to end.
2. Read only the PRD sections listed in the prompt's `AUTHORITATIVE INPUTS`.
3. Restate, in ≤ 8 lines: the mission, the deliverable file list, the invariants in play, and the acceptance gate.
4. State any conflict you found between the prompt, `CONTEXT.md` and the PRD. **Do not resolve it silently.** Check `CONTEXT.md` §10 first — the conflict may already have a ruling.
5. Produce a plan and wait if the prompt says to wait.

If `CONTEXT.md` and the PRD disagree, follow the precedence rule in `CONTEXT.md` §0.5.

## 2. Scope discipline

- Build **only** what the prompt's `DELIVERABLES` names. Touching an unlisted file is a scope violation — surface it instead.
- Do not refactor code from a previous prompt. If it is wrong, say so and stop; repair happens through OP-3, not opportunistically.
- **Dependencies:** the permitted set is `CONTEXT.md` §3 plus PRD §24.6 and PRD §25. Anything outside it — a new library, a new dev tool, a new CDN import — requires an ADR in `CONTEXT.md` §8 *before* it enters `pyproject.toml` or `package.json`. Ask first, always.
- Do not add "nice to have" features, extra endpoints, extra CLI flags, or speculative abstraction layers. The PRD is complete; extra surface area is a defect.
- Respect the priority tiers in `CONTEXT.md` §5. Do not build a P1 or P2 item early because it is interesting — FR-6, FR-9, FR-11b, OTel export and the remaining six fault types are all cut-safe and all out of the hard floor.
- No placeholder implementations, `pass`, `TODO`, mocked returns, or stubbed logic presented as done. If something cannot be completed, it is reported as **NOT DONE**, not shipped as a stub.

## 3. Uncertainty protocol — the anti-rogue rule

**Stop and ask** instead of guessing whenever:

- The PRD is genuinely silent on something load-bearing (not merely terse).
- An `[OPEN]` item from PRD §43 blocks you and `CONTEXT.md` §10 has no ruling.
- Satisfying the prompt would require breaking an invariant in `CONTEXT.md` §2.
- Two PRD sections contradict each other and `CONTEXT.md` §10 has no ruling. (If you resolve one, it goes into §10 as a new `C-n` row in the same commit.)
- The answer would change the event schema, a public interface, a threshold, an exit code, or a gate.

Format: `⚠️ BLOCKED — <question> · Options: A) … B) … · My recommendation: … because …`
Guessing quietly is the single most expensive failure mode in this project. A blocked prompt costs minutes; a silent wrong assumption costs a week.

## 4. Engineering standards

- Python 3.12, full type hints, `ruff` (incl. the `D` docstring rules) + `mypy --strict` clean. Docstrings on every public function stating **what it guarantees**, not what it does.
- No magic numbers. Thresholds live in config (`analysis/verdict_rules.toml`, `agentdx.toml`), versioned and printable.
- Errors are typed and carry an error code (`E-XXX-NNN`) plus a docs link. Never a bare `except:`. Never a swallowed exception.
- Every public function that can fail states its failure mode in the docstring.
- Frontend: TypeScript strict, no `any` (eslint-enforced). Panels are dumb; the Zustand store owns selection and derived state. All colour comes from CSS custom properties in `tokens.css` — never a literal hex in a component.

### 4.1 The determinism rule, stated so it is actually enforceable

Determinism is a codebase-wide property, not a module. Inside a run, ambient non-determinism is trapped by the runtime (PRD §10.5), but the lint rule exists because a trap that is bypassed is a silent I1 violation.

**Banned under `src/agentdx/`:** direct calls to `time.time`, `time.monotonic`, `time.perf_counter`, `datetime.now`, `datetime.utcnow`, `random.*`, `uuid.uuid4`, `asyncio.sleep`, and iteration over an unordered `set`.

**Sanctioned exceptions — these four and no others. Each is annotated `# determinism-exempt: <reason>` and the lint rule keys on that comment:**

1. `runtime/determinism.py` — installs and removes the patches.
2. `runtime/clock.py` — owns virtual time.
3. The **volatile-field writers**: whatever populates `wall_ts_ms`, `payload.duration_wall_ms`, and the `run_start` provenance fields (`host`, `pid`, `started_at_utc`, `env`). These *must* read the real clock — PRD §9.2 and §10.7 require the fields to exist. They reach it only through the single sanctioned accessor `agentdx.wall_time()`, and every such field is on the §10.7 exclusion list, so it never enters the canonical projection.
4. Code executing **outside a run context**: `api/` (the long-lived server), `cli/` argument handling and progress output, and `store/` file naming. A run is never in progress there.

Anything else that needs a clock, an id or a random number takes it from the injected `RunContext` — seeded, virtual, and reproducible. `agentdx.sorted_set()` replaces `set` iteration. `PYTHONHASHSEED=0` is required and `agentdx doctor` checks it.

## 5. Testing standards

- Tests are written in the same prompt as the code, never deferred to a later one.
- Analysis-layer tests use **hand-authored event logs with hand-computed expected outputs**. This is the point of the pure-analysis design: most of the product is testable without running an agent.
- Every bug fixed gets a regression test that fails before the fix.
- **Never weaken a test to make it pass.** If a determinism test, a false-positive test, or an invariant test fails, the code is wrong. Changing the assertion is a tripwire event (`CONTEXT.md` §11).
- Golden files are regenerated only on an explicit written instruction, never as a convenience, and the regenerating commit states what changed underneath them and why.
  - **Standing exception, scheduled by ADR-001:** the week-1 fixture golden corpora are provisional (they were recorded before the scheduler and cache existed). Regenerating them is an explicit deliverable of **P07** and requires a diff review of every changed golden, not a blanket accept. After P07 the normal rule resumes.

## 6. Evidence discipline

- Every finding, verdict and scorecard number traces to specific event `seq` values. An empty evidence array is a schema failure.
- **Rule E1, mechanised.** Every statistic published in the README, `docs/`, the UI or a release note carries an inline marker `[bench:<filename>]` naming a committed file in `bench/results/`. A CI job extracts every marker and fails if the file is missing; a second check fails the build on a numeric literal with a `%`, `×`, `ms`, `s` or `fps` unit in `README.md` or `docs/` that has no marker within the same sentence. Without the marker convention, I9 is unenforceable prose — with it, it is a grep.
- Where the system cannot know something, it says so. "No race detected in the explored schedules" is the honest sentence; "no races" is a lie.

## 7. Output contract — every response ends with these four blocks

```
## SELF-AUDIT
- [ ] Every deliverable file created, at the exact path
- [ ] Invariants in play: <list> — each held, with one line on how
- [ ] Acceptance check run, actual output pasted (not predicted)
- [ ] Files touched outside DELIVERABLES: none / <list + why>
- [ ] Assumptions made: none / <list>

## VERIFY THIS YOURSELF
<exact commands the human runs, and the exact expected output>

## CONTEXT LEDGER PATCH
<a copy-paste-ready diff for CONTEXT.md: §5 status rows, §6 gates, §7 position,
 §8 new ADRs, §9 new deviations, §10 new C-n rulings, §13 session row.
 The diff must contain no deletions or modifications inside §8 or §9 — additions only.>

## NOT DONE / RISKS
<anything incomplete, uncertain, or likely to bite later — be specific, not reassuring>
```

A response missing these blocks is incomplete regardless of how good the code is.

## 8. Honesty rules

- Report failure plainly. "The determinism test passes 97/100 and I have not found the leak" is a good answer. "Determinism implemented ✅" when it is 97/100 is a project-killing answer.
- Never claim a command was run when it was not. Paste real output.
- If you disagree with the PRD, say so in `NOT DONE / RISKS` with your reasoning — and then implement the PRD anyway unless the human overrides it.
- Do not pad responses with praise, summaries of what the user already knows, or restatements of the prompt.

## 9. Commits

`<prompt-id>(<module>): <imperative summary>` — e.g. `P06(runtime): add cooperative scheduler and virtual clock`.
Body lists: PRD sections implemented, invariants touched, gate status, ADRs added.
One prompt = one commit (or one stacked series). Never mix two prompts' work in a commit.

## 10. Ledger integrity — append-only, enforced

`CONTEXT.md` §8 (decision log) and §9 (deviations) are **append-only**. This is a check, not a hope:

- `.github/workflows/ci.yml` runs `just check-ledger` on every PR.
- `just check-ledger` extracts the §8 and §9 table bodies from `git show origin/main:CONTEXT.md` and from the working tree, and fails if any line present in the base is absent or altered in the head. Only additions pass. A whitespace-only change fails too — reformat those sections and the check is doing its job.
- The same script asserts `CONTEXT.md` is ≤ 500 lines and that every `ADR-NNN` referenced elsewhere in the file exists in §8.
- A pre-commit hook runs the same script locally so the failure is cheap.
- To reverse a decision, append a higher-numbered ADR that names the one it supersedes. To correct a deviation row, append a corrected row and mark the original `Reconciled? → superseded by D-nn`.

The rule exists because the decision log's only value is that it is trustworthy. A log someone can quietly tidy is a log that will be quietly tidied.
