"""Every symbol PRD §8.2 lists exists, is typed, and is reachable from `import agentdx`.

This is CONTEXT.md §11 tripwire 14 mechanised for P04: the tripwire that catches doing *too
little*. Nothing else in the suite fails if a public symbol is quietly missing, because a
missing symbol has no test — which is exactly how PRD §27.3's "or 50 ms" went unbuilt and
unnoticed for a whole prompt. This test is named for the requirement so its absence is loud.
"""

from __future__ import annotations

import inspect

import pytest

import agentdx

# PRD §8.2's five groups, transcribed. The comment beside each names the group it belongs to
# so that a symbol removed from the PRD is removed from here in the same edit.
PUBLIC_SURFACE = (
    "instrument",  # group 1 — the one-line path
    "agent",  # group 2 — decorators
    "tool",  # group 2
    "state",  # group 3 — explicit state access
    "lock",  # group 4 — synchronisation primitives
    "transaction",  # group 4
    "barrier",  # group 4 (PRD §8.1 names it in sync.py; §8.2 omits it — see docs/sdk.md)
    "run",  # group 5 — programmatic run control
    "send",  # PRD §8.4 — explicit message passing in generic mode
    "recv",  # PRD §8.4
)


@pytest.mark.parametrize("name", PUBLIC_SURFACE)
def test_every_prd_8_2_symbol_is_exported(name: str) -> None:
    assert hasattr(agentdx, name), f"PRD §8.2 names {name} and it is not exported"
    assert name in agentdx.__all__


@pytest.mark.parametrize("name", PUBLIC_SURFACE)
def test_every_public_symbol_is_annotated(name: str) -> None:
    symbol = getattr(agentdx, name)
    signature = inspect.signature(symbol)
    for parameter in signature.parameters.values():
        assert parameter.annotation is not inspect.Parameter.empty, (
            f"{name}({parameter.name}) has no annotation"
        )
    assert signature.return_annotation is not inspect.Signature.empty


def test_all_is_complete_and_resolvable() -> None:
    missing = [name for name in agentdx.__all__ if not hasattr(agentdx, name)]
    assert missing == []


def test_instrument_signature_matches_the_prd_example() -> None:
    # PRD §8.2: instrument(compiled_graph, name=..., capture_bodies=False, agent_from=...)
    parameters = inspect.signature(agentdx.instrument).parameters
    assert "name" in parameters
    assert "capture_bodies" in parameters
    assert "agent_from" in parameters
    assert parameters["capture_bodies"].default is None, (
        "capture_bodies must default to the resolved [privacy] setting, which is False (I8) — "
        "never to a literal that could drift from the config"
    )


def test_run_signature_matches_the_prd_example() -> None:
    # PRD §8.2: await agentdx.run(graph, task=..., scenario=..., seed=42)
    parameters = inspect.signature(agentdx.run).parameters
    for expected in ("task", "scenario", "seed"):
        assert expected in parameters


def test_the_wall_clock_accessor_landed_at_p06_and_returns_an_int() -> None:
    # AGENTS.md §4.1 clause 3 names `agentdx.wall_time()`; CONTEXT.md D-16 assigned it to
    # P06, which is where it now lives (runtime/clock.py, re-exported here). Before P06 this
    # test asserted the opposite — that it did NOT exist — so that the SDK could not acquire
    # a real clock early. Now that it is built, the regression this guards against is the
    # accessor silently returning a float (ADR-007: no floats in anything that could reach
    # the event log — `wall_ts_ms` is populated straight from this return value).
    assert hasattr(agentdx, "wall_time")
    value = agentdx.wall_time()
    assert isinstance(value, int)
    assert not isinstance(value, bool)
    assert value > 0


def test_the_sorted_set_helper_landed_at_p06() -> None:
    # Same story as wall_time(): PRD §10.5 names `agentdx.sorted_set()` as the replacement
    # for iterating a bare `set`; CONTEXT.md D-16 assigned it to P06 alongside wall_time().
    assert hasattr(agentdx, "sorted_set")
    assert agentdx.sorted_set({3, 1, 2}) == [1, 2, 3]
