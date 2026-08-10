# fixtures/

The three reference systems (FR-12), the demo, the regression suite and the false-positive control.
All three are built in week 1 under P05 (**ADR-001**, which overrides the PRD §40.1 schedule).

| Fixture | Role |
|---|---|
| `code_pipeline` | Seeded lost update on `draft.module_a` — gate G1 |
| `support_triage` | Seeded redundant work — the redundancy detector's only input |
| `research_fanout` | **Healthy control.** Must yield zero findings — gate G2, invariant I4. Never cut |
| `tasks/` | Task definitions referenced by scenarios |
| `perturbations/` | Curated confident-wrong responses for byzantine testing (PRD §11.8) |

Each fixture directory holds `graph.py`, `checks.py`, `cache/` and `golden_findings.json`. The
committed caches are what make the demo run offline with no API keys (PRD §38.2).
