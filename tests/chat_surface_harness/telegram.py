from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.integrations.telegram.adapter import (
    TelegramMessage,
    TelegramUpdatePoller,
)
from codex_autorunner.integrations.telegram.chat_adapter import TelegramChatAdapter
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService
from tests.chat_surface_lab.backend_runtime import app_server_fixture_command
from tests.chat_surface_lab.telegram_simulator import TelegramSurfaceSimulator

from .hermes import logger_for


class FakeTelegramBot(TelegramSurfaceSimulator):
    pass


def telegram_config(root: Path) -> TelegramBotConfig:
    return TelegramBotConfig.from_raw(
        {
            "enabled": True,
            "mode": "polling",
            "allowed_chat_ids": [123],
            "allowed_user_ids": [456],
            "require_topics": False,
            "app_server_command": app_server_fixture_command(),
        },
        root=root,
        env={
            "CAR_TELEGRAM_BOT_TOKEN": "test-token",
            "CAR_TELEGRAM_CHAT_ID": "123",
        },
    )


def telegram_message(
    text: str,
    *,
    chat_id: int = 123,
    thread_id: Optional[int] = None,
    user_id: int = 456,
    message_id: int = 1,
    update_id: int = 1,
) -> TelegramMessage:
    return TelegramMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        thread_id=thread_id,
        from_user_id=user_id,
        text=text,
        date=0,
        is_topic_message=thread_id is not None,
    )


async def drain_spawned_tasks(service: TelegramBotService) -> None:
    while True:
        while service._spawned_tasks:
            await asyncio.gather(*tuple(service._spawned_tasks))
        for runtime in service._router._topics.values():
            await runtime.queue.join_idle()
        if not service._spawned_tasks:
            return


@dataclass
class TelegramPmaEnvironment:
    service: TelegramBotService
    bot: FakeTelegramBot

    async def close(self) -> None:
        await self.service._app_server_supervisor.close_all()


async def build_telegram_pma_environment(
    root: Path,
    *,
    agent: str = "hermes",
) -> TelegramPmaEnvironment:
    seed_hub_files(root, force=True)
    service = TelegramBotService(telegram_config(root), hub_root=root)
    bot = FakeTelegramBot()
    service._bot = bot
    service._poller = TelegramUpdatePoller(bot)
    service._chat_adapter = TelegramChatAdapter(bot, poller=service._poller)
    service._chat_core._adapter = service._chat_adapter

    def apply(record) -> None:
        record.pma_enabled = True
        record.agent = agent

    await service._router.update_topic(123, None, apply)
    service._logger = logger_for("telegram")
    return TelegramPmaEnvironment(service=service, bot=bot)
