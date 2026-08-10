"""LLM record/replay/perturb cache and cache-key construction (PRD §11).

`replay` is the default mode and a replay-mode miss is a hard error (E-CACHE-001,
exit 3) — there is no fall-back-to-live flag and none may be added (I7).
Lands at P07.
"""
