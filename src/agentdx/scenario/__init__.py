"""The declarative surface and its validation — the safety gate for chaos (PRD §12).

Owns the YAML schema, semantic validation, matrix expansion and assertion evaluation,
including the chaos opt-in and blast-radius checks that make I12 enforceable (E-SCEN-004).
Imports nothing else in the package. Lands at P08.

**Deliberately no re-export surface.** A `from agentdx.scenario import validate` package-level
re-export of `validate.validate` (the function) would shadow `agentdx.scenario.validate` (the
submodule) for any caller that does `from agentdx.scenario import loader, validate` expecting
the submodule — exactly the pattern this package's own test suite and fixture `checks.py`
files use. Every public symbol is reached via its owning submodule instead: `from
agentdx.scenario import loader, validate, matrix, assertions` (submodules), or
`from agentdx.scenario.validate import validate` (the function, by its full path) when only
the function is wanted. See `docs/scenario-reference.md` for the format this package
implements, and each submodule's own `__all__` for its public surface.
"""
