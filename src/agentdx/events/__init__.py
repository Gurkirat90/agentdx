"""THE CONTRACT: event schema, validation, canonical form and the append-only writer.

The single most important package; changes here are breaking changes and require a
`schema_version` bump plus an ADR (CONTEXT.md §11 tripwire 6). Imports nothing else
in the package — it is the root of the layer contract (CONTEXT.md §4).
Will contain: schema.py, validators.py, canonical.py, writer.py, migrations/ (P02).
"""
