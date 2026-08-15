"""Unit tests for `runtime.context`: the scheduler's own ambient task identity."""

from __future__ import annotations

import dataclasses

import pytest

from agentdx.runtime.context import (
    SchedTaskContext,
    TaskContextError,
    active_task,
    current_task,
    use_task,
)


def test_current_task_raises_with_no_ambient_context() -> None:
    with pytest.raises(TaskContextError):
        current_task()


def test_active_task_returns_none_with_no_ambient_context() -> None:
    assert active_task() is None


def test_use_task_binds_and_restores_on_clean_exit() -> None:
    ctx = SchedTaskContext(task_id="t_1", agent_id="coder")
    assert active_task() is None
    with use_task(ctx):
        assert current_task() == ctx
        assert active_task() == ctx
    assert active_task() is None


def test_use_task_restores_even_when_the_block_raises() -> None:
    ctx = SchedTaskContext(task_id="t_1", agent_id="coder")
    with pytest.raises(ValueError, match="boom"), use_task(ctx):
        assert active_task() == ctx
        raise ValueError("boom")
    assert active_task() is None


def test_nested_use_task_restores_the_outer_context() -> None:
    outer = SchedTaskContext(task_id="t_outer", agent_id="planner")
    inner = SchedTaskContext(task_id="t_inner", agent_id="coder")
    with use_task(outer):
        assert active_task() == outer
        with use_task(inner):
            assert active_task() == inner
        assert active_task() == outer
    assert active_task() is None


def test_sched_task_context_is_frozen() -> None:
    ctx = SchedTaskContext(task_id="t_1", agent_id="coder")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.task_id = "t_2"  # type: ignore[misc]
