from __future__ import annotations

import logging
from typing import Optional

from ...integrations.chat.adapter import ChatAdapter
from ...integrations.discord.constants import DISCORD_MAX_MESSAGE_LENGTH
from .._chat_surface_port import ChatSurfacePort, ChatSurfacePortConfig

_DISCORD_SURFACE_KIND = "discord"


class DiscordSurfacePort(ChatSurfacePort):
    def __init__(
        self,
        *,
        adapter: Optional[ChatAdapter] = None,
        logger: Optional[logging.Logger] = None,
        poll_timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(
            adapter=adapter,
            config=ChatSurfacePortConfig(
                surface_kind=_DISCORD_SURFACE_KIND,
                supports_files=True,
                supports_reactions=False,
                supports_typing_indicator=True,
                max_message_length=DISCORD_MAX_MESSAGE_LENGTH,
                features=(
                    "gateway_ingress",
                    "slash_commands",
                    "component_interactions",
                    "pma_threads",
                    "managed_thread_turns",
                ),
                poll_timeout_seconds=poll_timeout_seconds,
            ),
            logger=logger,
        )


def build_discord_surface_port(
    *,
    adapter: Optional[ChatAdapter] = None,
    logger: Optional[logging.Logger] = None,
    poll_timeout_seconds: float = 30.0,
) -> DiscordSurfacePort:
    return DiscordSurfacePort(
        adapter=adapter,
        logger=logger,
        poll_timeout_seconds=poll_timeout_seconds,
    )


__all__ = ["DiscordSurfacePort", "build_discord_surface_port"]
