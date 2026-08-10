"""Thin transport over `store` + `analysis`: FastAPI REST, WebSocket, OpenAPI. No business logic.

Must not import `runtime` — the server launches runs as a subprocess and tails the
event table (PRD §24.2, CONTEXT.md §4). Binds 127.0.0.1:8420 with no auth; `--host`
is required to bind elsewhere and prints a warning. Will contain: app.py, ws.py,
models.py, routes/ (P14).
"""
