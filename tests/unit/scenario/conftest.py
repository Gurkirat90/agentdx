"""Shared fixtures for `tests/unit/scenario/`."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def repo_scenarios_dir() -> Path:
    """Return the repository's top-level `scenarios/` directory."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scenarios"
        if candidate.is_dir() and (candidate / "kill_reviewer.yaml").is_file():
            return candidate
    detail = "could not locate the repository's scenarios/ directory from tests/unit/scenario/"
    raise RuntimeError(detail)
