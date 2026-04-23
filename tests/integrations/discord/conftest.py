from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotDispatchConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


class FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.edited_original_interaction_responses: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []
        self.edited_channel_messages: list[dict[str, Any]] = []
        self.deleted_channel_messages: list[dict[str, Any]] = []
        self.typing_calls: list[str] = []
        self.command_sync_calls: list[dict[str, Any]] = []
        self.fetched_channel_messages: dict[tuple[str, str], dict[str, Any]] = {}

    async def create_interaction_response(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> None:
        self.interaction_responses.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )

    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        message_id = f"msg-{len(self.channel_messages) + 1}"
        self.channel_messages.append(
            {
                "channel_id": channel_id,
                "payload": payload,
                "message_id": message_id,
            }
        )
        return {"id": message_id, "channel_id": channel_id, "payload": payload}

    async def get_channel_message(
        self, *, channel_id: str, message_id: str
    ) -> dict[str, Any]:
        return dict(self.fetched_channel_messages.get((channel_id, message_id), {}))

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.edited_channel_messages.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "payload": payload,
            }
        )
        return {"id": message_id}

    async def delete_channel_message(self, *, channel_id: str, message_id: str) -> None:
        self.deleted_channel_messages.append(
            {"channel_id": channel_id, "message_id": message_id}
        )

    async def trigger_typing(self, *, channel_id: str) -> None:
        self.typing_calls.append(channel_id)

    async def create_followup_message(
        self,
        *,
        application_id: str | None = None,
        interaction_token: str,
        payload: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.followup_messages.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )
        return {"id": "followup-1", "payload": payload}

    async def edit_original_interaction_response(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.edited_original_interaction_responses.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )
        return {"id": "@original"}

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.command_sync_calls.append(
            {
                "application_id": application_id,
                "guild_id": guild_id,
                "commands": commands,
            }
        )
        return commands


class FakeGateway:
    def __init__(
        self, events: list[dict[str, Any] | tuple[str, dict[str, Any]]]
    ) -> None:
        self._events = events
        self.stopped = False

    async def run(self, on_dispatch) -> None:
        for item in self._events:
            if isinstance(item, tuple):
                event_type, payload = item
            else:
                event_type, payload = "INTERACTION_CREATE", item
            await on_dispatch(event_type, payload)

    async def stop(self) -> None:
        self.stopped = True


class FakeOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        await asyncio.Event().wait()


def build_discord_config(
    root: Path,
    *,
    allow_user_ids: frozenset[str] = frozenset({"user-1"}),
    command_registration_enabled: bool = True,
    command_scope: str = "guild",
    command_guild_ids: tuple[str, ...] = ("guild-1",),
    pma_enabled: bool = True,
    ack_budget_ms: int = 10_000,
) -> DiscordBotConfig:
    return DiscordBotConfig(
        root=root,
        enabled=True,
        bot_token_env="TOKEN_ENV",
        app_id_env="APP_ENV",
        bot_token="token",
        application_id="app-1",
        allowed_guild_ids=frozenset({"guild-1"}),
        allowed_channel_ids=frozenset({"channel-1"}),
        allowed_user_ids=allow_user_ids,
        command_registration=DiscordCommandRegistration(
            enabled=command_registration_enabled,
            scope=command_scope,
            guild_ids=command_guild_ids,
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=2000,
        message_overflow="split",
        pma_enabled=pma_enabled,
        dispatch=DiscordBotDispatchConfig(ack_budget_ms=ack_budget_ms),
    )


def build_discord_service(
    root: Path,
    *,
    rest: FakeRest | None = None,
    gateway: FakeGateway | None = None,
    store: DiscordStateStore | None = None,
    allow_user_ids: frozenset[str] = frozenset({"user-1"}),
    command_registration_enabled: bool = True,
    pma_enabled: bool = True,
    ack_budget_ms: int = 10_000,
) -> tuple[DiscordBotService, FakeRest, FakeGateway, DiscordStateStore]:
    rest = rest or FakeRest()
    gateway = gateway or FakeGateway([])
    if store is None:
        store = DiscordStateStore(root / ".codex-autorunner" / "discord_state.sqlite3")
    config = build_discord_config(
        root,
        allow_user_ids=allow_user_ids,
        command_registration_enabled=command_registration_enabled,
        pma_enabled=pma_enabled,
        ack_budget_ms=ack_budget_ms,
    )
    service = DiscordBotService(
        config,
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=FakeOutboxManager(),
    )
    return service, rest, gateway, store


@pytest.fixture
async def discord_store(tmp_path: Path) -> DiscordStateStore:
    store = DiscordStateStore(tmp_path / ".codex-autorunner" / "discord_state.sqlite3")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
async def discord_service_and_rest(
    tmp_path: Path, discord_store: DiscordStateStore
) -> tuple[DiscordBotService, FakeRest, DiscordStateStore]:
    service, rest, _, store = build_discord_service(tmp_path, store=discord_store)
    return service, rest, store
