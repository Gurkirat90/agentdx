"""The capture core: contexts, injected runtime protocols, spans, state and messages.

Everything the other SDK modules stand on lives here. Four decisions shape it, and each one
is load-bearing rather than stylistic.

**1. The SDK never stamps.** PRD §9.6 assigns `seq`, `sched_step`, `virtual_ts_ms`,
`wall_ts_ms`, `vclock`, `causal_parents` and `fault_id` under the scheduler lock, which is
P06's. So this module builds `events.DraftEvent`s and hands them to an injected `Recorder`,
which returns the `seq` it assigned. Nothing under `agentdx/sdk/` constructs an `Event` or a
`Stamp`; `tests/unit/sdk/test_sdk_never_stamps.py` asserts that against the AST rather than
against a promise, because a comment cannot survive a refactor and an AST check can.

**2. The SDK never reads a clock.** `AGENTS.md` §4.1 sanctions exactly four places that may
touch the real clock and `sdk/` is not one of them, so `wall_ts_ms` and
`payload.duration_wall_ms` — both volatile, both excluded from the canonical projection —
come from the injected `Clock`. The SDK asks; it does not read. That is what keeps invariant
I1 a property of the codebase rather than of this module's discipline.

**3. Attribution is a contextvar, and losing it is an error rather than a guess.** PRD §8.8
puts `RunContext` and `AgentContext` in `contextvars` so that `asyncio` tasks and
`asyncio.to_thread` handoffs inherit them without threading parameters through user code.
When a span-scoped event is emitted with no ambient `AgentContext`, this module raises
`E-INSTR-004` instead of attributing the event to a plausible agent. Wrong attribution does
not fail; it produces a confident wrong answer in every downstream analysis, which is the
worst thing this product can do.

**4. A capture hole is an event, not a silence.** `record_gap` writes an
`instrumentation_gap` event *and* raises a `InstrumentationGapWarning`, so a hole is visible
in the log the analysers read and in the console the user is watching. Fatal holes raise
(see `sdk/langgraph.py`).

PRD §8.1, §8.2, §8.4, §8.6, §8.8, §8.9, §8.11 · §6.1 (Agent, Span, Message, State
Operation) · §36 (`E-INSTR-*`). Error codes are documented in `docs/sdk.md`.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import warnings
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from hashlib import blake2b, sha1
from typing import ClassVar, Final, Protocol, runtime_checkable

from agentdx.config import AgentDXConfig
from agentdx.events.canonical import DIGEST_SIZE, HASH_PREFIX, encode_value
from agentdx.events.schema import DraftEvent, EventType, PayloadValue

_DOCS: Final = "docs/sdk.md"

ERROR_MESSAGE_LIMIT: Final = 512
"""PRD §8.9: `span_end.payload.error_message` is truncated to this many characters."""

REDACTION_TOKEN: Final = "[REDACTED]"  # noqa: S105  # a redaction marker, not a secret
"""What a `redact_patterns` match is replaced with, in errors and in captured bodies."""

SPAN_ID_LENGTH: Final = 12
"""PRD §8.8: span identity is `sha1(run_id ‖ agent_id ‖ span_seq)[:12]`."""

MISSING_VALUE_HASH: Final = HASH_PREFIX + "0" * (DIGEST_SIZE * 2)
"""The `value_hash` of a state key that does not exist.

`state_read.payload.value_hash` is required and not nullable, so a read of a missing key
still needs a string. An explicit all-zero digest is used rather than the hash of `null`,
so that "the key was absent" and "the key held null" are distinguishable in the log — two
facts a race detector must not confuse.
"""

_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")
"""Matches the address CPython's default `__repr__` embeds. A value whose representation
contains one cannot be hashed reproducibly, which is an I1 leak rather than an inconvenience.
"""


# ---------------------------------------------------------------------------------------
# Errors — every one carries an E-XXX-NNN code and a docs anchor (AGENTS.md §4)
# ---------------------------------------------------------------------------------------


class SdkError(Exception):
    """Base for every SDK failure. Carries a stable code and a docs anchor.

    Guarantees: `code` is part of the public contract — CI output and `agentdx doctor`
    branch on it — so renumbering one is a breaking change. The rendered message always
    names both the code and the remedy, per PRD §36 cross-cutting rule 3.
    """

    code: ClassVar[str] = "E-INSTR-000"

    def __init__(self, detail: str) -> None:
        """Build the error from a description of what went wrong and how to fix it."""
        super().__init__(f"[{self.code}] {detail} ({_DOCS}#{self.code.lower()})")


class InstrumentationError(SdkError):
    """The adapter could not attach to a framework construct it must capture.

    Carries `E-INSTR-002` (PRD §36). Raised — rather than warned — only when the missing
    binding would make the log *look* complete while being structurally incomplete: no node
    spans, or an unrecognised user channel whose reducer is therefore unknown. PRD §8.3
    names reducer awareness as the single highest-risk false-positive source in the product,
    so guessing it is not an option and continuing without it is not either.

    Every raise is preceded by an `instrumentation_gap` event, so the log records *what*
    could not be captured even though the run stops.
    """

    code: ClassVar[str] = "E-INSTR-002"


class RunContextError(SdkError):
    """An SDK call needs an active run and there is none.

    Carries `E-INSTR-003`. The SDK does not queue events for a run that may never start:
    a buffered event with no run is an event with no `seq`, and silently dropping it is
    exactly the partial capture this design exists to prevent.
    """

    code: ClassVar[str] = "E-INSTR-003"


class AgentContextError(SdkError):
    """A span-scoped event was emitted with no ambient agent to attribute it to.

    Carries `E-INSTR-004`. This is the guard behind PRD §8.8: `contextvars` propagate into
    `asyncio` tasks and `asyncio.to_thread` handoffs but not into a bare `threading.Thread`,
    so the loss is real and detectable. Attributing the event to the last agent seen would
    be a plausible-looking lie, and every downstream analysis would inherit it.
    """

    code: ClassVar[str] = "E-INSTR-004"


class AttributeTypeError(SdkError):
    """A user attached a value the event log cannot carry to a span attribute.

    Carries `E-INSTR-005`. Floats are the case this exists for: ADR-007 forbids them
    everywhere in the log because cross-platform float formatting is a determinism leak the
    canonical projection cannot normalise. A hard error here is deliberate — a silent
    coercion to int or str would move a number without telling anyone.
    """

    code: ClassVar[str] = "E-INSTR-005"


class UnsupportedTargetError(SdkError):
    """`instrument()` was handed an object it does not know how to capture.

    Carries `E-INSTR-006`. Named separately from `E-INSTR-002` because the remedy differs:
    this one means "use the decorator API", not "the adapter drifted".
    """

    code: ClassVar[str] = "E-INSTR-006"


class HookViolationError(SdkError):
    """A PRD §8.6 lifecycle hook emitted an event or wrote state.

    Carries `E-INSTR-007`. §8.6 requires hooks to be synchronous, to perform no I/O and to
    mutate nothing, "enforced by running them under a guard that raises on any event
    emission or state write". This is that guard.
    """

    code: ClassVar[str] = "E-INSTR-007"


class ValueRepresentationError(SdkError):
    """A value has no reproducible representation, so it cannot be hashed.

    Carries `E-INSTR-008`. The trigger is a default `__repr__`, which embeds the object's
    memory address: hashing it would make `value_hash` differ between two replays of the
    same run and turn gate G3 into a coin flip. Callers on the state path catch this and
    record an `instrumentation_gap` instead, so a user graph with an opaque object in state
    still runs — with the limitation stated.
    """

    code: ClassVar[str] = "E-INSTR-008"


class CacheMissError(SdkError):
    """A replay-mode LLM call missed the cache (invariant I7).

    Carries `E-CACHE-001` (PRD §36, exit code 3). There is no fallback to a live call and
    no flag that would enable one: a silent fallback makes CI non-hermetic, bundles
    unreproducible and cost unpredictable, all three at once (PRD §11.2).
    """

    code: ClassVar[str] = "E-CACHE-001"


class ProviderError(SdkError):
    """The provider refused or failed a call in `record` or `passthrough` mode.

    Carries `E-LLM-001` (PRD §36). The partial cache is retained; the run stops.
    """

    code: ClassVar[str] = "E-LLM-001"


class InstrumentationGapWarning(RuntimeWarning):
    """Raised alongside every `instrumentation_gap` event so the hole is visible live.

    The event makes the gap analysable; the warning makes it noticeable. PRD §36's first
    cross-cutting rule is "never fail silently", and a log entry nobody reads until analysis
    is, in practice, silence.
    """


# ---------------------------------------------------------------------------------------
# Injected runtime protocols — P06 and P07 supply the implementations
# ---------------------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """Virtual and wall time, both owned by the runtime (PRD §10.3, I11).

    Declared here rather than imported from `runtime/` so that `sdk/` can be tested with no
    scheduler in existence, and so the determinism rule stays mechanical: the SDK cannot
    read a real clock because it has no way to name one.
    """

    def virtual_ms(self) -> int:
        """Return milliseconds of *virtual* time elapsed since run start (I11)."""
        ...

    def wall_ms(self) -> int:
        """Return milliseconds of *wall* time elapsed since run start.

        Only ever written to fields on the PRD §10.7 exclusion list, so it never enters the
        canonical projection.
        """
        ...


@runtime_checkable
class Recorder(Protocol):
    """The stamping boundary, from the SDK's side (PRD §9.6 steps 2–4).

    The SDK produces `DraftEvent`s; an implementation of this protocol stamps, validates and
    enqueues them. P06 implements it under the scheduler lock.
    """

    def emit(self, draft: DraftEvent, causes: Sequence[int]) -> int:
        """Stamp, validate and enqueue one draft; return the `seq` it was assigned.

        `causes` are the seqs this event happens-after — a message's send, a lock's previous
        release. The implementation folds them into `causal_parents` and into the vector
        clock per PRD §14.2; the SDK supplies the causality because it is the only layer
        that knows it.
        """
        ...


@runtime_checkable
class Scheduler(Protocol):
    """The cooperative scheduler's yield point (PRD §8.5 item 4, §10.2).

    The provider shim yields around every model call so that concurrency is
    scheduler-visible even in passthrough mode, where nothing is being replayed.
    """

    async def yield_point(self, reason: str) -> None:
        """Give the scheduler an opportunity to run another task."""
        ...


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """One `llm_cache` row, as the shim sees it (PRD §11.6).

    Guarantees: `body` is the provider's response verbatim, so replay reproduces tool-call
    structures and `finish_reason`, not merely text.
    """

    body: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str | None = None
    duration_wall_ms: int | None = None
    recorded_run_id: str | None = None


@runtime_checkable
class LlmCache(Protocol):
    """The record/replay cache (PRD §11), implemented at P07."""

    def lookup(self, cache_key: str) -> CachedResponse | None:
        """Return the cached response for this key, or None on a miss."""
        ...

    def store(self, cache_key: str, response: CachedResponse) -> None:
        """Record a response against its key. Called only in `record` mode."""
        ...


# ---------------------------------------------------------------------------------------
# Defaults that hold the protocols' shape without pretending to be the runtime
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrozenClock:
    """A clock that does not advance. The correct default when no scheduler exists.

    Guarantees: returns 0 for both scales, always. That is not a placeholder — with no
    scheduler there *is* no virtual time to report, and inventing one from the wall clock
    would be the exact conflation invariant I11 forbids. P06 replaces it with
    `runtime/clock.py`; until then every duration in a P04-only log is honestly zero.
    """

    def virtual_ms(self) -> int:
        """Return 0: virtual time does not advance without a scheduler."""
        return 0

    def wall_ms(self) -> int:
        """Return 0: `sdk/` may not read a real clock (AGENTS.md §4.1)."""
        return 0


@dataclass(eq=False)
class ManualClock:
    """A clock advanced explicitly by the caller. Deterministic by construction.

    Guarantees: never reads a real clock, so a test that uses it produces the same durations
    on every machine. Both scales advance independently, which is what lets a test assert
    that the two are never conflated (I11).
    """

    virtual: int = 0
    wall: int = 0

    def virtual_ms(self) -> int:
        """Return the virtual time the caller has advanced to."""
        return self.virtual

    def wall_ms(self) -> int:
        """Return the wall time the caller has advanced to."""
        return self.wall

    def advance(self, *, virtual: int = 0, wall: int = 0) -> None:
        """Move both scales forward by the given non-negative amounts."""
        self.virtual += virtual
        self.wall += wall


@dataclass(frozen=True, slots=True)
class ImmediateScheduler:
    """A yield point that returns immediately — the no-scheduler default.

    Guarantees: introduces no ordering of its own, so a P04-only run executes in whatever
    order `asyncio` chooses and makes no determinism claim. P06's scheduler replaces it and
    is what makes I1 true; this exists so the shim's yield points are already in the code
    when it arrives, rather than being retrofitted through every call site.
    """

    async def yield_point(self, reason: str) -> None:
        """Return without yielding. Named `reason` for parity with the real scheduler."""
        return


@dataclass(frozen=True, slots=True)
class NoCache:
    """A cache that is always empty, and says so (PRD §11.7).

    Guarantees: `lookup` always misses and `store` always discards. In `replay` mode the
    shim turns that miss into `E-CACHE-001` and stops, which is the specified behaviour for
    a real empty cache too — so a run against this default fails in exactly the way I7
    requires rather than in a special "not implemented" way.
    """

    def lookup(self, cache_key: str) -> CachedResponse | None:
        """Return None: this cache holds nothing."""
        return None

    def store(self, cache_key: str, response: CachedResponse) -> None:
        """Discard the response. P07 implements persistence."""
        return


# ---------------------------------------------------------------------------------------
# Hashing and identity (PRD §8.8, §8.10)
# ---------------------------------------------------------------------------------------


def stable_text(value: object) -> str:
    """Return a reproducible textual form of any value, for hashing only.

    Guarantees: two structurally equal values produce the same text on any machine and any
    process. Mappings are emitted with sorted keys; sets are sorted by their own stable
    text, since set iteration order is not a contract (AGENTS.md §4.1).

    Raises:
        ValueRepresentationError: the value falls back to a default `__repr__`, which
            embeds a memory address and therefore cannot be reproduced (`E-INSTR-008`).
    """
    if value is None or isinstance(value, bool | int | str):
        payload: PayloadValue = value
        return encode_value(payload)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, bytes | bytearray):
        return '"bytes:' + bytes(value).hex() + '"'
    if isinstance(value, Mapping):
        items = sorted((str(k), v) for k, v in value.items())
        return "{" + ",".join(f"{encode_value(k)}:{stable_text(v)}" for k, v in items) + "}"
    if isinstance(value, frozenset | set):
        return "[" + ",".join(sorted(stable_text(v) for v in value)) + "]"
    if isinstance(value, Sequence):
        return "[" + ",".join(stable_text(v) for v in value) + "]"
    text = repr(value)
    if _ADDRESS.search(text):
        detail = (
            f"a value of type {type(value).__name__} has no reproducible representation "
            f"(its repr embeds a memory address), so hashing it would differ between "
            f"replays. Give the type a __repr__, or convert it before writing it to state"
        )
        raise ValueRepresentationError(detail)
    return encode_value(f"{type(value).__module__}.{type(value).__qualname__}:{text}")


def hash_text(text: str) -> str:
    """Return the `blake2b:` hash of a string, for prompt and response bodies.

    Guarantees: this is what invariant I8 stores in place of a body. It is a plain hash of
    the UTF-8 bytes with no salt, so two runs that sent the same prompt agree — which is
    what makes redundancy detection (PRD §16.3) possible without ever holding the text.
    """
    return HASH_PREFIX + blake2b(text.encode("utf-8"), digest_size=DIGEST_SIZE).hexdigest()


def span_id_for(run_id: str, agent_id: str, span_seq: int) -> str:
    """Return the span id for the `span_seq`-th span of an agent (PRD §8.8).

    Guarantees: `sha1(run_id ‖ agent_id ‖ span_seq)[:12]`, exactly as PRD §8.8 specifies —
    deterministic, and therefore stable across replays, which is what lets the UI keep a
    selection when a run is replayed. sha1 is used as a short identifier, never as a
    security primitive; content hashing uses blake2b (PRD §8.10).
    """
    material = f"{run_id}‖{agent_id}‖{span_seq}".encode()
    return sha1(material, usedforsecurity=False).hexdigest()[:SPAN_ID_LENGTH]


def message_id_for(run_id: str, sender: str, recipient: str, message_seq: int) -> str:
    """Return the id of the `message_seq`-th message in a run (PRD §6.1).

    Guarantees: deterministic and unique within a run. PRD §6.1 requires a `message_id` but
    does not specify its construction; this mirrors PRD §8.8's span identity so that both
    ids are derived from a counter rather than from `uuid4`, which AGENTS.md §4.1 bans.
    """
    material = f"{run_id}‖{sender}‖{recipient}‖{message_seq}".encode()
    return "m_" + sha1(material, usedforsecurity=False).hexdigest()[:SPAN_ID_LENGTH]


# ---------------------------------------------------------------------------------------
# Redaction and truncation (PRD §8.9, §8.11)
# ---------------------------------------------------------------------------------------


class Redactor:
    """Applies `[privacy] redact_patterns` to every string the SDK is about to write.

    Guarantees: `scrub` never raises. Its patterns were compiled when the configuration was
    resolved, and it is called while an error is being recorded — a redactor that failed
    there would destroy the error it was protecting.
    """

    def __init__(self, patterns: Sequence[str]) -> None:
        """Compile the patterns once. Invalid patterns are rejected by `config.py`."""
        self._patterns = tuple(re.compile(p) for p in patterns)

    def scrub(self, text: str) -> str:
        """Return `text` with every configured pattern replaced by `[REDACTED]`."""
        for pattern in self._patterns:
            text = pattern.sub(REDACTION_TOKEN, text)
        return text

    def scrub_error(self, text: str) -> str:
        """Return an error message redacted and truncated to PRD §8.9's 512 characters."""
        scrubbed = self.scrub(text)
        if len(scrubbed) <= ERROR_MESSAGE_LIMIT:
            return scrubbed
        return scrubbed[: ERROR_MESSAGE_LIMIT - 1] + "…"


# ---------------------------------------------------------------------------------------
# Lifecycle hooks (PRD §8.6)
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """What `on_span_end` receives: a closed span, already redacted.

    Guarantees: carries the seqs of both boundary events, so a hook that attributes custom
    timing has evidence references rather than wall-clock guesses (invariant I6).
    """

    span_id: str
    agent_id: str
    kind: str
    name: str
    status: str
    duration_virtual_ms: int
    duration_wall_ms: int
    start_seq: int
    end_seq: int


@dataclass(frozen=True, slots=True)
class RunResult:
    """What `agentdx.run` returns (PRD §8.2 item 5).

    Guarantees: `gaps` is the complete list of constructs the adapter could not capture, so
    a caller can refuse to trust an analysis rather than discovering the hole later (PRD
    §36 cross-cutting rule 2: partial results beat no results, *with their limits stated*).
    """

    run_id: str
    status: str
    output: object
    gaps: tuple[InstrumentationGap, ...] = ()


@dataclass(frozen=True, slots=True)
class LifecycleHooks:
    """The five PRD §8.6 hooks, as one injectable value.

    PRD §8.6 names the hooks and their guarantees but not how they are registered; passing
    this object to `instrument()` or `run()` is P04's answer and is recorded as a deviation.
    Every hook is optional.

    **Four of the five are invoked, and `on_fault` is not.** `on_run_start`,
    `on_agent_start`, `on_span_end` and `on_run_end` are each called through `call_hook`,
    which is the §8.6 guard: a hook that emits an event or writes state raises
    `E-INSTR-007` rather than corrupting the log it observes. `on_fault` fires
    "immediately after `fault_injected`" (PRD §8.6), and **nothing in this codebase injects
    a fault yet** — `runtime/faults/` is P09 and is empty. The field is declared here so the
    hook set is complete and the signature is fixed before P09 wires it; it has **no call
    site anywhere in the tree today**, and a user who passes one will never see it fire.
    Stated rather than implied, because a hook that is silently never called is
    indistinguishable from a fault that never happened. Building call sites for a module
    that does not exist would be a stub presented as done (`AGENTS.md` §2). See
    `docs/sdk.md` §2 and CONTEXT.md D-27.
    """

    on_run_start: Callable[[RunContext], None] | None = None
    on_agent_start: Callable[[RunContext, str], None] | None = None
    on_span_end: Callable[[RunContext, SpanRecord], None] | None = None
    on_fault: Callable[[RunContext, Mapping[str, PayloadValue]], None] | None = None
    on_run_end: Callable[[RunContext, RunResult], None] | None = None


# ---------------------------------------------------------------------------------------
# Instrumentation gaps (PRD §8.3, §36 E-INSTR-002)
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentationGap:
    """One construct the SDK could not capture, exactly as the event records it.

    Guarantees: `fatal` states whether the run was stopped. A non-fatal gap means the log is
    incomplete *in a named way*, which analysis can report; there is no third state in which
    the log is incomplete and nothing says so.
    """

    construct: str
    location: str
    reason: str
    fatal: bool = False


# ---------------------------------------------------------------------------------------
# Run and agent context (PRD §8.8)
# ---------------------------------------------------------------------------------------


@dataclass(eq=False)
class _RunRegistry:
    """The mutable bookkeeping one run needs: counters, state, mailboxes, locks.

    Separated from `RunContext` so the context itself can stay frozen — a run's identity,
    seed and mode must not be reassignable once a single event has been written.
    """

    span_seq: int = 0
    message_seq: int = 0
    txn_seq: int = 0
    agents: dict[str, int] = field(default_factory=dict)
    open_scopes: dict[str, int] = field(default_factory=dict)
    slot_seq: dict[str, int] = field(default_factory=dict)
    values: dict[str, object] = field(default_factory=dict)
    value_hashes: dict[str, str] = field(default_factory=dict)
    mailboxes: dict[str, asyncio.Queue[_Envelope]] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    lock_release_seq: dict[str, int] = field(default_factory=dict)
    lock_held_since: dict[str, int] = field(default_factory=dict)
    lock_holders: dict[str, str] = field(default_factory=dict)
    """Lock key → the `clock_slot` of the scope currently holding it.

    The holder's identity, not merely the fact that *someone* holds it: `lock_id` on a
    `state_write` is a claim that **this** write was made under that lock, and a global
    "is it held" check stamps an unsynchronised racer as protected — suppressing exactly
    the lost update the primitive exists to make findable.
    """
    barriers: dict[str, BarrierState] = field(default_factory=dict)
    gaps: list[InstrumentationGap] = field(default_factory=list)
    pending_deliveries: dict[str, deque[_Delivery]] = field(default_factory=dict)


@dataclass(eq=False)
class BarrierState:
    """One barrier's rendezvous state: who has arrived, and the gate that releases them.

    Guarantees: `released` is set exactly once, when the arrival count reaches the declared
    participant count. A barrier that never fills leaves its waiters blocked, which is a
    deadlock the scheduler reports as `E-SCHED-002` — deliberately, because a barrier that
    silently released early would be a coordination bug the tool invented.
    """

    expected: int
    arrived: int = 0
    released: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class _Envelope:
    """A generic-mode message in flight between `send` and `recv`."""

    message_id: str
    sender: str
    edge: str
    payload: object
    send_seq: int


@dataclass(frozen=True, slots=True)
class _Delivery:
    """A LangGraph edge traversal awaiting its `message_recv` at the consuming node."""

    message_id: str
    sender: str
    edge: str
    send_seq: int


@dataclass(frozen=True, slots=True)
class RunContext:
    """Everything one run needs, in a `contextvar` (PRD §8.8).

    Guarantees: immutable identity — `run_id`, `seed` and `mode` cannot change once a run
    has started, so a value recorded in `run_start` is the value that governed the whole
    run (PRD §10.10). The injected `recorder`, `clock`, `scheduler` and `cache` are the four
    runtime services the SDK does not implement; P06 and P07 supply the real ones.
    """

    run_id: str
    seed: int
    mode: str
    config: AgentDXConfig
    clock: Clock
    recorder: Recorder
    scheduler: Scheduler
    cache: LlmCache
    redactor: Redactor
    hooks: LifecycleHooks
    capture_bodies: bool
    registry: _RunRegistry

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        recorder: Recorder,
        config: AgentDXConfig | None = None,
        seed: int | None = None,
        mode: str | None = None,
        clock: Clock | None = None,
        scheduler: Scheduler | None = None,
        cache: LlmCache | None = None,
        hooks: LifecycleHooks | None = None,
        capture_bodies: bool | None = None,
    ) -> RunContext:
        """Build a run context, defaulting every runtime service that does not exist yet.

        Guarantees: `capture_bodies` defaults to the resolved `[privacy]` setting, which
        itself defaults to False (invariant I8). An explicit argument wins, because it is a
        deliberate act in the user's own code — the same position CLI flags occupy in the
        PRD §8.7 chain.

        Args:
            run_id: The run every event belongs to.
            recorder: The stamping boundary; P06 supplies the real one.
            config: Resolved configuration; loaded from `agentdx.toml` when omitted.
            seed: Overrides `[run] seed`.
            mode: Overrides `[run] mode`; one of `config.CACHE_MODES`.
            clock: Overrides the non-advancing default.
            scheduler: Overrides the immediate-return default.
            cache: Overrides the always-empty default.
            hooks: PRD §8.6 lifecycle hooks.
            capture_bodies: Overrides `[privacy] capture_bodies`.
        """
        resolved = AgentDXConfig.load() if config is None else config
        return cls(
            run_id=run_id,
            seed=resolved.run.seed if seed is None else seed,
            mode=resolved.run.mode if mode is None else mode,
            config=resolved,
            clock=FrozenClock() if clock is None else clock,
            recorder=recorder,
            scheduler=ImmediateScheduler() if scheduler is None else scheduler,
            cache=NoCache() if cache is None else cache,
            redactor=Redactor(resolved.privacy.redact_patterns),
            hooks=LifecycleHooks() if hooks is None else hooks,
            capture_bodies=(
                resolved.privacy.capture_bodies if capture_bodies is None else capture_bodies
            ),
            registry=_RunRegistry(),
        )

    @property
    def gaps(self) -> tuple[InstrumentationGap, ...]:
        """Return every construct this run could not capture, in discovery order."""
        return tuple(self.registry.gaps)


@dataclass(frozen=True, slots=True)
class AgentContext:
    """The agent an event belongs to, and its open span stack (PRD §8.8).

    Guarantees: immutable, so entering a span produces a *new* context rather than mutating
    the one a sibling `asyncio` task is holding. That is what makes concurrent spans within
    one agent correct rather than merely usually correct.
    """

    agent_id: str
    clock_slot: str
    spans: tuple[str, ...] = ()
    role: str | None = None

    @property
    def span_id(self) -> str | None:
        """Return the innermost open span, or None when no span is open."""
        return self.spans[-1] if self.spans else None


_RUN: ContextVar[RunContext | None] = ContextVar("agentdx_run", default=None)
_AGENT: ContextVar[AgentContext | None] = ContextVar("agentdx_agent", default=None)
_IN_HOOK: ContextVar[bool] = ContextVar("agentdx_in_hook", default=False)


def current_run() -> RunContext:
    """Return the active run context.

    Guarantees: never returns None. A caller that reaches this function has already decided
    it needs a run, so "there is no run" is an error rather than a value to check.

    Raises:
        RunContextError: no run is active (`E-INSTR-003`).
    """
    run = _RUN.get()
    if run is None:
        detail = (
            "no active AgentDX run. Instrumentation only records inside a run: call it "
            "under `agentdx.run(...)`, or pass an explicit context to "
            "`agentdx.instrument(..., context=...)`"
        )
        raise RunContextError(detail)
    return run


def active_run() -> RunContext | None:
    """Return the active run context, or None. The non-raising form of `current_run`."""
    return _RUN.get()


def current_agent() -> AgentContext:
    """Return the ambient agent context.

    Guarantees: never guesses. If the context was lost — a bare `threading.Thread`, a
    callback invoked outside the agent's scope — this raises instead of attributing the
    event to whichever agent ran last (PRD §8.8).

    Raises:
        AgentContextError: no agent context is ambient (`E-INSTR-004`).
    """
    agent = _AGENT.get()
    if agent is None:
        detail = (
            "no ambient agent context, so this event cannot be attributed. contextvars "
            "propagate into asyncio tasks and `asyncio.to_thread`, but not into a bare "
            "`threading.Thread` — hand work off with `asyncio.to_thread`, or wrap the "
            "callable with `contextvars.copy_context().run`"
        )
        raise AgentContextError(detail)
    return agent


def active_agent() -> AgentContext | None:
    """Return the ambient agent context, or None. The non-raising form of `current_agent`."""
    return _AGENT.get()


@contextmanager
def use_run(run: RunContext) -> Iterator[RunContext]:
    """Bind `run` as the active run for the duration of the block.

    Guarantees: restores the previous value on exit, including on an exception, so a nested
    or failed run cannot leave a stale context behind for the next one.
    """
    token: Token[RunContext | None] = _RUN.set(run)
    try:
        yield run
    finally:
        _RUN.reset(token)


@contextmanager
def use_agent(agent: AgentContext) -> Iterator[AgentContext]:
    """Bind `agent` as the ambient agent for the duration of the block."""
    token: Token[AgentContext | None] = _AGENT.set(agent)
    try:
        yield agent
    finally:
        _AGENT.reset(token)


# ---------------------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------------------


def emit(
    run: RunContext,
    event_type: EventType,
    payload: Mapping[str, PayloadValue],
    *,
    agent_id: str | None = None,
    clock_slot: str | None = None,
    span_id: str | None = None,
    causes: Sequence[int] = (),
) -> int:
    """Build a `DraftEvent` and hand it to the injected recorder; return its `seq`.

    This is the only place in `sdk/` that constructs an event of any kind, and it
    constructs the *unstamped* one. `seq`, `sched_step`, both timestamps, the vector clock,
    `causal_parents` and `fault_id` are the recorder's to assign (PRD §9.6 step 2).

    Guarantees: refuses to emit from inside a PRD §8.6 lifecycle hook, which is the guard
    §8.6 requires.

    Raises:
        HookViolationError: called from inside a lifecycle hook (`E-INSTR-007`).
    """
    if _IN_HOOK.get():
        detail = (
            f"a lifecycle hook tried to emit a {event_type.value} event. PRD §8.6 hooks "
            f"observe a run; they do not participate in it — move the emission into the "
            f"instrumented code itself"
        )
        raise HookViolationError(detail)
    draft = DraftEvent(
        type=event_type,
        payload=payload,
        agent_id=agent_id,
        clock_slot=clock_slot,
        span_id=span_id,
    )
    return run.recorder.emit(draft, tuple(causes))


def record_gap(
    run: RunContext | None,
    construct: str,
    location: str,
    reason: str,
    *,
    fatal: bool = False,
) -> InstrumentationGap:
    """Record a capture hole as an event, a warning and a run-level fact (PRD §36).

    Guarantees: all three at once. The `instrumentation_gap` event makes the hole visible to
    the analysers that will later be asked whether the log is complete; the warning makes it
    visible to the person watching the run; the entry in `run.gaps` makes it visible in the
    run summary and in `RunResult`. Silently doing fewer than three is the failure mode this
    function exists to make impossible.

    When `run` is None the event cannot be written — there is no recorder — so the warning
    and the returned value are all the caller gets, and the caller is expected to raise.
    """
    gap = InstrumentationGap(construct=construct, location=location, reason=reason, fatal=fatal)
    if run is not None:
        run.registry.gaps.append(gap)
        emit(
            run,
            EventType.INSTRUMENTATION_GAP,
            {"construct": construct, "location": location, "reason": reason},
        )
    warnings.warn(
        f"[{InstrumentationError.code}] {construct} at {location} was not instrumented: "
        f"{reason}. Analysis over this run will be incomplete in that respect.",
        InstrumentationGapWarning,
        stacklevel=3,
    )
    return gap


def call_hook(run: RunContext, hook: Callable[[], None] | None) -> None:
    """Run a PRD §8.6 lifecycle hook under the guard §8.6 requires.

    Guarantees: while the hook runs, any attempt to emit an event or write state raises
    `E-INSTR-007`. The flag is reset even if the hook raises, so one misbehaving hook cannot
    disable instrumentation for the rest of the run.
    """
    if hook is None:
        return
    token = _IN_HOOK.set(True)
    try:
        hook()
    finally:
        _IN_HOOK.reset(token)


# ---------------------------------------------------------------------------------------
# Attributes (ADR-007)
# ---------------------------------------------------------------------------------------


def check_attributes(attributes: Mapping[str, object] | None) -> dict[str, PayloadValue]:
    """Return user-supplied span attributes, rejecting anything the log cannot carry.

    Guarantees: the returned mapping contains only `str`, `int`, `bool`, `None`, and
    arrays/objects of those. A float raises rather than being coerced — ADR-007 forbids
    floats everywhere in the event log because cross-platform float formatting is a
    determinism leak the canonical projection cannot normalise, and a silent coercion to
    int would move the user's number without telling them.

    Raises:
        AttributeTypeError: a value is a float, or is not a permitted payload value
            (`E-INSTR-005`).
    """
    if not attributes:
        return {}
    out: dict[str, PayloadValue] = {}
    for key in sorted(attributes):
        out[str(key)] = _check_attribute_value(attributes[key], str(key))
    return out


def redact_attributes(redactor: Redactor, value: PayloadValue) -> PayloadValue:
    """Return `value` with every `redact_patterns` match in every string replaced.

    Guarantees: applied to span attributes as well as to bodies, and applied under the
    *default* configuration — attributes are not gated on `capture_bodies`. That matters
    because `span_start.payload.attributes` is an open user-supplied map, is marked STABLE
    (so it is inside the canonical projection and every event hash), and is exported in
    bundles by default. An API key pasted into an attribute would otherwise be the one
    plaintext route the PRD §8.11 opt-in never covered — strengthening I8 rather than
    reinterpreting it (CONTEXT.md D-28).

    Keys are left alone: a redacted key would silently rename the user's attribute, and the
    resulting log would claim a field name that was never set.
    """
    if isinstance(value, str):
        return redactor.scrub(value)
    if isinstance(value, Mapping):
        return {key: redact_attributes(redactor, item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [redact_attributes(redactor, item) for item in value]
    return value


def _check_attribute_value(value: object, path: str) -> PayloadValue:
    """Return `value` if the event log may carry it.

    Raises:
        AttributeTypeError: the value is a float or an unsupported type (`E-INSTR-005`).
    """
    if isinstance(value, float):
        detail = (
            f"attribute {path!r} is a float ({value!r}). Floats are forbidden everywhere in "
            f"the event log (ADR-007): cross-platform float formatting is a determinism leak "
            f"the canonical projection cannot normalise. Use integer milliseconds, integer "
            f"per-mille, or a string"
        )
        raise AttributeTypeError(detail)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Mapping):
        return {str(k): _check_attribute_value(v, f"{path}.{k}") for k, v in sorted(value.items())}
    if isinstance(value, Sequence):
        return [_check_attribute_value(v, f"{path}[{i}]") for i, v in enumerate(value)]
    detail = (
        f"attribute {path!r} has type {type(value).__name__}, which the event log cannot "
        f"carry. Permitted: str, int, bool, null, and arrays or objects of those"
    )
    raise AttributeTypeError(detail)


# ---------------------------------------------------------------------------------------
# Spans (PRD §8.4 step 2, §8.9)
# ---------------------------------------------------------------------------------------


@dataclass(eq=False)
class Span:
    """A span while it is open. Mutable only in the fields the closing event needs."""

    span_id: str
    kind: str
    name: str
    agent_id: str
    start_seq: int
    started_virtual_ms: int
    started_wall_ms: int
    status: str = "ok"
    error_type: str | None = None
    error_message: str | None = None


def _status_for(exc: BaseException) -> str:
    """Return the PRD §8.9 span status that corresponds to an exception."""
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, Exception):
        return "error"
    return "crashed"


def _begin_span(
    run: RunContext,
    agent: AgentContext,
    kind: str,
    name: str,
    attributes: Mapping[str, object] | None,
    causes: Sequence[int],
) -> tuple[Span, Token[AgentContext | None]]:
    """Emit `span_start` and push the span onto the ambient agent's stack.

    Returns the open span and the contextvar token `_finish_span` must reset.
    """
    registry = run.registry
    registry.span_seq += 1
    span_id = span_id_for(run.run_id, agent.agent_id, registry.span_seq)
    start_seq = emit(
        run,
        EventType.SPAN_START,
        {
            "kind": kind,
            "name": name,
            "parent_span_id": agent.span_id,
            "attributes": redact_attributes(run.redactor, check_attributes(attributes)),
        },
        agent_id=agent.agent_id,
        clock_slot=agent.clock_slot,
        span_id=span_id,
        causes=causes,
    )
    open_span = Span(
        span_id=span_id,
        kind=kind,
        name=name,
        agent_id=agent.agent_id,
        start_seq=start_seq,
        started_virtual_ms=run.clock.virtual_ms(),
        started_wall_ms=run.clock.wall_ms(),
    )
    token = _AGENT.set(
        AgentContext(
            agent_id=agent.agent_id,
            clock_slot=agent.clock_slot,
            spans=(*agent.spans, span_id),
            role=agent.role,
        )
    )
    return open_span, token


def _finish_span(
    run: RunContext,
    agent: AgentContext,
    open_span: Span,
    token: Token[AgentContext | None],
    exc: BaseException | None,
) -> None:
    """Restore the agent context and emit `span_end` exactly once."""
    _AGENT.reset(token)
    if exc is not None:
        open_span.status = _status_for(exc)
        open_span.error_type = type(exc).__name__
        open_span.error_message = run.redactor.scrub_error(str(exc) or type(exc).__name__)
    end_seq = emit(
        run,
        EventType.SPAN_END,
        {
            "status": open_span.status,
            "duration_virtual_ms": run.clock.virtual_ms() - open_span.started_virtual_ms,
            "duration_wall_ms": run.clock.wall_ms() - open_span.started_wall_ms,
            "error_type": open_span.error_type,
            "error_message": open_span.error_message,
        },
        agent_id=agent.agent_id,
        clock_slot=agent.clock_slot,
        span_id=open_span.span_id,
        causes=(open_span.start_seq,),
    )
    if run.hooks.on_span_end is not None:
        call_hook(run, lambda: _fire_span_end(run, open_span, end_seq))


@asynccontextmanager
async def span(
    kind: str,
    name: str,
    *,
    attributes: Mapping[str, object] | None = None,
    causes: Sequence[int] = (),
) -> AsyncIterator[Span]:
    """Open a span, yield it, and close it — emitting `span_start` and `span_end`.

    Guarantees: `span_end` is emitted exactly once, on every exit path including an
    exception, a cancellation and a timeout, with the PRD §8.9 status that corresponds to
    what happened. **The exception always propagates unchanged** — instrumentation never
    swallows an error (PRD §8.9). `error_message` is redacted and truncated to 512
    characters; the traceback is not recorded (see `docs/sdk.md`).

    Raises:
        RunContextError: no run is active (`E-INSTR-003`).
        AgentContextError: no agent context is ambient (`E-INSTR-004`).
        AttributeTypeError: an attribute value is not loggable (`E-INSTR-005`).
    """
    run = current_run()
    agent = current_agent()
    open_span, token = _begin_span(run, agent, kind, name, attributes, causes)
    try:
        yield open_span
    except BaseException as exc:
        _finish_span(run, agent, open_span, token, exc)
        raise
    else:
        _finish_span(run, agent, open_span, token, None)


@contextmanager
def sync_span(
    kind: str,
    name: str,
    *,
    attributes: Mapping[str, object] | None = None,
    causes: Sequence[int] = (),
) -> Iterator[Span]:
    """The synchronous form of `span`, for plain-function nodes and tools.

    Guarantees: identical events, identical ordering and identical failure modes to `span`.
    Both delegate to the same two helpers, so the two paths cannot drift into emitting
    different logs for the same work — which is exactly the drift that would make a
    sync-node graph and an async-node graph incomparable.
    """
    run = current_run()
    agent = current_agent()
    open_span, token = _begin_span(run, agent, kind, name, attributes, causes)
    try:
        yield open_span
    except BaseException as exc:
        _finish_span(run, agent, open_span, token, exc)
        raise
    else:
        _finish_span(run, agent, open_span, token, None)


def _fire_span_end(run: RunContext, open_span: Span, end_seq: int) -> None:
    """Invoke `on_span_end` with a closed, redacted `SpanRecord`."""
    hook = run.hooks.on_span_end
    if hook is None:  # pragma: no cover - guarded by the caller
        return
    hook(
        run,
        SpanRecord(
            span_id=open_span.span_id,
            agent_id=open_span.agent_id,
            kind=open_span.kind,
            name=open_span.name,
            status=open_span.status,
            duration_virtual_ms=run.clock.virtual_ms() - open_span.started_virtual_ms,
            duration_wall_ms=run.clock.wall_ms() - open_span.started_wall_ms,
            start_seq=open_span.start_seq,
            end_seq=end_seq,
        ),
    )


def _enter_agent(
    run: RunContext, agent_id: str, role: str | None, clock_slot: str | None = None
) -> tuple[AgentContext, Token[AgentContext | None]]:
    """Register an agent, allocate its clock slot and bind its context.

    **Concurrent scopes for the same agent receive derived clock slots** — `coder#1`,
    `coder#2` — so that an agent racing itself is detectable (PRD §8.8, §14.2). The first
    scope keeps the bare `agent_id`; a scope opened while another is still open gets the
    next index. The index comes from a counter, never from task or thread identity, so it is
    reproducible across replays.

    An explicit `clock_slot` overrides that allocation. The LangGraph adapter needs it: two
    *different nodes* mapped onto one agent id run in the same Pregel superstep and are
    concurrent by that framework's own semantics, yet their scopes do not overlap in wall
    order, so the counter above would put both on one slot and impose a total order the run
    did not have. Their happens-before, where there is one, is carried by the message events
    of PRD §8.3 binding 4 — never by slot identity.
    """
    registry = run.registry
    first_use = agent_id not in registry.agents
    if first_use:
        registry.agents[agent_id] = len(registry.agents)

    open_count = registry.open_scopes.get(agent_id, 0)
    if clock_slot is not None:
        slot = clock_slot
    elif open_count == 0:
        slot = agent_id
    else:
        registry.slot_seq[agent_id] = registry.slot_seq.get(agent_id, 0) + 1
        slot = f"{agent_id}#{registry.slot_seq[agent_id]}"
    registry.open_scopes[agent_id] = open_count + 1

    if first_use and run.hooks.on_agent_start is not None:
        call_hook(run, lambda: _fire_agent_start(run, agent_id))

    context = AgentContext(agent_id=agent_id, clock_slot=slot, role=role)
    return context, _AGENT.set(context)


def _exit_agent(run: RunContext, agent_id: str, token: Token[AgentContext | None]) -> None:
    """Release an agent's scope and restore the previous ambient context."""
    registry = run.registry
    registry.open_scopes[agent_id] = registry.open_scopes.get(agent_id, 1) - 1
    _AGENT.reset(token)


@asynccontextmanager
async def agent_scope(
    agent_id: str,
    *,
    name: str | None = None,
    role: str | None = None,
    attributes: Mapping[str, object] | None = None,
    causes: Sequence[int] = (),
    clock_slot: str | None = None,
) -> AsyncIterator[Span]:
    """Establish an `AgentContext` and open the agent's `agent_step` span (PRD §8.4).

    Guarantees:

    * The agent is registered in the run's agent set on first use, which is what allocates
      its vector-clock slot (PRD §8.4 step 3).
    * Concurrent scopes for the same agent receive derived clock slots (see `_enter_agent`),
      unless `clock_slot` names one explicitly.
    * Nested `@agentdx.tool` calls and provider-shim LLM calls inside the block attribute to
      this agent automatically (PRD §8.4 step 4).

    Raises:
        RunContextError: no run is active (`E-INSTR-003`).
    """
    run = current_run()
    context, token = _enter_agent(run, agent_id, role, clock_slot)
    try:
        async with span(
            "agent_step", name or agent_id, attributes=attributes, causes=causes
        ) as open_span:
            yield open_span
    finally:
        _exit_agent(run, context.agent_id, token)


@contextmanager
def sync_agent_scope(
    agent_id: str,
    *,
    name: str | None = None,
    role: str | None = None,
    attributes: Mapping[str, object] | None = None,
    causes: Sequence[int] = (),
    clock_slot: str | None = None,
) -> Iterator[Span]:
    """The synchronous form of `agent_scope`, for plain-function agents and nodes."""
    run = current_run()
    context, token = _enter_agent(run, agent_id, role, clock_slot)
    try:
        with sync_span(
            "agent_step", name or agent_id, attributes=attributes, causes=causes
        ) as open_span:
            yield open_span
    finally:
        _exit_agent(run, context.agent_id, token)


def _fire_agent_start(run: RunContext, agent_id: str) -> None:
    """Invoke `on_agent_start` for an agent's first span."""
    hook = run.hooks.on_agent_start
    if hook is not None:
        hook(run, agent_id)


# ---------------------------------------------------------------------------------------
# Shared state (PRD §8.2 item 3)
# ---------------------------------------------------------------------------------------


def state_facts(run: RunContext, key: str, value: object) -> tuple[str, str | None]:
    """Return `(value_hash, stable_text)` for a state value, degrading loudly if it has none.

    Guarantees: never raises. A value with no reproducible representation produces an
    `instrumentation_gap`, the all-zero digest and no text, so the run continues and the log
    says exactly which key it cannot vouch for — rather than either aborting a user's graph
    or recording a hash that would differ on the next replay. The text is computed once and
    reused for the optional body, so `capture_bodies=True` costs one extra assignment rather
    than a second traversal.
    """
    try:
        text = stable_text(value)
    except ValueRepresentationError as exc:
        record_gap(run, "state_value", key, str(exc))
        return MISSING_VALUE_HASH, None
    return hash_text(text), text


class StateHandle:
    """The object `async with agentdx.state() as s` yields (PRD §8.2 item 3).

    Guarantees: every `read` emits `state_read` and every `write` emits `state_write`, with
    the hashes a race detector needs — `value_hash` and `prev_value_hash` — and never the
    body unless `capture_bodies` is on (invariant I8). Reads and writes here create **no**
    happens-before edge, which is PRD §14.3's rule and the reason race detection works at
    all: shared state is the thing being raced over, not a synchronisation mechanism.
    """

    def __init__(self, run: RunContext, txn_id: str | None = None) -> None:
        """Bind a handle to a run, optionally inside a transaction."""
        self._run = run
        self._txn_id = txn_id
        self._buffered: list[tuple[str, object]] = []

    async def read(self, key: str) -> object:
        """Return the current value of `key`, emitting `state_read`.

        Guarantees: a missing key returns None and is recorded with `missing=true` and the
        all-zero `value_hash`, so "absent" and "present but null" stay distinguishable.

        Raises:
            AgentContextError: no ambient agent to attribute the read to (`E-INSTR-004`).
        """
        run = self._run
        agent = current_agent()
        registry = run.registry
        missing = key not in registry.values
        value = None if missing else registry.values[key]
        value_hash, text = (MISSING_VALUE_HASH, None) if missing else state_facts(run, key, value)
        payload: dict[str, PayloadValue] = {
            "key": key,
            "value_hash": value_hash,
            "missing": missing,
        }
        if run.capture_bodies and text is not None:
            payload["value"] = run.redactor.scrub(text)
        emit(
            run,
            EventType.STATE_READ,
            payload,
            agent_id=agent.agent_id,
            clock_slot=agent.clock_slot,
            span_id=_require_span(agent, "state_read"),
        )
        return value

    async def write(self, key: str, value: object, *, reducer: str | None = None) -> None:
        """Set `key` to `value`, emitting `state_write`.

        Inside a transaction the write is buffered and applied at commit, so a rolled-back
        transaction leaves no `state_write` in the log — a write that never took effect must
        not appear as one, or the race detector will report a conflict that never happened.

        Raises:
            AgentContextError: no ambient agent to attribute the write to (`E-INSTR-004`).
            HookViolationError: called from inside a lifecycle hook (`E-INSTR-007`).
        """
        if _IN_HOOK.get():
            detail = (
                f"a lifecycle hook tried to write state key {key!r}. PRD §8.6 hooks must "
                f"not mutate state — they observe a run, they do not participate in it"
            )
            raise HookViolationError(detail)
        if self._txn_id is not None:
            self._buffered.append((key, value))
            return
        self._apply(key, value, reducer)

    def _apply(self, key: str, value: object, reducer: str | None) -> int:
        """Apply one write and emit its `state_write`. Returns the event's seq."""
        run = self._run
        agent = current_agent()
        registry = run.registry
        prev_hash = registry.value_hashes.get(key)
        value_hash, text = state_facts(run, key, value)
        payload: dict[str, PayloadValue] = {
            "key": key,
            "value_hash": value_hash,
            "prev_value_hash": prev_hash,
            "reducer": reducer,
            "txn_id": self._txn_id,
            "lock_id": _held_lock(run, key, agent),
        }
        if run.capture_bodies and text is not None:
            payload["value"] = run.redactor.scrub(text)
        seq = emit(
            run,
            EventType.STATE_WRITE,
            payload,
            agent_id=agent.agent_id,
            clock_slot=agent.clock_slot,
            span_id=_require_span(agent, "state_write"),
        )
        registry.values[key] = value
        registry.value_hashes[key] = value_hash
        return seq

    def commit(self) -> None:
        """Apply every buffered write of a transaction, in the order they were made.

        Guarantees: the writes are emitted contiguously and all carry the same `txn_id`, so
        the race detector can treat the group as one intent rather than as N unrelated
        conflicts (PRD §14.5). Called by `agentdx.transaction` on clean exit.
        """
        for key, value in self._buffered:
            self._apply(key, value, None)
        self._buffered.clear()

    def rollback(self) -> None:
        """Discard every buffered write. Nothing is emitted.

        Guarantees: a rolled-back transaction leaves no `state_write` in the log at all.
        A write that never took effect must not appear as one, or every analyser downstream
        reports a conflict over a value nobody ever wrote.
        """
        self._buffered.clear()


def _require_span(agent: AgentContext, event_type: str) -> str:
    """Return the innermost open span id, or explain why the event cannot be attributed.

    Raises:
        AgentContextError: no span is open (`E-INSTR-004`).
    """
    span_id = agent.span_id
    if span_id is None:
        detail = (
            f"a {event_type} event needs an open span, and agent {agent.agent_id!r} has "
            f"none. Span-scoped events are emitted from inside `@agentdx.agent`, "
            f"`@agentdx.tool` or an instrumented node — not from module scope"
        )
        raise AgentContextError(detail)
    return span_id


def _held_lock(run: RunContext, key: str, writer: AgentContext) -> str | None:
    """Return the lock id covering `key`, but only when `writer` is the one holding it.

    Guarantees: never attributes a lock to a writer that does not hold it. The identity
    compared is the holder's `clock_slot`, which is stable for the whole of an agent scope
    (entering a span preserves it) and distinguishes an agent from a concurrent scope of
    itself. A writer that never acquired the lock gets `None`, which is the truth: PRD
    §14.7's lost-update rule treats a `lock_id` as evidence the write was declared-protected,
    and evidence that is wrong is worse than evidence that is absent.
    """
    holder = run.registry.lock_holders.get(key)
    return key if holder is not None and holder == writer.clock_slot else None


@asynccontextmanager
async def state() -> AsyncIterator[StateHandle]:
    """Open an explicit shared-state scope (PRD §8.2 item 3).

    Needed only when state is not a LangGraph channel: the LangGraph adapter captures
    channel reads and writes without any code change, and PRD §8.2's design constraint is
    that items 1 and 2 require none.

    Raises:
        RunContextError: no run is active (`E-INSTR-003`).
    """
    yield StateHandle(current_run())


# ---------------------------------------------------------------------------------------
# Explicit message passing (PRD §8.4)
# ---------------------------------------------------------------------------------------


def _mailbox(run: RunContext, agent_id: str) -> asyncio.Queue[_Envelope]:
    """Return (creating on first use) the mailbox of one agent."""
    box = run.registry.mailboxes.get(agent_id)
    if box is None:
        box = asyncio.Queue()
        run.registry.mailboxes[agent_id] = box
    return box


async def send(to: str, payload: object) -> str:
    """Send a message to another agent, emitting `message_send` (PRD §8.4).

    This is the one place generic mode requires code the user would not otherwise write, and
    it is required rather than convenient: without an explicit send/recv pair there is no
    happens-before edge between two agents, and PRD §14.3 makes messages the **only** carrier
    of that edge. Shared-state access deliberately does not create one. Without it, race
    detection cannot distinguish "concurrent" from "ordered" and has nothing to work with.

    Returns:
        The `message_id`, which `recv` reports on the receiving side.

    Raises:
        RunContextError: no run is active (`E-INSTR-003`).
        AgentContextError: no ambient agent or no open span (`E-INSTR-004`).
    """
    run = current_run()
    agent = current_agent()
    registry = run.registry
    registry.message_seq += 1
    message_id = message_id_for(run.run_id, agent.agent_id, to, registry.message_seq)
    edge = f"{agent.agent_id}->{to}"
    body = stable_text(payload)
    send_seq = emit(
        run,
        EventType.MESSAGE_SEND,
        {
            "message_id": message_id,
            "to": to,
            "edge": edge,
            "payload_hash": hash_text(body),
            "payload_bytes": len(body.encode("utf-8")),
        },
        agent_id=agent.agent_id,
        clock_slot=agent.clock_slot,
        span_id=_require_span(agent, "message_send"),
    )
    await _mailbox(run, to).put(
        _Envelope(
            message_id=message_id,
            sender=agent.agent_id,
            edge=edge,
            payload=payload,
            send_seq=send_seq,
        )
    )
    return message_id


async def recv() -> object:
    """Await the next message for this agent, emitting `message_recv` (PRD §8.4).

    Guarantees: the `message_recv` event names its `message_send` as a causal parent, which
    is the happens-before edge PRD §14.3 requires and the reason `send`/`recv` are explicit
    in generic mode. Messages are delivered FIFO per recipient.

    Raises:
        RunContextError: no run is active (`E-INSTR-003`).
        AgentContextError: no ambient agent or no open span (`E-INSTR-004`).
    """
    run = current_run()
    agent = current_agent()
    span_id = _require_span(agent, "message_recv")
    envelope = await _mailbox(run, agent.agent_id).get()
    emit(
        run,
        EventType.MESSAGE_RECV,
        {
            "message_id": envelope.message_id,
            "from": envelope.sender,
            "edge": envelope.edge,
            "delivered_virtual_ts_ms": run.clock.virtual_ms(),
            "reordered": False,
            "duplicate": False,
        },
        agent_id=agent.agent_id,
        clock_slot=agent.clock_slot,
        span_id=span_id,
        causes=(envelope.send_seq,),
    )
    return envelope.payload


# ---------------------------------------------------------------------------------------
# Read-only views used by the LangGraph adapter
# ---------------------------------------------------------------------------------------


@runtime_checkable
class RunHost(Protocol):
    """Opens and seals a run. The half of `agentdx.run` that P06 owns.

    The SDK cannot open a run by itself and should not pretend otherwise: `run_start` carries
    `host`, `pid`, `started_at_utc` and `env` (PRD §9.2), every one of which needs the real
    clock and the real environment, and AGENTS.md §4.1 does not sanction `sdk/` to read
    either. `run_end` needs the makespan, which needs the virtual clock. So the SDK asks a
    host to open the run, records everything in between, and asks the host to seal it.
    """

    async def open_run(self, *, task: str, scenario: str | None, seed: int | None) -> RunContext:
        """Create the run record, emit `run_start`, and return its context."""
        ...

    async def close_run(self, context: RunContext, *, status: str, output: object) -> RunResult:
        """Emit `run_end`, seal the log, and return the result."""
        ...


_HOST: RunHost | None = None


def install_runtime(host: RunHost | None) -> RunHost | None:
    """Install the process-wide runtime host and return the one it replaced.

    This is the seam PRD §8.2's `agentdx.run(graph, task=...)` needs to be callable with no
    extra arguments: something has to know how to open a run, and until P06 exists nothing
    does. `cli/` installs the real host once at start-up; a test installs a fake. It is
    deliberately explicit rather than an import-time side effect — an SDK that silently
    acquired a runtime by being imported would make "was this recorded?" unanswerable.

    Returns:
        The previously installed host, so a caller can restore it.
    """
    global _HOST
    previous = _HOST
    _HOST = host
    return previous


async def run(
    graph: object,
    *,
    task: str,
    scenario: str | None = None,
    seed: int | None = None,
    graph_input: object = None,
    host: RunHost | None = None,
) -> RunResult:
    """Execute an instrumented graph under a run, and return its result (PRD §8.2 item 5).

    This is what the CLI calls. It opens a run through the injected host, binds the run
    context so every SDK call inside the graph records into it, invokes the graph, and seals
    the log — emitting the PRD §8.6 `on_run_start` and `on_run_end` hooks around it.

    Guarantees:

    * The run is sealed on every exit path. A crashed run is still a run: PRD §36's second
      cross-cutting rule is that partial results beat no results, and an unsealed log is
      neither analysable nor honestly incomplete.
    * `RunResult.gaps` carries every construct the adapter could not capture, so a caller can
      refuse to trust an analysis rather than discovering the hole later.

    Args:
        graph: An object returned by `agentdx.instrument()`, or any awaitable/callable.
        task: The task text. PRD §8.2 shows it as a string and does not say how it reaches a
            graph's state; when `graph_input` is omitted the graph is invoked with
            `{"task": task}`. See `docs/sdk.md` — this is a derived decision, not the PRD's.
        scenario: Path to a scenario file. Interpreted by the host (P08).
        seed: Overrides `[run] seed`.
        graph_input: The exact input to hand the graph, bypassing the `{"task": ...}` default.
        host: The runtime host. Falls back to the one `install_runtime` installed.

    Raises:
        RunContextError: no host is installed and none was passed (`E-INSTR-003`).
    """
    active = host if host is not None else _HOST
    if active is None:
        detail = (
            "no AgentDX runtime is installed, so there is nothing that can open a run. "
            "`agentdx.run()` records; the scheduler, the clock and the cache that a run "
            "needs are supplied by the runtime (P06/P07). Install one with "
            "`agentdx.install_runtime(host)`, or pass `host=`"
        )
        raise RunContextError(detail)

    context = await active.open_run(task=task, scenario=scenario, seed=seed)
    payload = {"task": task} if graph_input is None else graph_input
    status = "complete"
    output: object = None
    with use_run(context):
        call_hook(
            context,
            None if context.hooks.on_run_start is None else lambda: _fire_run_start(context),
        )
        try:
            output = await _invoke(graph, payload)
        except BaseException:
            status = "failed"
            raise
        finally:
            result = await active.close_run(context, status=status, output=output)
            call_hook(
                context,
                None
                if context.hooks.on_run_end is None
                else lambda: _fire_run_end(context, result),
            )
    return result


async def _invoke(graph: object, payload: object) -> object:
    """Invoke a graph, whatever shape it is.

    Guarantees: prefers `ainvoke`, then `invoke`, then calling the object. Duck-typed rather
    than isinstance-checked so that `sdk/generic.py` need not import `sdk/langgraph.py`, and
    so a user's own graph type works without registering anything.

    Raises:
        UnsupportedTargetError: the object cannot be invoked (`E-INSTR-006`).
    """
    ainvoke = getattr(graph, "ainvoke", None)
    if callable(ainvoke):
        return await ainvoke(payload)
    invoke = getattr(graph, "invoke", None)
    if callable(invoke):
        return invoke(payload)
    if callable(graph):
        outcome = graph(payload)
        if inspect.isawaitable(outcome):
            return await outcome
        return outcome
    detail = (
        f"{type(graph).__name__} cannot be invoked: it has no `ainvoke`, no `invoke`, and is "
        f"not callable. Pass the object `agentdx.instrument()` returned, or a coroutine "
        f"function decorated with `@agentdx.agent`"
    )
    raise UnsupportedTargetError(detail)


def _fire_run_start(context: RunContext) -> None:
    """Invoke `on_run_start`."""
    hook = context.hooks.on_run_start
    if hook is not None:
        hook(context)


def _fire_run_end(context: RunContext, result: RunResult) -> None:
    """Invoke `on_run_end`."""
    hook = context.hooks.on_run_end
    if hook is not None:
        hook(context, result)


__all__ = [
    "ERROR_MESSAGE_LIMIT",
    "MISSING_VALUE_HASH",
    "REDACTION_TOKEN",
    "SPAN_ID_LENGTH",
    "AgentContext",
    "AgentContextError",
    "AttributeTypeError",
    "BarrierState",
    "CacheMissError",
    "CachedResponse",
    "Clock",
    "FrozenClock",
    "HookViolationError",
    "ImmediateScheduler",
    "InstrumentationError",
    "InstrumentationGap",
    "InstrumentationGapWarning",
    "LifecycleHooks",
    "LlmCache",
    "ManualClock",
    "NoCache",
    "ProviderError",
    "Recorder",
    "Redactor",
    "RunContext",
    "RunContextError",
    "RunHost",
    "RunResult",
    "Scheduler",
    "SdkError",
    "Span",
    "SpanRecord",
    "StateHandle",
    "UnsupportedTargetError",
    "ValueRepresentationError",
    "active_agent",
    "active_run",
    "agent_scope",
    "call_hook",
    "check_attributes",
    "current_agent",
    "current_run",
    "emit",
    "hash_text",
    "install_runtime",
    "message_id_for",
    "record_gap",
    "recv",
    "redact_attributes",
    "send",
    "span",
    "span_id_for",
    "stable_text",
    "state",
    "state_facts",
    "sync_agent_scope",
    "sync_span",
    "use_agent",
    "use_run",
]
