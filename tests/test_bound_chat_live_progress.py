from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.pma_notification_store import PmaNotificationStore
from codex_autorunner.core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    OutputDelta,
)
from codex_autorunner.integrations.chat import bound_live_progress as progress_module
from codex_autorunner.integrations.chat.bound_live_progress import (
    bound_chat_progress_send_record_id,
    build_bound_chat_live_progress_session,
    build_bound_chat_progress_cleanup_metadata,
)
from codex_autorunner.integrations.discord.config import DiscordBotConfig
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.discord.state import (
    OutboxRecord as DiscordOutboxRecord,
)
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService
from codex_autorunner.integrations.telegram.state import (
    OutboxRecord as TelegramOutboxRecord,
)
from codex_autorunner.integrations.telegram.state import TelegramStateStore
from tests.discord_message_turns_support import _config as discord_config


@pytest.mark.anyio
async def test_discord_bound_live_progress_enqueues_send_edit_and_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path

    class FakeBindingStore:
        def __init__(self, _hub_root: Path) -> None:
            _ = _hub_root

        def list_bindings(self, **_: object) -> list[object]:
            return [
                type(
                    "Binding",
                    (),
                    {"surface_kind": "discord", "surface_key": "channel-1"},
                )()
            ]

    monkeypatch.setattr(progress_module, "OrchestrationBindingStore", FakeBindingStore)
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
        send_record_id = bound_chat_progress_send_record_id(
            surface_kind="discord",
            surface_key="channel-1",
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
        )
        notification_store.mark_delivered(
            delivery_record_id=send_record_id,
            delivered_message_id="msg-1",
        )
        await store.mark_outbox_delivered(send_record_id)

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


def _telegram_config(root: Path) -> TelegramBotConfig:
    return TelegramBotConfig.from_raw(
        {
            "enabled": True,
            "allowed_chat_ids": [-1001],
            "allowed_user_ids": [42],
            "state_file": ".codex-autorunner/telegram_state.sqlite3",
        },
        root=root,
        env={"CAR_TELEGRAM_BOT_TOKEN": "test-token"},
    )


@pytest.mark.anyio
async def test_discord_service_cleanup_retires_progress_from_service_outbox_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config: DiscordBotConfig = discord_config(tmp_path)
    store = DiscordStateStore(config.state_file)
    await store.initialize()
    service = DiscordBotService(
        config,
        logger=logging.getLogger("test.discord.progress"),
        state_store=store,
    )
    try:
        progress_send_record_id = bound_chat_progress_send_record_id(
            surface_kind="discord",
            surface_key="channel-1",
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
        )
        PmaNotificationStore(tmp_path).record_notification(
            correlation_id="managed-thread-progress:thread-1:turn-1:discord",
            source_kind="managed_thread_live_progress",
            delivery_mode="bound",
            surface_kind="discord",
            surface_key="channel-1",
            delivery_record_id=progress_send_record_id,
            managed_thread_id="thread-1",
            context={"managed_turn_id": "turn-1"},
        )
        progress_record = DiscordOutboxRecord(
            record_id=progress_send_record_id,
            channel_id="channel-1",
            message_id=None,
            operation="send",
            payload_json={"content": "working"},
            created_at="2026-01-01T00:00:00Z",
        )
        await store.enqueue_outbox(progress_record)

        async def _noop_delivery(*args: object, **kwargs: object) -> None:
            _ = args, kwargs

        deleted: list[tuple[str, str, str | None]] = []

        async def _fake_delete(
            channel_id: str,
            message_id: str,
            *,
            record_id: str | None = None,
        ) -> bool:
            deleted.append((channel_id, message_id, record_id))
            return True

        monkeypatch.setattr(
            "codex_autorunner.integrations.discord.service._handle_discord_outbox_delivery_impl",
            _noop_delivery,
        )
        monkeypatch.setattr(service, "_delete_channel_message_safe", _fake_delete)

        await service._handle_discord_outbox_delivery(progress_record, "progress-msg-1")
        await service._handle_discord_outbox_delivery(
            DiscordOutboxRecord(
                record_id="managed-thread:final",
                channel_id="channel-1",
                message_id=None,
                operation="send",
                payload_json={
                    "content": "done",
                    "_codex_autorunner_cleanup": (
                        build_bound_chat_progress_cleanup_metadata(
                            surface_kind="discord",
                            surface_key="channel-1",
                            managed_thread_id="thread-1",
                            managed_turn_id="turn-1",
                        )
                    ),
                },
                created_at="2026-01-01T00:00:01Z",
            ),
            "final-msg-1",
        )

        conversation = PmaNotificationStore(tmp_path).get_by_delivery_record_id(
            progress_send_record_id
        )
        assert conversation is not None
        assert conversation.delivered_message_id == "progress-msg-1"
        assert deleted == [
            (
                "channel-1",
                "progress-msg-1",
                f"discord:managed-thread-progress-cleanup:{progress_send_record_id}",
            )
        ]
    finally:
        await service._store.close()
        await service._rest.close()


@pytest.mark.anyio
async def test_telegram_service_cleanup_retires_progress_from_service_outbox_delivery(
    tmp_path: Path,
) -> None:
    config = _telegram_config(tmp_path)
    store = TelegramStateStore(config.state_file)
    service = TelegramBotService(config, hub_root=tmp_path)

    async def _mark_notification_delivered(_req: object) -> None:
        return None

    service._hub_client = SimpleNamespace(
        mark_notification_delivered=_mark_notification_delivered
    )
    try:
        progress_send_record_id = bound_chat_progress_send_record_id(
            surface_kind="telegram",
            surface_key="-1001:77",
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
        )
        PmaNotificationStore(tmp_path).record_notification(
            correlation_id="managed-thread-progress:thread-1:turn-1:telegram",
            source_kind="managed_thread_live_progress",
            delivery_mode="bound",
            surface_kind="telegram",
            surface_key="-1001:77",
            delivery_record_id=progress_send_record_id,
            managed_thread_id="thread-1",
            context={"managed_turn_id": "turn-1"},
        )
        progress_record = TelegramOutboxRecord(
            record_id=progress_send_record_id,
            chat_id=-1001,
            thread_id=77,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="working",
            created_at="2026-01-01T00:00:00Z",
            operation="send",
            message_id=None,
        )
        await store.enqueue_outbox(progress_record)

        await service._handle_telegram_outbox_delivery(progress_record, 77)
        await service._handle_telegram_outbox_delivery(
            TelegramOutboxRecord(
                record_id="managed-thread:final",
                chat_id=-1001,
                thread_id=77,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="done",
                created_at="2026-01-01T00:00:01Z",
                operation="send",
                message_id=None,
                delivery_metadata=build_bound_chat_progress_cleanup_metadata(
                    surface_kind="telegram",
                    surface_key="-1001:77",
                    managed_thread_id="thread-1",
                    managed_turn_id="turn-1",
                ),
            ),
            88,
        )

        conversation = PmaNotificationStore(tmp_path).get_by_delivery_record_id(
            progress_send_record_id
        )
        assert conversation is not None
        assert conversation.delivered_message_id == "77"
        cleanup_records = [
            record
            for record in await store.list_outbox()
            if record.operation == "delete"
            and record.record_id.startswith("managed-thread-progress-cleanup:")
        ]
        assert len(cleanup_records) == 1
        assert cleanup_records[0].message_id == 77
        assert progress_send_record_id in cleanup_records[0].record_id
        assert cleanup_records[0].outbox_key is not None
        assert progress_send_record_id in cleanup_records[0].outbox_key
    finally:
        await service._bot.close()


@pytest.mark.anyio
async def test_bound_live_progress_isolates_adapter_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBindingStore:
        def __init__(self, _hub_root: Path) -> None:
            _ = _hub_root

        def list_bindings(self, **_: object) -> list[object]:
            return [
                type(
                    "Binding",
                    (),
                    {"surface_kind": "discord", "surface_key": "channel-broken"},
                )(),
                type(
                    "Binding",
                    (),
                    {"surface_kind": "telegram", "surface_key": "123:456"},
                )(),
            ]

    class BrokenAdapter:
        surface_kind = "discord"
        surface_key = "channel-broken"

        async def publish(self, text: str) -> bool:
            _ = text
            raise RuntimeError("boom")

        async def complete_success(self) -> None:
            raise RuntimeError("boom")

        async def complete_with_message(self, text: str) -> None:
            _ = text
            raise RuntimeError("boom")

        async def close(self) -> None:
            raise RuntimeError("boom")

    class HealthyAdapter:
        surface_kind = "telegram"
        surface_key = "123:456"

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def publish(self, text: str) -> bool:
            self.calls.append(f"publish:{text}")
            return True

        async def complete_success(self) -> None:
            self.calls.append("complete_success")

        async def complete_with_message(self, text: str) -> None:
            self.calls.append(f"complete_with_message:{text}")

        async def close(self) -> None:
            self.calls.append("close")

    healthy = HealthyAdapter()

    def _fake_build_adapter(**kwargs: object):
        surface_key = kwargs["surface_key"]
        if surface_key == "channel-broken":
            return BrokenAdapter()
        if surface_key == "123:456":
            return healthy
        return None

    monkeypatch.setattr(progress_module, "OrchestrationBindingStore", FakeBindingStore)
    monkeypatch.setattr(
        progress_module, "_build_bound_progress_adapter", _fake_build_adapter
    )

    session = build_bound_chat_live_progress_session(
        hub_root=tmp_path,
        raw_config={},
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        agent="codex",
        model="gpt-5",
    )

    await session.start()
    await session.apply_run_events(
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="still working",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            )
        ]
    )
    await session.finalize(status="ok")
    await session.close()

    assert any(call.startswith("publish:") for call in healthy.calls)
    assert "complete_success" in healthy.calls
    assert "close" in healthy.calls


@pytest.mark.anyio
async def test_discord_bound_live_progress_attempts_immediate_send_when_bot_token_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path

    class FakeBindingStore:
        def __init__(self, _hub_root: Path) -> None:
            _ = _hub_root

        def list_bindings(self, **_: object) -> list[object]:
            return [
                type(
                    "Binding",
                    (),
                    {"surface_kind": "discord", "surface_key": "channel-1"},
                )()
            ]

    monkeypatch.setattr(progress_module, "OrchestrationBindingStore", FakeBindingStore)
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
            bound_chat_progress_send_record_id(
                surface_kind="discord",
                surface_key="channel-1",
                managed_thread_id="thread-1",
                managed_turn_id="turn-1",
            )
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

    class FakeBindingStore:
        def __init__(self, _hub_root: Path) -> None:
            _ = _hub_root

        def list_bindings(self, **_: object) -> list[object]:
            return [
                type(
                    "Binding",
                    (),
                    {"surface_kind": "telegram", "surface_key": "123:456"},
                )()
            ]

    monkeypatch.setattr(progress_module, "OrchestrationBindingStore", FakeBindingStore)
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
        send_record_id = bound_chat_progress_send_record_id(
            surface_kind="telegram",
            surface_key="123:456",
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
        )
        notification_store.mark_delivered(
            delivery_record_id=send_record_id,
            delivered_message_id="77",
        )
        await store.delete_outbox(send_record_id)

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

    class FakeBindingStore:
        def __init__(self, _hub_root: Path) -> None:
            _ = _hub_root

        def list_bindings(self, **_: object) -> list[object]:
            return [
                type(
                    "Binding",
                    (),
                    {"surface_kind": "telegram", "surface_key": "123:456"},
                )()
            ]

    monkeypatch.setattr(progress_module, "OrchestrationBindingStore", FakeBindingStore)
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
            bound_chat_progress_send_record_id(
                surface_kind="telegram",
                surface_key="123:456",
                managed_thread_id="thread-1",
                managed_turn_id="turn-1",
            )
        )
        assert conversation is not None
        assert conversation.delivered_message_id == "77"
        await session.close()
    finally:
        await store.close()
