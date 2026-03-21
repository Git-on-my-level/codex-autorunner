from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from tests.integrations.discord.test_service_routing import (
    _config,
    _FakeGateway,
    _FakeOutboxManager,
    _FakeRest,
)

from codex_autorunner.integrations.discord import service as discord_service_module
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


@pytest.mark.asyncio
async def test_discord_backend_approval_request_accepts_zero_request_id(
    tmp_path: Path,
) -> None:
    rest = _FakeRest()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    try:
        service._discord_turn_approval_contexts["turn-1"] = (
            discord_service_module._DiscordTurnApprovalContext(channel_id="channel-1")
        )
        request = {
            "id": 0,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "turnId": "turn-1",
                "reason": "Need permission",
                "command": ["/bin/zsh", "-c", "pwd"],
            },
        }

        decision_task = asyncio.create_task(
            service._handle_backend_approval_request(request)
        )
        await asyncio.sleep(0)

        assert len(rest.channel_messages) == 1
        payload = rest.channel_messages[0]["payload"]
        accept_custom_id = payload["components"][0]["components"][0]["custom_id"]

        await service._handle_component_interaction_normalized(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
            custom_id=accept_custom_id,
            values=None,
            guild_id="guild-1",
            user_id="user-1",
        )

        assert await decision_task == "accept"
    finally:
        await store.close()
