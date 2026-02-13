from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.integrations.app_server.client import (
    CodexAppServerResponseError,
)
from codex_autorunner.surfaces.web.routes.app_server import build_app_server_routes


class _StubClient:
    def __init__(
        self,
        *,
        response: Any,
        fail_agent_request: bool = False,
        fail_code: int = -32602,
    ) -> None:
        self._response = response
        self._fail_agent_request = fail_agent_request
        self._fail_code = fail_code
        self.calls: list[dict[str, Any]] = []

    async def model_list(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        if kwargs.get("agent") == "codex" and self._fail_agent_request:
            raise CodexAppServerResponseError(
                method="model/list",
                code=self._fail_code,
                message="invalid params",
            )
        return self._response


class _StubSupervisor:
    def __init__(self, client: _StubClient) -> None:
        self._client = client

    async def get_client(self, _workspace_root: Path) -> _StubClient:
        return self._client


class _StubEngine:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root


def _build_app(client: _StubClient) -> FastAPI:
    app = FastAPI()
    app.state.engine = _StubEngine(Path("."))
    app.state.app_server_supervisor = _StubSupervisor(client)
    app.include_router(build_app_server_routes())
    return app


def test_app_server_models_uses_codex_agent_filter() -> None:
    client = _StubClient(response={"data": [{"id": "gpt-5.3-codex-spark"}]})
    app = _build_app(client)

    with TestClient(app) as test_client:
        response = test_client.get("/api/app-server/models")

    assert response.status_code == 200
    assert client.calls == [{"agent": "codex"}]


@pytest.mark.parametrize("fail_code", [-32600, -32602])
def test_app_server_models_falls_back_when_agent_filter_is_unsupported(
    fail_code: int,
) -> None:
    client = _StubClient(
        response={"data": [{"id": "gpt-5.3-codex-spark"}]},
        fail_agent_request=True,
        fail_code=fail_code,
    )
    app = _build_app(client)

    with TestClient(app) as test_client:
        response = test_client.get("/api/app-server/models")

    assert response.status_code == 200
    assert client.calls == [{"agent": "codex"}, {}]


def test_app_server_models_returns_bad_gateway_on_non_compat_errors() -> None:
    client = _StubClient(
        response={"data": []},
        fail_agent_request=True,
        fail_code=-32001,
    )
    app = _build_app(client)

    with TestClient(app) as test_client:
        response = test_client.get("/api/app-server/models")

    assert response.status_code == 502
    assert client.calls == [{"agent": "codex"}]
