from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.pma_notification_store import PmaNotificationStore
from codex_autorunner.core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    OutputDelta,
)
from codex_autorunner.integrations.chat import bound_live_progress as progress_module
from codex_autorunner.integrations.chat.bound_live_progress import (
    build_bound_chat_live_progress_session,
)
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.telegram.state import TelegramStateStore


@pytest.mark.anyio
async def test_discord_bound_live_progress_enqueues_send_edit_and_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path
    monkeypatch.setattr(
        progress_module,
        "active_chat_binding_metadata_by_thread",
        lambda *, hub_root: {
            "thread-1": {"binding_kind": "discord", "binding_id": "channel-1"}
        },
    )
    discord_state_path = tmp_path / ".codex-autorunner" / "discord_state.sqlite3"
    discord_state_path.parent.mkdir(parents=True, exist_ok=True)
    store = DiscordStateStore(discord_state_path)
    try:
        await store.initialize()
        session = build_bound_chat_live_progress_session(
            hub_root=hub_root,
            raw_config={"discord_bot": {"state_file": str(discord_state_path)}},
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
            agent="codex",
            model="gpt-5",
        )

        await session.start()

        records = await store.list_outbox()
        assert any(
            record.record_id.endswith(":send") and record.operation == "send"
            for record in records
        )

        notification_store = PmaNotificationStore(hub_root)
        notification_store.mark_delivered(
            delivery_record_id=("managed-thread-progress:discord:thread-1:turn-1:send"),
            delivered_message_id="msg-1",
        )
        await store.mark_outbox_delivered(
            "managed-thread-progress:discord:thread-1:turn-1:send"
        )

        await session.apply_run_events(
            [
                OutputDelta(
                    timestamp="2026-01-01T00:00:01Z",
                    content="checking review thread",
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                )
            ]
        )

        records = await store.list_outbox()
        assert any(
            record.operation == "edit" and record.message_id == "msg-1"
            for record in records
        )

        await session.finalize(status="ok")
        records = await store.list_outbox()
        assert any(
            record.operation == "delete" and record.message_id == "msg-1"
            for record in records
        )
        await session.close()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_discord_bound_live_progress_attempts_immediate_send_when_bot_token_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path
    monkeypatch.setattr(
        progress_module,
        "active_chat_binding_metadata_by_thread",
        lambda *, hub_root: {
            "thread-1": {"binding_kind": "discord", "binding_id": "channel-1"}
        },
    )
    discord_state_path = tmp_path / ".codex-autorunner" / "discord_state.sqlite3"
    discord_state_path.parent.mkdir(parents=True, exist_ok=True)

    class FakeDiscordRestClient:
        def __init__(self, *, bot_token: str) -> None:
            assert bot_token == "discord-token"

        async def create_channel_message(
            self,
            *,
            channel_id: str,
            payload: dict[str, object],
        ) -> dict[str, str]:
            assert channel_id == "channel-1"
            assert "content" in payload
            return {"id": "discord-msg-1"}

        async def edit_channel_message(self, **_: object) -> dict[str, object]:
            return {}

        async def delete_channel_message(self, **_: object) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(progress_module, "DiscordRestClient", FakeDiscordRestClient)

    store = DiscordStateStore(discord_state_path)
    try:
        await store.initialize()
        session = build_bound_chat_live_progress_session(
            hub_root=hub_root,
            raw_config={
                "discord_bot": {
                    "state_file": str(discord_state_path),
                    "bot_token": "discord-token",
                }
            },
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
            agent="codex",
            model="gpt-5",
        )

        await session.start()

        records = await store.list_outbox()
        assert not records
        notification_store = PmaNotificationStore(hub_root)
        conversation = notification_store.get_by_delivery_record_id(
            "managed-thread-progress:discord:thread-1:turn-1:send"
        )
        assert conversation is not None
        assert conversation.delivered_message_id == "discord-msg-1"
        await session.close()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_telegram_bound_live_progress_enqueues_send_edit_and_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path
    monkeypatch.setattr(
        progress_module,
        "active_chat_binding_metadata_by_thread",
        lambda *, hub_root: {
            "thread-1": {"binding_kind": "telegram", "binding_id": "123:456"}
        },
    )
    telegram_state_path = tmp_path / ".codex-autorunner" / "telegram_state.sqlite3"
    telegram_state_path.parent.mkdir(parents=True, exist_ok=True)
    store = TelegramStateStore(telegram_state_path)
    try:
        session = build_bound_chat_live_progress_session(
            hub_root=hub_root,
            raw_config={"telegram_bot": {"state_file": str(telegram_state_path)}},
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
            agent="codex",
            model="gpt-5",
        )

        await session.start()

        records = await store.list_outbox()
        assert any(
            record.record_id.endswith(":send") and record.operation == "send"
            for record in records
        )

        notification_store = PmaNotificationStore(hub_root)
        notification_store.mark_delivered(
            delivery_record_id=(
                "managed-thread-progress:telegram:thread-1:turn-1:send"
            ),
            delivered_message_id="77",
        )
        await store.delete_outbox(
            "managed-thread-progress:telegram:thread-1:turn-1:send"
        )

        await session.apply_run_events(
            [
                OutputDelta(
                    timestamp="2026-01-01T00:00:01Z",
                    content="checking review thread",
                    delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                )
            ]
        )

        records = await store.list_outbox()
        assert any(
            record.operation == "edit" and record.message_id == 77 for record in records
        )

        await session.finalize(status="ok")
        records = await store.list_outbox()
        assert any(
            record.operation == "delete" and record.message_id == 77
            for record in records
        )
        await session.close()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_telegram_bound_live_progress_attempts_immediate_send_when_bot_token_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path
    monkeypatch.setattr(
        progress_module,
        "active_chat_binding_metadata_by_thread",
        lambda *, hub_root: {
            "thread-1": {"binding_kind": "telegram", "binding_id": "123:456"}
        },
    )
    telegram_state_path = tmp_path / ".codex-autorunner" / "telegram_state.sqlite3"
    telegram_state_path.parent.mkdir(parents=True, exist_ok=True)

    class FakeTelegramBotClient:
        def __init__(self, bot_token: str, *, logger: object) -> None:
            assert bot_token == "telegram-token"
            assert logger is not None

        async def send_message(
            self,
            chat_id: int,
            text: str,
            *,
            message_thread_id: int | None = None,
            reply_to_message_id: int | None = None,
        ) -> dict[str, int]:
            assert chat_id == 123
            assert text
            assert message_thread_id == 456
            assert reply_to_message_id is None
            return {"message_id": 77}

        async def edit_message_text(
            self, *args: object, **kwargs: object
        ) -> dict[str, object]:
            _ = args, kwargs
            return {"ok": True}

        async def delete_message(self, *args: object, **kwargs: object) -> bool:
            _ = args, kwargs
            return True

        async def close(self) -> None:
            return None

    monkeypatch.setattr(progress_module, "TelegramBotClient", FakeTelegramBotClient)

    store = TelegramStateStore(telegram_state_path)
    try:
        session = build_bound_chat_live_progress_session(
            hub_root=hub_root,
            raw_config={
                "telegram_bot": {
                    "state_file": str(telegram_state_path),
                    "bot_token": "telegram-token",
                }
            },
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
            agent="codex",
            model="gpt-5",
        )

        await session.start()

        records = await store.list_outbox()
        assert not records
        notification_store = PmaNotificationStore(hub_root)
        conversation = notification_store.get_by_delivery_record_id(
            "managed-thread-progress:telegram:thread-1:turn-1:send"
        )
        assert conversation is not None
        assert conversation.delivered_message_id == "77"
        await session.close()
    finally:
        await store.close()
