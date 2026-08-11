"""The `.agentdx` replay bundle: export, import and verify (PRD §20.7, §31.3, §31.9).

A bundle is a zip archive that is **self-contained** — it carries the events, the cache
manifest, the scenario, the calibration profile, the graph identity, the versions and the
canonical log hash — and **self-verifying**: `verify` recomputes the hash chain and the
canonical log hash from the events in the archive and reports a mismatch rather than
trusting the number the sender wrote down.

**A bundle is data, never code (§31.9).** An imported bundle is untrusted input from
another machine. This module therefore: reads only an allowlist of member names; refuses
any member whose name is absolute, contains `..`, or is not on the list; refuses a member
whose declared size exceeds a cap; parses the scenario as *text* and never as YAML, because
`yaml.load` on untrusted input is code execution and even `safe_load` is a parser this
module has no need to run; imports nothing dynamically; and executes nothing. The graph is
referenced by hash, not shipped. `--verify` re-executing against a *local* graph whose hash
matches is a CLI concern (P17) and is deliberately not implemented here — this module
cannot run a graph, which is the strongest form of the guarantee.

**Import is idempotent by `run_id` + `canonical_log_hash` (PRD §27.5).** Importing the same
bundle twice is a no-op; importing a *different* log under a `run_id` that already exists is
refused, because that would silently replace recorded history.

**Deviation from §20.7, owner-approved.** §20.7 names the members `events.jsonl.zst` and
`cache/entries.jsonl.zst`. Zstd requires the `zstandard` distribution, which is outside the
permitted dependency set (AGENTS.md §2, ADR-004's enumeration). The members are therefore
`events.jsonl` and `cache/entries.jsonl`, compressed by the zip container itself with
DEFLATE — the archive is still compressed, no dependency is added, `uv pip install agentdx`
stays compiler-free (NFR-16), and zstd inside a zip would have been double compression in
any case. `manifest.json` records `compression` so a future zstd bundle is distinguishable
rather than merely different.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from agentdx.events.canonical import (
    CHAIN_GENESIS,
    build_chain,
    canonical_log_hash,
    decode_event,
    encode_event,
    encode_value,
    verify_chain,
)
from agentdx.events.schema import SCHEMA_VERSION, Event, EventType, PayloadValue
from agentdx.events.validators import validate_log
from agentdx.events.writer import ChainedEvent
from agentdx.store.snapshots import rebuild_snapshots, write_snapshots
from agentdx.store.sqlite import (
    FindingRecord,
    RunRecord,
    ScenarioRecord,
    ScorecardRecord,
    Store,
    StoreError,
)

BUNDLE_SUFFIX: Final = ".agentdx"
BUNDLE_FORMAT_VERSION: Final = 2
"""The bundle layout version, distinct from the event `schema_version` and the store
`db_version`. Bumped when a member is added, removed or renamed."""

MANIFEST: Final = "manifest.json"
RUN_JSON: Final = "run.json"
EVENTS_JSONL: Final = "events.jsonl"
EVENTS_SHA256: Final = "events.sha256"
EVENTS_CHAIN: Final = "events.chain"
SCENARIO_YAML: Final = "scenario.yaml"
CACHE_MANIFEST: Final = "cache/manifest.json"
CACHE_ENTRIES: Final = "cache/entries.jsonl"
CALIBRATION_JSON: Final = "calibration.json"
GRAPH_JSON: Final = "graph.json"

MEMBERS: Final[frozenset[str]] = frozenset(
    {
        MANIFEST,
        RUN_JSON,
        EVENTS_JSONL,
        EVENTS_SHA256,
        EVENTS_CHAIN,
        SCENARIO_YAML,
        CACHE_MANIFEST,
        CACHE_ENTRIES,
        CALIBRATION_JSON,
        GRAPH_JSON,
    }
)
"""The complete set of permitted member names.

An allowlist rather than a denylist of dangerous patterns. A denylist has to anticipate
every encoding of `..`, an absolute path, a Windows drive prefix, a symlink entry and a
device path; an allowlist has to anticipate nothing (§31.9).
"""

REQUIRED_MEMBERS: Final[tuple[str, ...]] = (
    MANIFEST,
    RUN_JSON,
    EVENTS_JSONL,
    EVENTS_SHA256,
    EVENTS_CHAIN,
)
"""Members without which a bundle cannot be verified, let alone imported.

`events.chain` joined this list at bundle format 2. Without the per-event chain, a
tampered log can be *detected* (the rolling log hash changes) but not *located*, and
PRD §36 specifies an `E-BUNDLE-001` message that names the failing event. A rolling
hash cannot produce that; the chain can.
"""

MAX_MEMBER_BYTES: Final = 4 * 1024 * 1024 * 1024
"""Per-member decompressed size cap, checked against the header before extraction.

Four gigabytes is far above a legitimate bundle — PRD §27.5 puts a 5 000-event run at about
3.5 MB — and far below the point at which a zip bomb exhausts a laptop. The check exists
because `ZipFile.read` will happily decompress whatever the header claims.
"""


class BundleError(RuntimeError):
    """A bundle could not be produced, verified or imported.

    Guarantees: carries a stable `E-BUNDLE-NNN` code (PRD §36) plus a docs anchor, and
    never leaves a partially imported run — `import_bundle` runs entirely inside one
    `Store.transaction()`, so an interruption at any point rolls the whole thing back and a
    retry succeeds.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from its stable code and a human-readable explanation."""
        self.code = code
        super().__init__(f"[{code}] {detail} (docs/storage.md#{code.lower()})")


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One LLM cache entry as carried by a bundle (PRD §11.6, §20.7).

    `body` is the recorded response and is present only with `--include-cache-bodies`.
    Without it the bundle is **viewable and analysable but not re-executable**, which
    `manifest.json` records and `verify` reports, so the recipient is told rather than
    discovering it when a replay fails (PRD §20.7, §31.3).
    """

    cache_key: str
    model: str
    prompt_hash: str
    response_hash: str
    body: str | None = None

    def as_mapping(self) -> dict[str, PayloadValue]:
        """Return the canonical-JSON form written to the bundle."""
        return {
            "cache_key": self.cache_key,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "response_hash": self.response_hash,
            "body": self.body,
        }


@dataclass(frozen=True, slots=True)
class BundleManifest:
    """`manifest.json` — everything needed to decide whether to trust the archive.

    Guarantees: `canonical_log_hash` and `chain_head` are claims by the sender. `verify`
    recomputes both from `events.jsonl` and compares; nothing in this module treats the
    manifest's figures as authoritative.
    """

    bundle_format_version: int
    schema_version: int
    agentdx_version: str
    run_id: str
    canonical_log_hash: str
    chain_head: str
    event_count: int
    created_at: str
    includes_cache_bodies: bool
    compression: str = "deflate"
    """Records how the members are compressed. `deflate` is the zip container's own; the
    value exists so a future zstd bundle is distinguishable rather than merely different."""

    MINIMUM_LOCATABLE_FORMAT: Final = 2
    """Bundle formats below this carry no `events.chain` and cannot locate a tampered
    event. Kept as a named constant so the refusal message can say *why*."""

    def as_mapping(self) -> dict[str, PayloadValue]:
        """Return the canonical-JSON form written to the archive."""
        return {
            "bundle_format_version": self.bundle_format_version,
            "schema_version": self.schema_version,
            "agentdx_version": self.agentdx_version,
            "run_id": self.run_id,
            "canonical_log_hash": self.canonical_log_hash,
            "chain_head": self.chain_head,
            "event_count": self.event_count,
            "created_at": self.created_at,
            "includes_cache_bodies": self.includes_cache_bodies,
            "compression": self.compression,
        }


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What `verify` found. Never raises for a *bad* bundle — it reports.

    A boolean would be useless to a user staring at a refused import, so every field a
    recipient needs to understand *what* is wrong is here. `ok` is True iff every check
    passed; `problems` is empty exactly then.

    Guarantees: `computed_log_hash` and `computed_chain_head` are recomputed from the
    events in the archive, never copied from the manifest.
    """

    ok: bool
    run_id: str
    event_count: int
    declared_log_hash: str
    computed_log_hash: str
    declared_chain_head: str
    computed_chain_head: str
    first_bad_seq: int | None
    includes_cache_bodies: bool
    problems: tuple[str, ...]

    @property
    def re_executable(self) -> bool:
        """Return True iff the bundle carries the cache bodies a replay would need.

        A bundle without them is viewable and analysable but not re-executable (PRD §20.7).
        Reported rather than inferred at replay time, so the limitation is stated up front.
        """
        return self.includes_cache_bodies


# ---------------------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------------------


def export_bundle(
    store: Store,
    run_id: str,
    dest: Path,
    *,
    created_at: str | None = None,
    cache_entries: Sequence[CacheEntry] = (),
    include_cache_bodies: bool = False,
    scenario: str | None = None,
    calibration: Mapping[str, PayloadValue] | None = None,
    graph: Mapping[str, PayloadValue] | None = None,
    agentdx_version: str | None = None,
) -> Path:
    """Write a self-contained, self-verifying `.agentdx` bundle and return its path.

    Guarantees: the archive is written to a staging file and renamed, so no reader ever
    sees a partial bundle. `events.jsonl` is produced with `canonical.encode_event`, the
    same serialiser the store writes with, so `export → import → canonical_log_hash` is
    byte-stable. Cache bodies are excluded unless explicitly requested (§31.3, I8), and
    `manifest.json` records which.

    Args:
        store: The store holding the run.
        run_id: The run to export.
        dest: Destination path; `.agentdx` is appended if absent.
        created_at: Bundle creation timestamp. Defaults to the run's `sealed_at`, so this
            module never reads a clock — `store/` is exempted for file naming only
            (AGENTS.md §4.1 clause 4) and taking a real timestamp is the CLI's job.
        cache_entries: The cache slice this run needs. When empty, the cache manifest is
            derived from the `llm_call` events themselves, which is sufficient to tell a
            recipient exactly which keys a replay would require.
        include_cache_bodies: Write `cache/entries.jsonl` with response bodies. Off by
            default for privacy (§31.3); the CLI prints a warning naming what is included.
        scenario: The exact scenario text including resolved defaults. Defaults to the
            stored scenario for this run, or an empty document.
        calibration: The calibration profile used (PRD §10.4).
        graph: Graph identity — nodes, edges, tools, hashes. Identity only: the graph is
            referenced by hash and never shipped as code (§31.9).
        agentdx_version: Overrides the version recorded in the run row.

    Returns:
        The path written.

    Raises:
        BundleError: `E-BUNDLE-003` the run does not exist · `E-BUNDLE-004` the run has no
            events.
    """
    record = store.get_run(run_id)
    if record is None:
        raise BundleError("E-BUNDLE-003", f"run {run_id!r} is not in {store.path}")
    events = tuple(store.read_events(run_id))
    if not events:
        raise BundleError("E-BUNDLE-004", f"run {run_id!r} has no events to export")

    log_hash = canonical_log_hash(events)
    chain = build_chain(events)
    manifest = BundleManifest(
        bundle_format_version=BUNDLE_FORMAT_VERSION,
        schema_version=events[0].schema_version,
        agentdx_version=agentdx_version or record.agentdx_version,
        run_id=run_id,
        canonical_log_hash=log_hash,
        chain_head=chain[-1][1],
        event_count=len(events),
        created_at=created_at or record.sealed_at or record.created_at,
        includes_cache_bodies=include_cache_bodies,
    )
    entries = tuple(cache_entries) if cache_entries else derive_cache_manifest(events)
    scenario_text = scenario if scenario is not None else _stored_scenario(store, record)

    target = dest if dest.suffix == BUNDLE_SUFFIX else dest.with_suffix(BUNDLE_SUFFIX)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(target.suffix + ".partial")
    with (
        _durable_export(store),
        zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED) as archive,
    ):
        archive.writestr(MANIFEST, encode_value(manifest.as_mapping()))
        archive.writestr(RUN_JSON, encode_value(_run_payload(store, record)))
        archive.writestr(EVENTS_JSONL, "".join(f"{encode_event(e)}\n" for e in events))
        archive.writestr(EVENTS_SHA256, log_hash)
        archive.writestr(EVENTS_CHAIN, "".join(f"{this}\n" for _, this in chain))
        archive.writestr(SCENARIO_YAML, scenario_text)
        archive.writestr(
            CACHE_MANIFEST,
            encode_value(
                {
                    "count": len(entries),
                    "includes_bodies": include_cache_bodies,
                    "keys": [
                        {
                            "cache_key": e.cache_key,
                            "model": e.model,
                            "prompt_hash": e.prompt_hash,
                            "response_hash": e.response_hash,
                        }
                        for e in entries
                    ],
                }
            ),
        )
        if include_cache_bodies:
            archive.writestr(
                CACHE_ENTRIES,
                "".join(f"{encode_value(e.as_mapping())}\n" for e in entries),
            )
        archive.writestr(CALIBRATION_JSON, encode_value(dict(calibration or {})))
        archive.writestr(GRAPH_JSON, encode_value(dict(graph or _graph_identity(record))))
    staging.replace(target)
    return target


@contextmanager
def _durable_export(store: Store) -> Iterator[None]:
    """Raise SQLite's durability to `FULL` for the duration of an export (PRD §27.3).

    §27.3 says bundle export uses `synchronous=FULL` while a run uses `NORMAL`. The export
    itself only reads, so this does not change the archive; it means a checkpoint taken
    while a bundle is being produced is fully durable rather than merely process-crash safe.
    The previous level is restored even if the export raises.
    """
    conn = store.connection
    row = conn.execute("PRAGMA synchronous").fetchone()
    previous = int(row[0]) if row is not None else 1
    conn.execute("PRAGMA synchronous=FULL")
    try:
        yield
    finally:
        conn.execute(f"PRAGMA synchronous={previous}")


def derive_cache_manifest(events: Iterable[Event]) -> tuple[CacheEntry, ...]:
    """Return the cache keys a run required, read from its own `llm_call` events.

    This is why a bundle can be self-contained without depending on the cache module: every
    `llm_call` payload carries `cache_key`, `model`, `prompt_hash` and `response_hash`
    (PRD §9.5), which is exactly the manifest PRD §20.7 asks for. Bodies live in `cache.db`
    and are added only on request.

    Guarantees: deduplicated and returned in cache-key order, so two exports of the same
    run produce byte-identical cache manifests. Never contains a body.
    """
    seen: dict[str, CacheEntry] = {}
    for event in events:
        if event.type is not EventType.LLM_CALL:
            continue
        key = event.payload.get("cache_key")
        if not isinstance(key, str) or key in seen:
            continue
        seen[key] = CacheEntry(
            cache_key=key,
            model=_payload_str(event, "model"),
            prompt_hash=_payload_str(event, "prompt_hash"),
            response_hash=_payload_str(event, "response_hash"),
        )
    return tuple(seen[k] for k in sorted(seen))


# ---------------------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BundleContents:
    """Everything an import needs, read from the archive exactly once.

    **This type exists to close a TOCTOU.** The previous shape had `import_bundle` call
    `verify(path)`, which opened the file, read it and closed it — and then reopen the same
    path to read the events it was about to store. Between those two opens the file can
    change: a synced folder, a shared temp directory, or any process with write access is
    enough. The bundle that was verified was then not the bundle that was imported, which
    makes the verification decorative. Reading once and passing the bytes around is the fix,
    and it is why `verify` and `import_bundle` both go through `_read_bundle`.
    """

    manifest: BundleManifest
    events: tuple[Event, ...]
    chain: tuple[str, ...]
    declared_sha: str
    run_payload: Mapping[str, PayloadValue]
    scenario_text: str
    decode_problems: tuple[str, ...]


def _read_bundle(path: Path) -> _BundleContents:
    """Open a bundle once and read every member it carries.

    Guarantees: the archive is opened exactly once and closed before returning, so nothing
    downstream can observe a different version of the file.

    Raises:
        BundleError: `E-BUNDLE-001` not a readable zip · `E-BUNDLE-005` an unsafe, unknown
            or duplicated member · `E-BUNDLE-006` a required member is missing.
    """
    with _safe_archive(path) as archive:
        manifest = _read_manifest(archive)
        events, decode_problems = _read_events(archive)
        chain = tuple(
            line.strip()
            for line in archive.read(EVENTS_CHAIN).decode("utf-8").splitlines()
            if line.strip()
        )
        declared_sha = archive.read(EVENTS_SHA256).decode("utf-8").strip()
        run_payload = _read_json_object(archive, RUN_JSON)
        scenario_text = (
            archive.read(SCENARIO_YAML).decode("utf-8") if _has(archive, SCENARIO_YAML) else ""
        )
    return _BundleContents(
        manifest=manifest,
        events=events,
        chain=chain,
        declared_sha=declared_sha,
        run_payload=run_payload,
        scenario_text=scenario_text,
        decode_problems=decode_problems,
    )


def verify(path: Path) -> VerificationResult:
    """Recompute a bundle's hashes from its own events and report what disagrees.

    This is the whole point of the format. `manifest.json`, `events.sha256` and
    `events.chain` are the sender's claims; this function recomputes all three from
    `events.jsonl` and compares. A tampered event changes the rolling log hash, which says
    *that* something moved, and breaks the per-event chain, which says *where* — the
    `E-BUNDLE-001` message PRD §36 specifies names the failing event, and a rolling hash
    alone could never produce that number.

    Guarantees: **reports, does not raise**, for every problem with the *contents* of a
    well-formed archive — a recipient needs to see what is wrong. It does raise for an
    archive that is unsafe to read at all (path traversal, unknown or duplicated member,
    oversized member), because that is a refusal to process rather than a verdict on data.

    Returns:
        A `VerificationResult` whose `ok` is True iff every check passed.

    Raises:
        BundleError: `E-BUNDLE-001` the file is not a readable zip · `E-BUNDLE-005` the
            archive contains a member that is not on the allowlist, is duplicated, or is
            oversized · `E-BUNDLE-006` a required member is missing.
    """
    return _verify_contents(_read_bundle(path))


def _verify_contents(contents: _BundleContents) -> VerificationResult:
    """Verify already-read bundle contents. The single definition of "is this bundle good".

    Split from `verify` so `import_bundle` can verify the exact bytes it is about to store
    rather than a second read of the same path.
    """
    manifest = contents.manifest
    events = contents.events
    problems = list(contents.decode_problems)
    computed_hash = canonical_log_hash(events) if events else ""
    computed_chain = build_chain(events) if events else ()
    computed_head = computed_chain[-1][1] if computed_chain else CHAIN_GENESIS

    if manifest.schema_version != SCHEMA_VERSION:
        problems.append(
            f"[E-BUNDLE-002] bundle event schema_version {manifest.schema_version} does not "
            f"match this build's {SCHEMA_VERSION}; migrate the bundle or upgrade AgentDX"
        )
    if manifest.bundle_format_version > BUNDLE_FORMAT_VERSION:
        problems.append(
            f"[E-BUNDLE-002] bundle format version {manifest.bundle_format_version} is newer "
            f"than this build understands ({BUNDLE_FORMAT_VERSION})"
        )
    if contents.declared_sha != manifest.canonical_log_hash:
        problems.append(
            f"[E-BUNDLE-001] {EVENTS_SHA256} ({contents.declared_sha}) disagrees with "
            f"{MANIFEST} ({manifest.canonical_log_hash}) inside the same archive"
        )
    if computed_hash != manifest.canonical_log_hash:
        problems.append(
            f"[E-BUNDLE-001] recomputed canonical log hash {computed_hash} does not match "
            f"the declared {manifest.canonical_log_hash}; the event log has been altered"
        )
    if computed_head != manifest.chain_head:
        problems.append(
            f"[E-BUNDLE-001] recomputed chain head {computed_head} does not match the "
            f"declared {manifest.chain_head}"
        )
    if len(events) != manifest.event_count:
        problems.append(
            f"[E-BUNDLE-001] archive holds {len(events)} events; the manifest declares "
            f"{manifest.event_count}"
        )

    first_bad = _first_inconsistent_seq(events)
    if first_bad is None:
        first_bad = _first_broken_chain_seq(events, contents.chain, problems)
    if first_bad is not None:
        problems.append(f"[E-BUNDLE-001] bundle integrity check failed at event {first_bad}")

    return VerificationResult(
        ok=not problems,
        run_id=manifest.run_id,
        event_count=len(events),
        declared_log_hash=manifest.canonical_log_hash,
        computed_log_hash=computed_hash,
        declared_chain_head=manifest.chain_head,
        computed_chain_head=computed_head,
        first_bad_seq=first_bad,
        includes_cache_bodies=manifest.includes_cache_bodies,
        problems=tuple(problems),
    )


def _first_broken_chain_seq(
    events: Sequence[Event], chain: Sequence[str], problems: list[str]
) -> int | None:
    """Return the seq of the first event whose stored chain entry does not verify.

    This is what makes PRD §36's "failed at event 1043" deliverable. `events.chain` holds
    one `this_hash` per event in seq order; `prev_hash` is the preceding entry, or
    `CHAIN_GENESIS` for the first. Recomputing the chain over the events in the archive and
    comparing entry by entry locates the first divergence, which a rolling log hash cannot.

    Guarantees: appends its own problem and returns None when the chain member is the wrong
    length — a length mismatch is a different defect from a content mismatch and reporting
    it as "event N is bad" would name an event that is fine.
    """
    if not events:
        return None
    if len(chain) != len(events):
        problems.append(
            f"[E-BUNDLE-001] {EVENTS_CHAIN} holds {len(chain)} hashes for {len(events)} "
            f"events; the chain cannot be checked against the log"
        )
        return None
    stored = [
        (CHAIN_GENESIS if index == 0 else chain[index - 1], this)
        for index, this in enumerate(chain)
    ]
    return verify_chain(events, stored)


# ---------------------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------------------


def import_bundle(store: Store, path: Path, *, rebuild_state_snapshots: bool = True) -> str:
    """Register a verified bundle's run in a local store and return its `run_id`.

    Nothing from the archive is executed, imported as a module, or evaluated. The scenario
    is stored as text; the graph is stored as the identity object it is. This function
    cannot run a graph — `--verify`'s re-execution against a matching *local* graph is a CLI
    concern (P17), and keeping the capability out of this module is the strongest available
    form of §31.9.

    Guarantees:

    * **Verified first, and the verified bytes are the imported bytes.** The archive is read
      exactly once; verification runs over those contents and the same contents are stored.
      Re-opening the path to import after verifying it is a TOCTOU, and a file on a synced
      or shared directory can change between the two reads.
    * **All or nothing.** The whole import — run row, scenario, every event batch, findings,
      scorecard, seal and snapshots — runs inside one transaction. An interruption leaves
      the database exactly as it was, so a retry works. Anything less leaves a truncated log
      under a run row that already claims a status, and the half-written row then fails the
      idempotence check on every subsequent attempt.
    * **Idempotent by `run_id` + `canonical_log_hash`** (PRD §27.5). Re-importing the same
      bundle is a no-op that returns the same `run_id`.
    * **Never overwrites history.** A different log under an existing `run_id` is refused.
    * **Chain preserved.** The stored `prev_hash`/`this_hash` are rebuilt from the events
      and equal the values the exporter wrote, so the imported run verifies locally.

    Args:
        store: The destination store; may be empty.
        path: The `.agentdx` file.
        rebuild_state_snapshots: Rebuild `state_snapshots` from the imported log. Snapshots
            are derived data and are never carried in the bundle — shipping them would
            create a second, un-verifiable account of the run's state.

    Returns:
        The imported `run_id`.

    Raises:
        BundleError: `E-BUNDLE-001` verification failed · `E-BUNDLE-002` incompatible
            schema · `E-BUNDLE-005` unsafe archive member · `E-BUNDLE-007` a different log
            already exists under this `run_id`.
        EventValidationError: the events are individually well-formed per the manifest but
            do not satisfy the event contract — an untrusted log is validated in full
            before it is stored (§31.9).
    """
    contents = _read_bundle(path)
    result = _verify_contents(contents)
    if not result.ok:
        detail = "bundle verification failed:\n  " + "\n  ".join(result.problems)
        raise BundleError("E-BUNDLE-001", detail)

    manifest = contents.manifest
    events = contents.events
    validate_log(events)

    existing = store.get_run(manifest.run_id)
    if existing is not None:
        if existing.canonical_log_hash == manifest.canonical_log_hash:
            return manifest.run_id
        detail = (
            f"run {manifest.run_id!r} already exists in {store.path} with a different "
            f"canonical log hash ({existing.canonical_log_hash} vs "
            f"{manifest.canonical_log_hash}). Import is idempotent by run_id + hash and "
            f"will not replace a recorded log"
        )
        raise BundleError("E-BUNDLE-007", detail)

    record = _run_record_from_payload(manifest, contents.run_payload)
    chain = build_chain(events)
    batch = [
        ChainedEvent(event=event, prev_hash=prev, this_hash=this)
        for event, (prev, this) in zip(events, chain, strict=True)
    ]
    size = store.config.append_batch_size

    # One transaction for the whole import. Anything less leaves a truncated log behind a
    # run row that already claims a status, which is the condition NFR-13 exists to prevent
    # — and it also poisons the run_id, because the half-written row then fails the
    # idempotence check on every retry.
    with store.transaction():
        store.create_run(record)
        if contents.scenario_text and record.scenario_id:
            store.upsert_scenario(
                ScenarioRecord(
                    scenario_id=record.scenario_id,
                    content=contents.scenario_text,
                    content_hash=record.scenario_hash,
                    version=1,
                )
            )
        for start in range(0, len(batch), size):
            store.append(batch[start : start + size])
        for finding in _findings_from_payload(manifest.run_id, contents.run_payload):
            store.upsert_finding(finding)
        scorecard = _scorecard_from_payload(manifest.run_id, contents.run_payload)
        if scorecard is not None:
            store.upsert_scorecard(scorecard)
        store.seal(manifest.run_id, chain[-1][1])
        if rebuild_state_snapshots:
            write_snapshots(
                store,
                manifest.run_id,
                rebuild_snapshots(events, store.config.snapshot_interval_events),
            )
    return manifest.run_id


# ---------------------------------------------------------------------------------------
# Archive safety (§31.9)
# ---------------------------------------------------------------------------------------


class _SafeArchive:
    """A zip opened with every member name and size checked before anything is read."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        """Wrap an open archive whose members have already been checked."""
        self._archive = archive

    def read(self, name: str) -> bytes:
        """Return a member's bytes. The name has already been allowlisted."""
        return self._archive.read(name)

    def names(self) -> tuple[str, ...]:
        """Return the member names present, sorted."""
        return tuple(sorted(self._archive.namelist()))

    def __enter__(self) -> _SafeArchive:
        """Enter the archive context."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the underlying archive."""
        self._archive.close()


def _safe_archive(path: Path) -> _SafeArchive:
    """Open a bundle, refusing anything unsafe to read (§31.9).

    Checks, in order: the file is a readable zip; no member name appears twice; every
    member name is on `MEMBERS`; no name is absolute, contains `..`, or is a directory
    traversal in any form; no member's declared decompressed size exceeds
    `MAX_MEMBER_BYTES`; every required member is present. The allowlist makes the traversal
    check redundant, and it is kept anyway because a future member added to `MEMBERS` should
    not silently re-open the hole.

    Raises:
        BundleError: `E-BUNDLE-001` not a readable zip · `E-BUNDLE-005` an unsafe or
            unknown member · `E-BUNDLE-006` a required member is missing.
    """
    if not path.is_file():
        raise BundleError("E-BUNDLE-001", f"{path} does not exist")
    try:
        archive = zipfile.ZipFile(path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise BundleError("E-BUNDLE-001", f"{path} is not a readable zip archive: {exc}") from exc

    try:
        _check_members(archive)
    except BundleError:
        archive.close()
        raise
    return _SafeArchive(archive)


def _check_members(archive: zipfile.ZipFile) -> None:
    """Refuse an archive whose members are unsafe to read (§31.9).

    Separated from `_safe_archive` so the refusals are raised outside the `try` that owns
    closing the handle; the two concerns are "is this safe" and "do not leak a file
    descriptor when it is not", and mixing them is how one of them gets forgotten.

    Raises:
        BundleError: `E-BUNDLE-005` an unknown, traversing or oversized member ·
            `E-BUNDLE-006` a required member is missing.
    """
    names = [info.filename for info in archive.infolist()]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        detail = (
            f"archive contains duplicate member name(s) {duplicates}. Two entries with one "
            f"name is a zip-confusion attack: this reader resolves the name to one entry "
            f"while a different unzip tool resolves it to the other, so what was verified "
            f"is not what a user inspecting the file would see"
        )
        raise BundleError("E-BUNDLE-005", detail)
    for info in archive.infolist():
        name = info.filename
        if name not in MEMBERS:
            detail = (
                f"archive member {name!r} is not a permitted bundle member. A bundle "
                f"is data, not an archive to unpack (§31.9); permitted members are "
                f"{sorted(MEMBERS)}"
            )
            raise BundleError("E-BUNDLE-005", detail)
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or "\\" in name:
            detail = f"archive member {name!r} is a path traversal attempt"
            raise BundleError("E-BUNDLE-005", detail)
        if info.file_size > MAX_MEMBER_BYTES:
            detail = (
                f"archive member {name!r} declares {info.file_size} bytes, above the "
                f"{MAX_MEMBER_BYTES}-byte cap"
            )
            raise BundleError("E-BUNDLE-005", detail)
    present = tuple(archive.namelist())
    missing = [m for m in REQUIRED_MEMBERS if m not in present]
    if missing:
        detail = f"bundle is missing required member(s) {missing}"
        raise BundleError("E-BUNDLE-006", detail)


# ---------------------------------------------------------------------------------------
# Member readers
# ---------------------------------------------------------------------------------------


def _has(archive: _SafeArchive, name: str) -> bool:
    """Return True iff the archive contains this member."""
    return name in archive.names()


def _read_manifest(archive: _SafeArchive) -> BundleManifest:
    """Return the manifest, refusing one whose fields are absent or of the wrong type.

    Guarantees: every field is type-checked. A manifest is the first thing an attacker
    controls, so "missing key" and "string where an integer belongs" are refusals rather
    than exceptions from deep inside the verification arithmetic.

    Raises:
        BundleError: `E-BUNDLE-002` the manifest is not the shape this build reads.
    """
    raw = _read_json_object(archive, MANIFEST)
    return BundleManifest(
        bundle_format_version=_require_int(raw, "bundle_format_version"),
        schema_version=_require_int(raw, "schema_version"),
        agentdx_version=_require_str(raw, "agentdx_version"),
        run_id=_require_str(raw, "run_id"),
        canonical_log_hash=_require_str(raw, "canonical_log_hash"),
        chain_head=_require_str(raw, "chain_head"),
        event_count=_require_int(raw, "event_count"),
        created_at=_require_str(raw, "created_at"),
        includes_cache_bodies=bool(raw.get("includes_cache_bodies")),
        compression=str(raw.get("compression", "deflate")),
    )


def _read_events(archive: _SafeArchive) -> tuple[tuple[Event, ...], tuple[str, ...]]:
    """Return the decoded events and any per-line decode problems.

    Decode failures are collected rather than raised so `verify` can report "line 1043 is
    malformed" alongside the hash mismatch it causes, instead of one masking the other.
    """
    text = archive.read(EVENTS_JSONL).decode("utf-8")
    events: list[Event] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(decode_event(line))
        except (ValueError, TypeError, KeyError) as exc:
            problems.append(f"[E-BUNDLE-001] {EVENTS_JSONL} line {number} is malformed: {exc}")
    return tuple(events), tuple(problems)


def _read_json_object(archive: _SafeArchive, name: str) -> Mapping[str, PayloadValue]:
    """Return a member parsed as a JSON object.

    Raises:
        BundleError: `E-BUNDLE-002` the member is not valid JSON, or is not an object.
    """
    try:
        parsed: object = json.loads(archive.read(name).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BundleError("E-BUNDLE-002", f"{name} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise BundleError("E-BUNDLE-002", f"{name} does not contain a JSON object")
    return {str(k): _coerce(v) for k, v in parsed.items()}


def _coerce(value: object) -> PayloadValue:
    """Return an untrusted parsed value narrowed to `PayloadValue`.

    Floats are converted to their string form rather than refused: unlike an event payload,
    a bundle's `run.json` may legitimately carry a figure produced by another tool, and
    refusing the whole bundle over one would be disproportionate. Nothing derived from
    `run.json` enters the canonical projection, so ruling R4 is not weakened.
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _coerce(v) for k, v in value.items()}
    return str(value)


def _require_str(raw: Mapping[str, PayloadValue], key: str) -> str:
    """Return a required string manifest field.

    Raises:
        BundleError: `E-BUNDLE-002` the field is absent or not a string.
    """
    value = raw.get(key)
    if not isinstance(value, str):
        raise BundleError("E-BUNDLE-002", f"{MANIFEST} field {key!r} must be a string")
    return value


def _require_int(raw: Mapping[str, PayloadValue], key: str) -> int:
    """Return a required integer manifest field.

    Raises:
        BundleError: `E-BUNDLE-002` the field is absent or not an integer.
    """
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BundleError("E-BUNDLE-002", f"{MANIFEST} field {key!r} must be an integer")
    return value


# ---------------------------------------------------------------------------------------
# run.json
# ---------------------------------------------------------------------------------------


def _run_payload(store: Store, record: RunRecord) -> dict[str, PayloadValue]:
    """Return `run.json`: run metadata plus the derived findings and scorecard.

    Findings and the scorecard travel with the bundle because PRD §20.7 lists "verdict,
    scorecard, analysis version" — a recipient should see what the sender concluded without
    re-running analysis. They are *derived* data and are re-derivable from the log, which is
    why a mismatch between them and a local re-analysis is informative rather than fatal.
    """
    findings = store.list_findings(record.run_id)
    scorecard = store.get_scorecard(record.run_id)
    return {
        "run": {
            "run_id": record.run_id,
            "scenario_id": record.scenario_id,
            "scenario_hash": record.scenario_hash,
            "graph_hash": record.graph_hash,
            "mode": record.mode,
            "seed": record.seed,
            "status": record.status,
            "created_at": record.created_at,
            "sealed_at": record.sealed_at,
            "virtual_makespan_ms": record.virtual_makespan_ms,
            "wall_makespan_ms": record.wall_makespan_ms,
            "canonical_log_hash": record.canonical_log_hash,
            "event_count": record.event_count,
            "baseline_of": record.baseline_of,
            "replay_of": record.replay_of,
            "explore_parent": record.explore_parent,
            "agentdx_version": record.agentdx_version,
            "schema_version": record.schema_version,
            "delay_schedule": record.delay_schedule,
            "calibration_id": record.calibration_id,
            "determinism_quality": record.determinism_quality,
        },
        "findings": [
            {
                "finding_id": f.finding_id,
                "type": f.type,
                "subtype": f.subtype,
                "severity": f.severity,
                "title": f.title,
                "description": f.description,
                "evidence": dict(f.evidence),
                "recommendation": f.recommendation,
                "suppressed_by": f.suppressed_by,
                "repro_scenario_path": f.repro_scenario_path,
                "analysis_version": f.analysis_version,
            }
            for f in findings
        ],
        "scorecard": (
            None
            if scorecard is None
            else {
                "payload": dict(scorecard.payload),
                "analysis_version": scorecard.analysis_version,
                "computed_at": scorecard.computed_at,
            }
        ),
    }


def _run_record_from_payload(
    manifest: BundleManifest, payload: Mapping[str, PayloadValue]
) -> RunRecord:
    """Return the `RunRecord` to create locally for an imported bundle.

    Guarantees: `canonical_log_hash` is taken from the *verified* manifest, not from
    `run.json`, so the row records the hash this build recomputed and agreed with.

    Raises:
        BundleError: `E-BUNDLE-002` `run.json` has no `run` object.
    """
    run = payload.get("run")
    if not isinstance(run, Mapping):
        raise BundleError("E-BUNDLE-002", f"{RUN_JSON} has no 'run' object")
    return RunRecord(
        run_id=manifest.run_id,
        scenario_id=_opt_str(run.get("scenario_id")),
        scenario_hash=str(run.get("scenario_hash", "")),
        graph_hash=str(run.get("graph_hash", "")),
        mode=str(run.get("mode", "replay")),
        seed=_opt_int(run.get("seed")) or 0,
        status=str(run.get("status", "complete")),
        created_at=str(run.get("created_at", manifest.created_at)),
        agentdx_version=manifest.agentdx_version,
        schema_version=manifest.schema_version,
        virtual_makespan_ms=_opt_int(run.get("virtual_makespan_ms")),
        wall_makespan_ms=_opt_int(run.get("wall_makespan_ms")),
        baseline_of=_opt_str(run.get("baseline_of")),
        replay_of=_opt_str(run.get("replay_of")),
        explore_parent=_opt_str(run.get("explore_parent")),
        delay_schedule=_opt_str(run.get("delay_schedule")),
        calibration_id=_opt_str(run.get("calibration_id")),
        determinism_quality=_opt_str(run.get("determinism_quality")),
    )


def _findings_from_payload(
    run_id: str, payload: Mapping[str, PayloadValue]
) -> Iterator[FindingRecord]:
    """Yield the findings carried by `run.json`, skipping malformed entries.

    A malformed finding is skipped rather than fatal: findings are derived data and can be
    regenerated locally, whereas refusing the import would lose the log, which cannot.
    """
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return
    for entry in findings:
        if not isinstance(entry, Mapping):
            continue
        evidence = entry.get("evidence")
        yield FindingRecord(
            finding_id=str(entry.get("finding_id", "")),
            run_id=run_id,
            type=str(entry.get("type", "")),
            severity=str(entry.get("severity", "")),
            title=str(entry.get("title", "")),
            description=str(entry.get("description", "")),
            evidence=dict(evidence) if isinstance(evidence, Mapping) else {},
            analysis_version=str(entry.get("analysis_version", "")),
            subtype=_opt_str(entry.get("subtype")),
            recommendation=_opt_str(entry.get("recommendation")),
            suppressed_by=_opt_str(entry.get("suppressed_by")),
            repro_scenario_path=_opt_str(entry.get("repro_scenario_path")),
        )


def _scorecard_from_payload(
    run_id: str, payload: Mapping[str, PayloadValue]
) -> ScorecardRecord | None:
    """Return the scorecard carried by `run.json`, or None when there is none."""
    scorecard = payload.get("scorecard")
    if not isinstance(scorecard, Mapping):
        return None
    body = scorecard.get("payload")
    return ScorecardRecord(
        run_id=run_id,
        payload=dict(body) if isinstance(body, Mapping) else {},
        analysis_version=str(scorecard.get("analysis_version", "")),
        computed_at=str(scorecard.get("computed_at", "")),
    )


# ---------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------


def _first_inconsistent_seq(events: Sequence[Event]) -> int | None:
    """Return the first seq that is out of order or duplicated, or None.

    A log whose seq values are not gapless from 0 is not a log this system wrote. Checked
    here rather than left to `validate_log` so `verify` can report it without raising.

    This finds *ordering* damage only. Content tampering is located by
    `_first_broken_chain_seq`, which is a different question and a different mechanism.
    """
    for index, event in enumerate(events):
        if event.seq != index:
            return event.seq
    return None


def _stored_scenario(store: Store, record: RunRecord) -> str:
    """Return the scenario text recorded for a run, or an empty document."""
    if record.scenario_id is None:
        return ""
    scenario = store.get_scenario(record.scenario_id)
    return "" if scenario is None else scenario.content


def _graph_identity(record: RunRecord) -> dict[str, PayloadValue]:
    """Return the minimal graph identity when the caller supplies none.

    Identity, never code: the hash the run pinned, and nothing that could be executed
    (§31.9). A richer object — nodes, edges, tools — is supplied by the SDK at P04, which
    is the layer that knows the graph.
    """
    return {"graph_hash": record.graph_hash, "nodes": [], "edges": [], "tools": []}


def _payload_str(event: Event, key: str) -> str:
    """Return a string payload field, or the empty string when it is absent."""
    value = event.payload.get(key)
    return value if isinstance(value, str) else ""


def _opt_str(value: object) -> str | None:
    """Return a nullable string field, preserving None."""
    return None if value is None else str(value)


def _opt_int(value: object) -> int | None:
    """Return a nullable integer field, preserving None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except ValueError:
        return None


def store_error_from(exc: BundleError) -> StoreError:
    """Return the `StoreError` equivalent of a bundle failure, for uniform CLI handling."""
    return StoreError(exc.code, str(exc))


__all__ = [
    "BUNDLE_FORMAT_VERSION",
    "BUNDLE_SUFFIX",
    "CACHE_ENTRIES",
    "CACHE_MANIFEST",
    "CALIBRATION_JSON",
    "EVENTS_JSONL",
    "EVENTS_SHA256",
    "GRAPH_JSON",
    "MANIFEST",
    "MAX_MEMBER_BYTES",
    "MEMBERS",
    "REQUIRED_MEMBERS",
    "RUN_JSON",
    "SCENARIO_YAML",
    "BundleError",
    "BundleManifest",
    "CacheEntry",
    "VerificationResult",
    "derive_cache_manifest",
    "export_bundle",
    "import_bundle",
    "store_error_from",
    "verify",
]
