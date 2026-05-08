from __future__ import annotations

import logging
from typing import Optional

from ...adapters.chat.adapter import ChatAdapter
from ...adapters.telegram.constants import TELEGRAM_MAX_MESSAGE_LENGTH
from .._chat_surface_port import ChatSurfacePort, ChatSurfacePortConfig

_TELEGRAM_SURFACE_KIND = "telegram"


class TelegramSurfacePort(ChatSurfacePort):
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
                surface_kind=_TELEGRAM_SURFACE_KIND,
                supports_files=True,
                supports_reactions=False,
                supports_typing_indicator=True,
                max_message_length=TELEGRAM_MAX_MESSAGE_LENGTH,
                features=(
                    "bot_update_ingress",
                    "commands",
                    "callback_queries",
                    "topics",
                    "pma_threads",
                    "managed_thread_turns",
                ),
                poll_timeout_seconds=poll_timeout_seconds,
            ),
            logger=logger,
        )


def build_telegram_surface_port(
    *,
    adapter: Optional[ChatAdapter] = None,
    logger: Optional[logging.Logger] = None,
    poll_timeout_seconds: float = 30.0,
) -> TelegramSurfacePort:
    return TelegramSurfacePort(
        adapter=adapter,
        logger=logger,
        poll_timeout_seconds=poll_timeout_seconds,
    )


__all__ = ["TelegramSurfacePort", "build_telegram_surface_port"]
