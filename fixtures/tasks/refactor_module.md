# Task: refactor_module

Referenced by `fixtures/code_pipeline/graph.py` (PRD §23.1 table, "Task" row).

Refactor `module_a.py`, a small utility module, so that its public function validates its
input and its tests pass. The module currently has no input validation and one failing edge
case.

```python
# module_a.py (as read_file("module_a.py") returns it — see cache/responses.json)
def normalise(value):
    return value.strip().lower()
```

**Acceptance:** `normalise` must not raise on `None`, and `run_tests("module_a")` must report
`passed`. Both `coder` and `reviewer` attempt this independently; see
`fixtures/code_pipeline/README.md` for why that is the fixture's seeded defect rather than a
deliberate two-reviewer workflow.
