# tests/determinism/

**Gate G3 / invariant I1.** Its own top-level suite because it gates everything else (PRD §25).
100 replays at seed 42 → 100 identical canonical-log hashes, at least 10 of them in fresh
processes. Never weaken an assertion here to make it pass — that is a tripwire event
(AGENTS.md §5, CONTEXT.md §11.1).
