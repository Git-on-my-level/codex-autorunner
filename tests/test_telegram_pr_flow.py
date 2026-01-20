import types
from pathlib import Path
from typing import Optional

import httpx
import pytest

import codex_autorunner.integrations.telegram.handlers.commands.github as github_commands
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord


class _PrFlowHandlerStub(TelegramCommandHandlers):
    def __init__(self, *, hub_root: Optional[Path]) -> None:
        self._hub_root = hub_root
        self._manifest_path = None


def _record(**kwargs: object) -> TelegramTopicRecord:
    return TelegramTopicRecord(**kwargs)


def test_pr_flow_api_base_repo_mode_builds_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _PrFlowHandlerStub(hub_root=None)
    config = types.SimpleNamespace(
        server_host="localhost",
        server_port=8123,
        server_base_path="/car",
        server_auth_token_env=None,
    )
    monkeypatch.setattr(github_commands, "load_repo_config", lambda *_a, **_k: config)
    record = _record(workspace_path="/tmp/workspace")
    base, headers = handler._pr_flow_api_base(record)
    assert base == "http://localhost:8123/car"
    assert headers == {}


def test_pr_flow_api_base_hub_mode_includes_repo_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _PrFlowHandlerStub(hub_root=Path("/tmp/hub"))
    config = types.SimpleNamespace(
        server_host="https://example.com",
        server_port=9999,
        server_base_path="hub",
        server_auth_token_env="CAR_AUTH",
    )
    monkeypatch.setenv("CAR_AUTH", "token-123")
    monkeypatch.setattr(github_commands, "load_hub_config", lambda *_a, **_k: config)
    record = _record(repo_id="repo-1")
    base, headers = handler._pr_flow_api_base(record)
    assert base == "https://example.com/hub/repos/repo-1"
    assert headers["Authorization"] == "Bearer token-123"


@pytest.mark.anyio
async def test_pr_flow_request_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _PrFlowHandlerStub(hub_root=None)
    config = types.SimpleNamespace(
        server_host="localhost",
        server_port=8451,
        server_base_path="/car",
        server_auth_token_env="CAR_AUTH",
    )
    monkeypatch.setenv("CAR_AUTH", "token-456")
    monkeypatch.setattr(github_commands, "load_repo_config", lambda *_a, **_k: config)
    record = _record(workspace_path="/tmp/workspace")
    calls: list[tuple[str, str, object, object]] = []

    class _ClientStub:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        async def __aenter__(self) -> "_ClientStub":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            json: Optional[dict[str, object]] = None,
            headers: Optional[dict[str, str]] = None,
        ) -> httpx.Response:
            calls.append((method, url, json, headers))
            request = httpx.Request(method, url)
            return httpx.Response(
                200,
                json={"status": "ok", "flow": {"status": "idle"}},
                request=request,
            )

    monkeypatch.setattr(github_commands.httpx, "AsyncClient", _ClientStub)

    payload = {"mode": "issue", "issue": "123"}
    data = await handler._pr_flow_request(
        record,
        method="POST",
        path="/api/github/pr_flow/start",
        payload=payload,
    )

    assert data["status"] == "ok"
    assert calls
    method, url, sent_payload, sent_headers = calls[0]
    assert method == "POST"
    assert url == "http://localhost:8451/car/api/github/pr_flow/start"
    assert sent_payload == payload
    assert sent_headers["Authorization"] == "Bearer token-456"


@pytest.mark.anyio
async def test_pr_flow_request_missing_base_is_explicit() -> None:
    handler = _PrFlowHandlerStub(hub_root=None)
    record = _record()
    with pytest.raises(RuntimeError, match="PR flow cannot start"):
        await handler._pr_flow_request(
            record,
            method="POST",
            path="/api/github/pr_flow/start",
            payload={"mode": "issue", "issue": "123"},
        )
