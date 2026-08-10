# Contributing to AgentDX

Read `CONTEXT.md` end to end before anything else, then the PRD sections your prompt names,
then `AGENTS.md`. That order is not a formality: `CONTEXT.md` is the running state, the PRD is
the running spec, and `AGENTS.md` wins over both on process (`CONTEXT.md` §0.5).

## The loop

Every unit of work — human or AI — follows the same six steps. The loop exists because the
expensive failure in this project is not a bug, it is a silent assumption that survives three
sessions before anyone notices.

### 1. Prompt

Work starts from a prompt with an explicit `DELIVERABLES` list and `AUTHORITATIVE INPUTS`.
Restate, in eight lines or fewer: the mission, the deliverable file list, the invariants in
play, and the acceptance gate. State any conflict you found between the prompt, `CONTEXT.md`
and the PRD — check `CONTEXT.md` §10 first, it may already have a ruling. **Never resolve a
conflict silently.**

If you are blocked, say so in the standard form and stop:

```
⚠️ BLOCKED — <question> · Options: A) … B) … · My recommendation: … because …
```

A blocked prompt costs minutes. A quiet guess costs a week.

### 2. Build

Build only what `DELIVERABLES` names. Touching an unlisted file is a scope violation, not
initiative. No new dependency enters `pyproject.toml` or `package.json` without an ADR in
`CONTEXT.md` §8 first — the permitted set is `CONTEXT.md` §3 plus PRD §24.6 and §25.

Tests are written in the same prompt as the code they cover, never deferred to a later one.
No placeholder implementations, no `pass`, no mocked returns presented as done. If something
cannot be completed, it is reported as **NOT DONE**.

### 3. Verify

```bash
just ci              # lint · typecheck · test · imports · determinism · ledger · bench markers
just lint-frontend   # tsc --noEmit + eslint
```

Each of these is also a CI job and a pre-commit hook, invoked identically. Run `just hooks`
once so the failure is cheap and local.

What each gate is actually protecting:

| Command | Protects |
|---|---|
| `just lint` / `just typecheck` | AGENTS.md §4 engineering standards |
| `just test` | everything; determinism and false-positive suites are never weakened to pass |
| `just check-imports` | **I3** analysis purity and **I13** no model in the analysis path |
| `just check-determinism` | **I1** — ambient clock, randomness, uuid and set-order dependence |
| `just check-ledger` | append-only decision and deviation logs (AGENTS.md §10) |
| `just check-bench` | **I9** / Rule E1 — no published number without a committed measurement |

**Never weaken a test to make it pass.** If a determinism test, a false-positive test or an
invariant test fails, the code is wrong. Changing the assertion is a tripwire event
(`CONTEXT.md` §11.1).

### 4. Audit

Close out with the four blocks from `AGENTS.md` §7 — self-audit, verify-this-yourself,
ledger patch, not-done/risks. Paste real command output; never claim a command was run when
it was not. "The determinism test passes 97/100 and I have not found the leak" is a good
answer; "determinism implemented ✅" when it is 97/100 is a project-killing one.

### 5. Ledger patch

Update `CONTEXT.md` in the same commit as the code:

- **§5** build state row, **§6** gate status, **§7** current position, **§13** session row —
  edit in place, these are living sections.
- **§8** decision log and **§9** deviations — **append only**. To reverse a decision, append a
  higher-numbered ADR naming the one it supersedes. To correct a deviation, append a corrected
  row and mark the original `superseded by D-nn`.
- Anything the code does that the PRD does not say goes in §9 **in the same commit**. An
  undeclared deviation is the single most likely cause of a bad handoff.

`just check-ledger` enforces the append-only rule, the 500-line cap and ADR reference
integrity. It counts whitespace, deliberately: a whitespace-only edit to a decision row is
exactly the quiet tidying the rule exists to prevent.

### 6. Commit

```
<prompt-id>(<module>): <imperative summary>
```

For example `P06(runtime): add cooperative scheduler and virtual clock`. The body lists the
PRD sections implemented, the invariants touched, gate status and any ADRs added. One prompt
is one commit, or one stacked series. Never mix two prompts' work in a commit.

## Setup

```bash
just sync            # Python 3.12 environment via uv
just sync-frontend   # npm ci
just hooks           # install the pre-commit hooks
```

## Where the rules actually live

| Rule | Written in | Enforced by |
|---|---|---|
| Layer contract | `CONTEXT.md` §4, PRD §24.3 | `.importlinter` |
| Determinism hygiene | `AGENTS.md` §4.1 | `scripts/check_determinism_hygiene.py` |
| Append-only ledger | `AGENTS.md` §10 | `scripts/check_ledger.py` |
| Published numbers | `AGENTS.md` §6, Rule E1 | `scripts/check_bench_markers.py` |
| Style and types | `AGENTS.md` §4 | `ruff`, `mypy --strict`, `eslint`, `tsc` |

An architectural rule that is not checked by CI is a comment. If you find a rule in a
document with no row in that table, that gap is the bug.
