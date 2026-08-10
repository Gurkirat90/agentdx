"""The user-facing capture surface: decorators, LangGraph adapter and provider shims.

Must survive LangGraph version drift and fail loudly rather than silently miss
capture. May import `events` and `runtime`; must not import `analysis`
(CONTEXT.md §4). Will contain: decorators.py, generic.py, sync.py, langgraph.py,
providers/ (P04).
"""
