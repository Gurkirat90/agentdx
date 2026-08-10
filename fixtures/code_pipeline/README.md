# code_pipeline

Four agents (planner → coder → reviewer → tester) over shared state, with a **seeded lost update**
on `draft.module_a`. Gate G1 requires at least one `lost_update` finding of severity `critical`
naming both writes. Built at P05 (ADR-001).
