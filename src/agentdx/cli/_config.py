"""Resolve `AgentDXConfig` from the PRD §37 global options.

`AgentDXConfig.load()` already implements the environment → `agentdx.toml` → per-call
argument → dataclass-default chain (PRD §8.7) and says explicitly that CLI flags sit above
all of it, applied by `cli/` as an explicit `with_overrides` — see its own docstring. This
module is that one, named, explicit application, so every command resolves config the same
way instead of five slightly different ways.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agentdx.config import AgentDXConfig

__all__ = ["GlobalOptions", "resolve_config"]


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    """The PRD §37 global options, exactly as Typer parsed them.

    Every field is `None`/`False` when the user did not pass the flag, so `resolve_config`
    can tell "not given" from "given as the default value" — the same distinction
    `with_overrides` needs to avoid clobbering a `agentdx.toml` value with an unset CLI flag.
    """

    data_dir: Path | None = None
    config_path: Path | None = None
    verbose: bool = False
    quiet: bool = False
    json_mode: bool = False
    no_color: bool = False
    seed: int | None = None
    strict: bool | None = None


def resolve_config(options: GlobalOptions) -> AgentDXConfig:
    """Resolve one `AgentDXConfig`, with `options` applied as the top precedence layer.

    Raises:
        ConfigError: `agentdx.toml` exists but fails to parse, or a resolved value fails its
            own section's validation (`agentdx.config.ConfigError`, `E-CONFIG-001`).
    """
    resolved = AgentDXConfig.load(config_path=options.config_path, env=os.environ)
    run_overrides: dict[str, object] = {}
    if options.data_dir is not None:
        run_overrides["data_dir"] = options.data_dir
    if options.seed is not None:
        run_overrides["seed"] = options.seed
    store_overrides: dict[str, object] = {}
    if options.data_dir is not None:
        store_overrides["data_dir"] = options.data_dir
    scheduler_overrides: dict[str, object] = {}
    if options.strict is not None:
        scheduler_overrides["strict_determinism"] = options.strict

    run = resolved.run.with_overrides(**run_overrides) if run_overrides else resolved.run
    store = resolved.store.with_overrides(**store_overrides) if store_overrides else resolved.store
    scheduler = (
        resolved.scheduler.with_overrides(**scheduler_overrides)
        if scheduler_overrides
        else resolved.scheduler
    )
    return AgentDXConfig(
        store=store,
        run=run,
        privacy=resolved.privacy,
        llm=resolved.llm,
        scheduler=scheduler,
        cache=resolved.cache,
        explore=resolved.explore,
        api=resolved.api,
    )
