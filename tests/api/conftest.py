"""Shared fixtures for `tests/api/`: a wired app + client over a populated store file.

Reuses `tests/unit/store/factories.py`/`conftest.py` (`build_log`, `populate`,
`run_record_for`) for realistic event logs rather than inventing a second log-building
helper — a run these tests exercise `GET`/`WS` endpoints against should be exactly the same
shape `store/`'s own suite already trusts.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentdx.api.app import create_app
from agentdx.api.deps import FaultArmResult, LaunchResult
from agentdx.config import AgentDXConfig
from agentdx.events.schema import Event
from agentdx.store.sqlite import Store
from tests.unit.store.conftest import populate
from tests.unit.store.factories import AGENTS, build_log


@dataclass(slots=True)
class FakeRunLauncher:
    """A `RunLauncher` test double: assigns a fixed identity, records what it was asked."""

    run_id: str = "r_fake01"
    graph_hash: str = "blake2b:fakegraph"
    calls: list[dict[str, object]] | None = None

    def launch(self, *, scenario_id: str, mode: str, seed: int, explore: bool) -> LaunchResult:
        """Return the fixed identity without starting anything real."""
        if self.calls is not None:
            self.calls.append(
                {"scenario_id": scenario_id, "mode": mode, "seed": seed, "explore": explore}
            )
        return LaunchResult(run_id=self.run_id, graph_hash=self.graph_hash)


@dataclass(slots=True)
class FakeFaultController:
    """A `FaultController` test double: arms a fixed identity, records what it was asked."""

    fault_id: str = "f_fake01"
    armed_at_virtual_ts: int = 1234
    calls: list[dict[str, object]] | None = None

    def arm(
        self,
        *,
        run_id: str,
        fault_type: str,
        target: str,
        params: dict[str, object],
        immediate: bool,
    ) -> FaultArmResult:
        """Return the fixed identity without arming anything real."""
        if self.calls is not None:
            self.calls.append(
                {
                    "run_id": run_id,
                    "fault_type": fault_type,
                    "target": target,
                    "params": params,
                    "immediate": immediate,
                }
            )
        return FaultArmResult(fault_id=self.fault_id, armed_at_virtual_ts=self.armed_at_virtual_ts)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return the one store file this test's app and any direct `Store.open` calls share."""
    return tmp_path / "agentdx.db"


@pytest.fixture
def scenarios_dir(tmp_path: Path) -> Path:
    """Return an empty, dedicated `[api] scenarios_dir` for this test — never the repo's own."""
    directory = tmp_path / "scenarios"
    directory.mkdir()
    return directory


@pytest.fixture
def api_config(tmp_path: Path, scenarios_dir: Path) -> AgentDXConfig:
    """Return a resolved config pointed at this test's temp data dir and scenarios dir.

    `AgentDXConfig.load`'s own precedence (PRD §8.7, `config.py::_resolve`'s docstring) puts
    `agentdx.toml` *above* the per-call argument layer this fixture would otherwise use —
    and `AgentDXConfig.load(config_path=None)` searches from the current working directory
    upwards, which finds this repository's own `agentdx.toml` (`[api] scenarios_dir =
    "scenarios"`) during a test run. An empty temp TOML file, passed explicitly, is what
    actually isolates a test from that file rather than merely intending to.
    """
    empty_config_file = tmp_path / "empty-agentdx.toml"
    empty_config_file.write_text("", encoding="utf-8")
    return AgentDXConfig.load(
        config_path=empty_config_file,
        store={"data_dir": str(tmp_path)},
        api={"scenarios_dir": str(scenarios_dir)},
    )


@pytest.fixture
def app(api_config: AgentDXConfig, db_path: Path) -> FastAPI:
    """Return a wired app with no launcher/controller configured (the `503`-refusal default)."""
    return create_app(config=api_config, store_path=db_path)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Return a `TestClient` for `app`."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def app_with_launcher(
    api_config: AgentDXConfig, db_path: Path
) -> tuple[FastAPI, FakeRunLauncher, FakeFaultController]:
    """Return an app wired with fake launcher/controller, plus the fakes themselves."""
    launcher = FakeRunLauncher(calls=[])
    controller = FakeFaultController(calls=[])
    wired = create_app(
        config=api_config, store_path=db_path, run_launcher=launcher, fault_controller=controller
    )
    return wired, launcher, controller


@pytest.fixture
def client_with_launcher(
    app_with_launcher: tuple[FastAPI, FakeRunLauncher, FakeFaultController],
) -> Iterator[tuple[TestClient, FakeRunLauncher, FakeFaultController]]:
    """Return a `TestClient` over `app_with_launcher`, plus the fakes it was wired with."""
    wired, launcher, controller = app_with_launcher
    with TestClient(wired) as test_client:
        yield test_client, launcher, controller


def make_sealed_run(
    db_path: Path, api_config: AgentDXConfig, *, spans: int = 4, run_id: str = "r_f2a91"
) -> tuple[str, tuple[Event, ...]]:
    """Create, populate and seal one run directly against `db_path`; return `(run_id, events)`.

    `run_id` defaults to `build_log`'s own default (`RUN_ID`) — pass a distinct value when
    calling this more than once against the same `db_path` (`Store.create_run` rejects a
    second row under the same id, `E-STORE-010`).
    """
    store = Store.open(db_path, config=api_config.store)
    try:
        events = build_log(spans=spans, run_id=run_id)
        created_run_id = populate(store, events)
    finally:
        store.close()
    return created_run_id, events


@pytest.fixture
def sealed_run(db_path: Path, api_config: AgentDXConfig) -> tuple[str, tuple[Event, ...]]:
    """Populate `db_path` with one sealed, complete run and return `(run_id, events)`."""
    return make_sealed_run(db_path, api_config)


@pytest.fixture
def running_run(db_path: Path, api_config: AgentDXConfig) -> tuple[str, tuple[Event, ...]]:
    """Populate `db_path` with one unsealed, `status="running"` run, for fault-injection tests."""
    store = Store.open(db_path, config=api_config.store)
    try:
        events = build_log(spans=2, sealed=False)
        run_id = populate(store, events, seal=False)
    finally:
        store.close()
    return run_id, events


__all__ = [
    "AGENTS",
    "FakeFaultController",
    "FakeRunLauncher",
    "make_sealed_run",
]
