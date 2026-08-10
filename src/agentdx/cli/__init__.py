"""Typer commands, exit codes and output formatting — one module per command (PRD §37).

May import every other layer; nothing imports it (CONTEXT.md §4). Exit codes 0–7
are authoritative and stable (PRD §37.2) — changing one is a breaking change.
Commands land at P17; `main.py` currently registers stubs so the entry point is
real from day one.
"""
