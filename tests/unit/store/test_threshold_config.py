"""The SQLite→DuckDB threshold is configurable and appears nowhere as a literal.

Design constraint 4 and CONTEXT.md §11 tripwire 5. Q-43.2.2 is still open and its default —
20 000 events — is expected to be tuned from benchmarks in week 5, so a copy of the number
inline in `store/` would go stale silently and the tuning would appear not to work.

The last test in this file is a source scan. It is deliberately crude: it greps the store
package for the default value. A subtler check would be easier to satisfy accidentally.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentdx.config import AgentDXConfig, ConfigError, StoreConfig
from agentdx.store import duckdb as analytics
from agentdx.store.sqlite import Store
from tests.unit.store.conftest import populate
from tests.unit.store.factories import build_log

DEFAULT_THRESHOLD = 20_000


def test_the_default_is_the_q43_2_2_recommendation() -> None:
    """The shipped default is 20 000 events, per PRD §27.1 and Q-43.2.2."""
    assert StoreConfig().duckdb_threshold_events == DEFAULT_THRESHOLD


def test_the_project_toml_sets_the_threshold_explicitly() -> None:
    """`agentdx.toml` states the threshold rather than relying on a code default.

    A threshold that exists only as a dataclass default is not printable, not reviewable in
    a diff and not tunable without a release (AGENTS.md §4).
    """
    text = (Path(__file__).resolve().parents[3] / "agentdx.toml").read_text(encoding="utf-8")
    assert "duckdb_threshold_events" in text
    assert "[store]" in text


def test_the_toml_value_overrides_the_default(tmp_path: Path) -> None:
    """A value in `agentdx.toml` beats the dataclass default (PRD §8.7)."""
    config_file = tmp_path / "agentdx.toml"
    config_file.write_text("[store]\nduckdb_threshold_events = 77\n", encoding="utf-8")
    config = AgentDXConfig.load(config_path=config_file, env={})
    assert config.store.duckdb_threshold_events == 77


def test_the_environment_overrides_the_toml(tmp_path: Path) -> None:
    """An environment variable beats `agentdx.toml` (PRD §8.7)."""
    config_file = tmp_path / "agentdx.toml"
    config_file.write_text("[store]\nduckdb_threshold_events = 77\n", encoding="utf-8")
    config = AgentDXConfig.load(
        config_path=config_file, env={"AGENTDX_STORE_DUCKDB_THRESHOLD_EVENTS": "123"}
    )
    assert config.store.duckdb_threshold_events == 123


def test_the_toml_beats_the_argument_layer(tmp_path: Path) -> None:
    """The file beats a programmatic argument, which is the §8.7 order."""
    config_file = tmp_path / "agentdx.toml"
    config_file.write_text("[store]\nduckdb_threshold_events = 77\n", encoding="utf-8")
    config = AgentDXConfig.load(
        config_path=config_file, env={}, store={"duckdb_threshold_events": 5}
    )
    assert config.store.duckdb_threshold_events == 77


def test_a_misspelt_environment_variable_is_an_error() -> None:
    """A typo is reported, never ignored.

    A threshold the user believes they set and which was silently dropped produces a
    benchmark describing a configuration nobody chose.
    """
    with pytest.raises(ConfigError) as excinfo:
        AgentDXConfig.load(env={"AGENTDX_STORE_DUCKDB_THRESHOLD_EVENT": "1"}, config_path=None)
    assert "unknown" in str(excinfo.value)


def test_a_non_numeric_threshold_is_an_error() -> None:
    """A value that cannot be an integer is refused rather than defaulted."""
    with pytest.raises(ConfigError):
        AgentDXConfig.load(env={"AGENTDX_STORE_DUCKDB_THRESHOLD_EVENTS": "lots"}, config_path=None)


def test_a_zero_threshold_is_an_error() -> None:
    """Out-of-range values fail as configuration errors, not as store bugs later."""
    with pytest.raises(ConfigError):
        AgentDXConfig.load(env={"AGENTDX_STORE_DUCKDB_THRESHOLD_EVENTS": "0"}, config_path=None)


def test_routing_follows_the_configured_threshold(tmp_path: Path) -> None:
    """`choose_route` switches at the configured value, whatever it is."""
    events = build_log(spans=4)
    with Store.open(tmp_path / "agentdx.db", config=StoreConfig(duckdb_threshold_events=5)) as s:
        run_id = populate(s, events)
        route = analytics.choose_route(s, run_id)
        assert route.event_count == len(events)
        assert route.threshold == 5
        assert route.use_duckdb is (analytics.analytics_available() and len(events) >= 5)

        high = analytics.choose_route(s, run_id, StoreConfig(duckdb_threshold_events=10**9))
        assert high.use_duckdb is False
        assert high.warning is None


def test_a_missing_duckdb_falls_back_with_a_warning(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """PRD §27.4: an optional accelerator must not be able to hard-fail the product.

    The fallback carries a warning rather than being silent (PRD §36 rule 1), and the
    warning names the install command rather than only the problem (AGENTS.md §4).
    """
    monkeypatch.setattr(analytics, "analytics_available", lambda: False)
    events = build_log(spans=4)
    with Store.open(tmp_path / "agentdx.db", config=StoreConfig(duckdb_threshold_events=1)) as s:
        run_id = populate(s, events)
        route = analytics.choose_route(s, run_id)
        assert route.use_duckdb is False
        assert route.warning is not None
        assert "E-STORE-016" in route.warning
        assert "uv sync" in route.warning


def test_no_module_in_store_hardcodes_the_threshold() -> None:
    """Tripwire 5, mechanised: the number 20000 appears in no `store/` module.

    Deliberately a source scan. A store module that needs the threshold reads it from
    `StoreConfig`; one that spells it out has created a second source of truth that the
    week-5 tuning will not reach.
    """
    package = Path(__file__).resolve().parents[3] / "src" / "agentdx" / "store"
    offenders = []
    for module in sorted(package.rglob("*.py")):
        text = module.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if "20000" in line.replace("_", "") and "duckdb_threshold_events" not in line:
                offenders.append(f"{module.name}:{number}: {line.strip()}")
    assert not offenders, "threshold literal found in store/:\n" + "\n".join(offenders)
