# tests/integration/

Each fixture end to end, offline (PRD §33.2): run → analyse → assert golden findings → export
bundle → import → verify. Plus the generic-decorator smoke test, a LangGraph subgraph test, and an
aborted-guard test asserting the partial log is still analysable (NFR-13).
