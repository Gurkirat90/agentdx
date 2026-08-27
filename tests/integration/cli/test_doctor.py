"""`agentdx doctor` correctly diagnoses three deliberately broken setups (mission DoD).

Each test breaks exactly one precondition `doctor` checks and asserts both the failing
check's name and its non-zero exit — the same "what failed, what to run next" shape PRD
§22.7/Design Constraint 5 require of every failure message.
"""

from __future__ import annotations

import socket
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentdx.cli import _exitcodes
from agentdx.cli.main import app


def test_doctor_diagnoses_wrong_hash_seed(
    cli_runner: CliRunner, isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broken setup 1: `PYTHONHASHSEED` unset or not `'0'` (AGENTS.md §4.1, invariant I1)."""
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    result = cli_runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == _exitcodes.USAGE_ERROR
    assert '"name": "hash-seed"' in result.stdout
    assert '"ok": false' in result.stdout


def test_doctor_diagnoses_a_downlevel_store_schema(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """Broken setup 2: an `agentdx.db` whose recorded schema version this build cannot read.

    Fabricates a `schema_meta` table claiming a future version — the same shape
    `store.migrations.current_version` reads, without needing a real migration to produce one.
    """
    store_path = isolated_data_dir / "agentdx.db"
    conn = sqlite3.connect(str(store_path))
    conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_meta (version) VALUES (999)")
    conn.commit()
    conn.close()

    result = cli_runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == _exitcodes.USAGE_ERROR
    assert '"name": "store-migration"' in result.stdout
    assert '"ok": false' in result.stdout


def test_doctor_port_check_fails_when_configured_port_is_bound(
    cli_runner: CliRunner, isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Broken setup 3, properly isolated: `[api].port` pointed at an already-bound port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    bound_port = sock.getsockname()[1]
    try:
        config_path = tmp_path / "agentdx.toml"
        config_path.write_text(f"[api]\nport = {bound_port}\n", encoding="utf-8")
        result = cli_runner.invoke(app, ["--config", str(config_path), "doctor", "--json"])
        assert result.exit_code == _exitcodes.USAGE_ERROR
        assert '"name": "port"' in result.stdout
        assert '"ok": false' in result.stdout
    finally:
        sock.close()


def test_doctor_passes_on_a_healthy_setup(cli_runner: CliRunner, isolated_data_dir: Path) -> None:
    """Control: a fresh, correctly configured environment reports every check green."""
    result = cli_runner.invoke(app, ["doctor"])
    assert result.exit_code == _exitcodes.OK
