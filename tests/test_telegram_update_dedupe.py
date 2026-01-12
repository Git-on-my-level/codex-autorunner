from pathlib import Path

import pytest

from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.constants import (
    UPDATE_ID_PERSIST_INTERVAL_SECONDS,
)
from codex_autorunner.integrations.telegram.service import TelegramBotService


@pytest.mark.anyio
async def test_update_dedupe_skips_frequent_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        key = "chat:thread"
        now = 100.0
        service._last_update_ids[key] = 10
        service._last_update_persisted_at[key] = now - (
            UPDATE_ID_PERSIST_INTERVAL_SECONDS / 2
        )
        calls: list[int] = []

        def fake_update_topic(_key, _apply):  # type: ignore[no-untyped-def]
            calls.append(1)

        monkeypatch.setattr(
            "codex_autorunner.integrations.telegram.service.time.monotonic",
            lambda: now,
        )
        service._store.update_topic = fake_update_topic  # type: ignore[assignment]
        service._should_process_update(key, 11)
        assert not calls
    finally:
        await service._bot.close()


@pytest.mark.anyio
async def test_update_dedupe_persists_after_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        key = "chat:thread"
        now = 200.0
        service._last_update_ids[key] = 10
        service._last_update_persisted_at[key] = (
            now - UPDATE_ID_PERSIST_INTERVAL_SECONDS - 1.0
        )
        calls: list[int] = []

        def fake_update_topic(_key, _apply):  # type: ignore[no-untyped-def]
            calls.append(1)

        monkeypatch.setattr(
            "codex_autorunner.integrations.telegram.service.time.monotonic",
            lambda: now,
        )
        service._store.update_topic = fake_update_topic  # type: ignore[assignment]
        service._should_process_update(key, 11)
        assert len(calls) == 1
    finally:
        await service._bot.close()
