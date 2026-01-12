from pathlib import Path

import pytest

from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService


@pytest.mark.anyio
async def test_spawned_task_tracking(tmp_path: Path) -> None:
    config = TelegramBotConfig.from_raw(
        {
            "enabled": True,
            "allowed_chat_ids": [123],
            "allowed_user_ids": [456],
        },
        root=tmp_path,
        env={"CAR_TELEGRAM_BOT_TOKEN": "test-token"},
    )
    service = TelegramBotService(config)
    try:

        async def _noop() -> None:
            return None

        task = service._spawn_task(_noop())
        await task
        assert task not in service._spawned_tasks
    finally:
        await service._bot.close()
