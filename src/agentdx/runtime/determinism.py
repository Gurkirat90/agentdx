"""Trapping ambient non-determinism (PRD §10.5) — a runtime feature, not a test.

**Why this file is allowlisted (AGENTS.md §4.1 clause 1 — "installs and removes the
patches").** Somewhere in the tree has to actually hold a reference to `random.random` and
overwrite it; that line is, by definition, a use of the `random` module outside the injected
`RunContext`. This is the one place that is true on purpose.

**Two treatments, not one, and the difference is load-bearing.** PRD §10.5's table lists six
sources — `random`, `numpy.random`, the three `time` functions, `datetime.now`/`utcnow`,
`uuid.uuid4`, `asyncio.sleep` — with the treatment "Redirected" or "Patched to return virtual
time". Those are **silently made correct** for the duration of a run: reading `time.time()`
inside a run is not a leak, because the patch means it returns a value derived from the
virtual clock, and G3 is unaffected. A second, smaller set — spawning an OS thread, calling
the blocking `time.sleep` — **cannot** be usefully redirected: there is no virtual
substitute for a call that blocks the single OS thread the scheduler runs on (PRD §10.2,
CONTEXT.md §3), so *those* are what "the leak detector" in the mission sense means, and
*those* are what raise in `strict` mode (`E-SCHED-004`) and warn otherwise. Conflating the
two would either make ordinary logging code raise (over-eager) or let a thread spawn silently
break I1 (under-eager); PRD §10.5's own column header — "Treatment" for the first six,
nothing that says "leak" — is the textual evidence for the split.

**What this file does NOT attempt**, stated rather than silently skipped (PRD §10.6):
arbitrary file or network I/O in user tools is real, is outside AgentDX's control, and no
generic socket/file patch is installed here to detect it — wrapping every I/O primitive in
the standard library is its own project and PRD §10.6 already concedes the honest scope:
"Detected and reported" applies to I/O performed through an **un-wrapped `@agentdx.tool`**
(a design constraint on the SDK, not a runtime patch), not to every `open()` call anywhere in
the process.

PRD §10.5 (the table this module implements), §10.6 (the honesty this module's `strict`
behaviour is bounded by), §36 `E-SCHED-004`.
"""

from __future__ import annotations

import datetime as _datetime_module  # patched below
import os
import random as _random_module  # patched below, never called directly here
import threading
import time as _time_module  # patched below
import traceback
import uuid as _uuid_module  # patched below
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import blake2b
from itertools import count
from typing import TYPE_CHECKING, Final, SupportsIndex, TypeVar

from agentdx.runtime.clock import VirtualClock
from agentdx.runtime.context import active_task

if TYPE_CHECKING:
    from random import Random

try:
    import numpy as _numpy  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - numpy is not in the ADR-004 dependency set
    _numpy = None

_DOCS: Final = "docs/determinism-guarantees.md"
_T = TypeVar("_T")

HASH_SEED_ENV: Final = "PYTHONHASHSEED"
"""PRD §10.5: hash() randomisation is trapped by requiring this rather than by patching —
`hash()` is a C-level builtin with no Python-visible seed to intercept after start-up."""

LeakSource = str
"""Mirrors `events.schema.PAYLOAD_SCHEMAS[EventType.NONDETERMINISM_WARNING]["source"].enum`
(`live_model_call`, `unmanaged_io`, `os_thread`, `ambient_clock`, `ambient_random`,
`ambient_uuid`, `unordered_iteration`). Not imported as an enum: this module must not import
`events.schema`'s closed enum machinery just to re-export seven string literals, and a bare
`str` costs nothing extra here — `runtime/scheduler.py`'s recorder is where a `LeakReport`
becomes an actual `nondeterminism_warning` event, and that is where the enum is checked."""


# ---------------------------------------------------------------------------------------
# Errors and reports
# ---------------------------------------------------------------------------------------


class DeterminismLeakError(RuntimeError):
    """A source of non-determinism was reached that cannot be safely redirected.

    Carries `E-SCHED-004` (PRD §36: "Determinism leak... `strict`: abort; else warn").
    Raised only for the two unpatchable cases (`threading.Thread.start`, `time.sleep`) and
    for a missing `PYTHONHASHSEED=0` at install time — never for the six PRD §10.5 sources
    this module successfully redirects, which never raise at all.
    """

    code: Final = "E-SCHED-004"

    def __init__(self, detail: str, *, stack: str | None = None) -> None:
        """Build the error, embedding the calling stack frame when one was captured.

        The mission's own example — "Being told exactly where the leak is beats
        discovering that the hash differs" — is why `stack` is not an afterthought: a leak
        report with no location is exactly as useful as the 97/100-pass symptom it exists
        to replace.
        """
        message = f"[{self.code}] {detail}"
        if stack:
            message = f"{message}\n{stack}"
        super().__init__(f"{message} ({_DOCS}#{self.code.lower()})")


class NondeterminismLeakWarning(RuntimeWarning):
    """Raised alongside a non-fatal leak report, mirroring `sdk.generic.InstrumentationGapWarning`.

    Non-strict mode never raises `DeterminismLeakError`; this is what makes the leak visible
    to whoever is watching the run instead.
    """


@dataclass(frozen=True, slots=True)
class LeakReport:
    """One ambient-non-determinism finding, ready to become a `nondeterminism_warning` event.

    Guarantees: `stack` is a short, human-readable rendering of the calling frames — enough
    to find the offending line without dumping the entire interpreter stack (which would
    itself be an unbounded, largely-`runtime/`-internal payload field).
    """

    source: LeakSource
    detail: str
    location: str | None
    stack: str


def _capture_stack(skip_frames: int = 2) -> str:
    """Return a short, deterministic-length rendering of the calling stack.

    `skip_frames` drops this function's own frame and its immediate caller (the patch
    wrapper), so the first line shown is the user code that actually reached for the
    unpatched source — "exactly where", not "somewhere inside AgentDX".
    """
    frames = traceback.extract_stack()[:-skip_frames]
    tail = frames[-8:]
    return "".join(traceback.format_list(tail)).rstrip("\n")


def _location() -> str | None:
    """Return `agent_id` of the ambient scheduler task, if one is active, else None."""
    task = active_task()
    return task.agent_id if task is not None else None


def _report(
    source: LeakSource,
    detail: str,
    *,
    strict: bool,
    on_leak: Callable[[LeakReport], None] | None,
    skip_frames: int = 3,
) -> None:
    """Report a leak: raise in `strict` mode, otherwise warn and hand it to `on_leak`.

    Raises:
        DeterminismLeakError: `strict` is True (`E-SCHED-004`).
    """
    stack = _capture_stack(skip_frames)
    location = _location()
    if strict:
        raise DeterminismLeakError(detail, stack=stack)
    report = LeakReport(source=source, detail=detail, location=location, stack=stack)
    if on_leak is not None:
        on_leak(report)
    warnings.warn(
        f"[{DeterminismLeakError.code}] {detail}\n{stack}", NondeterminismLeakWarning, stacklevel=3
    )


# ---------------------------------------------------------------------------------------
# PYTHONHASHSEED (PRD §10.5 row 8)
# ---------------------------------------------------------------------------------------


def hash_seed_is_pinned(env: dict[str, str] | None = None) -> bool:
    """Return True iff `PYTHONHASHSEED=0` is set in the process environment.

    `hash()` randomisation is seeded once at interpreter start-up and cannot be changed
    mid-process — there is no patch point, unlike every other row in PRD §10.5's table — so
    this is a precondition check, not a trap. `agentdx doctor` (P17) is where a violation
    gets fixed (by re-exec'ing with the variable set); this function only answers the
    question.
    """
    environment = os.environ if env is None else env
    return environment.get(HASH_SEED_ENV) == "0"


# ---------------------------------------------------------------------------------------
# agentdx.sorted_set() (PRD §10.5 row 9)
# ---------------------------------------------------------------------------------------


def sorted_set(values: Iterable[_T]) -> list[_T]:  # noqa: UP047  # D-08
    """Return the elements of an unordered collection in a stable, deterministic order.

    **This is `agentdx.sorted_set()`** — the helper PRD §10.5 names as the replacement for
    iterating a bare `set`, and that `agentdx/__init__.py` has documented as owed to P06
    alongside `wall_time()` (CONTEXT.md D-20). `scripts/check_determinism_hygiene.py` is the
    static half of this guarantee (it bans `set` iteration in `src/agentdx/`); this is the
    escape hatch it points to.

    Guarantees: natively orderable elements (the overwhelming case — every `set_valued`
    field in the event schema is `str[]`, e.g. `barrier.participants`,
    `schedule_decision.ready_task_ids`) sort by `<`. Elements that are not mutually
    orderable fall back to sorting by `repr()`, which is stable within one run but is not a
    promise that non-comparable objects sort in a semantically meaningful way — only that
    they sort the *same way twice*.
    """
    items = list(values)
    try:
        return sorted(items)  # type: ignore[type-var]
    except TypeError:
        return sorted(items, key=repr)


# ---------------------------------------------------------------------------------------
# random / numpy.random (PRD §10.5 rows 1–2)
# ---------------------------------------------------------------------------------------


def _seeded_random(seed: int) -> Random:
    """Return a fresh `random.Random` seeded from the run seed.

    The one call in this file that constructs the object every other function in this
    section redirects to.
    """
    return _random_module.Random(  # noqa: S311  # determinism-exempt: §4.1(1) the seeded source
        seed
    )


def _patch_random(seeded: Random) -> dict[str, object]:
    """Replace every top-level `random.*` callable this Python ships with `seeded`'s bound.

    Uses the method of the same name. Returns the saved originals, keyed by name.
    """
    saved: dict[str, object] = {}
    for name in dir(seeded):
        if name.startswith("_"):
            continue
        bound = getattr(seeded, name, None)
        if not callable(bound):
            continue
        if not hasattr(_random_module, name):
            continue
        saved[name] = getattr(_random_module, name)
        setattr(_random_module, name, bound)
    return saved


def _restore_random(saved: dict[str, object]) -> None:
    """Undo `_patch_random`, restoring every saved module-level `random.*` attribute."""
    for name, value in saved.items():
        setattr(_random_module, name, value)


def _seed_numpy(seed: int) -> bool:
    """Seed `numpy.random`'s global state if numpy is importable. Returns whether it ran.

    numpy is not in the ADR-004 dependency set, so this is best-effort and silent when
    absent — PRD §10.5 says "if numpy is importable", not "numpy is required".
    """
    if _numpy is None:
        return False
    _numpy.random.seed(seed % (2**32))
    return True


# ---------------------------------------------------------------------------------------
# time.time / monotonic / perf_counter / sleep (PRD §10.5 row 3)
# ---------------------------------------------------------------------------------------


def _patch_time(
    clock: VirtualClock, *, strict: bool, on_leak: Callable[[LeakReport], None] | None
) -> dict[str, object]:
    """Redirect the three read-only time functions to the virtual clock; block `time.sleep`.

    `time.sleep` is not in PRD §10.5's table, but `scripts/check_determinism_hygiene.py`
    bans it statically with the remedy "advance the virtual clock instead" — there is no
    virtual substitute for a call that blocks the one OS thread the scheduler runs on
    (PRD §10.2), so it is treated as unpatchable rather than redirected, and reported like
    a thread spawn.
    """
    saved: dict[str, object] = {
        "time": _time_module.time,
        "monotonic": _time_module.monotonic,
        "perf_counter": _time_module.perf_counter,
        "sleep": _time_module.sleep,
    }

    def _virtual_seconds() -> float:
        return clock.now_ms() / 1000.0

    def _blocked_sleep(seconds: float | SupportsIndex) -> None:
        _report(
            "ambient_clock",
            f"time.sleep({seconds!r}) blocks the single OS thread the cooperative scheduler "
            f"runs on (PRD §10.2); there is no virtual substitute for a blocking call. Await "
            f"an AgentDX-provided yield point instead",
            strict=strict,
            on_leak=on_leak,
        )

    _time_module.time = _virtual_seconds
    _time_module.monotonic = _virtual_seconds
    _time_module.perf_counter = _virtual_seconds
    _time_module.sleep = _blocked_sleep
    return saved


def _restore_time(saved: dict[str, object]) -> None:
    """Undo `_patch_time`."""
    _time_module.time = saved["time"]  # type: ignore[assignment]
    _time_module.monotonic = saved["monotonic"]  # type: ignore[assignment]
    _time_module.perf_counter = saved["perf_counter"]  # type: ignore[assignment]
    _time_module.sleep = saved["sleep"]  # type: ignore[assignment]


# ---------------------------------------------------------------------------------------
# datetime.now / utcnow / today (PRD §10.5 row 4)
# ---------------------------------------------------------------------------------------

_VIRTUAL_EPOCH: Final = _datetime_module.datetime(2024, 1, 1, tzinfo=_datetime_module.UTC)
"""An arbitrary fixed reference instant. Not the real epoch and not read from the real
clock — PRD §10.5 asks for "a virtual epoch derived from the seed"; the per-seed offset is
applied on top of this constant in `_frozen_datetime_class`, so two different seeds get two
different virtual "now"s even at virtual time 0, while neither ever touches `time.time()`."""


def _frozen_datetime_class(clock: VirtualClock, seed: int) -> type:
    """Return a `datetime.datetime` subclass whose `now`/`utcnow`/`today` read the virtual clock.

    **Why a subclass swapped into the module, not an attribute patch on the class itself.**
    `datetime.datetime` is a C-implemented immutable type: `datetime.datetime.now = ...`
    raises `TypeError: cannot set 'now' attribute of immutable type` (verified empirically —
    this is the "a required library holds nondeterministic internal state you cannot patch
    cleanly" case named in the mission's STOP CONDITIONS, and the resolution taken is the
    standard one `freezegun`/`time-machine` use). Reassigning the **module's** `datetime`
    attribute to point at a subclass *is* a plain attribute assignment and works — so
    `import datetime; datetime.datetime.now()` sees the patched class, because that
    expression re-reads `datetime.datetime` from the module on every call.

    **The one residual limit, stated rather than hidden**: code that did
    `from datetime import datetime` *before* this patch installs holds its own direct
    reference to the real class and will keep reading the real clock. This is the exact
    shape of limit `scripts/check_determinism_hygiene.py`'s own docstring already documents
    for the static check ("a banned call reached through a local alias... is caught at the
    assignment, not the call") — the runtime patch has the same blind spot for the same
    structural reason, and it is why the leak detector, not this patch, is the backstop.
    """
    real = _datetime_module.datetime
    offset = _datetime_module.timedelta(seconds=seed % 100_000)
    base = _VIRTUAL_EPOCH + offset

    class _FrozenDateTime(real):  # type: ignore[misc, valid-type]
        """A `datetime.datetime` whose `now`-family methods read the virtual clock."""

        @classmethod
        def now(cls, tz: _datetime_module.tzinfo | None = None) -> _FrozenDateTime:
            """Return the virtual "now", localised to `tz` if given (else naive)."""
            current = base + _datetime_module.timedelta(milliseconds=clock.now_ms())
            if tz is not None:
                return current.astimezone(tz)  # type: ignore[return-value]
            return real(
                current.year,
                current.month,
                current.day,
                current.hour,
                current.minute,
                current.second,
                current.microsecond,
            )  # type: ignore[return-value]

        @classmethod
        def utcnow(cls) -> _FrozenDateTime:
            """Return the virtual "now" as a naive UTC value.

            Simplification, stated once: a virtual run has no real timezone, so `now()` and
            `utcnow()` are answered from the same virtual instant rather than modelling the
            host's local timezone offset. Two replays on machines in different real
            timezones therefore still agree, which is the property that matters here.
            """
            return cls.now(tz=None)

        @classmethod
        def today(cls) -> _FrozenDateTime:
            """Return the virtual "now" (PRD §10.5 groups `date.today` with `datetime.now`)."""
            return cls.now(tz=None)

    return _FrozenDateTime


def _patch_datetime(clock: VirtualClock, seed: int) -> object:
    """Install the frozen `datetime.datetime` subclass. Returns the real class to restore."""
    saved = _datetime_module.datetime
    _datetime_module.datetime = _frozen_datetime_class(  # type: ignore[assignment, misc]
        clock, seed
    )
    return saved


def _restore_datetime(saved: object) -> None:
    """Undo `_patch_datetime`."""
    _datetime_module.datetime = saved  # type: ignore[assignment, misc]


# ---------------------------------------------------------------------------------------
# uuid.uuid4 / uuid1 (PRD §10.5 row 5)
# ---------------------------------------------------------------------------------------


def _make_seeded_uuid4(seed: int) -> Callable[[], object]:
    """Return a zero-argument callable producing RFC-4122-shaped, seed-derived UUIDs.

    Guarantees: the `n`-th call in a run always returns the same UUID at the same seed —
    `blake2b(f"{seed}:{n}")`, version and variant bits set per RFC 4122 so the result is
    indistinguishable in shape from a real `uuid4()`, never `uuid.uuid4()` itself (banned;
    see `scripts/check_determinism_hygiene.py`).
    """
    counter = count()

    def _uuid4() -> object:
        n = next(counter)
        digest = bytearray(blake2b(f"{seed}:{n}".encode(), digest_size=16).digest())
        digest[6] = (digest[6] & 0x0F) | 0x40
        digest[8] = (digest[8] & 0x3F) | 0x80
        return _uuid_module.UUID(bytes=bytes(digest))

    return _uuid4


def _patch_uuid(seed: int) -> dict[str, object]:
    """Replace `uuid.uuid4` and `uuid.uuid1` with the seeded generator. Returns the originals."""
    saved: dict[str, object] = {"uuid4": _uuid_module.uuid4, "uuid1": _uuid_module.uuid1}
    generator = _make_seeded_uuid4(seed)
    _uuid_module.uuid4 = generator  # type: ignore[assignment]
    _uuid_module.uuid1 = generator  # type: ignore[assignment]
    return saved


def _restore_uuid(saved: dict[str, object]) -> None:
    """Undo `_patch_uuid`."""
    _uuid_module.uuid4 = saved["uuid4"]  # type: ignore[assignment]
    _uuid_module.uuid1 = saved["uuid1"]  # type: ignore[assignment]


# ---------------------------------------------------------------------------------------
# threading (unpatchable — always reported like a blocking sleep)
# ---------------------------------------------------------------------------------------


def _patch_thread_spawn(
    *, strict: bool, on_leak: Callable[[LeakReport], None] | None
) -> Callable[[threading.Thread], None]:
    """Guard `threading.Thread.start`: report every call, per §10.6's "detected and rejected".

    Returns the real `start` to restore. In `strict` mode `_report` raises before the real
    `start` runs, so the thread never actually spawns (PRD §10.6: "creating a thread inside
    an instrumented span raises in strict mode"). In non-strict mode the warning fires and
    the thread starts anyway — best-effort, per the general `E-SCHED-004` handling ("else
    warn"), since PRD §10.6's own fallback path assumes some graphs need threads to be
    useful at all.
    """
    real_start = threading.Thread.start

    def _guarded_start(self: threading.Thread) -> None:
        """Report the spawn, then run the real `Thread.start` (non-strict mode only)."""
        _report(
            "os_thread",
            f"threading.Thread(name={self.name!r}) started inside an AgentDX run; the "
            f"scheduler cannot observe or order work on a second OS thread (PRD §10.6)",
            strict=strict,
            on_leak=on_leak,
        )
        real_start(self)

    threading.Thread.start = _guarded_start  # type: ignore[method-assign]
    return real_start


def _restore_thread_spawn(real_start: Callable[[threading.Thread], None]) -> None:
    """Undo `_patch_thread_spawn`."""
    threading.Thread.start = real_start  # type: ignore[method-assign, assignment]


# ---------------------------------------------------------------------------------------
# The guard — installs everything above for the duration of one run, and only one at a time
# ---------------------------------------------------------------------------------------

_INSTALL_LOCK_MESSAGE: Final = (
    "a DeterminismGuard is already installed in this process. PRD §10.2 assumes one "
    "cooperative scheduler per OS thread; nesting two guards would make 'which run's "
    "seed is random.random() serving right now' ambiguous, which is the exact failure "
    "mode this module exists to prevent"
)


class DeterminismGuard:
    """Installs and removes the PRD §10.5 patch set for the duration of one run.

    Guarantees: `install()` and `uninstall()` are idempotent-safe as a pair — `uninstall`
    restores every saved original exactly once, even if `install` partially failed (a
    `PYTHONHASHSEED` check failing in `strict` mode leaves nothing installed to undo). Use
    as a context manager; that is the only way `runtime/scheduler.py` uses it.
    """

    def __init__(
        self,
        *,
        seed: int,
        clock: VirtualClock,
        strict: bool,
        on_leak: Callable[[LeakReport], None] | None = None,
        check_hash_seed: bool = True,
    ) -> None:
        """Configure the guard. Nothing is patched until `install()`/`__enter__` runs.

        Args:
            seed: The run seed — the sole source of the seeded RNG and the seeded UUID
                generator, and of the datetime virtual-epoch offset.
            clock: The run's `VirtualClock`; `time.time`/`monotonic`/`perf_counter` and
                `datetime.now`/`utcnow`/`today` all read through it.
            strict: PRD §10.2/§10.6: unpatchable leaks raise `DeterminismLeakError`
                (`E-SCHED-004`) rather than warning. Sourced from
                `SchedulerConfig.strict_determinism`, never a literal at the call site.
            on_leak: Called with every non-fatal `LeakReport`; `runtime/scheduler.py`'s
                recorder turns it into a `nondeterminism_warning` event. `None` is
                permitted for standalone testing of this module.
            check_hash_seed: Whether `install()` checks `PYTHONHASHSEED=0`. Disabled only by
                this module's own tests, which must run under whatever hash seed the test
                runner was launched with.
        """
        self._seed = seed
        self._clock = clock
        self._strict = strict
        self._on_leak = on_leak
        self._check_hash_seed = check_hash_seed
        self._installed = False
        self._saved_random: dict[str, object] | None = None
        self._saved_time: dict[str, object] | None = None
        self._saved_datetime: object | None = None
        self._saved_uuid: dict[str, object] | None = None
        self._saved_thread_start: Callable[[threading.Thread], None] | None = None
        self.seeded_random: Random | None = None
        """The `random.Random` instance every patched `random.*` call now delegates to.
        Exposed so `runtime/scheduler.py`'s `choose()` can share the identical seeded stream
        PRD §10.2 requires — "the seeded RNG breaks any remaining ties" — rather than a
        second, uncoordinated `Random(seed)` that would desynchronise from user-code calls
        to `random.random()` interleaved with scheduler decisions."""

    @property
    def installed(self) -> bool:
        """Return True while this guard's patches are active."""
        return self._installed

    def install(self) -> None:
        """Patch every PRD §10.5 source.

        Raises before patching anything if the hash seed is unpinned in `strict` mode.

        Raises:
            DeterminismLeakError: another guard is already installed, or (`strict` only)
                `PYTHONHASHSEED` is not `"0"` (`E-SCHED-004`).
        """
        if _ACTIVE_GUARD[0] is not None:
            raise DeterminismLeakError(_INSTALL_LOCK_MESSAGE)
        if self._check_hash_seed and not hash_seed_is_pinned():
            detail = (
                "PYTHONHASHSEED is not pinned to '0' (PRD §10.5): hash() randomisation "
                "cannot be patched after interpreter start-up, so dict/set iteration order "
                "may differ between processes. `agentdx doctor` re-execs with it set"
            )
            if self._strict:
                raise DeterminismLeakError(detail)
            warnings.warn(detail, NondeterminismLeakWarning, stacklevel=2)

        self.seeded_random = _seeded_random(self._seed)
        self._saved_random = _patch_random(self.seeded_random)
        _seed_numpy(self._seed)
        self._saved_time = _patch_time(self._clock, strict=self._strict, on_leak=self._on_leak)
        self._saved_datetime = _patch_datetime(self._clock, self._seed)
        self._saved_uuid = _patch_uuid(self._seed)
        self._saved_thread_start = _patch_thread_spawn(strict=self._strict, on_leak=self._on_leak)
        self._installed = True
        _ACTIVE_GUARD[0] = self

    def uninstall(self) -> None:
        """Restore every real function this guard patched. Safe to call once, idempotently."""
        if not self._installed:
            return
        if self._saved_random is not None:
            _restore_random(self._saved_random)
        if self._saved_time is not None:
            _restore_time(self._saved_time)
        if self._saved_datetime is not None:
            _restore_datetime(self._saved_datetime)
        if self._saved_uuid is not None:
            _restore_uuid(self._saved_uuid)
        if self._saved_thread_start is not None:
            _restore_thread_spawn(self._saved_thread_start)
        self._installed = False
        if _ACTIVE_GUARD[0] is self:
            _ACTIVE_GUARD[0] = None

    def __enter__(self) -> DeterminismGuard:
        """Install the patch set and return self."""
        self.install()
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Restore the real functions, even if the run raised."""
        self.uninstall()


_ACTIVE_GUARD: list[DeterminismGuard | None] = [None]
"""Single-element list used as a mutable module-level cell (a plain `None` module global
cannot be rebound from inside `DeterminismGuard.install`/`uninstall` without `global`, and
a `global` statement over a name literally called `random`-adjacent invites exactly the kind
of grep-defeating indirection `scripts/check_determinism_hygiene.py`'s own docstring warns
about elsewhere in this codebase — a list cell sidesteps rebinding entirely)."""


def trap(
    *,
    seed: int,
    clock: VirtualClock,
    strict: bool,
    on_leak: Callable[[LeakReport], None] | None = None,
) -> DeterminismGuard:
    """Configure and return a `DeterminismGuard` for one run.

    Use as ``with trap(...) as guard:``. This is the module's one public entry point;
    everything else here is an implementation detail of what `install()` patches.
    """
    return DeterminismGuard(seed=seed, clock=clock, strict=strict, on_leak=on_leak)


__all__ = [
    "HASH_SEED_ENV",
    "DeterminismGuard",
    "DeterminismLeakError",
    "LeakReport",
    "NondeterminismLeakWarning",
    "hash_seed_is_pinned",
    "sorted_set",
    "trap",
]
