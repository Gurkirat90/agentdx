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

P04 added `[run]`, `[privacy]` and `[llm]` — the three tables PRD §8.7 requires the
instrumentation SDK to read. `[scheduler]` and `[analysis]` are deliberately still absent:
they belong to P06 and P10, and a section declared before its consumer exists is a
threshold nobody is enforcing. Unknown tables in `agentdx.toml` are ignored, so the file can
carry them ahead of their prompt (and it does).

Determinism note: this module reads the environment and the filesystem. Neither is a source
of non-determinism *inside a run* — configuration is resolved before a run starts and is
recorded in `run_start` (PRD §10.10), so a replay pins it. Nothing here reads a clock.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
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
        return _apply(self, "store", kwargs)


CACHE_MODES: Final = ("record", "replay", "perturb", "passthrough")
"""The four LLM-cache modes of PRD §11.2, in the table's order.

`replay` is the default (CONTEXT.md §3, invariant I7). `passthrough` exists only for the
NFR-1 overhead benchmark and is the one mode that is permitted to contact a provider on
every call; there is deliberately no "fall back to live" mode, because a silent fallback
would make CI non-hermetic, bundles unreproducible and cost unpredictable at once.
"""


@dataclass(frozen=True, slots=True)
class RunConfig:
    """The `[run]` table of PRD §8.7 — what a run is, before any scenario refines it.

    Guarantees: `mode` is always one of `CACHE_MODES`, so a consumer may branch on it
    without a fallback arm. `seed` is the single source of the run's randomness (I1).
    """

    seed: int = 42
    mode: str = "replay"
    data_dir: Path = Path("~/.agentdx")

    def with_overrides(self, **kwargs: object) -> RunConfig:
        """Return a copy with the non-None keyword arguments applied.

        Raises:
            ConfigError: a keyword names a field this section does not have.
        """
        return _apply(self, "run", kwargs)


@dataclass(frozen=True, slots=True)
class PrivacyConfig:
    """The `[privacy]` table of PRD §8.7 — invariant I8 / NFR-6, in configuration form.

    Guarantees: `capture_bodies` defaults to False and nothing in the SDK may default it
    any other way. Every pattern in `redact_patterns` has been compiled successfully by the
    time this object exists, so a redactor built from it cannot fail at emission time —
    a redaction that throws while an error is being recorded would lose the error.
    """

    capture_bodies: bool = False
    """PRD §8.11 / NFR-6 / I8. False means the *event log* carries `prompt_hash` and
    `response_hash` and never the bodies. The LLM cache still holds bodies — replay is
    impossible otherwise — but the cache is a separate file that bundles exclude by
    default (PRD §31.3)."""

    redact_patterns: tuple[str, ...] = (
        "sk-[A-Za-z0-9]{20,}",
        "AKIA[0-9A-Z]{16}",
    )
    """Applied to every string the SDK is about to write: error messages (always, per PRD
    §8.9) and bodies (only under `capture_bodies=True`). Defaults are PRD §8.7's."""

    def with_overrides(self, **kwargs: object) -> PrivacyConfig:
        """Return a copy with the non-None keyword arguments applied.

        Raises:
            ConfigError: a keyword names a field this section does not have.
        """
        return _apply(self, "privacy", kwargs)


@dataclass(frozen=True, slots=True)
class LlmConfig:
    """The `[llm]` table of PRD §8.7 — the default recording configuration.

    Guarantees: names a *provider profile* and a base URL, never a vendor SDK. PRD §8.5 is
    explicit that the shim targets the OpenAI-compatible HTTP surface so that a model
    deprecation cannot break the product; Groq ships as the default (CONTEXT.md §3).
    """

    provider: str = "groq"
    model: str = "llama-3.1-8b-instant"
    base_url: str = "https://api.groq.com/openai/v1"

    def with_overrides(self, **kwargs: object) -> LlmConfig:
        """Return a copy with the non-None keyword arguments applied.

        Raises:
            ConfigError: a keyword names a field this section does not have.
        """
        return _apply(self, "llm", kwargs)


@dataclass(frozen=True, slots=True)
class AgentDXConfig:
    """The resolved configuration for one process.

    Guarantees: immutable once built, so a value read at the start of a run cannot differ
    from the same value read at the end. Later prompts add sections; the resolution order
    lives in `load` and is never re-implemented per section.
    """

    store: StoreConfig = StoreConfig()
    run: RunConfig = RunConfig()
    privacy: PrivacyConfig = PrivacyConfig()
    llm: LlmConfig = LlmConfig()

    @classmethod
    def load(
        cls,
        *,
        config_path: Path | None = None,
        env: Mapping[str, str] | None = None,
        store: Mapping[str, object] | None = None,
        run: Mapping[str, object] | None = None,
        privacy: Mapping[str, object] | None = None,
        llm: Mapping[str, object] | None = None,
    ) -> AgentDXConfig:
        """Resolve configuration through the PRD §8.7 precedence chain.

        Precedence, highest first: environment variable → `agentdx.toml` → the per-section
        argument → the dataclass default. CLI flags sit above all of these and are applied
        by `cli/` as an explicit `with_overrides` on the result, because only the CLI knows
        which flags the user actually typed.

        Args:
            config_path: Path to `agentdx.toml`. When None, the file is searched for from
                the current working directory upwards; if none is found, defaults are used.
            env: Environment mapping; defaults to `os.environ`. Injectable so the
                precedence order is testable without mutating the process environment.
            store: Per-call `[store]` overrides — the "argument" layer.
            run: Per-call `[run]` overrides.
            privacy: Per-call `[privacy]` overrides.
            llm: Per-call `[llm]` overrides.

        Returns:
            A fully resolved, immutable configuration.

        Raises:
            ConfigError: a file, environment or argument value could not be coerced to the
                declared type, or names a setting that does not exist.
        """
        environment = os.environ if env is None else env
        path = _resolve_config_path(config_path)
        return cls(
            store=_coerce_store(_resolve(StoreConfig(), "store", path, environment, store)),
            run=_coerce_run(_resolve(RunConfig(), "run", path, environment, run)),
            privacy=_coerce_privacy(
                _resolve(PrivacyConfig(), "privacy", path, environment, privacy)
            ),
            llm=_coerce_llm(_resolve(LlmConfig(), "llm", path, environment, llm)),
        )


# ---------------------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------------------

_Section = TypeVar("_Section", StoreConfig, RunConfig, PrivacyConfig, LlmConfig)


def _slots_of(section: _Section) -> tuple[str, ...]:  # noqa: UP047  # D-08
    """Return the declared field names of a section dataclass.

    Guarantees: derived from the dataclass itself, so the set of settable keys cannot drift
    from the set of declared fields — the drift that lets a renamed setting keep silently
    accepting its old name.
    """
    return tuple(f.name for f in fields(section))


def _apply(  # noqa: UP047  # D-08: PEP 695 type parameters need a 3.12 toolchain
    section: _Section, name: str, values: Mapping[str, object]
) -> _Section:
    """Return `section` with the non-None entries of `values` applied.

    Guarantees: a None value never overrides, which is what makes "argument" a distinct,
    lower-priority layer than an explicitly-set value in the PRD §8.7 chain. Keys are
    applied in sorted order so an error names the alphabetically first unknown key on every
    platform.

    Raises:
        ConfigError: a key names a field this section does not have.
    """
    known = _slots_of(section)
    applied: dict[str, object] = {}
    for key, value in sorted(values.items()):
        if key not in known:
            detail = f"unknown [{name}] setting {key!r}; known: {sorted(known)}"
            raise ConfigError(detail)
        if value is not None:
            applied[key] = value
    return replace(section, **applied)  # type: ignore[arg-type]


def _resolve(  # noqa: UP047  # D-08
    default: _Section,
    name: str,
    path: Path | None,
    env: Mapping[str, str],
    argument: Mapping[str, object] | None,
) -> _Section:
    """Apply the PRD §8.7 layers to one section, lowest priority first.

    Guarantees: the order here is the *only* statement of precedence in the codebase —
    argument, then file, then environment, each overwriting the last, so the highest
    priority is applied last. A section added by a later prompt inherits it by calling this
    function rather than by re-implementing it.

    Raises:
        ConfigError: any layer names a setting the section does not declare.
    """
    resolved = _apply(default, name, argument or {})
    resolved = _apply(resolved, name, _read_toml_section(path, name))
    return _apply(resolved, name, _read_env_section(env, name, _slots_of(resolved)))


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
    env: Mapping[str, str], section: str, known: tuple[str, ...]
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


def _coerce_run(raw: RunConfig) -> RunConfig:
    """Return `raw` with every `[run]` field coerced to its declared type and checked.

    Guarantees: `mode` is one of `CACHE_MODES` on return, so no consumer needs a fallback
    arm for an unknown mode — and a misspelt `--mode replya` stops the run instead of
    silently selecting the default, which would be `replay` and would look like it worked.

    Raises:
        ConfigError: a value could not be coerced, or `mode` is not a known cache mode.
    """
    return RunConfig(
        seed=_non_negative(_as_int(raw.seed, "seed", "run"), "seed", "run"),
        mode=_as_choice(raw.mode, "mode", "run", CACHE_MODES),
        data_dir=Path(str(raw.data_dir)).expanduser(),
    )


def _coerce_privacy(raw: PrivacyConfig) -> PrivacyConfig:
    """Return `raw` with every `[privacy]` field coerced, checked and compiled.

    Guarantees: every returned pattern compiles as a regular expression. Validating here
    rather than at the redaction site is deliberate — redaction runs while an error is being
    recorded, and a redactor that raised there would lose the error it was protecting.

    Raises:
        ConfigError: `capture_bodies` is not a boolean, or a redaction pattern is not a
            string or does not compile.
    """
    return PrivacyConfig(
        capture_bodies=_as_bool(raw.capture_bodies, "capture_bodies", "privacy"),
        redact_patterns=_as_patterns(raw.redact_patterns),
    )


def _coerce_llm(raw: LlmConfig) -> LlmConfig:
    """Return `raw` with every `[llm]` field coerced to a non-empty string.

    Guarantees: no field is the empty string, because an empty `base_url` produces a
    request to a relative URL and an empty `model` produces a cache key for a model nobody
    named — both fail far from the mistake.

    Raises:
        ConfigError: a value is not a string, or is empty after stripping.
    """
    return LlmConfig(
        provider=_as_text(raw.provider, "provider", "llm"),
        model=_as_text(raw.model, "model", "llm"),
        base_url=_as_text(raw.base_url, "base_url", "llm").rstrip("/"),
    )


def _as_int(value: object, key: str, section: str = "store") -> int:
    """Return `value` as an int, accepting the string form an env var necessarily has.

    Raises:
        ConfigError: the value is not an integer and is not a string spelling one. `bool`
            is rejected explicitly, since `True` would otherwise silently become 1.
    """
    if isinstance(value, bool):
        detail = f"[{section}] {key} must be an integer, got a boolean"
        raise ConfigError(detail)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            detail = f"[{section}] {key} must be an integer, got {value!r}"
            raise ConfigError(detail) from exc
    detail = f"[{section}] {key} must be an integer, got {type(value).__name__}"
    raise ConfigError(detail)


def _as_bool(value: object, key: str, section: str) -> bool:
    """Return `value` as a bool, accepting the spellings an env var can carry.

    Guarantees: only `true/false/1/0/yes/no/on/off` (any case) are accepted. A value like
    `"False"` must not become True, which is what a bare `bool(str)` would do and is the
    single most damaging coercion bug possible for `capture_bodies` (I8).

    Raises:
        ConfigError: the value is not a boolean and is not a recognised spelling of one.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
    detail = (
        f"[{section}] {key} must be a boolean (true/false/1/0/yes/no/on/off), got {value!r}. "
        f"It is never coerced: a mistyped value here would silently switch a privacy default"
    )
    raise ConfigError(detail)


def _as_text(value: object, key: str, section: str) -> str:
    """Return `value` as a non-empty string.

    Raises:
        ConfigError: the value is not a string, or is empty once stripped.
    """
    if not isinstance(value, str):
        detail = f"[{section}] {key} must be a string, got {type(value).__name__}"
        raise ConfigError(detail)
    text = value.strip()
    if not text:
        detail = f"[{section}] {key} must not be empty"
        raise ConfigError(detail)
    return text


def _as_choice(value: object, key: str, section: str, permitted: tuple[str, ...]) -> str:
    """Return `value` as one of `permitted`.

    Raises:
        ConfigError: the value is not a string, or is not in `permitted`.
    """
    text = _as_text(value, key, section)
    if text not in permitted:
        detail = f"[{section}] {key} must be one of {list(permitted)}, got {value!r}"
        raise ConfigError(detail)
    return text


def _as_patterns(value: object) -> tuple[str, ...]:
    """Return `[privacy] redact_patterns` as a tuple of compilable regular expressions.

    Guarantees: accepts a TOML array or, from an environment variable, a JSON array — never
    a comma-separated string, because `sk-[A-Za-z0-9]{20,}` contains a comma and splitting
    on it would silently produce two broken patterns out of one working one.

    Raises:
        ConfigError: the value is not a list of strings, is not parseable JSON when it
            arrived as a string, or contains a pattern that does not compile.
    """
    items: Sequence[object]
    if isinstance(value, str):
        try:
            parsed: object = json.loads(value)
        except json.JSONDecodeError as exc:
            detail = (
                f"[privacy] redact_patterns must be a JSON array when set from the "
                f"environment (a pattern may contain a comma, so it is never split): {exc}"
            )
            raise ConfigError(detail) from exc
        if not isinstance(parsed, Sequence) or isinstance(parsed, str):
            detail = f"[privacy] redact_patterns must be a JSON array, got {value!r}"
            raise ConfigError(detail)
        items = parsed
    elif isinstance(value, Sequence):
        items = value
    else:
        detail = f"[privacy] redact_patterns must be a list, got {type(value).__name__}"
        raise ConfigError(detail)

    out: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            detail = f"[privacy] redact_patterns[{index}] must be a string, got {item!r}"
            raise ConfigError(detail)
        try:
            re.compile(item)
        except re.error as exc:
            detail = f"[privacy] redact_patterns[{index}] is not a valid regex: {item!r} ({exc})"
            raise ConfigError(detail) from exc
        out.append(item)
    return tuple(out)


def _non_negative(value: int, key: str, section: str) -> int:
    """Return `value` unchanged if it is >= 0.

    Raises:
        ConfigError: the value is negative.
    """
    if value < 0:
        detail = f"[{section}] {key} must be >= 0, got {value}"
        raise ConfigError(detail)
    return value


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
    "CACHE_MODES",
    "DEFAULT_CONFIG_FILENAME",
    "ENV_PREFIX",
    "AgentDXConfig",
    "ConfigError",
    "LlmConfig",
    "PrivacyConfig",
    "RunConfig",
    "StoreConfig",
]
