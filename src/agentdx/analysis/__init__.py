"""PURE analytical passes over a sealed event log. Deterministic, testable without a runtime.

Must not import `agentdx.runtime.*`, `agentdx.sdk.*` or any model client — no module
is exempt, `baseline` included (I3, I13, PRD §24.3). Baseline executes a run through a
`BaselineExecutor` protocol declared here and constructed in `cli`; injection is the
mechanism that avoids the import, not a licence to make it. Enforced by `.importlinter`
with no allowlist entry. Will contain: causality.py, race.py, timing.py, overhead.py,
redundancy.py, baseline.py, resilience.py, verdict.py, verdict_rules.toml (P10–P12).
"""
