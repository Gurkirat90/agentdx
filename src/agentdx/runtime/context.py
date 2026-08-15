"""The scheduler's own ambient state: which task is running now (PRD §8.8, one layer down).

**Why this exists as a separate module, and why it is not `sdk.generic.RunContext` again.**
`sdk/generic.py` already has a `RunContext`/`AgentContext` pair in `contextvars`, and the
reason it gives for the pattern — "asyncio tasks... inherit them without threading
parameters through user code" (PRD §8.8) — applies just as literally one layer down, to the
scheduler's own baton-passing tasks (PRD §10.2) and to `runtime/determinism.py`'s patched
builtins. Neither of those can be handed an explicit parameter:

* `runtime/scheduler.py`'s `yield_point(reason)` is called from arbitrary depth inside
  whatever coroutine currently holds the scheduler's baton (an LLM call, a tool call, deep
  inside a user's own `async def`). The `Scheduler.yield_point` Protocol
  (`sdk.generic.Scheduler`) takes only `reason: str` — there is no `task_id` parameter to
  thread through every call site — so the scheduler has to ask "who is calling?" ambiently.
* `random.random()`, `time.time()` and friends cannot be given an extra argument at all;
  `runtime/determinism.py`'s leak report ("`time.time()` called outside AgentDX in `coder`",
  PRD §36 `E-SCHED-004`'s example message) needs to name the offending agent, and the only
  way to know that from inside a patched builtin is to ask this module.

**This is deliberately a second, smaller contextvar than `sdk.generic`'s**, not a
replacement or a duplicate. `RunContext`/`AgentContext` carry the *SDK's* view — spans,
state, redaction, hooks. `SchedTaskContext` carries only what the *scheduler* needs to
identify who currently holds the baton, which is one string plus the agent it belongs to,
and it exists a layer below the SDK — `runtime/` must not import `sdk/` (CONTEXT.md §4), so
it cannot reuse the SDK's contextvar even if the shape happened to match.

**Not put here:** the seeded RNG, the virtual clock and the strict-mode flag. Those are
captured once as closures at `runtime/determinism.py`'s `trap()` call (PRD §10.5: "the
runtime installs the following, and removes them on exit" — a single bracket around one
run, not a per-call ambient lookup), because unlike "who is calling", "which run's RNG"
never changes mid-call and needs no contextvar to answer.

PRD §8.8 (the pattern this mirrors), §10.2 (yield points), §10.5 (leak attribution).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Final

_DOCS: Final = "docs/determinism-guarantees.md"


class TaskContextError(RuntimeError):
    """A scheduler-internal call needed to know the current task, and none was ambient.

    Carries `E-SCHED-001` (the same scheduler-internal-invariant code as `runtime.clock`'s
    `ClockError` — both mean "the scheduler's own bookkeeping is broken", never something a
    user's graph triggered). This can only happen if `yield_point` or a determinism patch is
    reached from outside a scheduler-managed task entirely — e.g. a coroutine the scheduler
    did not spawn calling `await run.scheduler.yield_point(...)` directly. Every task
    `runtime/scheduler.py` spawns binds this context before running a single line of the
    task's body, so a correctly operating scheduler never raises this from its own tasks.
    """

    code: Final = "E-SCHED-001"

    def __init__(self, detail: str) -> None:
        """Build the error from a description of what could not find its task context."""
        super().__init__(f"[{self.code}] {detail} ({_DOCS}#{self.code.lower()})")


@dataclass(frozen=True, slots=True)
class SchedTaskContext:
    """Identifies the scheduler task currently holding the baton (PRD §10.2).

    Guarantees: immutable, so a nested `yield_point` call or a task spawning a child cannot
    mutate the context a sibling coroutine is holding — the same reasoning `sdk.generic`
    gives for freezing `AgentContext` (entering a new scope produces a *new* context).
    """

    task_id: str
    agent_id: str


_CURRENT_TASK: ContextVar[SchedTaskContext | None] = ContextVar(
    "agentdx_runtime_task", default=None
)


def current_task() -> SchedTaskContext:
    """Return the scheduler task context for whichever coroutine calls this.

    Guarantees: never guesses. A missing context is reported rather than attributed to
    "whichever task ran last", for the identical reason `sdk.generic.current_agent` raises
    instead of guessing (PRD §8.8) — a wrong attribution here would misname the agent in a
    `schedule_decision` or `nondeterminism_warning` event, and wrong evidence is worse than
    an error (invariant I6, one layer up).

    Raises:
        TaskContextError: no scheduler task is ambient (`E-SCHED-001`).
    """
    task = _CURRENT_TASK.get()
    if task is None:
        detail = (
            "no ambient scheduler task context. This is reached only from inside a "
            "coroutine runtime/scheduler.py spawned; a coroutine invoked any other way "
            "(e.g. a raw asyncio.create_task outside the scheduler) has no scheduler "
            "identity to report"
        )
        raise TaskContextError(detail)
    return task


def active_task() -> SchedTaskContext | None:
    """Return the ambient scheduler task context, or None. Non-raising form of `current_task`."""
    return _CURRENT_TASK.get()


@contextmanager
def use_task(task: SchedTaskContext) -> Iterator[SchedTaskContext]:
    """Bind `task` as the ambient scheduler task for the duration of the block.

    Guarantees: restores the previous value on exit, including on an exception, so a task
    that raises cannot leave a stale context for the scheduler's own housekeeping code that
    resumes running immediately after it. `runtime/scheduler.py` wraps every spawned task's
    coroutine body in this exactly once, at the outermost level.
    """
    token: Token[SchedTaskContext | None] = _CURRENT_TASK.set(task)
    try:
        yield task
    finally:
        _CURRENT_TASK.reset(token)


__all__ = [
    "SchedTaskContext",
    "TaskContextError",
    "active_task",
    "current_task",
    "use_task",
]
