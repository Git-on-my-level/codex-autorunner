from pathlib import Path

import pytest

from codex_autorunner.core.orchestration import ChatOperationState
from codex_autorunner.integrations.telegram.adapter import (
    TelegramCallbackQuery,
    TelegramMessage,
    TelegramUpdate,
)
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.constants import (
    UPDATE_ID_PERSIST_INTERVAL_SECONDS,
)
from codex_autorunner.integrations.telegram.dispatch import dispatch_update
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

        async def fake_update_topic(_key, _apply):  # type: ignore[no-untyped-def]
            calls.append(1)

        monkeypatch.setattr(
            "codex_autorunner.integrations.telegram.service.time.monotonic",
            lambda: now,
        )
        service._store.update_topic = fake_update_topic  # type: ignore[assignment]
        await service._should_process_update(key, 11)
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

        async def fake_update_topic(_key, _apply):  # type: ignore[no-untyped-def]
            calls.append(1)

        monkeypatch.setattr(
            "codex_autorunner.integrations.telegram.service.time.monotonic",
            lambda: now,
        )
        service._store.update_topic = fake_update_topic  # type: ignore[assignment]
        await service._should_process_update(key, 11)
        assert len(calls) == 1
    finally:
        await service._bot.close()


@pytest.mark.anyio
async def test_dispatch_update_registers_shared_chat_operation(tmp_path: Path) -> None:
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
        message = TelegramMessage(
            update_id=21,
            message_id=7,
            chat_id=123,
            thread_id=None,
            from_user_id=456,
            text="hello from telegram",
            caption=None,
            date=1_700_000_000,
            is_topic_message=False,
            is_edited=False,
            reply_to_message_id=None,
        )

        async def _handle_message(_message):  # type: ignore[no-untyped-def]
            return None

        service._handle_message = _handle_message  # type: ignore[assignment]
        service._should_bypass_topic_queue = lambda _message: True  # type: ignore[assignment]

        await dispatch_update(
            service,
            TelegramUpdate(update_id=21, message=message, callback=None),
        )

        snapshot = service._chat_operation_store.get_operation("telegram:update:21")
        assert snapshot is not None
        assert snapshot.surface_kind == "telegram"
        assert snapshot.state is ChatOperationState.COMPLETED
        assert snapshot.metadata["kind"] == "message"
        assert snapshot.metadata["chat_id"] == 123
        assert snapshot.metadata["message_id"] == 7
    finally:
        await service._runtime_services.close()
        await service._store.close()
        await service._bot.close()


@pytest.mark.anyio
async def test_dispatch_update_uses_shared_ledger_to_reject_restart_duplicate_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.service.time.monotonic",
        lambda: 0.0,
    )
    message = TelegramMessage(
        update_id=33,
        message_id=9,
        chat_id=123,
        thread_id=None,
        from_user_id=456,
        text="restart duplicate",
        caption=None,
        date=1_700_000_000,
        is_topic_message=False,
        is_edited=False,
        reply_to_message_id=None,
    )
    executions: list[str] = []

    service = TelegramBotService(config)
    restarted = TelegramBotService(config)
    try:

        async def _handle_message(_message):  # type: ignore[no-untyped-def]
            executions.append("handled")
            return None

        service._handle_message = _handle_message  # type: ignore[assignment]
        restarted._handle_message = _handle_message  # type: ignore[assignment]
        service._should_bypass_topic_queue = lambda _message: True  # type: ignore[assignment]
        restarted._should_bypass_topic_queue = lambda _message: True  # type: ignore[assignment]

        update = TelegramUpdate(update_id=33, message=message, callback=None)
        await dispatch_update(service, update)
        await dispatch_update(restarted, update)

        assert executions == ["handled"]
        snapshot = restarted._chat_operation_store.get_operation("telegram:update:33")
        assert snapshot is not None
        assert snapshot.state is ChatOperationState.COMPLETED
    finally:
        await service._runtime_services.close()
        await service._store.close()
        await service._bot.close()
        await restarted._runtime_services.close()
        await restarted._store.close()
        await restarted._bot.close()


@pytest.mark.anyio
async def test_dispatch_update_uses_shared_ledger_to_reject_restart_duplicate_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.service.time.monotonic",
        lambda: 0.0,
    )
    callback = TelegramCallbackQuery(
        update_id=41,
        callback_id="cb-41",
        from_user_id=456,
        data="resume:thread-1",
        message_id=17,
        chat_id=123,
        thread_id=None,
    )
    executions: list[str] = []

    service = TelegramBotService(config)
    restarted = TelegramBotService(config)
    try:

        async def _handle_callback(_callback):  # type: ignore[no-untyped-def]
            executions.append("handled")
            return None

        async def _answer_callback(_callback, _text=""):  # type: ignore[no-untyped-def]
            return None

        service._handle_callback = _handle_callback  # type: ignore[assignment]
        restarted._handle_callback = _handle_callback  # type: ignore[assignment]
        service._answer_callback = _answer_callback  # type: ignore[assignment]
        restarted._answer_callback = _answer_callback  # type: ignore[assignment]

        update = TelegramUpdate(update_id=41, message=None, callback=callback)
        await dispatch_update(service, update)
        await dispatch_update(restarted, update)

        assert executions == ["handled"]
        snapshot = restarted._chat_operation_store.get_operation("telegram:update:41")
        assert snapshot is not None
        assert snapshot.state is ChatOperationState.COMPLETED
        assert snapshot.metadata["kind"] == "callback"
    finally:
        await service._runtime_services.close()
        await service._store.close()
        await service._bot.close()
        await restarted._runtime_services.close()
        await restarted._store.close()
        await restarted._bot.close()
