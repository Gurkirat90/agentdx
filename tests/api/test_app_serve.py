"""`api/app.py`: `create_app` wiring and `serve`'s Design Constraint 3 (0.0.0.0 binding)."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agentdx.api.app import _is_loopback, create_app, serve
from agentdx.config import AgentDXConfig


def test_create_app_mounts_rest_under_api_and_ws_at_root(
    api_config: AgentDXConfig, db_path: Path
) -> None:
    """Every REST router lives under `/api`; the WS route does not (PRD §26.2's one exception)."""
    app = create_app(config=api_config, store_path=db_path)
    schema = app.openapi()
    paths = set(schema["paths"])
    assert "/api/health" in paths
    assert "/api/runs" in paths
    assert "/api/runs/{run_id}/findings" in paths
    assert "/api/ws/runs/{run_id}" not in paths
    # FastAPI does not document WS routes in the OpenAPI schema, and this FastAPI version
    # wraps included routers in `_IncludedRouter` objects with no inspectable `.path` — the
    # end-to-end way to confirm the route is really mounted at `/ws/...`, not `/api/ws/...`,
    # is to connect to it: `test_ws.py` does exactly that against every app this fixture
    # builds, so a route mounted at the wrong path would already be failing there.
    with TestClient(app) as client:
        # `r_missing` doesn't exist, so a *mounted* route closes 4004 (PRD §26.2) — proof the
        # route exists and runs, not just that a connection attempt didn't crash outright.
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/ws/runs/r_missing"),
        ):
            pass
        assert excinfo.value.code == 4004


def test_serve_refuses_non_loopback_host_without_allow_non_local(
    api_config: AgentDXConfig, db_path: Path
) -> None:
    """Design Constraint 3: `0.0.0.0` without the explicit confirmation flag raises, never binds."""
    with pytest.raises(ValueError, match=re.escape("0.0.0.0")):
        serve(host="0.0.0.0", port=9999, config=api_config, store_path=db_path)


def test_serve_loopback_host_needs_no_confirmation(
    api_config: AgentDXConfig, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`127.0.0.1` never prints the warning and never requires `allow_non_local`."""
    with patch("agentdx.api.app.uvicorn.run") as mock_run:
        serve(host="127.0.0.1", port=9999, config=api_config, store_path=db_path)
    assert mock_run.call_count == 1
    assert capsys.readouterr().err == ""


def test_serve_non_loopback_with_allow_non_local_prints_the_warning_verbatim(
    api_config: AgentDXConfig, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`allow_non_local=True` (the CLI's `--host` flag) binds and prints the exact warning text.

    `uvicorn.run` is mocked — this asserts the guard/warning/wiring, not a real socket bind
    (a real bind is exercised manually; see CONTEXT.md's session log for the pasted output).
    """
    with patch("agentdx.api.app.uvicorn.run") as mock_run:
        serve(
            host="0.0.0.0",
            port=9999,
            allow_non_local=True,
            config=api_config,
            store_path=db_path,
        )
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "0.0.0.0:9999" in captured.err
    assert "NO AUTHENTICATION" in captured.err
    mock_run.assert_called_once()
    _app_arg, kwargs = mock_run.call_args
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9999


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("10.0.0.5", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    """`_is_loopback` recognises exactly the addresses Design Constraint 3 exempts."""
    assert _is_loopback(host) is expected


def test_serve_defaults_come_from_config(api_config: AgentDXConfig, db_path: Path) -> None:
    """Omitting `host`/`port` uses `config.api.host`/`.port`, not a hardcoded literal."""
    with patch("agentdx.api.app.uvicorn.run") as mock_run:
        serve(config=api_config, store_path=db_path)
    _app_arg, kwargs = mock_run.call_args
    assert kwargs["host"] == api_config.api.host
    assert kwargs["port"] == api_config.api.port


def test_created_app_actually_serves(api_config: AgentDXConfig, db_path: Path) -> None:
    """Sanity: the app `create_app` builds is a real, working ASGI app end-to-end."""
    app = create_app(config=api_config, store_path=db_path)
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200


def test_no_static_dir_means_root_404s_the_ordinary_way(
    api_config: AgentDXConfig, db_path: Path
) -> None:
    """No `static/` (every environment but a Docker image built by this project) — `/` 404s.

    `_STATIC_DIR` is a real, fixed path (`api/static/`) that does not exist in a source
    checkout or this test suite's own environment, so this is the actual, unpatched default —
    proof `create_app` does not invent a frontend that was never built.
    """
    app = create_app(config=api_config, store_path=db_path)
    with TestClient(app) as client:
        assert client.get("/").status_code == 404


def test_static_dir_present_serves_index_at_root(
    api_config: AgentDXConfig, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a built frontend present, `/` serves its `index.html`, byte for byte."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>Control Tower</title>")
    monkeypatch.setattr("agentdx.api.app._STATIC_DIR", static_dir)

    app = create_app(config=api_config, store_path=db_path)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Control Tower" in response.text


def test_static_dir_present_serves_a_real_asset_file(
    api_config: AgentDXConfig, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real file under `static/` (Vite's hashed bundle output) is served as itself."""
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html><title>Control Tower</title>")
    (static_dir / "assets" / "index-deadbeef.js").write_text("console.log('agentdx');")
    monkeypatch.setattr("agentdx.api.app._STATIC_DIR", static_dir)

    app = create_app(config=api_config, store_path=db_path)
    with TestClient(app) as client:
        response = client.get("/assets/index-deadbeef.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_spa_fallback_serves_index_for_a_client_side_route(
    api_config: AgentDXConfig, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deep link / hard refresh on a client-side route falls back to `index.html`.

    `frontend/src/routes/router.tsx` is a real History-API router (`/runs/{id}`), not a hash
    router — without this fallback, refreshing anywhere but `/` 404s. No file exists at
    `/runs/r_abc123` on disk; the SPA shell is what must load so the client router can render it.
    """
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>Control Tower</title>")
    monkeypatch.setattr("agentdx.api.app._STATIC_DIR", static_dir)

    app = create_app(config=api_config, store_path=db_path)
    with TestClient(app) as client:
        response = client.get("/runs/r_abc123/scorecard")
    assert response.status_code == 200
    assert "Control Tower" in response.text


def test_static_catch_all_never_shadows_api_or_ws_routes(
    api_config: AgentDXConfig, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frontend catch-all is registered last — `/api/health` still resolves, not the SPA shell.

    Regression guard for the ordering `_mount_frontend`'s own docstring depends on: if a future
    edit moved `_mount_frontend`'s call before `app.include_router(api_router)`, this is the
    test that would catch it — `/api/health` would start returning `index.html` instead of the
    real health payload.
    """
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>Control Tower</title>")
    monkeypatch.setattr("agentdx.api.app._STATIC_DIR", static_dir)

    app = create_app(config=api_config, store_path=db_path)
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert "Control Tower" not in response.text


def test_static_catch_all_rejects_path_traversal(
    api_config: AgentDXConfig, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crafted path cannot escape `static_dir` to read an arbitrary file off the image.

    Writes a real secret file just outside `static_dir` and confirms a traversal attempt
    returns the SPA shell (the safe fallback), never the secret's contents.
    """
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>Control Tower</title>")
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-serve-me")
    monkeypatch.setattr("agentdx.api.app._STATIC_DIR", static_dir)

    app = create_app(config=api_config, store_path=db_path)
    with TestClient(app) as client:
        response = client.get("/../secret.txt")
    assert "do-not-serve-me" not in response.text
