"""`@agentdx.agent` and `@agentdx.tool` — the decorator path for plain Python (PRD §8.4).

The design constraint PRD §8.2 states for items 1 and 2 is the one that matters here: the
decorators require **zero changes to prompt or agent logic**. They wrap; they do not inspect
arguments, rewrite prompts, or alter what the function returns. A decorated function raises
exactly what it raised before, with exactly the same traceback — PRD §8.9 requires that
instrumentation never swallows an error, and a decorator is the easiest place in the system
to break it by accident.

Both decorators work on coroutine functions and on plain functions. The two paths share the
same span helpers in `generic.py`, so a graph of sync nodes and a graph of async nodes
produce the same event shapes; if they did not, the two would not be comparable and the
baseline in PRD §17 would be measuring the instrumentation rather than the system.

PRD §8.2 items 2 · §8.4 · §8.9 · §6.1 (Agent, Span). ADR-007 governs `attributes`.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping
from typing import ParamSpec, TypeVar, cast

from agentdx.events.schema import EventType, PayloadValue
from agentdx.sdk.generic import (
    MISSING_VALUE_HASH,
    RunContext,
    Span,
    ValueRepresentationError,
    agent_scope,
    check_attributes,
    current_agent,
    current_run,
    emit,
    hash_text,
    record_gap,
    span,
    stable_text,
    sync_agent_scope,
    sync_span,
)

_P = ParamSpec("_P")
_R = TypeVar("_R")

AGENT_ROLES = ("worker", "orchestrator", "router", "tool_proxy")
"""PRD §6.1's agent roles. Role affects overhead bucketing in PRD §16.2.

Not enforced as a closed set: `role` is carried on the span's `attributes`, which is an open
user-supplied map, and rejecting an unlisted role would break a user graph for a label that
only changes a bucket heading.
"""


def agent(
    agent_id: str,
    *,
    role: str | None = None,
    attributes: Mapping[str, object] | None = None,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Instrument a function as an agent (PRD §8.2 item 2, §8.4).

    Wrapping a function with this decorator does four things and nothing else:

    1. establishes an `AgentContext` in a `contextvar` for the duration of the call;
    2. emits `span_start`/`span_end` with `kind=agent_step`;
    3. registers the agent in the run's agent set, allocating its vector-clock slot on
       first use;
    4. makes nested `@agentdx.tool` calls and provider-shim LLM calls attribute to this
       agent automatically.

    Guarantees: the wrapped function's arguments, return value and exceptions are passed
    through unchanged, and `functools.wraps` preserves its name, docstring and signature —
    so a decorated function remains usable as a LangGraph node, a callback, or anything
    else that introspects it.

    Args:
        agent_id: The stable identity of this agent (PRD §8.8). It must be stable across
            runs or baseline comparison in PRD §17 breaks.
        role: One of `AGENT_ROLES`; affects overhead bucketing (PRD §16.2).
        attributes: Extra span attributes. Floats are rejected — see ADR-007.

    Raises:
        AttributeTypeError: an attribute value is not loggable (`E-INSTR-005`), raised when
            the decorated function is *called*, not when it is decorated.
        RunContextError: the function was called outside a run (`E-INSTR-003`).
    """
    merged = dict(attributes or {})
    if role is not None:
        merged.setdefault("role", role)

    def decorate(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        """Return `fn` wrapped so that every call becomes an agent span."""
        name = getattr(fn, "__name__", agent_id)
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_agent(*args: _P.args, **kwargs: _P.kwargs) -> _R:
                async with agent_scope(agent_id, name=name, role=role, attributes=merged):
                    return await cast(Callable[_P, object], fn)(*args, **kwargs)  # type: ignore[misc,no-any-return]

            return cast(Callable[_P, _R], async_agent)

        @functools.wraps(fn)
        def sync_agent(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with sync_agent_scope(agent_id, name=name, role=role, attributes=merged):
                return fn(*args, **kwargs)

        return sync_agent

    return decorate


def tool(
    name: str,
    *,
    attributes: Mapping[str, object] | None = None,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Instrument a function as a tool call (PRD §8.2 item 2, §6.1 Span).

    Emits a `tool_call` span — `span_start(kind=tool_call)`, a `tool_call` event carrying
    the hashes, then `span_end` — attributed to whichever agent is ambient.

    Guarantees:

    * `args_hash` is the hash of `(args, kwargs)` and `result_hash` the hash of the return
      value. Neither body is written unless `capture_bodies` is on (invariant I8, PRD
      §8.11). The pair is what PRD §16.3's redundancy detector compares, so two calls with
      the same arguments hash the same in every run.
    * A raising tool still emits its `tool_call`, with `status="error"`, before the
      exception propagates. A tool that failed is evidence, not an absence.

    Args:
        name: The tool's stable name, used for redundancy grouping.
        attributes: Extra span attributes. Floats are rejected — see ADR-007.

    Raises:
        AgentContextError: called with no ambient agent (`E-INSTR-004`).
        RunContextError: called outside a run (`E-INSTR-003`).
    """
    checked = check_attributes(attributes)

    def decorate(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        """Return `fn` wrapped so that every call becomes a tool span."""
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_tool(*args: _P.args, **kwargs: _P.kwargs) -> _R:
                run = current_run()
                async with span("tool_call", name, attributes=checked) as open_span:
                    call = _hash_call(run, name, args, kwargs)
                    started = run.clock.virtual_ms()
                    try:
                        result = await cast(Callable[_P, object], fn)(*args, **kwargs)  # type: ignore[misc]
                    except Exception:
                        _emit_tool_call(run, open_span, name, call, None, "error", started)
                        raise
                    _emit_tool_call(run, open_span, name, call, result, "ok", started)
                    return cast(_R, result)

            return cast(Callable[_P, _R], async_tool)

        @functools.wraps(fn)
        def sync_tool(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            run = current_run()
            with sync_span("tool_call", name, attributes=checked) as open_span:
                call = _hash_call(run, name, args, kwargs)
                started = run.clock.virtual_ms()
                try:
                    result = fn(*args, **kwargs)
                except Exception:
                    _emit_tool_call(run, open_span, name, call, None, "error", started)
                    raise
                _emit_tool_call(run, open_span, name, call, result, "ok", started)
                return result

        return sync_tool

    return decorate


def _hash_call(
    run: RunContext, name: str, args: tuple[object, ...], kwargs: Mapping[str, object]
) -> tuple[str, str | None]:
    """Return `(args_hash, args_text)` for one tool invocation.

    Guarantees: never raises. Arguments the SDK cannot represent reproducibly produce an
    `instrumentation_gap`, the all-zero digest and no text, so the tool still runs and the
    log states that this one call's arguments are not comparable — rather than silently
    hashing a memory address, which would make PRD §16.3's redundancy detector miss real
    duplicates.

    The hashed material is `{"tool": ..., "args": [...], "kwargs": {...}}`, which is the
    shape CONTEXT.md §3's redundancy rule names: an exact hash of
    `(tool_name ‖ canonical_json(args))`.
    """
    material = {"tool": name, "args": list(args), "kwargs": dict(kwargs)}
    try:
        text = stable_text(material)
    except ValueRepresentationError as exc:
        record_gap(run, "tool_args", name, str(exc))
        return MISSING_VALUE_HASH, None
    return hash_text(text), text


def _hash_result(run: RunContext, name: str, result: object) -> tuple[str, str | None]:
    """Return `(result_hash, result_text)`, degrading loudly (see `_hash_call`)."""
    try:
        text = stable_text(result)
    except ValueRepresentationError as exc:
        record_gap(run, "tool_result", name, str(exc))
        return MISSING_VALUE_HASH, None
    return hash_text(text), text


def _emit_tool_call(
    run: RunContext,
    open_span: Span,
    name: str,
    call: tuple[str, str | None],
    result: object,
    status: str,
    started_virtual_ms: int,
) -> int:
    """Emit the `tool_call` event for a completed or failed invocation."""
    agent = current_agent()
    args_hash, args_text = call
    result_hash, result_text = (
        (MISSING_VALUE_HASH, None) if status == "error" else _hash_result(run, name, result)
    )
    payload: dict[str, PayloadValue] = {
        "tool": name,
        "args_hash": args_hash,
        "result_hash": result_hash,
        "status": status,
        "duration_virtual_ms": run.clock.virtual_ms() - started_virtual_ms,
    }
    if run.capture_bodies:
        if args_text is not None:
            payload["args"] = run.redactor.scrub(args_text)
        if result_text is not None:
            payload["result"] = run.redactor.scrub(result_text)
    return emit(
        run,
        EventType.TOOL_CALL,
        payload,
        agent_id=agent.agent_id,
        clock_slot=agent.clock_slot,
        span_id=open_span.span_id,
        causes=(open_span.start_seq,),
    )


__all__ = ["AGENT_ROLES", "agent", "tool"]
