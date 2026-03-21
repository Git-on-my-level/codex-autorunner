from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes import system as system_routes_module
from codex_autorunner.surfaces.web.routes.system import build_system_routes


class _AlivePty:
    def isalive(self) -> bool:
        return True


class _AliveSession:
    def __init__(self) -> None:
        self.pty = _AlivePty()


def _build_app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(build_system_routes())
    app.state.config = None
    app.state.logger = logging.getLogger("test")
    app.state.terminal_sessions = {}
    return app


def test_system_update_warns_when_terminal_sessions_are_active(
    tmp_path: Path, monkeypatch
) -> None:
    app = _build_app(tmp_path)
    app.state.terminal_sessions = {"session-1": _AliveSession()}

    monkeypatch.setattr(
        system_routes_module,
        "resolve_update_paths",
        lambda config=None: SimpleNamespace(cache_dir=tmp_path / "update-cache"),
    )
    monkeypatch.setattr(
        system_routes_module,
        "_spawn_update_process",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not spawn")),
    )

    with TestClient(app) as client:
        response = client.post("/system/update", json={"target": "both"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "warning"
    assert payload["requires_confirmation"] is True
    assert "terminal session" in payload["message"].lower()


def test_system_update_force_bypasses_active_session_warning(
    tmp_path: Path, monkeypatch
) -> None:
    app = _build_app(tmp_path)
    app.state.terminal_sessions = {"session-1": _AliveSession()}

    observed: dict[str, Any] = {}

    monkeypatch.setattr(
        system_routes_module,
        "resolve_update_paths",
        lambda config=None: SimpleNamespace(cache_dir=tmp_path / "update-cache"),
    )

    def _fake_spawn_update_process(**kwargs: Any) -> None:
        observed.update(kwargs)

    monkeypatch.setattr(
        system_routes_module,
        "_spawn_update_process",
        _fake_spawn_update_process,
    )

    with TestClient(app) as client:
        response = client.post("/system/update", json={"target": "both", "force": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["requires_confirmation"] is False
    assert observed["update_target"] == "both"
