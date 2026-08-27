"""Shared fixtures for `tests/integration/cli/`.

Every test invokes the real Typer `app` in-process via `typer.testing.CliRunner`, exactly the
`agentdx` console script's own entry point (`cli.main.app`) — not a re-implementation of
argument parsing. Each test gets an isolated `AGENTDX_STORE_DATA_DIR` so runs, caches and the
event store never leak between tests or touch the developer's real `~/.agentdx`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture
def cli_runner() -> CliRunner:
    """A Typer `CliRunner` bound to the real `agentdx` app."""
    return CliRunner()


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every `[store]`/`[run]` `data_dir` at a fresh temp directory for one test."""
    data_dir = tmp_path / "agentdx-data"
    data_dir.mkdir()
    monkeypatch.setenv("AGENTDX_STORE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AGENTDX_RUN_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    yield data_dir


@pytest.fixture
def repo_root() -> Path:
    """The checkout root, so tests can point at real fixtures/scenarios by relative path."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "fixtures").is_dir():
            return parent
    msg = "could not find the repo root from tests/integration/cli/conftest.py"
    raise RuntimeError(msg)
