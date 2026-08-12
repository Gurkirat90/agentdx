# cache/

**Correction (2026-08-13, found by an independent OP-2 audit).** This file previously described
a "compressed SQLite LLM cache." That was never true of this directory and was stale P01
scaffolding nobody updated when P05 actually populated it — `support_triage/cache/` and
`research_fanout/cache/` never had an equivalent file, so the false description wasn't even a
consistently-applied pattern, just an orphan.

What's actually here: `responses.json`, a plain, committed JSON map from a JSON-stringified,
sorted-args key to a deterministic tool response — a tool-response pool, not an LLM cache.
PRD §23.1's own "Tools" row for `code_pipeline` lists no LLM call, so there is nothing here for
`runtime/cache/`'s record/replay/perturb modes (P07, `NOT STARTED`) to eventually own; I7
(offline by default) is satisfied unconditionally by this pool rather than through a cache mode.
See `fixtures/_harness.py`'s `ResponsePool` docstring and `docs/fixtures.md` for the full
explanation — both already stated this correctly; only this file disagreed with them.

Recorded at P05. Regenerated at P07 alongside the rest of each fixture's golden corpus
(ADR-001 consequence 2), same as everything else `fixtures/_harness.py` stamps — not because
this file becomes a real LLM cache then, but because the stamping around it does.
