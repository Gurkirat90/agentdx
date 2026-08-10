"""`docs/event-schema.md` must agree with the code it documents.

The contract document is the artifact another language implements from, and PRD §10.7's own
exclusion list is the cautionary tale: it calls itself exhaustive and is missing a field.
These tests make the documented list and the executed list the same list.
"""

from __future__ import annotations

import dataclasses
import pathlib

from agentdx.events.schema import PAYLOAD_SCHEMAS, EventType, excluded_field_paths
from agentdx.events.validators import check_cross_event, check_semantic, check_structural
from tests.unit.events import factories

DOCS = pathlib.Path(__file__).parents[3] / "docs" / "event-schema.md"


def _text() -> str:
    return DOCS.read_text(encoding="utf-8")


def test_the_contract_document_exists() -> None:
    assert DOCS.is_file()


def test_every_excluded_path_appears_in_the_document() -> None:
    """The documented exclusion list is generated; drift here is impossible by construction."""
    text = _text()
    for path in excluded_field_paths():
        assert path in text, f"{path} is excluded in code but absent from the contract doc"


def test_every_event_type_has_a_section() -> None:
    text = _text()
    for event_type in EventType:
        assert f"### `{event_type.value}`" in text


def test_every_payload_field_appears_in_the_document() -> None:
    text = _text()
    for specs in PAYLOAD_SCHEMAS.values():
        for spec in specs:
            assert f"`{spec.name}`" in text


def test_every_raised_error_code_is_documented() -> None:
    """A code that fires but is not in the table is a code a caller cannot look up."""
    text = _text()
    seen: set[str] = set()
    broken = dataclasses.replace(factories.make_event(), schema_version=99, span_id=None)
    seen |= {e.code for e in check_structural(broken)}
    seen |= {e.code for e in check_semantic(broken, None)}
    log = factories.make_log()
    tainted = dataclasses.replace(log[0], fault_id="f_01")
    seen |= {e.code for e in check_cross_event([tainted, *log[1:]])}

    assert seen, "the probe produced no errors; it is no longer probing anything"
    for code in seen:
        assert f"`{code}`" in text, f"{code} is raised but not in the §8 error-code table"


def test_the_freeze_warning_is_present() -> None:
    """The document must say that it freezes; that is its most load-bearing sentence."""
    assert "Freezes at the end of week 1" in _text()
