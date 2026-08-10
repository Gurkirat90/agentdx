"""Ordered, forward-only event-schema migrations, one module per schema_version bump.

A migration never rewrites history in place: the event log is append-only and
immutable (I2). Migrations project old logs into the current schema on read.
Lands at P02.
"""
