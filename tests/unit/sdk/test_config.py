"""The `[run]`, `[privacy]` and `[llm]` sections and the PRD §8.7 precedence chain.

The `[store]` section's own tests live in `tests/unit/store/test_threshold_config.py` and are
P03's; this file covers what P04 added, plus one test that the shared precedence machinery
still behaves the same for `[store]` after being generalised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentdx.config import AgentDXConfig, ConfigError, LlmConfig, PrivacyConfig, RunConfig

TOML = """
[run]
seed = 7
mode = "record"

[privacy]
capture_bodies = true
redact_patterns = ["tok-[0-9]{4}"]

[llm]
provider = "anthropic"
model = "claude-haiku-4-5"
base_url = "https://api.anthropic.com/v1/"

[store]
duckdb_threshold_events = 999
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "agentdx.toml"
    path.write_text(TOML, encoding="utf-8")
    return path


def test_defaults_are_the_prd_8_7_defaults() -> None:
    config = AgentDXConfig()
    assert config.run == RunConfig(seed=42, mode="replay", data_dir=Path("~/.agentdx"))
    assert config.privacy.capture_bodies is False
    assert config.privacy.redact_patterns == ("sk-[A-Za-z0-9]{20,}", "AKIA[0-9A-Z]{16}")
    assert config.llm == LlmConfig(
        provider="groq",
        model="llama-3.1-8b-instant",
        base_url="https://api.groq.com/openai/v1",
    )


def test_the_file_overrides_the_argument_layer(config_file: Path) -> None:
    config = AgentDXConfig.load(config_path=config_file, env={}, run={"seed": 1})
    assert config.run.seed == 7
    assert config.run.mode == "record"
    assert config.privacy.capture_bodies is True
    assert config.llm.provider == "anthropic"
    assert config.llm.base_url == "https://api.anthropic.com/v1"


def test_the_environment_overrides_the_file(config_file: Path) -> None:
    config = AgentDXConfig.load(
        config_path=config_file,
        env={"AGENTDX_RUN_SEED": "99", "AGENTDX_LLM_MODEL": "gpt-4o-mini"},
    )
    assert config.run.seed == 99
    assert config.llm.model == "gpt-4o-mini"


def test_the_argument_layer_applies_when_nothing_higher_sets_it(
    config_file: Path, tmp_path: Path
) -> None:
    config = AgentDXConfig.load(config_path=config_file, env={}, llm={"provider": "local"})
    assert config.llm.provider == "anthropic", "the file must win over the argument"

    quiet = tmp_path / "quiet.toml"
    quiet.write_text("[run]\nseed = 3\n", encoding="utf-8")
    config = AgentDXConfig.load(config_path=quiet, env={}, llm={"provider": "local"})
    assert config.llm.provider == "local", "with no [llm] table the argument layer applies"
    assert config.run.seed == 3


def test_the_store_section_still_resolves_after_generalisation(config_file: Path) -> None:
    # P03's precedence machinery was generalised at P04; this asserts it did not move.
    config = AgentDXConfig.load(config_path=config_file, env={})
    assert config.store.duckdb_threshold_events == 999
    config = AgentDXConfig.load(
        config_path=config_file, env={"AGENTDX_STORE_DUCKDB_THRESHOLD_EVENTS": "10"}
    )
    assert config.store.duckdb_threshold_events == 10


def test_capture_bodies_is_never_coerced_from_a_string() -> None:
    # The single most damaging coercion bug available: bool("False") is True, and a privacy
    # default that flips because of it would put prompt bodies in the log (I8).
    config = AgentDXConfig.load(env={"AGENTDX_PRIVACY_CAPTURE_BODIES": "False"}, config_path=None)
    assert config.privacy.capture_bodies is False
    with pytest.raises(ConfigError, match="must be a boolean"):
        AgentDXConfig.load(env={"AGENTDX_PRIVACY_CAPTURE_BODIES": "maybe"}, config_path=None)


def test_an_unknown_mode_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(ConfigError, match="must be one of"):
        AgentDXConfig.load(env={"AGENTDX_RUN_MODE": "replya"}, config_path=None)


def test_an_unknown_setting_is_reported_not_ignored() -> None:
    with pytest.raises(ConfigError, match="unknown"):
        AgentDXConfig.load(env={"AGENTDX_RUN_SEEED": "1"}, config_path=None)
    with pytest.raises(ConfigError, match="unknown"):
        PrivacyConfig().with_overrides(capture_body=True)


def test_an_invalid_redaction_pattern_fails_at_load_not_at_emission() -> None:
    # Redaction runs while an error is being recorded; a redactor that raised there would
    # destroy the error it was protecting.
    with pytest.raises(ConfigError, match="not a valid regex"):
        AgentDXConfig.load(
            env={"AGENTDX_PRIVACY_REDACT_PATTERNS": json.dumps(["sk-[0-9"])},
            config_path=None,
        )


def test_redaction_patterns_from_the_environment_are_json_not_comma_separated() -> None:
    # `sk-[A-Za-z0-9]{20,}` contains a comma; splitting on it would silently turn one working
    # pattern into two broken ones.
    config = AgentDXConfig.load(
        env={"AGENTDX_PRIVACY_REDACT_PATTERNS": json.dumps(["sk-[A-Za-z0-9]{20,}"])},
        config_path=None,
    )
    assert config.privacy.redact_patterns == ("sk-[A-Za-z0-9]{20,}",)
    with pytest.raises(ConfigError, match="JSON array"):
        AgentDXConfig.load(
            env={"AGENTDX_PRIVACY_REDACT_PATTERNS": "sk-[A-Za-z0-9]{20,},AKIA"},
            config_path=None,
        )


def test_an_empty_llm_field_is_refused() -> None:
    with pytest.raises(ConfigError, match="must not be empty"):
        AgentDXConfig.load(env={"AGENTDX_LLM_BASE_URL": "   "}, config_path=None)


def test_the_repository_agentdx_toml_loads() -> None:
    # tripwire 5: thresholds live in agentdx.toml, so the shipped file must actually parse
    # into the sections the SDK reads.
    root = Path(__file__).resolve().parents[3]
    config = AgentDXConfig.load(config_path=root / "agentdx.toml", env={})
    assert config.run.mode == "replay"
    assert config.privacy.capture_bodies is False
    assert config.llm.provider == "groq"
