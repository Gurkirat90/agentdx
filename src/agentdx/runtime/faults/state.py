"""State-class faults (PRD §12.2): `state_corrupt`.

**Deferred — P1, not in this build's MVP set.**

Same posture as `semantic.py` — read that module's docstring first; this one states only what is
specific to `state_corrupt`. No executable fault logic lives here, deliberately (AGENTS.md §3).

**Why `state_corrupt` is deferred, not attempted.** CONTEXT.md §3 locks the MVP set to `latency`,
`agent_crash`, `message_drop`, `tool_failure`; `state_corrupt` (PRD §12.2, tier P1,
`scenario.schema.FAULT_CATALOGUE["state_corrupt"]`) is outside it. Its `target_kinds` is
`(TargetKind.STATE_KEY,)` and its triggers include `ON_STATE_WRITE` — both already fully modelled
by `registry.BlastRadius` (glob-matched `state_keys`, PRD §13.4) and `triggers.should_fire`
(the `ON_STATE_WRITE` case is implemented and unit-testable today). What is genuinely missing is
not trigger/authorisation plumbing but an actual interception point: PRD §12.1 does not name one
for a state write at all (the diagram's 7 points stop at `pre_state_write` only as a label in
prose, absent from the ASCII diagram itself — a PRD gap, not this module's to resolve), and
`agentdx.state()` (the SDK's shared-state write primitive the fixtures use) has no hook a fault
could intercept without an `sdk/generic.py` (P04) change, exactly the same class of gap
`transport.py`/`dependency.py` document for their own MVP faults — except here it is compounded
by tier (P1) as well, so it is not even attempted as pure decision-only logic the way `latency`/
`message_drop`/`tool_failure` are: a fault whose whole effect *is* mutating a specific value
(`mutation`: drop/truncate/swap/stale/type_change) needs a real value to mutate, and no
interception point in this build ever hands one to a fault-class module.

**Structurally unreachable, not just undocumented.** Same mechanism as `semantic.py`:
`registry.FaultRegistry.from_resolved_scenario` raises `FaultNotImplementedError` (`E-CHAOS-002`)
for `state_corrupt` before any code here would run.

See `docs/chaos-safety.md` §"MVP fault set" and the closing NOT DONE block.
"""

from __future__ import annotations

__all__: list[str] = []
