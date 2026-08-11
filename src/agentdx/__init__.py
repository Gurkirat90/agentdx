"""AgentDX — deterministic replay runtime, coordination debugger and chaos harness.

This module is the public API surface of PRD §8.2, and it is deliberately small. Five groups:

```python
import agentdx

# 1. Graph-level instrumentation — the one-line path
graph = agentdx.instrument(compiled_graph, name="code_pipeline")

# 2. Decorator path — for plain Python / non-LangGraph systems
@agentdx.agent("coder", role="worker")
async def coder(state: dict) -> dict: ...

@agentdx.tool("vector_search")
async def vector_search(query: str, k: int = 5) -> list[str]: ...

# 3. Explicit state access (only when state is not a LangGraph channel)
async with agentdx.state() as s:
    plan = await s.read("plan")
    await s.write("draft.module_a", value)

# 4. Synchronisation primitives — teach the race detector about your intent
async with agentdx.lock("draft.module_a"):
    ...
async with agentdx.transaction("plan_update") as txn:
    await txn.write("plan", p)

# 5. Programmatic run control (what the CLI calls)
result = await agentdx.run(graph, task="...", scenario="scenarios/x.yaml", seed=42)
```

Groups 1 and 2 require **zero changes to prompt or agent logic** (PRD §8.2's design
constraint). Groups 3 and 4 are optional refinements that *reduce* false positives; the
product is useful without them.

**Two symbols named elsewhere in the project are not here yet.** `agentdx.wall_time()` — the
single sanctioned real-clock accessor of AGENTS.md §4.1 clause 3 — and `agentdx.sorted_set()`
belong to the runtime, which lands at P06 (CONTEXT.md deviation D-16). Nothing in `sdk/`
needs either: the SDK reads time only through the injected `Clock`, and it sorts explicitly
at every emission site rather than iterating a set.

**`send`/`recv` are here because PRD §8.4 requires them.** Message passing in generic mode is
explicit, and that is a trade-off rather than an oversight: without an explicit send/recv pair
there is no happens-before edge between two agents, and PRD §14.3 makes messages the only
carrier of that edge. Shared-state access deliberately does not create one.

PRD §8.1 (package structure) · §8.2 (this surface, complete) · §8.4 · §8.6 · §8.7.
"""

from agentdx.config import (
    AgentDXConfig,
    ConfigError,
    LlmConfig,
    PrivacyConfig,
    RunConfig,
    StoreConfig,
)
from agentdx.sdk.decorators import agent, tool
from agentdx.sdk.generic import (
    AgentContext,
    AgentContextError,
    AttributeTypeError,
    CachedResponse,
    CacheMissError,
    Clock,
    FrozenClock,
    HookViolationError,
    ImmediateScheduler,
    InstrumentationError,
    InstrumentationGap,
    InstrumentationGapWarning,
    LifecycleHooks,
    LlmCache,
    ManualClock,
    NoCache,
    ProviderError,
    Recorder,
    RunContext,
    RunContextError,
    RunHost,
    RunResult,
    Scheduler,
    SdkError,
    Span,
    SpanRecord,
    StateHandle,
    UnsupportedTargetError,
    ValueRepresentationError,
    install_runtime,
    recv,
    run,
    send,
    state,
)
from agentdx.sdk.langgraph import InstrumentedGraph, instrument
from agentdx.sdk.sync import barrier, lock, transaction

__version__ = "0.1.0"
"""Recorded in `run_start.payload.agentdx_version` for provenance (PRD §10.10)."""

__all__ = [
    "AgentContext",
    "AgentContextError",
    "AgentDXConfig",
    "AttributeTypeError",
    "CacheMissError",
    "CachedResponse",
    "Clock",
    "ConfigError",
    "FrozenClock",
    "HookViolationError",
    "ImmediateScheduler",
    "InstrumentationError",
    "InstrumentationGap",
    "InstrumentationGapWarning",
    "InstrumentedGraph",
    "LifecycleHooks",
    "LlmCache",
    "LlmConfig",
    "ManualClock",
    "NoCache",
    "PrivacyConfig",
    "ProviderError",
    "Recorder",
    "RunConfig",
    "RunContext",
    "RunContextError",
    "RunHost",
    "RunResult",
    "Scheduler",
    "SdkError",
    "Span",
    "SpanRecord",
    "StateHandle",
    "StoreConfig",
    "UnsupportedTargetError",
    "ValueRepresentationError",
    "__version__",
    "agent",
    "barrier",
    "install_runtime",
    "instrument",
    "lock",
    "recv",
    "run",
    "send",
    "state",
    "tool",
    "transaction",
]
