"""Regression tests for Telegram topic queue status message refresh."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codex_autorunner.integrations.chat.queue_status import QueueStatusItem
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
async def test_refresh_topic_queue_status_resends_when_edit_fails(
    tmp_path: Path,
) -> None:
    """If the stored status message was deleted, recreate it instead of looping on edit."""
    service = TelegramBotService(_config(tmp_path), hub_root=tmp_path)
    try:
        edit = AsyncMock(return_value=False)
        placeholder = AsyncMock(return_value=200)
        clear_mock = MagicMock()
        set_mock = MagicMock()

        items = [QueueStatusItem(item_id="m1", preview="hello")]

        with (
            patch.object(
                service,
                "_queue_status_items_for_topic",
                new=AsyncMock(return_value=items),
            ),
            patch.object(service, "_get_queue_status_message_id", return_value=100),
            patch.object(service, "_edit_message_text", new=edit),
            patch.object(service, "_send_placeholder", new=placeholder),
            patch.object(service, "_delete_message", new=AsyncMock(return_value=True)),
            patch.object(service, "_clear_queue_status_message_id", new=clear_mock),
            patch.object(service, "_set_queue_status_message_id", new=set_mock),
        ):
            mid = await service._refresh_topic_queue_status_message(
                topic_key="10:11",
                chat_id=10,
                thread_id=11,
                repost=False,
            )

        assert mid == 200
        clear_mock.assert_called_once_with(10, 11)
        edit.assert_awaited_once()
        call = edit.await_args
        assert call.args[0] == 10 and call.args[1] == 100
        assert call.kwargs.get("message_thread_id") == 11
        placeholder.assert_awaited_once()
        set_mock.assert_called_once_with(10, 11, 200)
    finally:
        await service._bot.close()
