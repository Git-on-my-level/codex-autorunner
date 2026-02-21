from __future__ import annotations

import logging
from typing import Any

from ...core.logging_utils import log_event
from .rest import DiscordRestClient


async def sync_commands(
    rest: DiscordRestClient,
    *,
    application_id: str,
    commands: list[dict[str, Any]],
    scope: str,
    guild_ids: tuple[str, ...],
    logger: logging.Logger,
) -> None:
    normalized_scope = scope.strip().lower()
    if normalized_scope == "global":
        updated = await rest.bulk_overwrite_application_commands(
            application_id=application_id,
            commands=commands,
        )
        log_event(
            logger,
            logging.INFO,
            "discord.commands.sync.overwrite",
            scope="global",
            application_id=application_id,
            command_count=len(commands),
            updated_count=len(updated),
        )
        return

    if normalized_scope != "guild":
        raise ValueError("scope must be 'global' or 'guild'")

    normalized_guild_ids = tuple(
        sorted({guild_id.strip() for guild_id in guild_ids if guild_id.strip()})
    )
    if not normalized_guild_ids:
        raise ValueError("guild scope requires at least one guild_id")

    for guild_id in normalized_guild_ids:
        updated = await rest.bulk_overwrite_application_commands(
            application_id=application_id,
            guild_id=guild_id,
            commands=commands,
        )
        log_event(
            logger,
            logging.INFO,
            "discord.commands.sync.overwrite",
            scope="guild",
            guild_id=guild_id,
            application_id=application_id,
            command_count=len(commands),
            updated_count=len(updated),
        )
