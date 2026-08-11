"""Design constraint 2 / NFR-6 / invariant I8: the plaintext scan of a default-config run.

This is the enforcement of NFR-6, and it is deliberately blunt: run a fixture with the
**default** configuration, then read the SQLite file off disk **as raw bytes** and search it
for known plaintext. Not the payload dicts, not the decoded rows — the bytes, because that is
what leaves the machine when a bundle is shared or a `.db` is copied.

Three cases, and the second and third are what make the first mean anything:

* **default** — the run context is built through the *argument-omitted* entry point.
  `RunContext.create` is called with no `config=` and no `capture_bodies=` at all, so
  `AgentDXConfig`'s own resolution is what decides the behaviour. An earlier version of this
  file passed `capture_bodies=False` explicitly, which tested the branch that takes the
  caller's word for it and never touched the config→context wiring — an implementation that
  ignored `[privacy] capture_bodies` entirely would have passed it.
* **env opt-in** — the same fixture with `AGENTDX_PRIVACY_CAPTURE_BODIES=true` injected into
  the config's environment mapping, asserting the scan *does* find plaintext. That is the
  other direction of the same wire: the setting reaches the context from configuration, not
  only from an argument.
* **explicit opt-in** — `capture_bodies=True` passed as an argument, the original control.

A privacy test that cannot fail is not a privacy test, and the most common way to ship one is
to scan for a string the fixture never contained.

The scan globs `runs.db*` rather than naming `runs.db`, because the store runs in WAL mode:
bytes that have not been checkpointed live in `runs.db-wal`, and a scan that ignored it would
have a blind spot exactly the size of the most recent writes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest

import agentdx
from agentdx.config import AgentDXConfig
from agentdx.events.schema import SCHEMA_VERSION
from agentdx.events.writer import EventWriter
from agentdx.sdk.generic import CachedResponse, RunContext, span, use_run
from agentdx.sdk.providers import groq
from agentdx.sdk.providers.openai_compatible import cache_key_for
from agentdx.store.snapshots import SnapshottingStore
from agentdx.store.sqlite import RunRecord
from tests.unit.sdk.fakes import StampingRecorder

RUN_ID = "r_0f1a2"

SECRET_PROMPT = "PLAINTEXT-PROMPT-acquisition-of-Northwind-for-420-million"  # noqa: S105  # a needle for the scan, not a secret
SECRET_RESPONSE = "PLAINTEXT-RESPONSE-the-board-approved-it-on-Tuesday"  # noqa: S105  # a needle for the scan, not a secret
SECRET_TOOL_ARG = "PLAINTEXT-TOOLARG-employee-salary-table"  # noqa: S105  # a needle for the scan, not a secret
SECRET_STATE = "PLAINTEXT-STATE-draft-press-release"  # noqa: S105  # a needle for the scan, not a secret
SECRET_MESSAGE = "PLAINTEXT-MESSAGE-legal-review-required"  # noqa: S105  # a needle for the scan, not a secret
API_KEY_IN_PROMPT = "sk-" + "Z" * 32
API_KEY_IN_ATTRIBUTE = "sk-" + "Q" * 32

ALL_SECRETS = (
    SECRET_PROMPT,
    SECRET_RESPONSE,
    SECRET_TOOL_ARG,
    SECRET_STATE,
    SECRET_MESSAGE,
    API_KEY_IN_PROMPT,
    API_KEY_IN_ATTRIBUTE,
)


def _cached_body() -> str:
    return json.dumps(
        {
            "model": "llama-3.1-8b-instant",
            "choices": [{"message": {"content": SECRET_RESPONSE}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 9},
        }
    )


class _PreloadedCache:
    """A cache holding the bodies. It *must* — replay is impossible otherwise (PRD §8.11)."""

    def __init__(self) -> None:
        messages = [{"role": "user", "content": f"{SECRET_PROMPT} {API_KEY_IN_PROMPT}"}]
        self.entries = {
            cache_key_for("llama-3.1-8b-instant", messages, {}): CachedResponse(
                body=_cached_body(),
                model="llama-3.1-8b-instant",
                prompt_tokens=12,
                completion_tokens=9,
            )
        }

    def lookup(self, cache_key: str) -> CachedResponse | None:
        return self.entries.get(cache_key)

    def store(self, cache_key: str, response: CachedResponse) -> None:
        self.entries[cache_key] = response


async def _run_fixture(
    tmp_path: Path,
    *,
    capture_bodies: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Run a fixture that touches every body-bearing surface, and return the database path.

    With neither `capture_bodies` nor `env`, the context is built through the fully
    argument-omitted path — which is the only way this file can claim to test the *default*.
    """
    database = tmp_path / "runs.db"
    store = SnapshottingStore.open(database)
    store.create_run(
        RunRecord(
            run_id=RUN_ID,
            scenario_hash="blake2b:" + "3" * 64,
            graph_hash="blake2b:" + "4" * 64,
            mode="baseline",
            seed=42,
            status="running",
            created_at="2026-08-11T00:00:00Z",
            agentdx_version=agentdx.__version__,
            schema_version=SCHEMA_VERSION,
        )
    )
    writer = EventWriter(RUN_ID, store)
    recorder = StampingRecorder(RUN_ID, writer=writer)
    cache = _PreloadedCache()

    if capture_bodies is not None:
        # The explicit-argument case: the caller's word wins (PRD §8.7's CLI-flag position).
        context = RunContext.create(
            run_id=RUN_ID,
            recorder=recorder,  # type: ignore[arg-type]
            cache=cache,  # type: ignore[arg-type]
            capture_bodies=capture_bodies,
        )
    elif env is not None:
        # The configuration case: the setting must reach the context through config alone.
        context = RunContext.create(
            run_id=RUN_ID,
            recorder=recorder,  # type: ignore[arg-type]
            cache=cache,  # type: ignore[arg-type]
            config=AgentDXConfig.load(env=env),
        )
    else:
        # The default case: nothing is passed that could decide this but the config itself.
        context = RunContext.create(
            run_id=RUN_ID,
            recorder=recorder,  # type: ignore[arg-type]
            cache=cache,  # type: ignore[arg-type]
        )

    def _explode(request: httpx.Request) -> httpx.Response:
        detail = "this fixture runs offline"
        raise AssertionError(detail)

    client = groq.client(transport=httpx.MockTransport(_explode))

    @agentdx.tool("payroll_lookup")
    async def payroll_lookup(table: str) -> str:
        return f"rows from {table}"

    @agentdx.agent("analyst")
    async def analyst() -> None:
        await client.chat([{"role": "user", "content": f"{SECRET_PROMPT} {API_KEY_IN_PROMPT}"}])
        await payroll_lookup(SECRET_TOOL_ARG)
        async with span("tool_call", "annotate", attributes={"credential": API_KEY_IN_ATTRIBUTE}):
            pass
        async with agentdx.state() as shared:
            await shared.write("draft", SECRET_STATE)
            await shared.read("draft")
        await agentdx.send(to="editor", payload={"note": SECRET_MESSAGE})

    with use_run(context):
        await analyst()
    writer.flush()
    store.close()
    return database


def _database_bytes(database: Path) -> bytes:
    """Return every byte of the store, including the WAL the checkpoint has not folded in."""
    return b"".join(sorted(path.read_bytes() for path in database.parent.glob(f"{database.name}*")))


def _found_in(database: Path, needle: str) -> bool:
    return needle.encode("utf-8") in _database_bytes(database)


@pytest.mark.asyncio
async def test_a_default_config_run_writes_no_plaintext_body_to_the_database(
    tmp_path: Path,
) -> None:
    # The default really is the default: `_run_fixture` passes neither capture_bodies nor a
    # config, so `AgentDXConfig`'s own resolution is the only thing that can decide this.
    assert AgentDXConfig().privacy.capture_bodies is False

    database = await _run_fixture(tmp_path)

    leaked = [secret for secret in ALL_SECRETS if _found_in(database, secret)]
    assert leaked == [], (
        f"NFR-6 / invariant I8: the event log must contain hashes, never bodies. Leaked: {leaked}"
    )
    # And the hashes that replace them are present, so the log is still useful.
    assert b"blake2b:" in _database_bytes(database)


@pytest.mark.asyncio
async def test_the_config_opt_in_reaches_the_context_from_the_environment(
    tmp_path: Path,
) -> None:
    # The other direction of the wire the test above depends on. If `RunContext.create`
    # ignored `[privacy] capture_bodies` and hardcoded a default, exactly one of these two
    # tests would fail — which is the property the pair exists to have.
    env = {"AGENTDX_PRIVACY_CAPTURE_BODIES": "true"}
    assert AgentDXConfig.load(env=env).privacy.capture_bodies is True

    database = await _run_fixture(tmp_path, env=env)

    assert _found_in(database, SECRET_PROMPT)
    assert _found_in(database, SECRET_RESPONSE)
    assert _found_in(database, SECRET_TOOL_ARG)
    assert _found_in(database, SECRET_STATE)


@pytest.mark.asyncio
async def test_the_scan_finds_the_plaintext_when_capture_bodies_is_on(tmp_path: Path) -> None:
    # The control. Without it the default test could pass by scanning for a string the fixture
    # never produced, which is the standard way a privacy test silently stops testing.
    database = await _run_fixture(tmp_path, capture_bodies=True)

    assert _found_in(database, SECRET_PROMPT)
    assert _found_in(database, SECRET_RESPONSE)
    assert _found_in(database, SECRET_TOOL_ARG)
    assert _found_in(database, SECRET_STATE)


@pytest.mark.asyncio
async def test_redaction_applies_even_under_opt_in(tmp_path: Path) -> None:
    # PRD §8.11: bodies are stored "inline, after redaction". An opt-in is not a licence to
    # write an API key into a file that gets shared in a bundle.
    database = await _run_fixture(tmp_path, capture_bodies=True)
    assert not _found_in(database, API_KEY_IN_PROMPT)
    assert _found_in(database, "[REDACTED]")


@pytest.mark.asyncio
async def test_redaction_covers_span_attributes_not_only_bodies(tmp_path: Path) -> None:
    # `redact_patterns` is applied to span attribute strings as well as to bodies. An
    # attribute is user-supplied, written under the default configuration (attributes are not
    # gated on `capture_bodies`), and lands in a field that is inside the canonical
    # projection — so an unredacted key there is a leak the opt-in never guarded.
    database = await _run_fixture(tmp_path)
    assert not _found_in(database, API_KEY_IN_ATTRIBUTE)
    assert _found_in(database, "[REDACTED]")
