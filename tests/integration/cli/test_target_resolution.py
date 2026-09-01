"""Regression tests for `run.py::_is_scenario_path` — the fixture-vs-scenario-directory bug.

**What was wrong.** `_is_scenario_path` ended in a bare `return path.is_dir()`, so *every*
existing directory was claimed as a directory of scenario files. `_execute` consults this
predicate before anything else, so `agentdx run fixtures/code_pipeline` — PRD §38.1's own
literal quickstart command, and the command `docker-compose.yml`'s `seed` service and
`just demo-offline` both issue three times — was routed to `discover_scenarios`, which globbed
`fixtures/code_pipeline/` for `*.yaml`, found none, and returned exit 7
(`"no scenario files found under fixtures/code_pipeline"`). `resolve_target` and
`is_fixture_name` — which have always handled the `fixtures/<name>` path form correctly — were
never reached.

**Why it matters beyond one command.** This is the confirmed cause of gate **G9**'s exit 7,
recorded in CONTEXT.md §6 since 2026-08-27, and of the identical exit 7 from the Docker demo's
`seed` container, observed on real hardware 2026-08-29. Neither was the `sdk/`-spawn deadlock
(**D-62**) that both had previously been assumed to be: exit 7 is raised *before* any agent
runs, and D-62 surfaces as exit 5. Fixing this does not make G9 or G10 pass — it moves both
from "the target never resolved" to "the run deadlocked", which is the honest next failure.
See **D-77**.

**The first repair was too broad, and these tests did not catch it.** `_is_scenario_path` was
narrowed by delegating to `is_fixture_name`, which reduced its argument to `Path(x).name` — the
basename, parent path discarded. That claimed *any* directory sharing a name with a fixture, so
`my_scenarios/code_pipeline/` (a real directory of scenario files) resolved to the shipped
`code_pipeline` fixture and ran the wrong graph, exit 0, silently. The original suite swept
every `fixtures/<name>` and reported the fix "general"; generality across members proves nothing
about non-members that collide on the same key. See `test_a_scenario_directory_named_after_a_
fixture_is_still_claimed`, which fails against that first repair.

**Discriminating power depends on the working directory, so these tests pin it.** Every
assertion here passes a *relative* target, which `Path.is_dir()` resolves against the process
cwd, while `repo_root` is absolute. Run from anywhere but the checkout root, the two disagree
and the direct regression assertions pass vacuously — against the unfixed code too. `chdir` is
therefore part of each test's setup, not incidental, and
`test_the_predicate_does_not_depend_on_the_working_directory` asserts the property directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentdx.cli._target import is_fixture_name
from agentdx.cli.commands.run import _is_scenario_path


@pytest.fixture(autouse=True)
def _pin_cwd(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    """Resolve every relative target in this module against the checkout root.

    Without this the relative-path assertions below are satisfied by any cwd in which the path
    simply does not exist, which is indistinguishable from the property under test holding.
    """
    monkeypatch.chdir(repo_root)


def test_a_fixture_directory_is_not_a_scenario_directory(repo_root: Path) -> None:
    """A path naming a real fixture must fall through to the fixture branch.

    This is the regression test for D-77. Against the pre-fix
    `return path.is_dir()` this assertion fails, because the directory exists.
    """
    assert (repo_root / "fixtures" / "code_pipeline" / "graph.py").is_file()
    assert _is_scenario_path("fixtures/code_pipeline") is False


def test_every_shipped_fixture_is_excluded_not_just_code_pipeline(repo_root: Path) -> None:
    """The fix must be general, not a special case for the one fixture that exposed it.

    CONTEXT.md's own history is the reason this test exists: C-13 was first repaired with a
    denylist containing a single directory name, and the second independent OP-2 found the
    identical bug still live against a second real directory. A fix that names one fixture
    would repeat that exactly.
    """
    fixture_dirs = [
        entry
        for entry in sorted((repo_root / "fixtures").iterdir())
        if entry.is_dir() and (entry / "graph.py").is_file()
    ]
    assert len(fixture_dirs) >= 3, "expected the three reference fixtures to be present"
    for entry in fixture_dirs:
        assert _is_scenario_path(f"fixtures/{entry.name}") is False, entry.name
        assert is_fixture_name(f"fixtures/{entry.name}") == entry.name


def test_a_trailing_slash_does_not_reopen_the_bug(repo_root: Path) -> None:
    """`fixtures/code_pipeline/` must behave identically to `fixtures/code_pipeline`.

    `is_fixture_name` strips a trailing separator; this asserts the predicate inherits that
    rather than re-deriving its own path handling.
    """
    assert (repo_root / "fixtures" / "code_pipeline").is_dir()
    assert _is_scenario_path("fixtures/code_pipeline/") is False


def test_a_real_scenario_directory_is_still_claimed(repo_root: Path) -> None:
    """The fix must not break the branch it narrows — `scenarios/` still routes to scenarios."""
    assert list((repo_root / "scenarios").glob("*.yaml")), "expected shipped scenario files"
    assert _is_scenario_path("scenarios") is True


def test_a_single_scenario_file_is_still_claimed(repo_root: Path) -> None:
    """A `.yaml` file target is unaffected by the directory-branch change."""
    assert (repo_root / "scenarios" / "kill_reviewer.yaml").is_file()
    assert _is_scenario_path("scenarios/kill_reviewer.yaml") is True


def test_the_fixtures_parent_directory_is_still_a_scenario_directory(repo_root: Path) -> None:
    """`fixtures/` itself is not a fixture, so it keeps the scenario-directory reading.

    There is no `fixtures/fixtures/graph.py`, so `is_fixture_name` declines it and the
    predicate claims it — which is correct: a directory *of* fixtures is not itself runnable
    as one, and globbing it for scenarios is the sensible remaining interpretation.
    """
    assert not (repo_root / "fixtures" / "fixtures" / "graph.py").is_file()
    assert _is_scenario_path("fixtures") is True


def test_an_empty_directory_is_still_claimed_and_still_exits_seven(tmp_path: Path) -> None:
    """An ordinary empty directory keeps the old behaviour: claimed, then legitimately empty.

    Exit 7 is the right answer for a directory that genuinely holds no scenarios. The bug was
    never that exit 7 exists — it was that a fixture reached it.
    """
    empty = tmp_path / "no_scenarios_here"
    empty.mkdir()
    assert _is_scenario_path(str(empty)) is True


def test_a_nonexistent_path_is_not_a_scenario_path(tmp_path: Path) -> None:
    """A path that does not exist is neither a scenario file nor a scenario directory."""
    assert _is_scenario_path(str(tmp_path / "does_not_exist")) is False


def test_a_scenario_directory_named_after_a_fixture_is_still_claimed(tmp_path: Path) -> None:
    """A directory of scenarios keeps its §37.1 reading even when named after a fixture.

    The regression test for the *first* repair of D-77, which reduced its argument to
    `Path(x).name` and so claimed any directory sharing a fixture's basename. Against that
    version this returns False and the shipped `code_pipeline` graph runs instead of the
    user's scenarios — silently, exit 0. `TARGET` is "a fixture name ... or a directory of
    scenarios" (§37.1): a name collision does not merge the two.
    """
    collision = tmp_path / "code_pipeline"
    collision.mkdir()
    (collision / "some_scenario.yaml").write_text("name: x\n", encoding="utf-8")
    assert is_fixture_name(str(collision)) is None
    assert _is_scenario_path(str(collision)) is True


def test_an_explicitly_local_directory_is_not_the_fixture_of_the_same_name(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path
) -> None:
    """`./code_pipeline` means the directory here; bare `code_pipeline` means the fixture.

    PRD §37.1 is silent on which reading wins, so D-77 records the precedence rather than
    leaving it to whichever branch happens to run first. This asserts the escape hatch works:
    `pathlib` normalises `./x` to `x`, so a fix discriminating on `Path.parts` rather than on
    the raw string would route this into the bare-name branch and answer both the same way.

    `root=repo_root` is passed explicitly so both halves are decided by the precedence rule
    and not by whether a checkout happens to be findable from `tmp_path` — otherwise the
    dotted half would return `None` for the uninteresting reason that there is no repo here.
    """
    local = tmp_path / "code_pipeline"
    local.mkdir()
    (local / "s.yaml").write_text("name: x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert is_fixture_name("code_pipeline", root=repo_root) == "code_pipeline"
    assert is_fixture_name("./code_pipeline", root=repo_root) is None
    assert _is_scenario_path("./code_pipeline") is True


def test_an_absolute_fixture_path_resolves_from_outside_the_checkout(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path
) -> None:
    """A fixture path stays a fixture path when the caller is standing somewhere else.

    `find_repo_root()` walks up from the cwd, so the first repair of D-77 found no checkout
    here, declined the fixture, and handed the path back to `discover_scenarios` — exit 7,
    the original bug reproduced by the fixed code. The container only escaped it because
    `WORKDIR /app` happens to be the checkout root.
    """
    monkeypatch.chdir(tmp_path)
    target = str(repo_root / "fixtures" / "code_pipeline")
    assert is_fixture_name(target) == "code_pipeline"
    assert _is_scenario_path(target) is False


def test_the_predicate_does_not_depend_on_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path
) -> None:
    """The same absolute target gets the same answer from every working directory.

    Stated as its own property because both D-77 defects were cwd-sensitivity in different
    clothes, and because the assertions above use relative paths that cannot express it.
    """
    target = str(repo_root / "fixtures" / "code_pipeline")
    answers = set()
    for cwd in (repo_root, repo_root / "fixtures", tmp_path):
        monkeypatch.chdir(cwd)
        answers.add((is_fixture_name(target), _is_scenario_path(target)))
    assert answers == {("code_pipeline", False)}
