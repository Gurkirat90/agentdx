"""AgentDX's own benchmark harnesses and published results (PRD §34).

Not part of the installed `agentdx` package (`src/agentdx/` is what `uv build` ships) — this
exists so `bench.harness.*` modules can import each other by dotted path (e.g.
`bench.harness.race_accuracy` imports `bench.harness.race_accuracy_corpus`) the same way the
rest of this repository's own tooling does, rather than each harness inserting its neighbour
onto `sys.path` by hand.
"""
