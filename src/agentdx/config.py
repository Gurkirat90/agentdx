"""Layered configuration: CLI flag → env var → agentdx.toml → argument → default (PRD §8.7).

Owns the precedence chain and the typed config object. No threshold or weight is
ever written inline in code; every one of them resolves through here or through
`analysis/verdict_rules.toml` (AGENTS.md §4, CONTEXT.md §11 tripwire 5).
Implementation lands at P03.
"""
