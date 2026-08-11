"""Definition of done: export → import into a *fresh* database → canonical hash identical.

A fresh database rather than the same one, because importing into the store that produced
the bundle would let a shared cache, a shared connection or a leftover row make the test
pass for the wrong reason. The importing store is opened on a different file, in a
different directory, with a different configuration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentdx.config import StoreConfig
from agentdx.events.canonical import canonical_log_hash
from agentdx.store import bundle as bundles
from agentdx.store.snapshots import (
    SnapshottingStore,
    rebuild_snapshots,
    state_at,
    state_by_replay,
    stored_snapshots,
)
from agentdx.store.sqlite import FindingRecord, ScorecardRecord, Store
from tests.unit.store.conftest import populate
from tests.unit.store.factories import build_log


@pytest.fixture
def source(tmp_path: Path) -> tuple[Store, str, Path]:
    """Return a populated source store, its run id, and a directory for bundles."""
    config = StoreConfig(snapshot_interval_events=5)
    store = SnapshottingStore.open(tmp_path / "source" / "agentdx.db", config=config)
    events = build_log(spans=10)
    run_id = populate(store, events)
    store.upsert_finding(
        FindingRecord(
            finding_id="f_0001",
            run_id=run_id,
            type="race",
            severity="critical",
            title="lost update on draft.module_a",
            description="two concurrent writes, second overwrote the first",
            evidence={"event_seqs": [4, 9], "computation": "vclock incomparable"},
            analysis_version="0.1.0",
        )
    )
    store.upsert_scorecard(
        ScorecardRecord(
            run_id=run_id,
            payload={"achieved_speedup_milli": 1420, "ideal_speedup_milli": 3000},
            analysis_version="0.1.0",
            computed_at="2026-08-11T09:05:00Z",
        )
    )
    return store, run_id, tmp_path / "bundles"


def test_round_trip_preserves_the_canonical_log_hash(
    source: tuple[Store, str, Path], tmp_path: Path
) -> None:
    """The definition-of-done check: the hash is identical after a full round trip.

    The canonical log hash is the quantity gate G3 compares (PRD §10.7). If a bundle could
    not preserve it, a run could not be shared and reproduced, which is the point of the
    format.
    """
    store, run_id, bundle_dir = source
    original_hash = store.canonical_log_hash(run_id)

    path = bundles.export_bundle(store, run_id, bundle_dir / "run.agentdx")

    with Store.open(tmp_path / "fresh" / "other.db", config=StoreConfig()) as fresh:
        imported = bundles.import_bundle(fresh, path)
        assert imported == run_id
        assert fresh.canonical_log_hash(run_id) == original_hash

        record = fresh.get_run(run_id)
        assert record is not None
        assert record.canonical_log_hash == original_hash


def test_round_trip_preserves_every_event_byte_for_byte(
    source: tuple[Store, str, Path], tmp_path: Path
) -> None:
    """Each event's canonical bytes survive export and import unchanged."""
    from agentdx.events.canonical import canonical_bytes

    store, run_id, bundle_dir = source
    original = [canonical_bytes(e) for e in store.read_events(run_id)]
    path = bundles.export_bundle(store, run_id, bundle_dir / "run.agentdx")

    with Store.open(tmp_path / "fresh" / "other.db") as fresh:
        bundles.import_bundle(fresh, path)
        assert [canonical_bytes(e) for e in fresh.read_events(run_id)] == original


def test_the_imported_run_verifies_its_own_chain(
    source: tuple[Store, str, Path], tmp_path: Path
) -> None:
    """The hash chain is rebuilt on import and verifies locally (PRD §9.7)."""
    store, run_id, bundle_dir = source
    head = store.chain_head(run_id)
    path = bundles.export_bundle(store, run_id, bundle_dir / "run.agentdx")

    with Store.open(tmp_path / "fresh" / "other.db") as fresh:
        bundles.import_bundle(fresh, path)
        assert fresh.verify_chain(run_id) is None
        assert fresh.chain_head(run_id) == head


def test_the_imported_run_is_sealed_and_refuses_appends(
    source: tuple[Store, str, Path], tmp_path: Path
) -> None:
    """An imported run is recorded history and is closed to further writes (I2)."""
    from agentdx.store.sqlite import StoreError
    from tests.unit.store.conftest import chain

    store, run_id, bundle_dir = source
    path = bundles.export_bundle(store, run_id, bundle_dir / "run.agentdx")

    with Store.open(tmp_path / "fresh" / "other.db") as fresh:
        bundles.import_bundle(fresh, path)
        record = fresh.get_run(run_id)
        assert record is not None and record.sealed
        with pytest.raises(StoreError) as excinfo:
            fresh.append(chain(build_log(spans=1))[:1])
        assert excinfo.value.code in {"E-STORE-004", "E-STORE-005"}


def test_findings_and_scorecard_travel_with_the_bundle(
    source: tuple[Store, str, Path], tmp_path: Path
) -> None:
    """PRD §20.7's `run.json` carries the verdict, scorecard and analysis version.

    A recipient sees what the sender concluded without re-running analysis. They are derived
    data, so a later disagreement with a local re-analysis is informative rather than fatal.
    """
    store, run_id, bundle_dir = source
    path = bundles.export_bundle(store, run_id, bundle_dir / "run.agentdx")

    with Store.open(tmp_path / "fresh" / "other.db") as fresh:
        bundles.import_bundle(fresh, path)
        findings = fresh.list_findings(run_id)
        assert [f.finding_id for f in findings] == ["f_0001"]
        assert findings[0].evidence["event_seqs"] == [4, 9]
        scorecard = fresh.get_scorecard(run_id)
        assert scorecard is not None
        assert scorecard.payload["achieved_speedup_milli"] == 1420


def test_import_is_idempotent_by_run_id_and_hash(
    source: tuple[Store, str, Path], tmp_path: Path
) -> None:
    """PRD §27.5: importing the same bundle twice is a no-op, not a duplicate or an error."""
    store, run_id, bundle_dir = source
    path = bundles.export_bundle(store, run_id, bundle_dir / "run.agentdx")

    with Store.open(tmp_path / "fresh" / "other.db") as fresh:
        assert bundles.import_bundle(fresh, path) == run_id
        count = fresh.event_count(run_id)
        assert bundles.import_bundle(fresh, path) == run_id
        assert fresh.event_count(run_id) == count
        assert len(fresh.list_runs()) == 1


def test_importing_a_different_log_under_the_same_run_id_is_refused(
    source: tuple[Store, str, Path], tmp_path: Path
) -> None:
    """`E-BUNDLE-007`: a different log under an existing `run_id` never replaces it.

    Idempotence is keyed on `run_id` *and* `canonical_log_hash` precisely so that a
    same-id-different-content bundle is a refusal rather than a silent overwrite of
    recorded history.
    """
    store, run_id, bundle_dir = source
    first = bundles.export_bundle(store, run_id, bundle_dir / "first.agentdx")

    other_store = SnapshottingStore.open(tmp_path / "other-source" / "agentdx.db")
    try:
        other_events = build_log(spans=3)
        populate(other_store, other_events)
        second = bundles.export_bundle(other_store, run_id, bundle_dir / "second.agentdx")
        assert canonical_log_hash(other_events) != store.canonical_log_hash(run_id)
    finally:
        other_store.close()

    with Store.open(tmp_path / "fresh" / "other.db") as fresh:
        bundles.import_bundle(fresh, first)
        with pytest.raises(bundles.BundleError) as excinfo:
            bundles.import_bundle(fresh, second)
        assert excinfo.value.code == "E-BUNDLE-007"
        assert fresh.canonical_log_hash(run_id) == store.canonical_log_hash(run_id)


def test_snapshots_are_rebuilt_on_import_not_shipped(
    source: tuple[Store, str, Path], tmp_path: Path
) -> None:
    """The bundle carries no snapshots; the importer regenerates them from the log.

    Shipping snapshots would put a second, un-verifiable account of the run's state inside
    an untrusted archive. Rebuilding them means the imported snapshots are provably
    consistent with the imported log — and identical to the exporter's, because both come
    from the same fold.
    """
    import zipfile

    store, run_id, bundle_dir = source
    path = bundles.export_bundle(store, run_id, bundle_dir / "run.agentdx")
    with zipfile.ZipFile(path, "r") as archive:
        assert not any("snapshot" in name for name in archive.namelist())

    config = StoreConfig(snapshot_interval_events=5)
    with Store.open(tmp_path / "fresh" / "other.db", config=config) as fresh:
        bundles.import_bundle(fresh, path)
        events = tuple(fresh.read_events(run_id))
        assert stored_snapshots(fresh, run_id) == rebuild_snapshots(events, 5)
        assert stored_snapshots(fresh, run_id) == stored_snapshots(store, run_id)


def test_state_reconstruction_agrees_across_the_round_trip(
    source: tuple[Store, str, Path], tmp_path: Path
) -> None:
    """§20.4 reconstruction gives the same answer on both sides of the bundle."""
    store, run_id, bundle_dir = source
    events = tuple(store.read_events(run_id))
    path = bundles.export_bundle(store, run_id, bundle_dir / "run.agentdx")

    config = StoreConfig(snapshot_interval_events=5)
    with Store.open(tmp_path / "fresh" / "other.db", config=config) as fresh:
        bundles.import_bundle(fresh, path)
        for event in events:
            timestamp = event.virtual_ts_ms
            assert state_at(fresh, run_id, timestamp) == state_at(store, run_id, timestamp)
            assert state_at(fresh, run_id, timestamp) == state_by_replay(fresh, run_id, timestamp)


def test_a_bundle_of_a_crashed_run_round_trips_too(tmp_path: Path) -> None:
    """A partial log is shareable. NFR-13 says it is analysable; this says it travels.

    A crashed run is often the interesting one — PRD §36 keeps the partial log for deadlocks
    precisely because it is evidence — so a format that could only carry complete runs would
    fail at the moment it mattered most.
    """
    config = StoreConfig(snapshot_interval_events=5)
    store = SnapshottingStore.open(tmp_path / "src" / "agentdx.db", config=config)
    try:
        events = build_log(spans=6, sealed=False)
        run_id = populate(store, events, seal=False)
        original = store.canonical_log_hash(run_id)
        path = bundles.export_bundle(store, run_id, tmp_path / "partial.agentdx")
    finally:
        store.close()

    result = bundles.verify(path)
    assert result.ok, result.problems

    with Store.open(tmp_path / "fresh" / "other.db", config=config) as fresh:
        bundles.import_bundle(fresh, path)
        assert fresh.canonical_log_hash(run_id) == original
        assert fresh.verify_chain(run_id) is None


# ---------------------------------------------------------------------------------------
# OP-2 regressions. Each failed before the OP-3 repair; the finding is named.
# ---------------------------------------------------------------------------------------


def test_a_failed_import_leaves_nothing_and_can_be_retried(
    source: tuple[Store, str, Path], tmp_path: Path
) -> None:
    """OP-2 F3: an interrupted import is atomic, and the run_id is not poisoned.

    Before the repair, `create_run` wrote the run row *first* and each `append` committed
    separately, so an I/O failure part-way left a truncated log under a row whose status
    came from the bundle — `status='complete'` over 16 of 72 events. That is exactly the
    condition NFR-13 exists to prevent, and the crash path avoids it only because a killed
    process commits nothing further.

    It was also unrecoverable: the half-written row has `canonical_log_hash = NULL`, so the
    idempotence check saw "same run_id, different hash" and refused every retry with
    `E-BUNDLE-007`. One transient failure permanently burned that run_id.
    """
    store, run_id, bundle_dir = source
    path = bundles.export_bundle(store, run_id, bundle_dir / "run.agentdx")
    total = store.event_count(run_id)

    with Store.open(
        tmp_path / "fresh" / "other.db", config=StoreConfig(append_batch_size=8)
    ) as fresh:
        healthy = fresh.append
        calls = {"n": 0}

        def flaky(batch: object) -> None:
            calls["n"] += 1
            if calls["n"] == 3:
                message = "simulated I/O failure mid-import"
                raise OSError(message)
            healthy(batch)  # type: ignore[arg-type]

        fresh.append = flaky  # type: ignore[assignment, method-assign]
        with pytest.raises(OSError, match="simulated I/O failure"):
            bundles.import_bundle(fresh, path)

        assert fresh.list_runs() == (), "a run row survived a rolled-back import"
        assert fresh.event_count(run_id) == 0, "events survived a rolled-back import"

        fresh.append = healthy  # type: ignore[method-assign]
        assert bundles.import_bundle(fresh, path) == run_id, "the run_id was poisoned"
        assert fresh.event_count(run_id) == total
        assert fresh.verify_chain(run_id) is None
        record = fresh.get_run(run_id)
        assert record is not None and record.sealed


def test_verification_and_import_read_the_same_bytes(
    source: tuple[Store, str, Path], tmp_path: Path
) -> None:
    """OP-2 F1: closes the TOCTOU between verifying a bundle and importing it.

    Before the repair, `import_bundle` called `verify(path)` — which opened, read and closed
    the file — and then reopened the same path to read the events it stored. Substituting
    the file between those two opens got an unverified log into the store while verification
    reported success. A synced folder or a shared temp directory is enough.

    The substitution below happens *after* verification and still has no effect, because the
    archive is now read exactly once and the verified contents are the stored contents.
    """
    store, run_id, bundle_dir = source
    good = bundles.export_bundle(store, run_id, bundle_dir / "good.agentdx")
    good_hash = store.canonical_log_hash(run_id)

    other = SnapshottingStore.open(tmp_path / "attacker" / "agentdx.db")
    try:
        populate(other, build_log(spans=2))
        evil = bundles.export_bundle(other, run_id, bundle_dir / "evil.agentdx")
        evil_hash = other.canonical_log_hash(run_id)
    finally:
        other.close()
    assert evil_hash != good_hash

    honest = bundles._verify_contents

    def swap_after_verifying(contents: object) -> object:
        result = honest(contents)  # type: ignore[arg-type]
        good.write_bytes(evil.read_bytes())  # the attacker substitutes the file
        return result

    bundles._verify_contents = swap_after_verifying  # type: ignore[assignment]
    try:
        with Store.open(tmp_path / "fresh" / "other.db") as fresh:
            bundles.import_bundle(fresh, good)
            assert fresh.canonical_log_hash(run_id) == good_hash
            assert fresh.canonical_log_hash(run_id) != evil_hash
    finally:
        bundles._verify_contents = honest  # type: ignore[assignment]


def test_seal_checkpoints_the_wal(tmp_path: Path) -> None:
    """PRD §27.3: "WAL checkpoint at run seal", so the `.db` alone is a complete copy.

    Before the repair the checkpoint only happened at `close()`, so a run sealed by a
    long-lived process left its most recent events in the `-wal` file — and §27.5's
    "copying the data dir is the backup" quietly became "copying the .db loses data".
    """
    store = SnapshottingStore.open(tmp_path / "agentdx.db", config=StoreConfig())
    try:
        populate(store, build_log(spans=6))
        wal = Path(str(store.path) + "-wal")
        assert not wal.exists() or wal.stat().st_size == 0
    finally:
        store.close()
