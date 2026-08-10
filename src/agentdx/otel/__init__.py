"""OpenTelemetry GenAI span export (PRD §30).

P1, scope-cut #5. Export only: AgentDX never depends on OTel for its own
correctness, because the event log is the contract and OTel is a projection of it.
Lands at P19 if not cut.
"""
