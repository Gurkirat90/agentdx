"""The fault registry: arms declared faults against a resolved scenario (PRD §12.1, §13.3–13.4).

**Safety before faults (mission Design Constraint 1).** This module is the gate every fault
passes through before it can ever be evaluated by `triggers.should_fire`. A fault that fails
to construct here — wrong tier, target outside the blast radius, chaos not opted into — never
becomes an `ArmedFault`, so `runtime.faults.triggers`/the per-class execution modules cannot
reach it by construction, not merely by a runtime `if` that a future edit could remove.

**Deliberate, declared duplication of `agentdx.scenario.schema` (PRD catalogue) — read this
before touching either file.** `runtime/` importing `agentdx.scenario` is not forbidden by
`.importlinter` (only the reverse direction is: `scenario/` may import nothing, per
`agentdx.scenario`'s own `scenario-is-declarative` contract), and `scenario.schema` is a pure,
side-effect-free data module (enums, frozen dataclasses, no I/O) — no different in kind from
importing `events.schema`. Re-deriving `FAULT_CATALOGUE`'s bounds here instead would recreate
the exact duplicated-source-of-truth defect this project repeatedly rules against (D-12, D-39,
D-41) for a table that P08 already cross-checked against PRD §12.2/§12.3 twice, under two
independent audits (CONTEXT.md §5 row 8). Importing it is the smaller risk.

**MVP scope (CONTEXT.md §3, locked): `latency`, `agent_crash`, `message_drop`, `tool_failure`
only.** A scenario may *declare* any PRD §12.2 fault (P08 validates all ten), but arming one
outside the MVP set raises `FaultNotImplementedError` rather than silently no-op'ing it — PRD
§12.5's own warning is the reason: "A fault that never fires must be surfaced prominently. A
chaos experiment whose fault silently did not apply produces a falsely reassuring result — the
most dangerous failure mode this subsystem has." An unimplemented fault that loaded cleanly and
then just never fired would be exactly that failure mode, at construction time instead of at
run time. See `docs/chaos-safety.md` §"MVP fault set" for the six deferred types and why each
one is deferred rather than partially guessed at.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, cast

from agentdx.events.schema import PayloadValue
from agentdx.scenario.schema import (
    BLAST_RADIUS_KEYS,
    FAULT_CATALOGUE,
    TARGET_FIELD,
    TRIGGER_FIELD,
    FaultTier,
    TargetKind,
    TriggerKind,
)

_DOCS: Final = "docs/chaos-safety.md"

MVP_FAULT_TYPES: Final[frozenset[str]] = frozenset(
    {"latency", "agent_crash", "message_drop", "tool_failure"}
)
"""CONTEXT.md §3 locked decision, transcribed. The only fault types `FaultRegistry.arm`
constructs an `ArmedFault` for; everything else in `FAULT_CATALOGUE` is P1 and rejected."""


class ChaosSafetyError(RuntimeError):
    """Base class for every error this module raises.

    Never caught and silenced — always a reason to abort or refuse to construct a registry,
    never a warning.
    """


class FaultNotImplementedError(ChaosSafetyError):
    """A scenario declared a fault type this build does not execute.

    Carries `E-CHAOS-002` (this build's own code — PRD §36 does not reserve a code for "fault
    declared but not executable"; the nearest neighbour, `E-CHAOS-001`, means something already
    specified and different — outside blast radius). Deliberately fatal at registry
    construction: PRD §12.5's warning about a silently-inert fault applies just as much to a
    fault that was never armed at all as to one that armed and never fired.
    """

    code: Final = "E-CHAOS-002"

    def __init__(self, fault_type: str, *, tier: FaultTier) -> None:
        """Build the error naming the fault type, its tier, and the MVP set that is armable."""
        super().__init__(
            f"[{self.code}] fault type {fault_type!r} is {tier.value} and not in this build's "
            f"MVP set {sorted(MVP_FAULT_TYPES)} — declared, not executed "
            f"({_DOCS}#mvp-fault-set)."
        )


class ChaosAuthorizationError(ChaosSafetyError):
    """A fault could not be armed because chaos authorisation (PRD §13.3/§13.10) failed.

    Carries `E-CHAOS-001` when the cause is a blast-radius violation (PRD §36's own text:
    "Fault `tool_failure(deploy)` outside declared blast radius"), reused here for the
    registry-construction-time check that mirrors the runtime re-check `safety.py` performs at
    fire time (PRD §13.4 point 2: "enforced at two layers"). A user-graph scenario with faults
    but no `chaos_opt_in`/blast radius should already have failed `scenario.validate`'s
    `E-SCEN-004` before reaching this module at all; this is defence-in-depth, not the primary
    gate, and should be unreachable in a correctly validated scenario.
    """

    code: Final = "E-CHAOS-001"

    def __init__(self, detail: str) -> None:
        """Build the error from a description of what authorisation check failed."""
        super().__init__(f"[{self.code}] {detail} ({_DOCS}#e-chaos-001)")


def _e_chaos_003(detail: str) -> ChaosSafetyError:
    """Build the `E-CHAOS-003` (malformed fault entry — defence-in-depth only) exception.

    Factored out so every raise site below passes a bare local name to `raise`, not an inline
    f-string (ruff `TRY003` — the project's own convention, matching `runtime.cache.*`'s
    `raise SomeError(code, detail)` two-arg shape for the same reason).
    """
    return ChaosSafetyError(f"[E-CHAOS-003] {detail} ({_DOCS}#e-chaos-003)")


@dataclass(frozen=True, slots=True)
class Trigger:
    """One fault's parsed trigger (PRD §12.3): a `TriggerKind` plus its typed value.

    `value` is `None` for `TriggerKind.ALWAYS` (no field carries a value for it — PRD §12.3's
    `case Always(): return True` takes no argument), an `int` for
    `AT_VIRTUAL_TS`/`AT_SPAN_N`/`AFTER_N_MESSAGES`/`PROBABILITY` (the last is a permille,
    0-1000, ADR-007), and a `str` for `ON_STATE_WRITE` (the state key).
    """

    kind: TriggerKind
    value: int | str | None


@dataclass(frozen=True, slots=True)
class FaultDecl:
    """One fault, as declared in a resolved scenario — immutable, PRD §12.2 catalogue-checked.

    `fault_id` is assigned by `FaultRegistry.from_resolved_scenario` in declaration order
    (`f_00`, `f_01`, ...) — deterministic because it depends only on list position, never on a
    clock or a random draw (I1).
    """

    fault_id: str
    fault_type: str
    target_kind: TargetKind
    target: str
    trigger: Trigger
    params: Mapping[str, object]


def params_payload(params: Mapping[str, object]) -> dict[str, PayloadValue]:
    """Return `params` as an `Event.payload`-safe mapping.

    Every fault-class execution module (`process.py`, `transport.py`, `dependency.py`) embeds a
    fault's `params` verbatim into its `fault_injected` payload — `events.schema.
    PAYLOAD_SCHEMAS[EventType.FAULT_INJECTED]` is exactly `{fault_id, fault_type, target,
    params, trigger}` (PRD §12.5). `FaultDecl.params` is typed `Mapping[str, object]` here to
    avoid this module depending on `events.schema` at declaration time, but every value it ever
    actually holds is one of `FAULT_CATALOGUE`'s own `ParamConstraint.py_type`s (`bool`, `int`,
    `str` today) — a `JsonScalar`, and therefore a `PayloadValue`, by construction, since
    `FaultRegistry.from_resolved_scenario` only ever copies catalogue-constrained, already-
    validated YAML values into a `FaultDecl.params`. This function is the one place that
    invariant is made visible to mypy, via an explicit, narrow `cast` (not `Any`) rather than
    three fault-class modules each carrying an unexplained `# type: ignore`.
    """
    return cast("dict[str, PayloadValue]", dict(params))


def fault_effect_payload(
    *,
    fault_id: str,
    effect: str,
    target: str,
    delay_virtual_ms: int | None = None,
    exception_type: str | None = None,
    message_id: str | None = None,
) -> dict[str, PayloadValue]:
    """Return a `fault_effect` payload matching its locked P02 schema, field-for-field.

    `events.schema.PAYLOAD_SCHEMAS[EventType.FAULT_EFFECT]` is exactly `{fault_id, effect,
    target, delay_virtual_ms, exception_type, message_id}` — no `fault_type`/`params`/
    `trigger` (those live on the fault's own `fault_injected` event; a `fault_effect` event
    joins back to it by `fault_id`) — and `effect` is a closed enum, `{"delay", "exception",
    "drop", "crash"}`, one per MVP fault type (CONTEXT.md §3). Every field is `required=True`
    in the schema (`events.schema.FieldSpec`'s own default), including the three that are
    `nullable=True` — a fault-class module that only has, say, `delay_virtual_ms` to report
    still passes `exception_type=None`/`message_id=None` explicitly rather than omitting the
    keys, because `check_structural`'s `E-EVENT-001` (missing required field) does not treat
    "absent" and "present but null" as the same thing for a `nullable` field.
    """
    return {
        "fault_id": fault_id,
        "effect": effect,
        "target": target,
        "delay_virtual_ms": delay_virtual_ms,
        "exception_type": exception_type,
        "message_id": message_id,
    }


def fault_effect_span_id(fault_id: str, target: str) -> str:
    """Return a deterministic synthetic `span_id` for a `fault_effect` event.

    `events.schema.EVENT_SCOPES[EventType.FAULT_EFFECT]` is `SPAN` — `check_semantic`'s
    `E-EVENT-023` rejects a `fault_effect` with no `span_id` at all. None of this build's
    fault-class execution modules has a real SDK span in hand at the point they emit one
    (`CrashInjector` intercepts at the scheduler's task-lifecycle boundary, `Transport
    FaultInjector`/`DependencyFaultInjector` decide at a synthetic call site — see each
    module's own docstring): there is no live `agentdx.span()` context to borrow an id
    from. This function is that id instead — a fixed function of `(fault_id, target)`, so
    the same fault firing against the same target always gets the same `span_id`, every
    replay, at every seed (I1). It intentionally does not attempt to correlate with any
    *other* span the target agent's own SDK-authored events might carry; `fault_id` (not
    `span_id`) is the join key a consumer uses to relate a `fault_effect` back to its cause.
    """
    return f"fault_span_{fault_id}_{target}"


def param_int(params: Mapping[str, object], key: str, default: int) -> int:
    """Read an `int`-typed fault param, defensively narrowing `object` to `int` for mypy.

    `FAULT_CATALOGUE`'s `ParamConstraint` already type-checks every param at scenario-validation
    time (P08) — this is the same defence-in-depth posture as `safety.reauthorize` re-checking
    an already-authorised target, applied to a param's *type* rather than its authorisation.
    Returns `default` (never raises) if `key` is absent or its value is not a plain `int`
    (`bool` is explicitly excluded — `bool` is an `int` subclass in Python, but a fault param
    typed `bool` in `FAULT_CATALOGUE` and one typed `int` are never the same param).
    """
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def param_str(params: Mapping[str, object], key: str, default: str) -> str:
    """Read a `str`-typed fault param, defensively narrowing `object` to `str` for mypy.

    Same posture as `param_int` — see that function's docstring.
    """
    value = params.get(key, default)
    return value if isinstance(value, str) else default


@dataclass(eq=False)
class ArmedFault:
    """A `FaultDecl` plus the runtime's own mutable firing bookkeeping (PRD §12.3, §12.5).

    Not frozen: `fired`, `fire_count` and `first_fired_virtual_ts_ms` are updated in place by
    `triggers.should_fire`/the per-class execution modules as the run proceeds — this is the
    *only* mutable state in the fault engine's declarative surface, and it lives here rather
    than scattered across the execution modules so `registry.py`'s own `summary()` can report
    PRD §12.5's `fault_summary` fields (`fired_count`, `first_fired_at`, `targets_affected`)
    from one place. `eq=False`: two `ArmedFault`s are never compared for equality — each one is
    a distinct mutable object even if two faults happen to declare identical parameters, so the
    dataclass-generated `__eq__` (field-by-field) would be actively misleading (`is` is what
    every consumer actually needs, e.g. de-duplicating a fault against itself in a set).
    """

    decl: FaultDecl
    fired: bool = False
    fire_count: int = 0
    first_fired_virtual_ts_ms: int | None = None
    targets_affected: set[str] = field(default_factory=set)

    def record_fire(self, *, virtual_ts_ms: int, target: str) -> None:
        """Record one concrete application of this fault (PRD §12.5's `fault_effect` count)."""
        self.fired = True
        self.fire_count += 1
        if self.first_fired_virtual_ts_ms is None:
            self.first_fired_virtual_ts_ms = virtual_ts_ms
        self.targets_affected.add(target)


@dataclass(frozen=True, slots=True)
class BlastRadius:
    """The resolved `blast_radius:` section (PRD §13.4) — membership checks, no execution.

    `agents`/`tools`/`providers`: explicit names only (PRD §13.4 rule 3: "agents and tools may
    not [use globs], explicit naming only, to avoid a typo widening the radius"). `edges`: also
    explicit (same rule — an edge is a `"a->b"` string, not a glob-bearing path). `state_keys`:
    globs permitted (PRD §13.4's own example: `"draft.*"`), matched with `fnmatch`.

    `universal`: PRD §13.3's fixture default ("the fixture's blast radius defaults to
    'everything in the fixture'") — `True` only when the scenario targets a fixture *and*
    declared no explicit `blast_radius` section at all. `contains` short-circuits to `True` in
    that case without consulting the (empty) member sets.
    """

    agents: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()
    edges: frozenset[str] = frozenset()
    state_keys: tuple[str, ...] = ()
    providers: frozenset[str] = frozenset()
    universal: bool = False

    def contains(self, kind: TargetKind, value: str) -> bool:
        """Return whether `value` (of kind `kind`) is inside this blast radius.

        `STATE_KEY` matches by glob (`fnmatch.fnmatch`); every other kind is exact membership.
        """
        if self.universal:
            return True
        if kind is TargetKind.AGENT:
            return value in self.agents
        if kind is TargetKind.TOOL:
            return value in self.tools
        if kind is TargetKind.EDGE:
            return value in self.edges
        if kind is TargetKind.PROVIDER:
            return value in self.providers
        return any(fnmatch.fnmatch(value, pattern) for pattern in self.state_keys)
        # No trailing `return False`: mypy proves the five `TargetKind` members above are
        # exhaustive (a closed enum) and flags a further fallback as unreachable (`warn_
        # unreachable`) — `STATE_KEY` is therefore the plain final branch, not an `if`.


def _parse_trigger(entry: Mapping[str, object], *, fault_type: str) -> Trigger:
    """Return the `Trigger` declared on one fault entry.

    Raises:
        ChaosSafetyError: none of the fault's permitted trigger fields is present. Scenario
            validation (`E-SCEN-*`) should already guarantee exactly one is — this is a
            defence-in-depth check, not the primary gate (same posture as
            `ChaosAuthorizationError`).
    """
    spec = FAULT_CATALOGUE[fault_type]
    for kind in spec.trigger_kinds:
        field_name = TRIGGER_FIELD[kind]
        if field_name in entry:
            value = entry[field_name]
            if kind is TriggerKind.ALWAYS:
                return Trigger(kind=kind, value=None)
            if isinstance(value, (int, str)) and not isinstance(value, bool):
                return Trigger(kind=kind, value=value)
            detail = f"fault {fault_type!r} trigger field {field_name!r} has unexpected type"
            raise _e_chaos_003(detail)
    detail = f"fault {fault_type!r} declares no recognised trigger field"
    raise _e_chaos_003(detail)


def _parse_blast_radius(resolved: Mapping[str, object], *, universal: bool) -> BlastRadius:
    """Return the `BlastRadius` from a resolved scenario's `blast_radius:` section."""
    raw = resolved.get("blast_radius")
    section: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}

    def _strs(key: str) -> tuple[str, ...]:
        value = section.get(key, [])
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return tuple(value)
        return ()

    return BlastRadius(
        agents=frozenset(_strs("agents")),
        tools=frozenset(_strs("tools")),
        edges=frozenset(_strs("edges")),
        state_keys=_strs("state_keys"),
        providers=frozenset(_strs("providers")),
        universal=universal,
    )


@dataclass
class FaultRegistry:
    """Every fault armed for one run, plus the blast radius they are checked against.

    Constructed once, at RESOLVE time (PRD §12.4), before the baseline phase runs — never
    mutated after construction except through each `ArmedFault`'s own bookkeeping. This is the
    object `triggers.should_fire` and the per-class execution modules (`process.py`,
    `transport.py`, `dependency.py`) are handed; nothing outside this module ever constructs an
    `ArmedFault` directly.
    """

    faults: tuple[ArmedFault, ...]
    blast_radius: BlastRadius
    chaos_opt_in: bool
    is_fixture_target: bool

    @classmethod
    def from_resolved_scenario(
        cls, resolved: Mapping[str, object], *, is_fixture_target: bool
    ) -> FaultRegistry:
        """Build a `FaultRegistry` from a fully-resolved scenario document.

        `resolved` is the output of `scenario.loader.resolve_defaults`, already validated by
        `scenario.validate.validate_or_raise` — this method re-checks PRD §13.3/§13.4's
        authorisation rules defensively (I12: "enforced at two layers") rather than trusting
        that validation already ran, but does not re-derive `scenario.validate`'s own
        structural checks (unknown keys, missing target, etc.) — those are that module's job.

        Args:
            resolved: The resolved scenario document (`scenario.loader.resolve_defaults`'s
                output — a plain dict, never a second competing dataclass, matching this
                package's own D-42 ruling).
            is_fixture_target: Whether `resolved["target"]` names a fixture (`True`) or a user
                graph (`False`). The caller resolves this (`scenario.validate.resolve_graph_
                identity`'s own distinction) — this module has no graph-identity logic of its
                own, deliberately, to avoid a second one.

        Raises:
            FaultNotImplementedError: a declared fault's type is outside `MVP_FAULT_TYPES`.
            ChaosAuthorizationError: a user-graph scenario declares faults without
                `chaos_opt_in: true` and a non-empty blast radius (`E-CHAOS-001`), or a fault's
                target is outside the resolved blast radius at arm time.
            ChaosSafetyError: a fault entry is malformed in a way scenario validation should
                have already caught (defence-in-depth only).
        """
        raw_faults = resolved.get("faults", [])
        fault_entries: Sequence[Mapping[str, object]] = (
            [f for f in raw_faults if isinstance(f, Mapping)]
            if isinstance(raw_faults, list)
            else []
        )
        chaos_opt_in = bool(resolved.get("chaos_opt_in", False))
        raw_blast_radius = resolved.get("blast_radius")
        blast_radius_section: Mapping[str, object] = (
            raw_blast_radius if isinstance(raw_blast_radius, Mapping) else {}
        )
        # `BLAST_RADIUS_KEYS` ("agents", "tools", ...) — not `TARGET_FIELD.values()` ("agent",
        # "tool", ...), which names a *fault entry's own* target field (e.g. `agent: reviewer`)
        # and is a different vocabulary from a `blast_radius:` section's own (plural) keys.
        blast_radius_declared = isinstance(raw_blast_radius, Mapping) and any(
            blast_radius_section.get(k) for k in BLAST_RADIUS_KEYS
        )

        if fault_entries and not is_fixture_target:
            if not chaos_opt_in or not blast_radius_declared:
                detail = (
                    "user-graph scenario declares faults without both chaos_opt_in: true and "
                    "a non-empty blast_radius (PRD §13.3/§13.10) — scenario validation "
                    "(E-SCEN-004) should already have rejected this document"
                )
                raise ChaosAuthorizationError(detail)

        universal = is_fixture_target and not blast_radius_declared
        blast_radius = _parse_blast_radius(resolved, universal=universal)

        armed: list[ArmedFault] = []
        for index, entry in enumerate(fault_entries):
            fault_type_value = entry.get("type")
            if not isinstance(fault_type_value, str) or fault_type_value not in FAULT_CATALOGUE:
                detail = f"fault entry {index} has unknown type {fault_type_value!r}"
                raise _e_chaos_003(detail)
            fault_type = fault_type_value
            spec = FAULT_CATALOGUE[fault_type]
            if fault_type not in MVP_FAULT_TYPES:
                raise FaultNotImplementedError(fault_type, tier=spec.tier)

            target_kind = spec.target_kinds[0] if len(spec.target_kinds) == 1 else None
            for candidate_kind in spec.target_kinds:
                field_name = TARGET_FIELD[candidate_kind]
                if field_name in entry:
                    target_kind = candidate_kind
                    break
            if target_kind is None:
                detail = f"fault entry {index} ({fault_type}) names no recognised target field"
                raise _e_chaos_003(detail)
            target_value = entry.get(TARGET_FIELD[target_kind])
            if not isinstance(target_value, str):
                detail = f"fault entry {index} ({fault_type}) target is not a string"
                raise _e_chaos_003(detail)

            if not blast_radius.contains(target_kind, target_value):
                detail = (
                    f"fault entry {index} ({fault_type}) targets {target_kind.value}="
                    f"{target_value!r}, outside the resolved blast radius"
                )
                raise ChaosAuthorizationError(detail)

            trigger = _parse_trigger(entry, fault_type=fault_type)
            params = {k: v for k, v in entry.items() if k in spec.params}
            decl = FaultDecl(
                fault_id=f"f_{index:02d}",
                fault_type=fault_type,
                target_kind=target_kind,
                target=target_value,
                trigger=trigger,
                params=params,
            )
            armed.append(ArmedFault(decl=decl))

        return cls(
            faults=tuple(armed),
            blast_radius=blast_radius,
            chaos_opt_in=chaos_opt_in,
            is_fixture_target=is_fixture_target,
        )

    def by_type(self, fault_type: str) -> tuple[ArmedFault, ...]:
        """Return every armed fault of `fault_type`, in declaration order."""
        return tuple(f for f in self.faults if f.decl.fault_type == fault_type)

    def summary(self) -> tuple[Mapping[str, object], ...]:
        """Return PRD §12.5's `fault_summary` entry for every armed fault.

        One dict per fault: `fault_id`, `fault_type`, `target`, `fired_count`,
        `first_fired_at` (virtual ms, `None` if never fired), `targets_affected` (sorted),
        and `fault_not_triggered` (PRD §12.5: "must be surfaced prominently").
        """
        return tuple(
            {
                "fault_id": f.decl.fault_id,
                "fault_type": f.decl.fault_type,
                "target": f.decl.target,
                "fired_count": f.fire_count,
                "first_fired_at": f.first_fired_virtual_ts_ms,
                "targets_affected": sorted(f.targets_affected),
                "fault_not_triggered": not f.fired,
            }
            for f in self.faults
        )


__all__ = [
    "MVP_FAULT_TYPES",
    "ArmedFault",
    "BlastRadius",
    "ChaosAuthorizationError",
    "ChaosSafetyError",
    "FaultDecl",
    "FaultNotImplementedError",
    "FaultRegistry",
    "Trigger",
    "fault_effect_payload",
    "fault_effect_span_id",
    "param_int",
    "param_str",
    "params_payload",
]
