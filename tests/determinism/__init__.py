"""Gate G3 — the determinism suite that gates everything else (P06 mission, DELIVERABLES).

Every other test suite in this repository can be wrong in a way that only shows up here:
a scheduler that passes `tests/unit/runtime/` in isolation can still fail to reproduce a
byte-identical canonical projection across two runs at the same seed. This package is
the DEFINITION OF DONE for P06, run separately from `tests/unit/` because it is slower
(it spawns real OS subprocesses) and because a failure here means the product's central
claim — PRD §10.1's "same seed in, same canonical log out" — does not hold, which is a
different severity of failure than an ordinary unit regression.
"""
