from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from codex_autorunner.integrations.chat.command_diagnostics import ActiveFlowInfo
from codex_autorunner.integrations.discord.service import DiscordBotService


class _StoreStub:
    def __init__(self, binding: Optional[dict[str, Any]]) -> None:
        self._binding = binding

    async def get_binding(self, *, channel_id: str) -> Optional[dict[str, Any]]:
        _ = channel_id
        return self._binding


class _ClientStub:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.requests: list[tuple[str, Optional[dict[str, Any]], float]] = []

    async def request(
        self,
        method: str,
        *,
        params: Optional[dict[str, Any]] = None,
        timeout: float,
    ) -> dict[str, Any]:
        self.requests.append((method, params, timeout))
        return self.payload


def _collaboration_result() -> SimpleNamespace:
    return SimpleNamespace(
        command_allowed=True,
        destination_mode="active",
        plain_text_trigger="always",
        reason="allowed",
        matched_destination=None,
    )


def _capture_message(
    sent_messages: list[str],
):
    async def _capture(
        _interaction_id: str,
        _interaction_token: str,
        content: str,
    ) -> None:
        sent_messages.append(content)

    return _capture


@pytest.mark.anyio
async def test_status_bound_channel_includes_shared_and_discord_specific_details(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = {
        "workspace_path": str(workspace),
        "repo_id": "repo-123",
        "guild_id": "guild-456",
        "pma_enabled": False,
        "updated_at": "2026-03-24T08:00:00Z",
        "agent": "codex",
        "model_override": "gpt-5.4",
        "reasoning_effort": "high",
        "approval_mode": "yolo",
        "approval_policy": "never",
        "sandbox_policy": {"type": "dangerFullAccess", "networkAccess": True},
    }
    service = object.__new__(DiscordBotService)
    sent_messages: list[str] = []
    service._store = _StoreStub(binding)
    service._respond_ephemeral = AsyncMock(side_effect=_capture_message(sent_messages))
    service._evaluate_channel_collaboration_summary = lambda **_kwargs: (
        _collaboration_result(),
        _collaboration_result(),
    )
    service._get_active_flow_info = AsyncMock(
        return_value=ActiveFlowInfo(flow_id="flow-123", status="running")
    )
    service._read_status_rate_limits = AsyncMock(
        return_value={"primary": {"used_percent": 5, "window_minutes": 300}}
    )

    await DiscordBotService._handle_status(
        service,
        "interaction-1",
        "token-1",
        channel_id="channel-789",
        guild_id="guild-456",
        user_id="user-999",
    )

    assert len(sent_messages) == 1
    content = sent_messages[0]
    assert "Mode: workspace" in content
    assert "Channel is bound." in content
    assert f"Workspace: {workspace}" in content
    assert "Repo ID: repo-123" in content
    assert "Guild ID: guild-456" in content
    assert "Channel ID: channel-789" in content
    assert "Active flow: flow-123 (running)" in content
    assert "Agent: codex" in content
    assert "Resume: supported" in content
    assert "Model: gpt-5.4" in content
    assert "Effort: high" in content
    assert "Approval mode: yolo" in content
    assert "Approval policy: never" in content
    assert "Sandbox policy: dangerFullAccess, network=True" in content
    assert "Limits: [5h: 5%]" in content
    assert "Use /car flow status for ticket flow details." in content
    service._get_active_flow_info.assert_awaited_once_with(str(workspace))
    service._read_status_rate_limits.assert_awaited_once_with(
        str(workspace), agent="codex"
    )


@pytest.mark.anyio
async def test_status_unbound_channel_keeps_flow_hint() -> None:
    service = object.__new__(DiscordBotService)
    sent_messages: list[str] = []
    service._store = _StoreStub(None)
    service._respond_ephemeral = AsyncMock(side_effect=_capture_message(sent_messages))
    service._evaluate_channel_collaboration_summary = lambda **_kwargs: (
        _collaboration_result(),
        _collaboration_result(),
    )
    service._get_active_flow_info = AsyncMock()
    service._read_status_rate_limits = AsyncMock()

    await DiscordBotService._handle_status(
        service,
        "interaction-1",
        "token-1",
        channel_id="channel-789",
        guild_id="guild-456",
        user_id="user-999",
    )

    assert len(sent_messages) == 1
    content = sent_messages[0]
    assert "This channel is not bound." in content
    assert "Agent:" not in content
    assert "Then use /car flow status once flow commands are enabled." in content
    service._get_active_flow_info.assert_not_awaited()
    service._read_status_rate_limits.assert_not_awaited()


@pytest.mark.anyio
async def test_read_status_rate_limits_uses_app_server_client() -> None:
    service = object.__new__(DiscordBotService)
    client = _ClientStub(
        {"rateLimits": {"primary": {"used_percent": 4, "window_minutes": 300}}}
    )
    service._client_for_workspace = AsyncMock(return_value=client)

    rate_limits = await DiscordBotService._read_status_rate_limits(
        service,
        "/workspace",
        agent="codex",
    )

    assert rate_limits == {"primary": {"used_percent": 4, "window_minutes": 300}}
    assert client.requests == [("account/rateLimits/read", None, 5.0)]
