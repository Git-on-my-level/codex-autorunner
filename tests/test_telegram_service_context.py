from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.integrations.app_server.event_buffer import AppServerEventBuffer
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService


def _config(root: Path) -> TelegramBotConfig:
    return TelegramBotConfig.from_raw(
        {
            "enabled": True,
            "allowed_chat_ids": [123],
            "allowed_user_ids": [456],
        },
        root=root,
        env={"CAR_TELEGRAM_BOT_TOKEN": "test-token"},
    )


@pytest.mark.anyio
async def test_service_exposes_app_server_event_buffer_context(tmp_path: Path) -> None:
    service = TelegramBotService(_config(tmp_path), hub_root=tmp_path)
    try:
        assert isinstance(service.app_server_events, AppServerEventBuffer)
        assert service.app_server_supervisor is service._app_server_supervisor
        assert service.opencode_supervisor is service._opencode_supervisor

        handler = getattr(service._app_server_supervisor, "_notification_handler", None)
        assert callable(handler)

        await handler(
            {
                "method": "turn/error",
                "params": {
                    "threadId": "telegram-thread-1",
                    "turnId": "telegram-turn-1",
                    "message": "failed",
                },
            }
        )

        events = await service.app_server_events.list_events(
            "telegram-thread-1",
            "telegram-turn-1",
        )
        assert len(events) == 1
    finally:
        await service._app_server_supervisor.close_all()
        await service._bot.close()
