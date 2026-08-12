"""The three FR-12 reference fixtures, plus their shared tooling.

`fixtures/__init__.py` and each fixture's own `__init__.py` exist only so `graph.py` and
`checks.py` are importable as `fixtures.<name>.graph` / `fixtures.<name>.checks` from
`tests/golden/` and from `just fixtures-check` — packaging, not a feature, in the same sense
as `tests/__init__.py` (P02, deviation D-11). See `fixtures/README.md` and `docs/fixtures.md`.
"""
