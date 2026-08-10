# cache/

Committed, compressed SQLite LLM cache for this fixture. Shipped as package data (PRD §39.4) so the
demo runs offline: replay is the default mode and a miss is a hard error, never a live call (I7).
Recorded at P05 and **regenerated at P07** once the scheduler and cache exist (ADR-001).
