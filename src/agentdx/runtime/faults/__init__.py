"""Fault registry, triggers, interception points and blast-radius enforcement (PRD §13).

MVP fault set is `latency`, `agent_crash`, `message_drop`, `tool_failure` only; the
other six are P1 (CONTEXT.md §3). Chaos is fixture-only unless the scenario file
opts in and declares a non-empty blast radius (I12). Lands at P09.
"""
