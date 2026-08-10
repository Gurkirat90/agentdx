"""Everything that executes — and therefore the only place non-determinism can enter.

Because it is the only entry point for ambient non-determinism, it is also the only
place non-determinism is trapped (I1, PRD §10.5). May import `events` and `store`;
must not import `analysis` or `sdk` (CONTEXT.md §4).
Will contain: scheduler.py, clock.py, context.py, determinism.py, faults/, cache/ (P06–P09).
"""
