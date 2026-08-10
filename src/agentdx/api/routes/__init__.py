"""One module per REST resource: runs, findings, analysis, scorecard, health (PRD §26).

Routers are thin: they validate, call `store`/`analysis`, and serialise Pydantic
response models. Every response carrying findings also carries the verbatim
coverage statement (I10). Lands at P14.
"""
