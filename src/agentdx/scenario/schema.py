"""The v1 scenario schema: constants, enums, the fault catalogue, and the resolved dataclasses.

Everything here is data, not behaviour — `validate.py` walks a parsed document against these
registries, `loader.py` merges `DEFAULTS`-shaped structures declared in `loader.py` itself
(kept there, not here, since defaults are a RESOLVE-phase concern per PRD §12.4 and this
module is the schema *shape*, not the resolution pipeline). Splitting it this way means a
future prompt extending the fault catalogue (P09, the other six fault types moving from
declared-but-unexecuted to executed) touches exactly one table in exactly one file.

**PRD §21.1** is "the complete v1 schema" and is transcribed field-for-field below.
**PRD §12.2** is the fault catalogue; `FAULT_CATALOGUE` is transcribed from its Target and
Parameters rows. Both were read and cross-checked against each other before any code was
written here (P08 prompt STOP CONDITION 1): `agent_crash`'s `agent`/`at_virtual_ts`/
`recoverable` fields in §21.1's worked example match §12.2's Process-class row for the same
fault exactly, and no other §21.1 example exercises a second fault type, so no disagreement
was found — this module does not silently paper over one.

**ADR-007** (already adopted, CONTEXT.md §8: "floats are forbidden everywhere in the event
log... durations are integer milliseconds and ratios integer per-mille, project-wide") is
applied here to the two PRD §12.2 fault parameters PRD gives as bare floats: `agent_slow`'s
`factor` becomes `factor_milli: int` (ADR-007's own consequence 2 names this exact
substitution as owed "when FR-4's P1 set lands") and every `probability` parameter
(`message_drop`, `message_duplicate`) becomes `probability_permille: int` (0-1000). This is
not a fresh interpretation of §12.2 — it is §12.2 read through an ADR the project already
adopted before this prompt started.

**STOP CONDITION 1's original scope, and what it missed.** The cross-check at build time
covered `agent_crash`'s fields against §21.1's worked example only — CONTEXT.md's own summary
of it dropped that scoping. An independent OP-2 audit (2026-08-16) found `tool_failure`'s
trigger vocabulary was never cross-checked at all: §12.2's column reads `always | first_n |
after_virtual_ts | probability`, but `first_n` has no case in §12.3's canonical `should_fire`
pseudocode, and the shipped code had silently substituted `AFTER_N_MESSAGES` (a Transport-class
concept) for both missing/ambiguous entries instead of the much closer match, `AT_VIRTUAL_TS`.
Fixed under **C-14** (CONTEXT.md §10) in the P08 OP-3 repair pass; see `FAULT_CATALOGUE
["tool_failure"]` below.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

SCHEMA_VERSION: Final[int] = 1

# ---------------------------------------------------------------------------------------
# Enums (PRD §21.1)
# ---------------------------------------------------------------------------------------


class Mode(StrEnum):
    """`mode:` — PRD §21.1. `replay` is the default (I7: offline by default)."""

    REPLAY = "replay"
    RECORD = "record"
    PERTURB = "perturb"
    PASSTHROUGH = "passthrough"


class SuccessCheckType(StrEnum):
    """`success_check.type:` — PRD §21.6."""

    PYTHON = "python"
    SHELL = "shell"
    NONE = "none"


class Severity(StrEnum):
    """Finding severity — PRD §18.3. Ordered `INFO < LOW < MEDIUM < HIGH < CRITICAL`."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_ORDER: Final[dict[Severity, int]] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class ComparabilityGrade(StrEnum):
    """Baseline comparability grade — PRD §17.5. `A` best, `C` worst."""

    A = "A"
    B = "B"
    C = "C"


_GRADE_ORDER: Final[dict[ComparabilityGrade, int]] = {
    ComparabilityGrade.C: 0,
    ComparabilityGrade.B: 1,
    ComparabilityGrade.A: 2,
}


def grade_at_least(grade: ComparabilityGrade, minimum: ComparabilityGrade) -> bool:
    """Return True if `grade` is at least as good as `minimum` (PRD §17.5 ordering: A > B > C)."""
    return _GRADE_ORDER[grade] >= _GRADE_ORDER[minimum]


# ---------------------------------------------------------------------------------------
# Fault catalogue (PRD §12.2), field names cross-checked against §21.1's worked example
# ---------------------------------------------------------------------------------------


class FaultTier(StrEnum):
    """MVP fault set (P0, CONTEXT.md §3 locked decisions) vs. the six P1 fault types.

    Scenario YAML represents the complete v1 catalogue regardless of tier — a scenario
    author may *declare* a P1 fault today even though `runtime/faults/` (P09) does not
    execute it yet; that is a P09 concern (OUT OF SCOPE for this module), not a P08 one.
    """

    P0 = "P0"
    P1 = "P1"


class TargetKind(StrEnum):
    """What a fault's `Target` row in PRD §12.2 names: what kind of graph element it hits."""

    AGENT = "agent"
    EDGE = "edge"
    TOOL = "tool"
    STATE_KEY = "state_key"
    PROVIDER = "provider"


class TriggerKind(StrEnum):
    """PRD §12.3's `should_fire` match arms — the trigger vocabulary every fault shares."""

    AT_VIRTUAL_TS = "at_virtual_ts"
    AT_SPAN_N = "at_span_n"
    AFTER_N_MESSAGES = "after_n_messages"
    ON_STATE_WRITE = "on_state_write"
    PROBABILITY = "probability"
    ALWAYS = "always"


@dataclass(frozen=True, slots=True)
class ParamConstraint:
    """One fault parameter's (or trigger field's) accepted shape, enforced by `validate.py`.

    OP-3 repair (2026-08-16, following the P08 OP-2 FAIL): before this, `_check_faults` only
    checked that a parameter's *name* was recognised, never its *value* — `at_virtual_ts:
    "not-a-timestamp"` and `window: 999999` both validated with zero errors. This is the
    per-value counterpart to `FaultSpec.params`' key-presence check.

    `py_type`: the accepted Python type once YAML-parsed. `bool` is checked before `int`
    matters here — `isinstance(True, int)` is `True` in Python, so a constraint of `int` must
    explicitly reject `bool` values (`validate.py` does this, not this dataclass, since the
    check is the same for every `int` constraint).
    `minimum`/`maximum`: inclusive bounds for `int`/`float`; `None` means unbounded that side.
    Sourced from PRD §12.2's "Safety" row where one exists (`message_reorder.window <= 16`,
    `message_duplicate.copies <= 5`, `agent_slow.factor <= 100` i.e. `factor_milli <=
    100_000` under ADR-007) — this module has no authority to invent a bound the PRD does not
    give, so a parameter with no stated bound gets `minimum`/`maximum` of `None`, not a
    guessed number.
    `choices`: a closed string enum (e.g. `tool_failure.mode`); `None` means unconstrained
    beyond `py_type`.
    """

    py_type: type
    minimum: float | None = None
    maximum: float | None = None
    choices: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class FaultSpec:
    """One row of PRD §12.2's fault catalogue table, as data.

    `target_kinds`: more than one entry means the fault may target either kind (`latency`
    targets "edge or agent" per §12.2's Transport-class table). `params`: the fault-specific
    keys from §12.2's Parameters row, ADR-007-adjusted where §12.2 names a float.
    `param_constraints`: the type/bound for each key in `params` that PRD §12.2 gives enough
    information to check (added in the P08 OP-3 repair; a key present in `params` but absent
    from `param_constraints` is a declared gap, not a silent one — see that key's fault type
    for why, e.g. `agent_crash.restart_after_ms` has no PRD-given bound to enforce).
    """

    tier: FaultTier
    target_kinds: tuple[TargetKind, ...]
    trigger_kinds: tuple[TriggerKind, ...]
    params: frozenset[str]
    param_constraints: dict[str, ParamConstraint] = field(default_factory=dict)


FAULT_CATALOGUE: Final[dict[str, FaultSpec]] = {
    # --- Transport class (PRD §12.2) ---
    "latency": FaultSpec(
        tier=FaultTier.P0,
        target_kinds=(TargetKind.EDGE, TargetKind.AGENT),
        trigger_kinds=(TriggerKind.AT_VIRTUAL_TS, TriggerKind.AFTER_N_MESSAGES, TriggerKind.ALWAYS),
        params=frozenset({"delay_ms", "jitter_ms", "pattern"}),
        param_constraints={
            "delay_ms": ParamConstraint(py_type=int, minimum=0),
            "jitter_ms": ParamConstraint(py_type=int, minimum=0),
            "pattern": ParamConstraint(
                py_type=str, choices=frozenset({"constant", "spike", "degrade"})
            ),
        },
    ),
    "message_drop": FaultSpec(
        tier=FaultTier.P0,
        target_kinds=(TargetKind.EDGE,),
        trigger_kinds=(TriggerKind.AT_VIRTUAL_TS, TriggerKind.AFTER_N_MESSAGES, TriggerKind.ALWAYS),
        params=frozenset({"probability_permille"}),  # ADR-007: probability -> permille int
        param_constraints={
            "probability_permille": ParamConstraint(py_type=int, minimum=0, maximum=1000),
        },
    ),
    "message_reorder": FaultSpec(
        tier=FaultTier.P1,
        target_kinds=(TargetKind.EDGE,),
        trigger_kinds=(TriggerKind.AT_VIRTUAL_TS, TriggerKind.AFTER_N_MESSAGES, TriggerKind.ALWAYS),
        params=frozenset({"window"}),
        # PRD §12.2 Safety row: "Window <= 16".
        param_constraints={"window": ParamConstraint(py_type=int, minimum=1, maximum=16)},
    ),
    "message_duplicate": FaultSpec(
        tier=FaultTier.P1,
        target_kinds=(TargetKind.EDGE,),
        trigger_kinds=(TriggerKind.AT_VIRTUAL_TS, TriggerKind.AFTER_N_MESSAGES, TriggerKind.ALWAYS),
        params=frozenset({"probability_permille", "copies"}),
        # PRD §12.2 Safety row: "copies <= 5".
        param_constraints={
            "probability_permille": ParamConstraint(py_type=int, minimum=0, maximum=1000),
            "copies": ParamConstraint(py_type=int, minimum=1, maximum=5),
        },
    ),
    # --- Process class (PRD §12.2) ---
    "agent_crash": FaultSpec(
        tier=FaultTier.P0,
        target_kinds=(TargetKind.AGENT,),
        trigger_kinds=(
            TriggerKind.AT_VIRTUAL_TS,
            TriggerKind.AT_SPAN_N,
            TriggerKind.ON_STATE_WRITE,
        ),
        params=frozenset({"recoverable", "restart_after_ms", "allow_total_failure"}),
        param_constraints={
            "recoverable": ParamConstraint(py_type=bool),
            "restart_after_ms": ParamConstraint(py_type=int, minimum=0),
            "allow_total_failure": ParamConstraint(py_type=bool),
            # PRD §12.2 Safety row ("cannot crash the last live agent unless
            # allow_total_failure: true") is NOT enforced here: it depends on the graph's
            # live-agent count at fire time, which this static, pre-execution validator
            # cannot know (that is `runtime/faults/`'s (P09) concern, not this module's —
            # declared, not a silent gap).
        },
    ),
    "agent_slow": FaultSpec(
        tier=FaultTier.P1,
        target_kinds=(TargetKind.AGENT,),
        trigger_kinds=(TriggerKind.AT_VIRTUAL_TS, TriggerKind.ALWAYS),
        params=frozenset({"factor_milli"}),  # ADR-007 consequence 2: factor -> factor_milli
        # PRD §12.2: "factor (float >= 1.0)", Safety row "factor <= 100" -> under ADR-007,
        # factor_milli in [1000, 100_000].
        param_constraints={
            "factor_milli": ParamConstraint(py_type=int, minimum=1000, maximum=100_000)
        },
    ),
    # --- Dependency class (PRD §12.2) ---
    "tool_failure": FaultSpec(
        tier=FaultTier.P0,
        target_kinds=(TargetKind.TOOL,),
        # OP-3 repair (2026-08-16, C-14): PRD §12.2's own trigger column for `tool_failure`
        # reads "always | first_n | after_virtual_ts | probability", but `first_n` has no
        # corresponding case in §12.3's canonical `should_fire` pseudocode at all, and the
        # code previously substituted `AFTER_N_MESSAGES` (a Transport-class, edge-message
        # concept with no clear meaning for a tool-targeted fault) for BOTH `first_n` and
        # `after_virtual_ts` without ever being cross-checked against §12.3 or logged as a
        # ruling. `AT_VIRTUAL_TS` is the exact, unambiguous match for `after_virtual_ts` and
        # already exists as a `TriggerKind` member; `first_n` has no clean §12.3 equivalent
        # and is dropped rather than guessed at (see C-14, CONTEXT.md §10).
        trigger_kinds=(TriggerKind.ALWAYS, TriggerKind.AT_VIRTUAL_TS, TriggerKind.PROBABILITY),
        params=frozenset({"mode", "count"}),
        param_constraints={
            "mode": ParamConstraint(
                py_type=str, choices=frozenset({"timeout", "429", "500", "malformed"})
            ),
            "count": ParamConstraint(py_type=int, minimum=1),
        },
    ),
    "rate_limit": FaultSpec(
        tier=FaultTier.P1,
        target_kinds=(TargetKind.PROVIDER,),
        trigger_kinds=(TriggerKind.ALWAYS,),
        params=frozenset({"rps_cap", "retry_after_ms"}),
        param_constraints={
            "rps_cap": ParamConstraint(py_type=int, minimum=1),
            "retry_after_ms": ParamConstraint(py_type=int, minimum=0),
        },
    ),
    # --- Semantic and State classes (PRD §12.2) ---
    "byzantine": FaultSpec(
        tier=FaultTier.P1,
        target_kinds=(TargetKind.AGENT,),
        trigger_kinds=(TriggerKind.AT_SPAN_N, TriggerKind.ALWAYS),
        params=frozenset({"mode", "pool"}),
        param_constraints={
            "mode": ParamConstraint(
                py_type=str,
                choices=frozenset({"stale_output", "contradictory", "confident_wrong"}),
            ),
            # `pool` names a declared response pool (PRD §12.2 Safety: "only from a declared
            # pool; never generated by a model") — a string identifier here, not the pool's
            # contents; this module has no registry of declared pools to check membership
            # against (that is a `runtime/faults/` (P09) concern), so only the type is
            # checked, not which pool.
            "pool": ParamConstraint(py_type=str),
        },
    ),
    "state_corrupt": FaultSpec(
        tier=FaultTier.P1,
        target_kinds=(TargetKind.STATE_KEY,),
        trigger_kinds=(TriggerKind.AT_VIRTUAL_TS, TriggerKind.ON_STATE_WRITE),
        params=frozenset({"mutation"}),
        param_constraints={
            "mutation": ParamConstraint(
                py_type=str,
                choices=frozenset({"drop", "truncate", "swap", "stale", "type_change"}),
            ),
        },
    ),
}

# Trigger *field values* (not just their presence) — PRD §12.3's `should_fire` cases give each
# a clear type; added in the P08 OP-3 repair alongside `FaultSpec.param_constraints` since the
# same gap applied to trigger fields (`at_virtual_ts: "not-a-timestamp"` validated cleanly
# before this). Keyed by `TriggerKind`, applied by `validate.py` via `TRIGGER_FIELD`.
TRIGGER_FIELD_CONSTRAINTS: Final[dict[TriggerKind, ParamConstraint]] = {
    TriggerKind.AT_VIRTUAL_TS: ParamConstraint(py_type=int, minimum=0),
    TriggerKind.AT_SPAN_N: ParamConstraint(py_type=int, minimum=0),
    TriggerKind.AFTER_N_MESSAGES: ParamConstraint(py_type=int, minimum=1),
    TriggerKind.ON_STATE_WRITE: ParamConstraint(py_type=str),
    TriggerKind.PROBABILITY: ParamConstraint(py_type=int, minimum=0, maximum=1000),
    TriggerKind.ALWAYS: ParamConstraint(py_type=bool),
}

# The field name on a fault entry that carries each TargetKind's value, e.g. `agent: reviewer`.
TARGET_FIELD: Final[dict[TargetKind, str]] = {
    TargetKind.AGENT: "agent",
    TargetKind.EDGE: "edge",
    TargetKind.TOOL: "tool",
    TargetKind.STATE_KEY: "state_key",
    TargetKind.PROVIDER: "provider",
}

# The field name on a fault entry that carries each TriggerKind's value.
TRIGGER_FIELD: Final[dict[TriggerKind, str]] = {
    TriggerKind.AT_VIRTUAL_TS: "at_virtual_ts",
    TriggerKind.AT_SPAN_N: "at_span_n",
    TriggerKind.AFTER_N_MESSAGES: "after_n_messages",
    TriggerKind.ON_STATE_WRITE: "on_state_write",
    TriggerKind.PROBABILITY: "probability_permille",
    TriggerKind.ALWAYS: "always",
}

FAULT_COMMON_KEYS: Final[frozenset[str]] = frozenset(
    {"type", *TARGET_FIELD.values(), *TRIGGER_FIELD.values()}
)

# ---------------------------------------------------------------------------------------
# Known keys per section (drives E-SCEN-002, "unknown top-level or nested key")
# ---------------------------------------------------------------------------------------

TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "version",
        "scenario",
        "description",
        "extends",
        "matrix",
        "target",
        "task",
        "seed",
        "mode",
        "repeats",
        "chaos_opt_in",
        "hypothesis",
        "blast_radius",
        "faults",
        "guards",
        "baseline",
        "exploration",
        "success_check",
        "assertions",
    }
)

TARGET_KEYS: Final[frozenset[str]] = frozenset({"fixture", "graph"})
HYPOTHESIS_KEYS: Final[frozenset[str]] = frozenset(
    {"task_success", "p95_virtual_duration_ms", "max_token_spend"}
)
BLAST_RADIUS_KEYS: Final[frozenset[str]] = frozenset(
    {"agents", "tools", "edges", "state_keys", "providers"}
)
GUARD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "max_virtual_duration_ms",
        "max_tokens",
        "max_retries",
        "max_wall_duration_s",
        "max_events",
        "max_llm_calls",
    }
)
BASELINE_KEYS: Final[frozenset[str]] = frozenset({"generate", "prompt", "allow_low_comparability"})
EXPLORATION_KEYS: Final[frozenset[str]] = frozenset({"enabled", "k", "max_schedules"})
SUCCESS_CHECK_KEYS: Final[frozenset[str]] = frozenset({"type", "ref"})

MAX_FINDINGS_KEYS: Final[frozenset[str]] = frozenset({"severity", "count"})
CRITICAL_PATH_SHARE_KEYS: Final[frozenset[str]] = frozenset({"edge", "cmp"})
"""`critical_path_share`'s shape (PRD §21.7 gives it in prose shorthand, `{edge, "<= 0.4"}`,
not literal YAML); this module formalises it as `{edge: <name>, cmp: "<op> <v>"}` for the
same reason every other comparison-bearing assertion carries its operator as a string — see
`docs/scenario-reference.md` for the worked example.
"""

BUILT_IN_ASSERTION_NAMES: Final[frozenset[str]] = frozenset(
    {
        "no_state_conflicts",
        "speedup_vs_baseline",
        "resilience_score",
        "max_findings",
        "critical_path_share",
        "token_cost_multiplier",
        "no_silent_failures",
        "task_success",
        "deterministic_replay",
    }
)
"""PRD §21.7's built-in assertion table, transcribed."""

# ---------------------------------------------------------------------------------------
# PRD §21.4 defaults (values only; `loader.DEFAULTS` assembles the full shape)
# ---------------------------------------------------------------------------------------

DEFAULT_MODE: Final[str] = Mode.REPLAY.value
DEFAULT_SEED: Final[int] = 0
DEFAULT_REPEATS: Final[int] = 1
DEFAULT_CHAOS_OPT_IN: Final[bool] = False
DEFAULT_BASELINE_GENERATE: Final[bool] = True
"""PRD §21.4: "true for graphs with >= 2 agents". Whether a *specific* target has >= 2
agents is a graph-identity question `loader.resolve_defaults` cannot answer without doing
the same static introspection `validate.py` does for E-SCEN-005 — so the single-function
defaulting pass (Design Constraint 4) applies the unconditional `True` PRD §21.1's own
worked example shows (`baseline: {generate: true}` is absent there, i.e. defaulted True for
a 4-agent fixture), and single-agent-graph refinement is deferred to whichever module
actually runs the baseline (PRD §17's baseline generator, P11, NOT STARTED) — declared, not
silently narrowed here.
"""
DEFAULT_SUCCESS_CHECK_TYPE: Final[str] = SuccessCheckType.NONE.value

# PRD §13.6 abort-guard defaults, `[SOURCE]`. Guard *ceilings* (E-SCEN-008) are a separate,
# P08-introduced safety concept the PRD does not give literal numbers for; per AGENTS.md §4
# ("no magic numbers... thresholds live in config") they are read from `agentdx.toml`'s new
# `[scenario]` section by `validate.py`, not hardcoded here — see that module's docstring
# for why `agentdx.toml` gained a new section rather than `config.py` gaining new plumbing.
DEFAULT_GUARDS: Final[dict[str, int]] = {
    "max_virtual_duration_ms": 120_000,
    "max_tokens": 200_000,
    "max_retries": 20,
    "max_wall_duration_s": 300,
    "max_events": 500_000,
    "max_llm_calls": 500,
}


# ---------------------------------------------------------------------------------------
# Comparison expressions — `hypothesis`/`assertions` values like `">= 0.9"` (PRD §21.1)
# ---------------------------------------------------------------------------------------

_COMPARISON_RE: Final = re.compile(r"^\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")

_OPS: Final[dict[str, Callable[[float, float], bool]]] = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


@dataclass(frozen=True, slots=True)
class Comparison:
    """One parsed `"<op> <value>"` expression, e.g. `">= 0.9"` -> `Comparison(">=", 0.9)`."""

    op: str
    value: float

    def evaluate(self, actual: float) -> bool:
        """Return whether `actual <op> value` holds."""
        return _OPS[self.op](actual, self.value)

    def __str__(self) -> str:
        """Render back to the PRD §21.1 surface form, e.g. `>= 0.9`."""
        rendered = f"{self.value:g}"
        return f"{self.op} {rendered}"


def parse_comparison(expr: str) -> Comparison | None:
    """Parse a `"<op> <value>"` string. Returns None if `expr` does not match that shape."""
    match = _COMPARISON_RE.match(expr)
    if match is None:
        return None
    return Comparison(op=match.group(1), value=float(match.group(2)))


__all__ = [
    "BASELINE_KEYS",
    "BLAST_RADIUS_KEYS",
    "BUILT_IN_ASSERTION_NAMES",
    "CRITICAL_PATH_SHARE_KEYS",
    "DEFAULT_BASELINE_GENERATE",
    "DEFAULT_CHAOS_OPT_IN",
    "DEFAULT_GUARDS",
    "DEFAULT_MODE",
    "DEFAULT_REPEATS",
    "DEFAULT_SEED",
    "DEFAULT_SUCCESS_CHECK_TYPE",
    "EXPLORATION_KEYS",
    "FAULT_CATALOGUE",
    "FAULT_COMMON_KEYS",
    "GUARD_KEYS",
    "HYPOTHESIS_KEYS",
    "MAX_FINDINGS_KEYS",
    "SCHEMA_VERSION",
    "SEVERITY_ORDER",
    "SUCCESS_CHECK_KEYS",
    "TARGET_FIELD",
    "TARGET_KEYS",
    "TOP_LEVEL_KEYS",
    "TRIGGER_FIELD",
    "TRIGGER_FIELD_CONSTRAINTS",
    "ComparabilityGrade",
    "Comparison",
    "FaultSpec",
    "FaultTier",
    "Mode",
    "ParamConstraint",
    "Severity",
    "SuccessCheckType",
    "TargetKind",
    "TriggerKind",
    "grade_at_least",
    "parse_comparison",
]
