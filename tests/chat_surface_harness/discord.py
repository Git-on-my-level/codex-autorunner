from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.integrations.chat.collaboration_policy import CollaborationPolicy
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotMediaConfig,
    DiscordBotShellConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore
from tests.chat_surface_lab.discord_simulator import DiscordSurfaceSimulator

from .hermes import logger_for


class FakeDiscordRest(DiscordSurfaceSimulator):
    def __init__(self) -> None:
        super().__init__()


class FakeDiscordGateway:
    def __init__(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self._events = list(events)

    async def run(self, on_dispatch: Any) -> None:
        for event_type, payload in self._events:
            await on_dispatch(event_type, payload)
        await asyncio.sleep(0.05)

    async def stop(self) -> None:
        return None


class FakeDiscordOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        await asyncio.Event().wait()


def discord_config(
    root: Path,
    *,
    pma_enabled: bool = True,
    collaboration_policy: CollaborationPolicy | None = None,
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
        allowed_user_ids=frozenset(),
        command_registration=DiscordCommandRegistration(
            enabled=False,
            scope="guild",
            guild_ids=("guild-1",),
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=2000,
        message_overflow="split",
        pma_enabled=pma_enabled,
        shell=DiscordBotShellConfig(
            enabled=True,
            timeout_ms=120000,
            max_output_chars=3800,
        ),
        media=DiscordBotMediaConfig(
            enabled=True,
            voice=True,
            max_voice_bytes=10 * 1024 * 1024,
        ),
        collaboration_policy=collaboration_policy,
    )


def discord_message_create(
    content: str,
    *,
    message_id: str = "m-1",
    guild_id: str = "guild-1",
    channel_id: str = "channel-1",
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "guild_id": guild_id,
        "content": content,
        "author": {"id": "user-1", "bot": False},
        "attachments": [],
    }


@dataclass
class DiscordPmaEnvironment:
    service: DiscordBotService
    rest: FakeDiscordRest
    store: DiscordStateStore

    async def close(self) -> None:
        await self.store.close()


async def drain_spawned_tasks(service: DiscordBotService) -> None:
    while service._background_tasks:
        await asyncio.gather(*list(service._background_tasks), return_exceptions=True)


async def build_discord_pma_environment(
    root: Path,
    *,
    message_content: str,
    agent: str = "hermes",
) -> DiscordPmaEnvironment:
    seed_hub_files(root, force=True)
    workspace = root / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(root / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    await store.update_pma_state(channel_id="channel-1", pma_enabled=True)
    await store.update_agent_state(channel_id="channel-1", agent=agent)

    rest = FakeDiscordRest()
    gateway = FakeDiscordGateway(
        [("MESSAGE_CREATE", discord_message_create(message_content))]
    )
    service = DiscordBotService(
        discord_config(root),
        logger=logger_for("discord"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=FakeDiscordOutboxManager(),
    )
    return DiscordPmaEnvironment(service=service, rest=rest, store=store)
