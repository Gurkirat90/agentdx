"""Direct boundary-value tests for `[explore]`'s own coercion (`config._coerce_explore`).

op2-audit-p13.md finding #5: no test anywhere directly exercised `_coerce_explore`'s five
checks — only indirect regression coverage that *other* config sections still resolve after
`[explore]` was added (`tests/unit/sdk/test_config.py`, `tests/unit/store/
test_threshold_config.py`). The audit live-verified the underlying logic is fully correct (9/9
boundary probes correctly rejected) but flagged the coverage gap itself as real. These tests
promote that live verification into the permanent suite.

Each test passes an explicit `config_path` pointing at an empty file (never `None`, and never
the repo's own `agentdx.toml`) so `AgentDXConfig.load`'s file layer cannot mask a broken
argument-layer override — the exact mistake the audit's own first pass made and caught.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentdx.config import AgentDXConfig, ConfigError, ExploreConfig


@pytest.fixture
def empty_config(tmp_path: Path) -> Path:
    """A real, existing, but empty `agentdx.toml` — no `[explore]` table of its own.

    So every resolved value in these tests comes from the argument layer or the dataclass
    default, never a file value silently agreeing with (and so hiding a break in) the code
    under test.
    """
    path = tmp_path / "agentdx.toml"
    path.write_text("", encoding="utf-8")
    return path


def test_defaults_match_the_prd_15_1_table(empty_config: Path) -> None:
    config = AgentDXConfig.load(config_path=empty_config, env={})
    assert config.explore == ExploreConfig(
        delay_bound_k=2,
        schedule_cap_n=200,
        time_budget_s=120.0,
        strategy="delay_bounded",
        upgrade_reduction_if_redundancy_over=0.40,
    )


@pytest.mark.parametrize("value", [6, -1])
def test_delay_bound_k_out_of_range_0_to_5(empty_config: Path, value: int) -> None:
    with pytest.raises(ConfigError, match=r"delay_bound_k must be between 0 and 5"):
        AgentDXConfig.load(config_path=empty_config, env={}, explore={"delay_bound_k": value})


@pytest.mark.parametrize("value", [0, 10_001])
def test_schedule_cap_n_out_of_range_1_to_10000(empty_config: Path, value: int) -> None:
    with pytest.raises(ConfigError, match=r"schedule_cap_n must be between 1 and 10000"):
        AgentDXConfig.load(config_path=empty_config, env={}, explore={"schedule_cap_n": value})


@pytest.mark.parametrize("value", [0.0, -5.0])
def test_time_budget_s_must_be_positive(empty_config: Path, value: float) -> None:
    with pytest.raises(ConfigError, match=r"time_budget_s must be > 0"):
        AgentDXConfig.load(config_path=empty_config, env={}, explore={"time_budget_s": value})


def test_strategy_rejects_an_unrecognised_name(empty_config: Path) -> None:
    with pytest.raises(ConfigError, match=r"strategy must be one of"):
        AgentDXConfig.load(config_path=empty_config, env={}, explore={"strategy": "bogus_strategy"})


@pytest.mark.parametrize("value", ["delay_bounded", "random", "replay_set"])
def test_strategy_accepts_every_declared_name(empty_config: Path, value: str) -> None:
    config = AgentDXConfig.load(config_path=empty_config, env={}, explore={"strategy": value})
    assert config.explore.strategy == value


@pytest.mark.parametrize("value", [1.5, -0.1])
def test_upgrade_reduction_threshold_out_of_range_0_to_1(empty_config: Path, value: float) -> None:
    with pytest.raises(
        ConfigError, match=r"upgrade_reduction_if_redundancy_over must be between 0.0 and 1.0"
    ):
        AgentDXConfig.load(
            config_path=empty_config,
            env={},
            explore={"upgrade_reduction_if_redundancy_over": value},
        )


def test_a_valid_override_at_each_boundary_is_accepted(empty_config: Path) -> None:
    # The inclusive edges themselves (0/5, 1/10000, 0.0/1.0) must not be rejected — only
    # values strictly outside them.
    config = AgentDXConfig.load(
        config_path=empty_config,
        env={},
        explore={
            "delay_bound_k": 0,
            "schedule_cap_n": 1,
            "time_budget_s": 0.001,
            "upgrade_reduction_if_redundancy_over": 0.0,
        },
    )
    assert config.explore.delay_bound_k == 0
    assert config.explore.schedule_cap_n == 1
    assert config.explore.upgrade_reduction_if_redundancy_over == 0.0

    config2 = AgentDXConfig.load(
        config_path=empty_config,
        env={},
        explore={
            "delay_bound_k": 5,
            "schedule_cap_n": 10_000,
            "upgrade_reduction_if_redundancy_over": 1.0,
        },
    )
    assert config2.explore.delay_bound_k == 5
    assert config2.explore.schedule_cap_n == 10_000
    assert config2.explore.upgrade_reduction_if_redundancy_over == 1.0
