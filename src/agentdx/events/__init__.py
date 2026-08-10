"""THE CONTRACT: event schema, validation, canonical form and the append-only writer.

The single most important package; changes here are breaking changes and require a
`schema_version` bump plus an ADR (CONTEXT.md §11 tripwire 6). Imports nothing else
in the package — it is the root of the layer contract (CONTEXT.md §4).

The public surface named in PRD §24.6 is `EventWriter.write(Event)` and
`canonical_log_hash(events)`; both are re-exported here. The human-readable form of this
contract, complete enough to implement a compatible writer in another language, is
`docs/event-schema.md`.

Volatility is a first-class property of the schema (`schema.Volatility`), and the canonical
projection is derived from it rather than from a parallel list. That is the single design
decision this package exists to enforce: see `canonical.py` for why.
"""

from agentdx.events.canonical import (
    CHAIN_GENESIS,
    build_chain,
    canonical_bytes,
    canonical_log_hash,
    canonical_projection,
    chain_hash,
    decode_event,
    encode_event,
    verify_chain,
)
from agentdx.events.schema import (
    EVENT_SCOPES,
    PAYLOAD_SCHEMAS,
    SCHEMA_VERSION,
    DraftEvent,
    Event,
    EventScope,
    EventType,
    FieldSpec,
    Stamp,
    Volatility,
    excluded_field_paths,
)
from agentdx.events.validators import (
    EventValidationError,
    ValidationError,
    check_cross_event,
    check_semantic,
    check_structural,
    validate_event,
    validate_log,
)
from agentdx.events.writer import ChainedEvent, EventSink, EventWriter, WriterStateError

__all__ = [
    "CHAIN_GENESIS",
    "EVENT_SCOPES",
    "PAYLOAD_SCHEMAS",
    "SCHEMA_VERSION",
    "ChainedEvent",
    "DraftEvent",
    "Event",
    "EventScope",
    "EventSink",
    "EventType",
    "EventValidationError",
    "EventWriter",
    "FieldSpec",
    "Stamp",
    "ValidationError",
    "Volatility",
    "WriterStateError",
    "build_chain",
    "canonical_bytes",
    "canonical_log_hash",
    "canonical_projection",
    "chain_hash",
    "check_cross_event",
    "check_semantic",
    "check_structural",
    "decode_event",
    "encode_event",
    "excluded_field_paths",
    "validate_event",
    "validate_log",
    "verify_chain",
]
