"""Shared fixtures for the `runtime/faults/` unit suite.

`resolved_scenario` builds the same shape `scenario.loader.resolve_defaults` produces — a
plain dict, per that module's own D-42 ruling — without going through YAML parsing, so a test
can hand-author exactly the fault/blast-radius/hypothesis/guards combination it needs.

`ValidatingStamp` is a fake `stamp` callable that runs every event a fault-class module emits
through the real `validate_event` — the same schema-conformance check `_SchedulerRecorder.write`
performs — without needing a live `Scheduler`. `test_process.py`'s `CrashInjector` tests already
drive a real `Scheduler` and so get this for free; `TransportFaultInjector`/
`DependencyFaultInjector` have no real scheduler integration to drive (see their own module
docstrings), so this fixture is how their unit tests get the same payload-schema guarantee.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from agentdx.events.schema import DraftEvent, Event, Stamp
from agentdx.events.validators import validate_event
from agentdx.scenario.schema import DEFAULT_GUARDS


def resolved_scenario(
    *,
    faults: Sequence[Mapping[str, object]] = (),
    chaos_opt_in: bool = False,
    blast_radius: Mapping[str, object] | None = None,
    hypothesis: Mapping[str, object] | None = None,
    guards: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return a minimal resolved-scenario dict for `FaultRegistry.from_resolved_scenario`.

    Mirrors `scenario.loader.DEFAULTS`'s shape for exactly the keys this package's modules
    read (`faults`, `chaos_opt_in`, `blast_radius`, `hypothesis`, `guards`) — no `target`/
    `task`/`seed`/etc., since nothing in `runtime/faults/` reads those.
    """
    return {
        "faults": list(faults),
        "chaos_opt_in": chaos_opt_in,
        "blast_radius": dict(blast_radius) if blast_radius is not None else {},
        "hypothesis": dict(hypothesis) if hypothesis is not None else {},
        "guards": dict(guards) if guards is not None else dict(DEFAULT_GUARDS),
    }


@dataclass
class ValidatingStamp:
    """A `stamp`-compatible callable that assigns real `seq`s and validates every event.

    Not a `Scheduler` stand-in for causality/vclocks (every event gets `causal_parents=()`
    and a trivial per-agent-slot vclock) — only for the one thing `TransportFaultInjector`/
    `DependencyFaultInjector` unit tests need that a bare list-appending fake cannot give
    them: proof that the payload each module builds actually satisfies `events.schema.
    PAYLOAD_SCHEMAS` and `events.schema.EVENT_SCOPES`, the same check a real run would run.
    """

    run_id: str = "r_faults_unit"
    events: list[Event] = field(default_factory=list)
    _seq: int = field(default=0, repr=False)

    def __call__(self, draft: DraftEvent, causes: Sequence[int] = ()) -> Event:
        """Stamp, validate and record `draft`; return the resulting `Event`."""
        slot = draft.clock_slot or draft.agent_id or "run"
        stamp = Stamp(
            seq=self._seq,
            sched_step=self._seq,
            virtual_ts_ms=self._seq * 10,
            wall_ts_ms=self._seq,
            vclock={slot: 1},
            causal_parents=tuple(causes),
        )
        event = Event.from_draft(draft, stamp, self.run_id)
        validate_event(event, self.events[-1] if self.events else None)
        self._seq += 1
        self.events.append(event)
        return event
