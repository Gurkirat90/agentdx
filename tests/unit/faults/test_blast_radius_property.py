"""Property tests for the blast-radius invariant, PRD §33.6's second sentence.

    "Plus a property test: for all scenarios and seeds, no fault ever affects a target
    outside the blast radius."

§33.6's first sentence ("one test per fault type asserting ... blast-radius refusal") is
already covered, per fault class, by the `test_fire_time_reauthorization_refuses_*` tests in
`test_process.py`/`test_transport.py`/`test_dependency.py`, and by the hand-picked
in-radius/out-of-radius cases in `test_safety.py`'s `test_blast_radius_*` tests. All of those
are example-based: a small, fixed, hand-chosen set of blast radii and targets. Nothing in the
suite before this file used Hypothesis (`@given`) to generate the blast radii and targets
themselves — `grep -rl '@given' tests/` before this file existed returned two files, neither
about faults. This file is that missing property test.

**What "for all scenarios and seeds" is read to mean here.** `hypothesis`'s own random seed
*is* "for all ... seeds" in the sense that matters: `@settings(max_examples=...)` runs the
property against many independently-drawn random blast-radius declarations and targets, and
Hypothesis additionally shrinks and re-tries any failure to find a minimal counterexample —
strictly more adversarial than picking a handful of fixed seeds by hand. "Scenarios" is read
as "blast-radius declarations and fault targets", the two axes the invariant actually
quantifies over (PRD §13.4's own rule text), rather than full YAML scenario documents, since
the invariant is about the enforcement primitive (`BlastRadius.contains` / `safety.
reauthorize` / `FaultRegistry.arm`-time refusal), not about any one scenario's authoring
surface.

Two levels are tested:

1. **The enforcement primitive itself** (`BlastRadius.contains`, `safety.reauthorize`) —
   across all five `TargetKind` members, not just the three MVP-armable ones, because this is
   the actual complete safety mechanism PRD §13.4 describes and it is fully decoupled from any
   one fault type.
2. **Arm-time refusal** (`FaultRegistry.from_resolved_scenario`) — restricted to the three
   `TargetKind`s an MVP fault type can actually declare (`AGENT`, `EDGE`, `TOOL` — CONTEXT.md
   §3's locked MVP set is `latency`, `agent_crash`, `message_drop`, `tool_failure`, and none of
   the four declares a `STATE_KEY` or `PROVIDER` target; see `scenario/schema.py`'s
   `FAULT_CATALOGUE`), generalising the fixed cases in `test_registry.py` to randomly
   generated blast radii, fault types and targets.
"""

from __future__ import annotations

import fnmatch
import string
from dataclasses import replace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agentdx.runtime.faults import registry as registry_module
from agentdx.runtime.faults.registry import (
    ArmedFault,
    BlastRadius,
    FaultDecl,
    FaultRegistry,
    Trigger,
)
from agentdx.runtime.faults.safety import ChaosAuthorizationError, reauthorize
from agentdx.scenario.schema import TARGET_FIELD, TargetKind, TriggerKind

# ---------------------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------------------

# Plain lowercase names for AGENT/TOOL/EDGE/PROVIDER — these match by exact membership
# (PRD §13.4 rule 3: "explicit naming only"), so glob metacharacters are irrelevant to what
# is being tested and are kept out to avoid an unrelated, coincidental match.
_NAMES = st.text(alphabet=string.ascii_lowercase + "_", min_size=1, max_size=10)
_NAME_SETS = st.frozensets(_NAMES, max_size=5)

# STATE_KEY matches by `fnmatch` glob (PRD §13.4's own "draft.*" example). A mix of literal
# dotted keys and glob patterns built from the same alphabet, so both branches of `fnmatch`
# (an exact literal match, and a real wildcard match) get exercised.
_KEY_SEGMENT = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6)
_STATE_KEY_PATTERNS = st.one_of(
    st.builds(lambda a, b: f"{a}.{b}", _KEY_SEGMENT, _KEY_SEGMENT),
    st.builds(lambda a: f"{a}.*", _KEY_SEGMENT),
)

_BLAST_RADII = st.builds(
    BlastRadius,
    agents=_NAME_SETS,
    tools=_NAME_SETS,
    edges=_NAME_SETS,
    state_keys=st.lists(_STATE_KEY_PATTERNS, max_size=4).map(tuple),
    providers=_NAME_SETS,
    universal=st.booleans(),
)

_ALL_TARGET_KINDS = st.sampled_from(list(TargetKind))
_MVP_ARMABLE_TARGET_KINDS = st.sampled_from([TargetKind.AGENT, TargetKind.EDGE, TargetKind.TOOL])
_MVP_FAULT_TYPE_FOR_KIND = {
    TargetKind.AGENT: "agent_crash",
    TargetKind.EDGE: "message_drop",
    TargetKind.TOOL: "tool_failure",
}


def _armed_fault_targeting(kind: TargetKind, target: str) -> ArmedFault:
    """Build a minimal, otherwise-arbitrary `ArmedFault` naming `(kind, target)`.

    `fault_type` is fixed to `"agent_crash"` regardless of `kind` — neither `BlastRadius.
    contains` nor `safety.reauthorize` ever consults `fault_type`, only `target_kind`/`target`
    (read both functions' own source before assuming otherwise), so this is not a scope
    narrowing, just avoiding an unused, irrelevant parameter draw.
    """
    decl = FaultDecl(
        fault_id="f_00",
        fault_type="agent_crash",
        target_kind=kind,
        target=target,
        trigger=Trigger(kind=TriggerKind.ALWAYS, value=None),
        params={},
    )
    return ArmedFault(decl=decl)


def _reference_contains(radius: BlastRadius, kind: TargetKind, value: str) -> bool:
    """An oracle for "value is inside radius", written independently of `BlastRadius.contains`.

    Derived directly from PRD §13.4's own rule text ("STATE_KEY matches by glob; every other
    kind is exact membership") and `registry.py`'s own docstring, not from reading `contains`'s
    implementation — so a defect in that implementation (the wrong field for a kind, a
    stray `not`, a kind routed to the wrong set) has something independent to disagree with,
    rather than this test restating the same three lines it is meant to check.
    """
    if radius.universal:
        return True
    member_set_by_kind = {
        TargetKind.AGENT: radius.agents,
        TargetKind.TOOL: radius.tools,
        TargetKind.EDGE: radius.edges,
        TargetKind.PROVIDER: radius.providers,
    }
    member_set = member_set_by_kind.get(kind)
    if member_set is not None:
        return value in member_set
    assert kind is TargetKind.STATE_KEY
    return any(fnmatch.fnmatch(value, pattern) for pattern in radius.state_keys)


# ---------------------------------------------------------------------------------------
# Level 1 — BlastRadius.contains / safety.reauthorize (all five TargetKind members)
# ---------------------------------------------------------------------------------------


@given(radius=_BLAST_RADII, kind=_ALL_TARGET_KINDS, target=_NAMES)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_property_contains_matches_the_prd_spec_for_every_target_kind(
    radius: BlastRadius, kind: TargetKind, target: str
) -> None:
    """`BlastRadius.contains` never disagrees with the independent PRD-derived oracle above."""
    assert radius.contains(kind, target) == _reference_contains(radius, kind, target)


@given(radius=_BLAST_RADII, kind=_ALL_TARGET_KINDS, target=_NAMES)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_property_reauthorize_raises_exactly_when_the_target_is_outside_the_radius(
    radius: BlastRadius, kind: TargetKind, target: str
) -> None:
    """The PRD §33.6 property, at its actual enforcement point.

    `safety.reauthorize` is the fire-time gate every fault-class execution module
    (`process.py`/`transport.py`/`dependency.py`) calls immediately before mutating anything
    observable — `safety.py`'s own module docstring: "none of them import `registry.
    BlastRadius` directly for their own re-checks; all of them call this module's
    `reauthorize`." If this property holds for every `(radius, kind, target)` triple Hypothesis
    can draw, no fault can ever fire against a target outside its declared blast radius,
    for any blast-radius declaration and any target — which is the literal PRD sentence this
    file exists to test, not a paraphrase of it.
    """
    armed = _armed_fault_targeting(kind, target)
    # Decided by the independent oracle, not by `radius.contains` itself — using `contains`
    # here would make this test circular: `reauthorize` calls `contains` internally, so a
    # mutated `contains` and this test's own expectation would always agree with each other
    # regardless of what the mutation broke. Caught live: an earlier draft used `radius.
    # contains(kind, target)` as the oracle, and a mutation forcing the TOOL branch of
    # `contains` to always return True left this specific test green (only the two tests
    # that consult `_reference_contains` caught it) — fixed to what is written below.
    if _reference_contains(radius, kind, target):
        reauthorize(armed, radius)  # must not raise
    else:
        with pytest.raises(ChaosAuthorizationError) as excinfo:
            reauthorize(armed, radius)
        assert "E-CHAOS-001" in str(excinfo.value)


@given(radius=_BLAST_RADII, kind=_ALL_TARGET_KINDS, member=_NAMES)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_property_a_target_just_added_to_the_radius_is_always_accepted(
    radius: BlastRadius, kind: TargetKind, member: str
) -> None:
    """Complementary direction: a target just added to the radius is always accepted.

    A target-side sanity check on `contains`/`reauthorize` agreeing with each other, not just
    on each other's absence of a false negative.
    """
    # Explicit per-field branches, not `replace(radius, **{field_name: ...})`: a dynamic
    # kwargs dict defeats mypy's own check of `BlastRadius.replace`'s field types (confirmed —
    # an earlier draft using that form type-checked to two `arg-type` errors, since mypy has
    # no way to know which field a `str`-keyed dict entry targets), and this project's own
    # convention (`AGENTS.md`) is `mypy --strict` clean on everything, tests included.
    if kind is TargetKind.AGENT:
        widened = replace(radius, agents=radius.agents | frozenset({member}))
    elif kind is TargetKind.TOOL:
        widened = replace(radius, tools=radius.tools | frozenset({member}))
    elif kind is TargetKind.EDGE:
        widened = replace(radius, edges=radius.edges | frozenset({member}))
    elif kind is TargetKind.PROVIDER:
        widened = replace(radius, providers=radius.providers | frozenset({member}))
    else:
        assert kind is TargetKind.STATE_KEY
        widened = replace(radius, state_keys=(*radius.state_keys, member))
    assert widened.contains(kind, member) is True
    reauthorize(_armed_fault_targeting(kind, member), widened)  # must not raise


# ---------------------------------------------------------------------------------------
# Level 2 — FaultRegistry.from_resolved_scenario arm-time refusal (MVP-armable kinds only)
# ---------------------------------------------------------------------------------------

_BLAST_RADII_EXPLICIT = st.builds(
    BlastRadius,
    agents=_NAME_SETS,
    tools=_NAME_SETS,
    edges=_NAME_SETS,
    state_keys=st.just(()),
    providers=_NAME_SETS,
    universal=st.just(False),
)
"""Same shape as `_BLAST_RADII`, minus `state_keys`/`universal` — `FaultRegistry.from_resolved_
scenario` has no YAML surface for either (there is no `universal: true` key; `universal` is
derived purely from `is_fixture_target and not blast_radius_declared`, PRD §13.3), and the
three MVP-armable kinds tested at this level never touch `state_keys` anyway (see the module
docstring's account of `FAULT_CATALOGUE`)."""


def _resolved_scenario_with_one_fault(
    radius: BlastRadius, kind: TargetKind, target: str
) -> dict[str, object]:
    """One minimal, otherwise-valid resolved user-graph scenario declaring a single MVP fault.

    `at_virtual_ts: 0` as the trigger field for every fault type: all four MVP fault types
    (`latency`, `agent_crash`, `message_drop`, `tool_failure`) list `TriggerKind.AT_VIRTUAL_TS`
    among their permitted `trigger_kinds` (`scenario/schema.py`'s `FAULT_CATALOGUE`, checked
    directly rather than assumed) — the one trigger field common to all three kinds tested
    here, so the fault entry is valid regardless of which of the three fault types is used.
    """
    return {
        "chaos_opt_in": True,
        "blast_radius": {
            "agents": sorted(radius.agents),
            "tools": sorted(radius.tools),
            "edges": sorted(radius.edges),
            "providers": sorted(radius.providers),
        },
        "faults": [
            {
                "type": _MVP_FAULT_TYPE_FOR_KIND[kind],
                TARGET_FIELD[kind]: target,
                "at_virtual_ts": 0,
            }
        ],
    }


@given(radius=_BLAST_RADII_EXPLICIT, kind=_MVP_ARMABLE_TARGET_KINDS, target=_NAMES)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_property_registry_never_arms_a_fault_outside_the_resolved_blast_radius(
    radius: BlastRadius, kind: TargetKind, target: str
) -> None:
    """Generalises `test_registry.py`'s hand-picked in/out-of-radius cases to random inputs.

    `FaultRegistry.from_resolved_scenario` either arms the fault with its declared target
    intact, or refuses to arm it at all (`registry.ChaosAuthorizationError`) — there is no
    third outcome, and in particular there is no outcome where the fault is armed against a
    target the resolved blast radius does not contain.
    """
    resolved = _resolved_scenario_with_one_fault(radius, kind, target)
    inside = radius.contains(kind, target)
    if inside:
        armed_registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)
        assert len(armed_registry.faults) == 1
        assert armed_registry.faults[0].decl.target == target
        assert armed_registry.faults[0].decl.target_kind is kind
    else:
        with pytest.raises(registry_module.ChaosAuthorizationError) as excinfo:
            FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)
        assert "E-CHAOS-001" in str(excinfo.value)
