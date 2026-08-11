"""Layered configuration: CLI flag → env var → agentdx.toml → argument → default (PRD §8.7).

Owns the precedence chain and the typed config object. No threshold or weight is
ever written inline in code; every one of them resolves through here or through
`analysis/verdict_rules.toml` (AGENTS.md §4, CONTEXT.md §11 tripwire 5).

**Why this module exists at all, and why it is small.** P03 needs exactly one thing from
configuration — the SQLite→DuckDB threshold of Q-43.2.2, which its prompt forbids
hardcoding. The alternative was a second settings loader inside `store/`, which is the
duplicated-source-of-truth pattern this codebase treats as a defect: P08 and P14 would then
each have to decide which loader they meant. So the precedence chain lands here, once, and
this module currently exposes only the sections P03 needs. Later prompts extend
`AgentDXConfig` with their own frozen section dataclasses; they do not re-implement the
resolution order.

Determinism note: this module reads the environment and the filesystem. Neither is a source
of non-determinism *inside a run* — configuration is resolved before a run starts and is
recorded in `run_start` (PRD §10.10), so a replay pins it. Nothing here reads a clock.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, TypeVar

_DOCS: Final = "docs/configuration.md"

DEFAULT_CONFIG_FILENAME: Final = "agentdx.toml"
"""The project-local configuration file (PRD §8.7)."""

ENV_PREFIX: Final = "AGENTDX_"
"""Environment variables are `AGENTDX_<SECTION>_<KEY>`, upper-cased."""

_T = TypeVar("_T", bound=str | int | bool)


class ConfigError(ValueError):
    """Configuration could not be resolved into a valid typed value.

    Carries `E-CONFIG-001`. Raised rather than defaulted: a threshold the user believes
    they set, silently ignored because it was misspelt or mistyped, is worse than a stop —
    it produces a benchmark number that describes a configuration nobody chose.
    """

    code: Final = "E-CONFIG-001"

    def __init__(self, detail: str) -> None:
        """Build the error from a description of what could not be resolved."""
        super().__init__(f"[{self.code}] {detail} ({_DOCS}#{self.code.lower()})")


# ---------------------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoreConfig:
    """Storage thresholds and paths (PRD §27).

    Guarantees: every field here is resolvable from `agentdx.toml`, an environment variable
    and a keyword argument, in the PRD §8.7 order. No consumer in `store/` holds a literal
    for any of them — `duckdb_threshold_events` in particular is Q-43.2.2 and is expected
    to be tuned from benchmarks in week 5, so a hardcoded copy would go stale silently.
    """

    data_dir: Path = Path("~/.agentdx")
    duckdb_threshold_events: int = 20_000
    """Q-43.2.2. At or above this many events a run is exported to Parquet on seal and
    analysed through DuckDB; below it, analysis reads SQLite directly. PRD §27.1 is explicit
    that this is a performance decision and never a semantic one, so both paths must produce
    identical results and a test asserts exactly that."""

    snapshot_interval_events: int = 500
    """PRD §20.4: state snapshots are materialised every Nth event, bounding reconstruction
    at O(N) regardless of run length. A snapshot is an optimisation, never the source of
    truth — the log is."""

    append_batch_size: int = 128
    """Events per `Store.append` transaction. PRD §27.3 says "64 events or 50 ms"; the
    writer's own `DEFAULT_BATCH_SIZE` is 128 and it is the component that decides when to
    flush, so this is the store-side ceiling used by the bulk paths (bundle import, Parquet
    round-trips) rather than a second flush policy."""

    synchronous: str = "NORMAL"
    """PRD §27.3. NORMAL during a run: a crash may lose the last batch, which is acceptable
    because an interrupted run is not analysable *past that point* — the prefix still is
    (NFR-13). Bundle export switches to FULL."""

    def with_overrides(self, **kwargs: object) -> StoreConfig:
        """Return a copy with the non-None keyword arguments applied.

        Guarantees: a None value never overrides; that is what makes "argument" a distinct,
        lower-priority layer than an explicitly-set value in the §8.7 chain.

        Raises:
            ConfigError: a keyword names a field this section does not have.
        """
        known = StoreConfig.__slots__
        applied: dict[str, object] = {}
        for key, value in sorted(kwargs.items()):
            if key not in known:
                detail = f"unknown [store] setting {key!r}; known: {sorted(known)}"
                raise ConfigError(detail)
            if value is not None:
                applied[key] = value
        return replace(self, **applied)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class AgentDXConfig:
    """The resolved configuration for one process.

    Guarantees: immutable once built, so a value read at the start of a run cannot differ
    from the same value read at the end. Later prompts add sections; the resolution order
    lives in `load` and is never re-implemented per section.
    """

    store: StoreConfig = StoreConfig()

    @classmethod
    def load(
        cls,
        *,
        config_path: Path | None = None,
        env: Mapping[str, str] | None = None,
        store: Mapping[str, object] | None = None,
    ) -> AgentDXConfig:
        """Resolve configuration through the PRD §8.7 precedence chain.

        Precedence, highest first: environment variable → `agentdx.toml` → the `store`
        argument → the dataclass default. CLI flags sit above all of these and are applied
        by `cli/` as an explicit `with_overrides` on the result, because only the CLI knows
        which flags the user actually typed.

        Args:
            config_path: Path to `agentdx.toml`. When None, the file is searched for from
                the current working directory upwards; if none is found, defaults are used.
            env: Environment mapping; defaults to `os.environ`. Injectable so the
                precedence order is testable without mutating the process environment.
            store: Per-call `[store]` overrides — the "argument" layer.

        Returns:
            A fully resolved, immutable configuration.

        Raises:
            ConfigError: a file, environment or argument value could not be coerced to the
                declared type, or names a setting that does not exist.
        """
        environment = os.environ if env is None else env
        file_values = _read_toml_section(_resolve_config_path(config_path), "store")

        resolved = StoreConfig().with_overrides(**dict(store or {}))
        resolved = resolved.with_overrides(**file_values)
        resolved = resolved.with_overrides(**_read_env_section(environment, "store", resolved))
        return cls(store=_coerce_store(resolved))


# ---------------------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------------------


def _resolve_config_path(explicit: Path | None) -> Path | None:
    """Return the `agentdx.toml` to read, or None when there is none.

    Guarantees: an explicit path that does not exist is an error rather than a silent
    fallback to defaults — a user who names a config file expects it to be read.

    Raises:
        ConfigError: `explicit` was given and does not exist.
    """
    if explicit is not None:
        if not explicit.is_file():
            detail = f"config file {explicit} does not exist"
            raise ConfigError(detail)
        return explicit
    here = Path.cwd().resolve()
    for directory in (here, *here.parents):
        candidate = directory / DEFAULT_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _read_toml_section(path: Path | None, section: str) -> dict[str, object]:
    """Return one table of a TOML file as plain objects, or an empty mapping.

    Guarantees: never returns a nested `Any`-typed structure to callers; values are handed
    back as `object` and narrowed by the coercion helpers, so `mypy --strict` sees a real
    type at every use site.

    Raises:
        ConfigError: the file is not valid TOML, or the named key is not a table.
    """
    if path is None:
        return {}
    try:
        parsed: object = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        detail = f"{path} is not valid TOML: {exc}"
        raise ConfigError(detail) from exc
    if not isinstance(parsed, Mapping):
        detail = f"{path} does not contain a table at the top level"
        raise ConfigError(detail)
    table = parsed.get(section)
    if table is None:
        return {}
    if not isinstance(table, Mapping):
        detail = f"[{section}] in {path} is not a table"
        raise ConfigError(detail)
    return {str(k): v for k, v in table.items()}


def _read_env_section(
    env: Mapping[str, str], section: str, current: StoreConfig
) -> dict[str, object]:
    """Return the `AGENTDX_<SECTION>_<KEY>` overrides present in `env`.

    Guarantees: only names matching a declared field are consulted, and a variable naming a
    field that does not exist is reported rather than ignored — a typo in
    `AGENTDX_STORE_DUCKDB_THRESHOLD_EVENT` must not look like success.

    Raises:
        ConfigError: an `AGENTDX_<SECTION>_*` variable names an unknown setting.
    """
    prefix = f"{ENV_PREFIX}{section.upper()}_"
    out: dict[str, object] = {}
    known = type(current).__slots__
    for name in sorted(env):
        if not name.startswith(prefix):
            continue
        key = name[len(prefix) :].lower()
        if key not in known:
            detail = (
                f"environment variable {name} names an unknown [{section}] setting "
                f"{key!r}; known: {sorted(known)}"
            )
            raise ConfigError(detail)
        out[key] = env[name]
    return out


def _coerce_store(raw: StoreConfig) -> StoreConfig:
    """Return `raw` with every field coerced to its declared type and range-checked.

    Guarantees: after this call every field holds the declared type, so no consumer needs
    to defend against a string that arrived from an environment variable. Range checks are
    here rather than at the use site because a threshold of 0 or -1 fails in a way that
    looks like a store bug rather than a configuration mistake.

    Raises:
        ConfigError: a value could not be coerced, or is outside its permitted range.
    """
    return StoreConfig(
        data_dir=Path(str(raw.data_dir)).expanduser(),
        duckdb_threshold_events=_positive(
            _as_int(raw.duckdb_threshold_events, "duckdb_threshold_events"),
            "duckdb_threshold_events",
        ),
        snapshot_interval_events=_positive(
            _as_int(raw.snapshot_interval_events, "snapshot_interval_events"),
            "snapshot_interval_events",
        ),
        append_batch_size=_positive(
            _as_int(raw.append_batch_size, "append_batch_size"), "append_batch_size"
        ),
        synchronous=_as_synchronous(raw.synchronous),
    )


def _as_int(value: object, key: str) -> int:
    """Return `value` as an int, accepting the string form an env var necessarily has.

    Raises:
        ConfigError: the value is not an integer and is not a string spelling one. `bool`
            is rejected explicitly, since `True` would otherwise silently become 1.
    """
    if isinstance(value, bool):
        detail = f"[store] {key} must be an integer, got a boolean"
        raise ConfigError(detail)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            detail = f"[store] {key} must be an integer, got {value!r}"
            raise ConfigError(detail) from exc
    detail = f"[store] {key} must be an integer, got {type(value).__name__}"
    raise ConfigError(detail)


def _positive(value: int, key: str) -> int:
    """Return `value` unchanged if it is >= 1.

    Raises:
        ConfigError: the value is zero or negative.
    """
    if value < 1:
        detail = f"[store] {key} must be >= 1, got {value}"
        raise ConfigError(detail)
    return value


def _as_synchronous(value: object) -> str:
    """Return the SQLite `synchronous` pragma level, upper-cased and checked.

    Guarantees: the returned string is one SQLite accepts, so it can be interpolated into a
    PRAGMA statement — which cannot be parameterised — without becoming an injection point.

    Raises:
        ConfigError: the value is not one of OFF, NORMAL, FULL, EXTRA.
    """
    permitted = ("EXTRA", "FULL", "NORMAL", "OFF")
    text = str(value).strip().upper()
    if text not in permitted:
        detail = f"[store] synchronous must be one of {list(permitted)}, got {value!r}"
        raise ConfigError(detail)
    return text


__all__ = [
    "DEFAULT_CONFIG_FILENAME",
    "ENV_PREFIX",
    "AgentDXConfig",
    "ConfigError",
    "StoreConfig",
]
