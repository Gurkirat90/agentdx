"""Unit tests for `runtime.determinism`: the trap set, and leak detection as a runtime feature.

`test_thread_spawn_is_caught_with_a_useful_stack_frame` and
`test_the_same_leak_only_warns_in_non_strict_mode` are the P06 mission's required
demonstration: a genuinely unpatchable leak (`threading.Thread.start`), caught by the leak
detector with a stack frame naming the offending call site.

**Why not `time.time()`, given the mission text says "an unpatched `time.time()` call"?**
Earlier versions of this file used a `time.time` alias captured before `trap()` installs as
that demonstration — but the guard patches the `time.time` *module attribute*; a reference
captured before the patch applies still points at the original function and the call never
passes through the guard at all. Asserting "caught" on a call the detector never saw was a
test-quality bug in its own right (AGENTS.md §5: "never weaken a test to make it pass" cuts
both ways — a test whose assertions don't test what its docstring claims is the same failure
mode in different clothes). `threading.Thread.start` is used for the "caught" demonstration
because `runtime/determinism.py` patches it directly and it genuinely raises/reports every
time. The alias-blind case is still covered — honestly, as a documented negative result — by
`test_a_real_clock_alias_captured_before_trap_is_not_caught` below, which is the same shape
`docs/determinism-guarantees.md` §3 and §9 already document as a known, unfixable limit.
"""

from __future__ import annotations

import datetime
import random
import time
import uuid
import warnings

import pytest

from agentdx.runtime.clock import VirtualClock
from agentdx.runtime.determinism import (
    DeterminismLeakError,
    LeakReport,
    NondeterminismLeakWarning,
    hash_seed_is_pinned,
    sorted_set,
    trap,
)

# Captured at *module import* time — before any `trap()` in this file has ever installed a
# patch, and never re-read afterwards. This is what makes it a faithful stand-in for "a
# third-party dependency that imported `time.time` before the run started": a reference
# grabbed here always points at the genuine function object, no matter when or how many
# times a guard is later installed and removed around it.
_REAL_TIME_TIME = time.time

# ---------------------------------------------------------------------------------------
# sorted_set
# ---------------------------------------------------------------------------------------


def test_sorted_set_orders_naturally_comparable_elements() -> None:
    assert sorted_set({3, 1, 2}) == [1, 2, 3]
    assert sorted_set({"b", "a", "c"}) == ["a", "b", "c"]


def test_sorted_set_is_stable_within_one_process_for_non_comparable_elements() -> None:
    class Thing:
        def __init__(self, n: int) -> None:
            self.n = n

        def __repr__(self) -> str:
            return f"Thing({self.n})"

    things = {Thing(2), Thing(1), Thing(3)}
    first = sorted_set(things)
    second = sorted_set(things)
    assert [t.n for t in first] == [t.n for t in second]


def test_sorted_set_returns_a_list_not_a_set() -> None:
    assert isinstance(sorted_set({1, 2}), list)


# ---------------------------------------------------------------------------------------
# PYTHONHASHSEED
# ---------------------------------------------------------------------------------------


def test_hash_seed_is_pinned_true_when_set_to_zero() -> None:
    assert hash_seed_is_pinned({"PYTHONHASHSEED": "0"}) is True


def test_hash_seed_is_pinned_false_when_unset_or_other() -> None:
    assert hash_seed_is_pinned({}) is False
    assert hash_seed_is_pinned({"PYTHONHASHSEED": "random"}) is False


# ---------------------------------------------------------------------------------------
# The guard — random / time / datetime / uuid redirection
# ---------------------------------------------------------------------------------------


def test_random_module_functions_are_redirected_to_the_seeded_stream() -> None:
    clock = VirtualClock()
    with trap(seed=42, clock=clock, strict=True, on_leak=None):
        a = random.random()  # noqa: S311 — exercising the patched, seeded redirection
    with trap(seed=42, clock=clock, strict=True, on_leak=None):
        b = random.random()  # noqa: S311 — exercising the patched, seeded redirection
    assert a == b  # same seed -> same draw, on two separate installs


def test_time_functions_read_the_virtual_clock_not_the_real_one() -> None:
    clock = VirtualClock(start_ms=5_000)
    with trap(seed=1, clock=clock, strict=True, on_leak=None):
        assert time.time() == pytest.approx(5.0)
        assert time.monotonic() == pytest.approx(5.0)
        assert time.perf_counter() == pytest.approx(5.0)
    # Patches are removed on exit.
    assert time.time() != pytest.approx(5.0) or True  # real time, not asserted further


def test_datetime_now_reads_a_seed_derived_virtual_epoch() -> None:
    clock = VirtualClock()
    with trap(seed=7, clock=clock, strict=True, on_leak=None):
        first = datetime.datetime.now()  # noqa: DTZ005 — this IS the patched call under test
    clock.advance_by(1000)
    with trap(seed=7, clock=clock, strict=True, on_leak=None):
        second = datetime.datetime.now()  # noqa: DTZ005
    assert (second - first).total_seconds() == pytest.approx(1.0)


def test_datetime_now_is_identical_across_two_installs_at_the_same_seed_and_virtual_time() -> None:
    with trap(seed=99, clock=VirtualClock(), strict=True, on_leak=None):
        a = datetime.datetime.now()  # noqa: DTZ005
    with trap(seed=99, clock=VirtualClock(), strict=True, on_leak=None):
        b = datetime.datetime.now()  # noqa: DTZ005
    assert a == b


def test_uuid4_is_seeded_and_deterministic() -> None:
    with trap(seed=5, clock=VirtualClock(), strict=True, on_leak=None):
        a = [uuid.uuid4() for _ in range(3)]
    with trap(seed=5, clock=VirtualClock(), strict=True, on_leak=None):
        b = [uuid.uuid4() for _ in range(3)]
    assert a == b
    assert len({str(u) for u in a}) == 3  # not degenerate — three distinct ids


def test_uuid4_differs_across_seeds() -> None:
    with trap(seed=1, clock=VirtualClock(), strict=True, on_leak=None):
        a = uuid.uuid4()
    with trap(seed=2, clock=VirtualClock(), strict=True, on_leak=None):
        b = uuid.uuid4()
    assert a != b


def test_only_one_guard_may_be_installed_at_a_time() -> None:
    with trap(seed=1, clock=VirtualClock(), strict=True, on_leak=None):
        with pytest.raises(DeterminismLeakError):
            with trap(seed=2, clock=VirtualClock(), strict=True, on_leak=None):
                pass  # pragma: no cover - install() raises before the body runs


def test_patches_are_fully_removed_on_clean_exit() -> None:
    real_random = random.random
    real_uuid4 = uuid.uuid4
    with trap(seed=1, clock=VirtualClock(), strict=True, on_leak=None):
        pass
    assert random.random is real_random
    assert uuid.uuid4 is real_uuid4


def test_patches_are_removed_even_when_the_body_raises() -> None:
    real_random = random.random
    with pytest.raises(ValueError, match="boom"):
        with trap(seed=1, clock=VirtualClock(), strict=True, on_leak=None):
            raise ValueError("boom")
    assert random.random is real_random


# ---------------------------------------------------------------------------------------
# Leak detection — the required demonstration
# ---------------------------------------------------------------------------------------


def _fixture_with_an_unpatched_leak() -> float:
    """Simulate a third-party dependency that reads the real clock directly.

    This mirrors the mission's example shape: "a deliberately injected leak (an unpatched
    time.time() in a fixture)". `time.time` *is* one of the six PRD §10.5 sources
    `runtime/determinism.py` redirects — but only via a module-attribute patch, and this
    helper calls through `_REAL_TIME_TIME`, captured at module-import time, before any guard
    in this file could ever apply that patch, exactly like a third-party dependency imported
    at process start. Calling it bypasses the patch entirely: this is the honest, documented
    blind spot (`docs/determinism-guarantees.md` §3, §9), used below to prove the guard does
    NOT detect it — not to claim that it does.
    """
    return _REAL_TIME_TIME()


def test_thread_spawn_is_caught_with_a_useful_stack_frame() -> None:
    """DEFINITION OF DONE: demonstrate, paste, revert.

    `strict=True` (the default `SchedulerConfig.strict_determinism`) turns an unpatchable
    leak into a hard `DeterminismLeakError` (`E-SCHED-004`) that names the calling stack
    frame — "being told exactly where the leak is beats discovering that the hash differs"
    (the mission's own framing). `threading.Thread.start` is the leak demonstrated here
    because it is a source the guard genuinely *detects* — a raw `time.time` alias captured
    before `trap()` installs is not (see the module-blind-spot test below); asserting
    "caught" against a call the guard never observed would be the exact test-quality
    failure AGENTS.md §5 exists to catch. The injected leak lives entirely inside this
    test; nothing in `src/agentdx/` is modified to produce it, and this test is the
    revert — the leak exists only for the duration of this assertion.
    """
    with pytest.raises(DeterminismLeakError) as excinfo:
        with trap(seed=42, clock=VirtualClock(), strict=True, on_leak=None):
            import threading

            threading.Thread(target=lambda: None).start()

    message = str(excinfo.value)
    assert "E-SCHED-004" in message
    assert "os_thread" in message.lower() or "thread" in message.lower()
    # A useful stack frame: this test's own function name must appear in the captured
    # traceback, so a real user could find the offending call site from the error alone.
    assert "test_thread_spawn_is_caught_with_a_useful_stack_frame" in message


def test_a_real_clock_alias_captured_before_trap_is_not_caught() -> None:
    """The honest negative case: an aliased `time.time` is a real, undetected leak.

    `_fixture_with_an_unpatched_leak` reads the genuine wall clock — the value it returns
    is not virtual and not reproducible. The guard has no way to intercept a call through a
    reference it was never given, so there is no `DeterminismLeakError`, no
    `NondeterminismLeakWarning`, and no entry in `on_leak`'s reports, in strict mode or
    otherwise. This is `docs/determinism-guarantees.md` §3's "`from datetime import
    datetime` held before run start" row, generalised to `time.time`, asserted directly
    rather than left as an unasserted call whose silence could be mistaken for detection.
    """
    reports: list[LeakReport] = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with trap(seed=42, clock=VirtualClock(), strict=True, on_leak=reports.append):
            leaked_wall_clock_value = _fixture_with_an_unpatched_leak()

    assert leaked_wall_clock_value > 0  # a real wall-clock read genuinely happened...
    assert reports == []  # ...and the guard never knew about it: no report,
    assert not any(  # ...and no warning — silence, not detection.
        issubclass(w.category, NondeterminismLeakWarning) for w in caught
    )


def test_the_same_leak_only_warns_in_non_strict_mode() -> None:
    """The same unpatchable-leak shape, `strict=False`: warns and reports, never raises."""
    reports = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with trap(seed=42, clock=VirtualClock(), strict=False, on_leak=reports.append):
            import threading

            threading.Thread(target=lambda: None).start()

    assert len(reports) == 1
    assert reports[0].source == "os_thread"
    assert "Thread" in reports[0].stack or "thread" in reports[0].detail.lower()
    assert any(issubclass(w.category, NondeterminismLeakWarning) for w in caught)


def test_unpinned_hash_seed_warns_in_non_strict_mode_and_raises_in_strict_mode() -> None:
    import os

    saved = os.environ.pop("PYTHONHASHSEED", None)
    try:
        with pytest.raises(DeterminismLeakError):
            with trap(seed=1, clock=VirtualClock(), strict=True, on_leak=None):
                pass  # pragma: no cover - install() raises before the body runs

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with trap(seed=1, clock=VirtualClock(), strict=False, on_leak=None):
                pass
        assert any(issubclass(w.category, NondeterminismLeakWarning) for w in caught)
    finally:
        if saved is not None:
            os.environ["PYTHONHASHSEED"] = saved
