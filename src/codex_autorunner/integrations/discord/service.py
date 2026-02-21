from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...core.utils import canonicalize_path
from .allowlist import DiscordAllowlist, allowlist_allows
from .config import DiscordBotConfig
from .gateway import DiscordGatewayClient
from .interactions import (
    extract_channel_id,
    extract_command_path_and_options,
    extract_guild_id,
    extract_interaction_id,
    extract_interaction_token,
)
from .outbox import DiscordOutboxManager
from .rest import DiscordRestClient
from .state import DiscordStateStore

DISCORD_EPHEMERAL_FLAG = 64


class DiscordBotService:
    def __init__(
        self,
        config: DiscordBotConfig,
        *,
        logger: logging.Logger,
        rest_client: Optional[DiscordRestClient] = None,
        gateway_client: Optional[DiscordGatewayClient] = None,
        state_store: Optional[DiscordStateStore] = None,
        outbox_manager: Optional[DiscordOutboxManager] = None,
    ) -> None:
        self._config = config
        self._logger = logger

        self._rest = (
            rest_client
            if rest_client is not None
            else DiscordRestClient(bot_token=config.bot_token or "")
        )
        self._owns_rest = rest_client is None

        self._gateway = (
            gateway_client
            if gateway_client is not None
            else DiscordGatewayClient(
                bot_token=config.bot_token or "",
                intents=config.intents,
                logger=logger,
            )
        )
        self._owns_gateway = gateway_client is None

        self._store = (
            state_store
            if state_store is not None
            else DiscordStateStore(config.state_file)
        )
        self._owns_store = state_store is None

        self._outbox = (
            outbox_manager
            if outbox_manager is not None
            else DiscordOutboxManager(
                self._store,
                send_message=self._send_channel_message,
                logger=logger,
            )
        )
        self._allowlist = DiscordAllowlist(
            allowed_guild_ids=config.allowed_guild_ids,
            allowed_channel_ids=config.allowed_channel_ids,
            allowed_user_ids=config.allowed_user_ids,
        )

    async def run_forever(self) -> None:
        await self._store.initialize()
        self._outbox.start()
        outbox_task = asyncio.create_task(self._outbox.run_loop())
        try:
            log_event(
                self._logger,
                logging.INFO,
                "discord.bot.starting",
                state_file=str(self._config.state_file),
            )
            await self._gateway.run(self._on_dispatch)
        finally:
            outbox_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outbox_task
            await self._shutdown()

    async def _shutdown(self) -> None:
        if self._owns_gateway:
            with contextlib.suppress(Exception):
                await self._gateway.stop()
        if self._owns_rest and hasattr(self._rest, "close"):
            with contextlib.suppress(Exception):
                await self._rest.close()  # type: ignore[func-returns-value]
        if self._owns_store:
            with contextlib.suppress(Exception):
                await self._store.close()

    async def _send_channel_message(
        self, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._rest.create_channel_message(
            channel_id=channel_id, payload=payload
        )

    async def _on_dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type != "INTERACTION_CREATE":
            return
        await self._handle_interaction(payload)

    async def _handle_interaction(self, interaction_payload: dict[str, Any]) -> None:
        interaction_id = extract_interaction_id(interaction_payload)
        interaction_token = extract_interaction_token(interaction_payload)
        channel_id = extract_channel_id(interaction_payload)
        guild_id = extract_guild_id(interaction_payload)

        if not interaction_id or not interaction_token or not channel_id:
            return

        if not allowlist_allows(interaction_payload, self._allowlist):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This Discord command is not authorized for this channel/user/guild.",
            )
            return

        command_path, options = extract_command_path_and_options(interaction_payload)

        if command_path == ("car", "bind"):
            await self._handle_bind(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                options=options,
            )
            return
        if command_path == ("car", "status"):
            await self._handle_status(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
            )
            return

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Command not implemented yet for Discord.",
        )

    async def _handle_bind(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        options: dict[str, Any],
    ) -> None:
        raw_path = options.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Missing required option: path",
            )
            return

        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self._config.root / candidate
        workspace = canonicalize_path(candidate)
        if not workspace.exists() or not workspace.is_dir():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Workspace path does not exist: {workspace}",
            )
            return

        await self._store.upsert_binding(
            channel_id=channel_id,
            guild_id=guild_id,
            workspace_path=str(workspace),
            repo_id=None,
        )
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Bound this channel to workspace: {workspace}",
        )

    async def _handle_status(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = (
                "This channel is not bound. Use /car bind path:<workspace>. "
                "Then use /car flow status once flow commands are enabled."
            )
        else:
            text = (
                "Channel is bound to workspace: "
                f"{binding['workspace_path']}. "
                "Use /car flow status once flow commands are enabled."
            )
        await self._respond_ephemeral(interaction_id, interaction_token, text)

    async def _respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        await self._rest.create_interaction_response(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            payload={
                "type": 4,
                "data": {
                    "content": text,
                    "flags": DISCORD_EPHEMERAL_FLAG,
                },
            },
        )


def create_discord_bot_service(
    config: DiscordBotConfig,
    *,
    logger: logging.Logger,
) -> DiscordBotService:
    return DiscordBotService(config, logger=logger)
