from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.orchestration.chat_operation_state import ChatOperationState
from codex_autorunner.core.pma_notification_store import PmaNotificationStore
from codex_autorunner.core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    OutputDelta,
)
from codex_autorunner.integrations.chat import bound_live_progress as progress_module
from codex_autorunner.integrations.chat.bound_live_progress import (
    bound_chat_progress_delete_record_id,
    bound_chat_progress_edit_operation_id,
    bound_chat_progress_send_record_id,
    build_bound_chat_live_progress_session,
    build_bound_chat_progress_cleanup_metadata,
    build_bound_chat_queue_execution_controller,
    mark_bound_chat_progress_delivered,
    resolve_bound_chat_queue_progress_context,
)
from codex_autorunner.integrations.chat.managed_thread_turns import (
    ManagedThreadFinalizationResult,
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
async def test_bound_chat_queue_execution_controller_records_completed_targets_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeSession:
        surface_targets = (("discord", "channel-1"),)

        async def start(self) -> bool:
            calls.append("start")
            return True

        async def apply_run_events(self, events: list[object]) -> None:
            _ = events
            calls.append("progress")

        async def finalize(
            self,
            *,
            status: str,
            failure_message: str | None = None,
        ) -> None:
            _ = failure_message
            calls.append(f"finalize:{status}")

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(
        progress_module,
        "build_bound_chat_live_progress_session",
        lambda **_: FakeSession(),
    )
    controller = build_bound_chat_queue_execution_controller(
        hub_root=tmp_path,
        raw_config={},
        managed_thread_id="thread-1",
        retain_completed_surface_targets=True,
    )
    started = SimpleNamespace(
        execution=SimpleNamespace(execution_id="turn-1"),
        thread=SimpleNamespace(agent_id="codex"),
        request=SimpleNamespace(model="gpt-5"),
    )

    assert controller.hooks.on_execution_started is not None
    assert controller.hooks.on_progress_event is not None
    assert controller.hooks.on_execution_finalized is not None

    await controller.hooks.on_execution_started(started)
    await controller.hooks.on_progress_event(
        OutputDelta(
            timestamp="2026-01-01T00:00:01Z",
            content="working",
            delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
        )
    )
    await controller.hooks.on_execution_finalized(
        started,
        ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="done",
            error=None,
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
            backend_thread_id="backend-1",
        ),
    )

    assert controller.surface_targets_for("turn-1") == (("discord", "channel-1"),)
    controller.clear_surface_targets("turn-1")
    assert controller.surface_targets_for("turn-1") == ()
    assert calls == ["start", "progress", "close"]


@pytest.mark.anyio
async def test_bound_chat_queue_execution_controller_does_not_retain_targets_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        surface_targets = (("discord", "channel-1"),)

        async def start(self) -> bool:
            return True

        async def apply_run_events(self, events: list[object]) -> None:
            _ = events

        async def finalize(
            self,
            *,
            status: str,
            failure_message: str | None = None,
        ) -> None:
            _ = status, failure_message

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        progress_module,
        "build_bound_chat_live_progress_session",
        lambda **_: FakeSession(),
    )
    controller = build_bound_chat_queue_execution_controller(
        hub_root=tmp_path,
        raw_config={},
        managed_thread_id="thread-1",
    )
    started = SimpleNamespace(
        execution=SimpleNamespace(execution_id="turn-default"),
        thread=SimpleNamespace(agent_id="codex"),
        request=SimpleNamespace(model="gpt-5"),
    )

    assert controller.hooks.on_execution_started is not None
    assert controller.hooks.on_execution_finalized is not None

    await controller.hooks.on_execution_started(started)
    await controller.hooks.on_execution_finalized(
        started,
        ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="done",
            error=None,
            managed_thread_id="thread-1",
            managed_turn_id="turn-default",
            backend_thread_id="backend-1",
        ),
    )

    assert controller.surface_targets_for("turn-default") == ()


@pytest.mark.anyio
async def test_bound_chat_queue_execution_controller_finalizes_error_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeSession:
        surface_targets = (("telegram", "123:55"),)

        async def start(self) -> bool:
            calls.append("start")
            return True

        async def apply_run_events(self, events: list[object]) -> None:
            _ = events

        async def finalize(
            self,
            *,
            status: str,
            failure_message: str | None = None,
        ) -> None:
            calls.append(f"finalize:{status}:{failure_message}")

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(
        progress_module,
        "build_bound_chat_live_progress_session",
        lambda **_: FakeSession(),
    )
    controller = build_bound_chat_queue_execution_controller(
        hub_root=tmp_path,
        raw_config={},
        managed_thread_id="thread-1",
    )
    started = SimpleNamespace(
        execution=SimpleNamespace(execution_id="turn-2"),
        thread=SimpleNamespace(agent_id="codex"),
        request=SimpleNamespace(model="gpt-5"),
    )

    assert controller.hooks.on_execution_started is not None
    assert controller.hooks.on_execution_error is not None

    await controller.hooks.on_execution_started(started)
    await controller.hooks.on_execution_error(started, RuntimeError("boom"))

    assert controller.surface_targets_for("turn-2") == ()
    assert calls == ["start", "finalize:error:boom", "close"]


@pytest.mark.anyio
async def test_resolve_bound_chat_queue_progress_context_injects_discord_service_credentials(
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
                    {"surface_kind": "discord", "surface_key": "channel-1"},
                )()
            ]

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

    monkeypatch.setattr(progress_module, "OrchestrationBindingStore", FakeBindingStore)
    monkeypatch.setattr(progress_module, "DiscordRestClient", FakeDiscordRestClient)

    state_path = tmp_path / ".codex-autorunner" / "discord_state.sqlite3"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    owner = SimpleNamespace(
        _config=SimpleNamespace(
            root=tmp_path,
            raw={"discord_bot": {"state_file": str(state_path)}},
            bot_token="discord-token",
            application_id="app-1",
            state_file=state_path,
        )
    )

    hub_root, raw_config = resolve_bound_chat_queue_progress_context(
        owner,
        fallback_root=tmp_path / "fallback",
    )

    assert hub_root == tmp_path
    assert raw_config["discord_bot"]["bot_token"] == "discord-token"
    assert raw_config["discord_bot"]["state_file"] == str(state_path)

    store = DiscordStateStore(state_path)
    try:
        await store.initialize()
        session = build_bound_chat_live_progress_session(
            hub_root=hub_root,
            raw_config=raw_config,
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
            agent="codex",
            model="gpt-5",
        )

        await session.start()

        assert await store.list_outbox() == []
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
async def test_bound_chat_queue_execution_controller_ignores_startup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeSession:
        surface_targets = (("discord", "channel-1"),)

        async def start(self) -> bool:
            calls.append("start")
            raise RuntimeError("boom")

        async def apply_run_events(self, events: list[object]) -> None:
            _ = events

        async def finalize(
            self,
            *,
            status: str,
            failure_message: str | None = None,
        ) -> None:
            _ = status, failure_message

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(
        progress_module,
        "build_bound_chat_live_progress_session",
        lambda **_: FakeSession(),
    )
    controller = build_bound_chat_queue_execution_controller(
        hub_root=tmp_path,
        raw_config={},
        managed_thread_id="thread-1",
    )
    started = SimpleNamespace(
        execution=SimpleNamespace(execution_id="turn-3"),
        thread=SimpleNamespace(agent_id="codex"),
        request=SimpleNamespace(model="gpt-5"),
    )

    assert controller.hooks.on_execution_started is not None
    await controller.hooks.on_execution_started(started)

    assert controller.surface_targets_for("turn-3") == ()
    assert calls == ["start", "close"]


@pytest.mark.anyio
async def test_bound_chat_queue_execution_controller_only_runs_post_start_hook_after_visible_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeSession:
        surface_targets = (("discord", "channel-1"),)

        async def start(self) -> bool:
            calls.append("start")
            return False

        async def apply_run_events(self, events: list[object]) -> None:
            _ = events

        async def finalize(
            self,
            *,
            status: str,
            failure_message: str | None = None,
        ) -> None:
            _ = status, failure_message

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(
        progress_module,
        "build_bound_chat_live_progress_session",
        lambda **_: FakeSession(),
    )
    controller = build_bound_chat_queue_execution_controller(
        hub_root=tmp_path,
        raw_config={},
        managed_thread_id="thread-1",
        on_progress_session_started=lambda _started: calls.append("post-start"),
    )
    started = SimpleNamespace(
        execution=SimpleNamespace(execution_id="turn-4"),
        thread=SimpleNamespace(agent_id="codex"),
        request=SimpleNamespace(model="gpt-5"),
    )

    assert controller.hooks.on_execution_started is not None
    assert controller.hooks.on_execution_finished is not None

    await controller.hooks.on_execution_started(started)
    await controller.hooks.on_execution_finished(started)

    assert calls == ["start", "close"]


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
        stale_edit_operation_id = bound_chat_progress_edit_operation_id(
            surface_kind="discord",
            surface_key="channel-1",
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
        )
        stale_delete_record_id = bound_chat_progress_delete_record_id(
            surface_kind="discord",
            surface_key="channel-1",
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
        )
        await store.enqueue_outbox(
            DiscordOutboxRecord(
                record_id=f"{stale_edit_operation_id}:stale-edit",
                channel_id="channel-1",
                message_id="msg-1",
                operation="edit",
                payload_json={"content": "working 1"},
                created_at="2026-01-01T00:00:01Z",
                operation_id=stale_edit_operation_id,
            )
        )
        await store.enqueue_outbox(
            DiscordOutboxRecord(
                record_id=stale_delete_record_id,
                channel_id="channel-1",
                message_id="msg-1",
                operation="delete",
                payload_json={},
                created_at="2026-01-01T00:00:01Z",
            )
        )

        await session.finalize(status="ok")
        records = await store.list_outbox()
        assert any(
            record.operation == "delete" and record.message_id == "msg-1"
            for record in records
        )
        assert await store.get_outbox(f"{stale_edit_operation_id}:stale-edit") is None
        delete_record = await store.get_outbox(stale_delete_record_id)
        assert delete_record is not None
        assert delete_record.created_at != "2026-01-01T00:00:01Z"
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
        stale_edit_operation_id = bound_chat_progress_edit_operation_id(
            surface_kind="discord",
            surface_key="channel-1",
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
        )
        stale_delete_record_id = bound_chat_progress_delete_record_id(
            surface_kind="discord",
            surface_key="channel-1",
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
        )
        await store.enqueue_outbox(
            DiscordOutboxRecord(
                record_id=f"{stale_edit_operation_id}:stale-edit",
                channel_id="channel-1",
                message_id="progress-msg-1",
                operation="edit",
                payload_json={"content": "working 1"},
                created_at="2026-01-01T00:00:00Z",
                operation_id=stale_edit_operation_id,
            )
        )
        await store.enqueue_outbox(
            DiscordOutboxRecord(
                record_id=stale_delete_record_id,
                channel_id="channel-1",
                message_id="progress-msg-1",
                operation="delete",
                payload_json={},
                created_at="2026-01-01T00:00:00Z",
            )
        )

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
        assert await store.get_outbox(f"{stale_edit_operation_id}:stale-edit") is None
        assert await store.get_outbox(stale_delete_record_id) is None
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
        stale_edit_operation_id = bound_chat_progress_edit_operation_id(
            surface_kind="telegram",
            surface_key="-1001:77",
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
        )
        stale_delete_record_id = bound_chat_progress_delete_record_id(
            surface_kind="telegram",
            surface_key="-1001:77",
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
        )
        await store.enqueue_outbox(
            TelegramOutboxRecord(
                record_id=f"{stale_edit_operation_id}:stale-edit",
                chat_id=-1001,
                thread_id=77,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="working 1",
                created_at="2026-01-01T00:00:00Z",
                operation="edit",
                message_id=77,
                operation_id=stale_edit_operation_id,
            )
        )
        await store.enqueue_outbox(
            TelegramOutboxRecord(
                record_id=stale_delete_record_id,
                chat_id=-1001,
                thread_id=77,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="",
                created_at="2026-01-01T00:00:00Z",
                operation="delete",
                message_id=77,
            )
        )

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
        assert await store.get_outbox(f"{stale_edit_operation_id}:stale-edit") is None
        assert await store.get_outbox(stale_delete_record_id) is None
    finally:
        await service._bot.close()


@pytest.mark.anyio
async def test_telegram_service_does_not_mark_failed_outbox_delivery_as_delivered(
    tmp_path: Path,
) -> None:
    config = _telegram_config(tmp_path)
    service = TelegramBotService(config, hub_root=tmp_path)
    state_changes: list[dict[str, object]] = []

    async def _mark_chat_operation_state(
        operation_id: str | None,
        *,
        state: object,
        **changes: object,
    ) -> None:
        state_changes.append(
            {
                "operation_id": operation_id,
                "state": state,
                **changes,
            }
        )

    service._mark_chat_operation_state = _mark_chat_operation_state  # type: ignore[method-assign]
    try:
        await service._handle_telegram_outbox_delivery(
            TelegramOutboxRecord(
                record_id="terminal:telegram:failed",
                chat_id=123,
                thread_id=77,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="final reply",
                created_at="2026-01-01T00:00:00Z",
                operation="send",
                message_id=None,
                operation_id="op-123",
                delivery_metadata=build_bound_chat_progress_cleanup_metadata(
                    surface_kind="telegram",
                    surface_key="123:77",
                    managed_thread_id="thread-1",
                    managed_turn_id="turn-1",
                ),
            ),
            None,
        )

        assert state_changes == [
            {
                "operation_id": "op-123",
                "state": ChatOperationState.FAILED,
                "delivery_state": "failed",
                "terminal_outcome": "failed",
            }
        ]
    finally:
        await service._bot.close()


@pytest.mark.anyio
async def test_telegram_service_marks_edit_delivery_without_message_id_completed(
    tmp_path: Path,
) -> None:
    config = _telegram_config(tmp_path)
    service = TelegramBotService(config, hub_root=tmp_path)
    state_changes: list[dict[str, object]] = []

    async def _mark_chat_operation_state(
        operation_id: str | None,
        *,
        state: object,
        **changes: object,
    ) -> None:
        state_changes.append(
            {
                "operation_id": operation_id,
                "state": state,
                **changes,
            }
        )

    service._mark_chat_operation_state = _mark_chat_operation_state  # type: ignore[method-assign]
    try:
        await service._handle_telegram_outbox_delivery(
            TelegramOutboxRecord(
                record_id="terminal:telegram:edit",
                chat_id=123,
                thread_id=77,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="updated",
                created_at="2026-01-01T00:00:00Z",
                operation="edit",
                message_id=55,
                operation_id="op-edit-1",
            ),
            None,
        )

        assert state_changes == [
            {
                "operation_id": "op-edit-1",
                "state": ChatOperationState.COMPLETED,
                "delivery_state": "delivered",
            }
        ]
    finally:
        await service._bot.close()


@pytest.mark.anyio
async def test_telegram_service_failed_terminal_send_retires_progress_anchor(
    tmp_path: Path,
) -> None:
    config = _telegram_config(tmp_path)
    service = TelegramBotService(config, hub_root=tmp_path)

    progress_send_record_id = bound_chat_progress_send_record_id(
        surface_kind="telegram",
        surface_key="123:77",
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
    )
    PmaNotificationStore(tmp_path).record_notification(
        correlation_id="managed-thread-progress:thread-1:turn-1:telegram",
        source_kind="managed_thread_live_progress",
        delivery_mode="bound",
        surface_kind="telegram",
        surface_key="123:77",
        delivery_record_id=progress_send_record_id,
        managed_thread_id="thread-1",
        context={"managed_turn_id": "turn-1"},
    )
    mark_bound_chat_progress_delivered(
        hub_root=tmp_path,
        delivery_record_id=progress_send_record_id,
        delivered_message_id="77",
    )
    try:
        await service._handle_telegram_outbox_delivery(
            TelegramOutboxRecord(
                record_id="terminal:telegram:failed",
                chat_id=123,
                thread_id=77,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="final reply",
                created_at="2026-01-01T00:00:00Z",
                operation="send",
                message_id=None,
                operation_id="op-123",
                delivery_metadata=build_bound_chat_progress_cleanup_metadata(
                    surface_kind="telegram",
                    surface_key="123:77",
                    managed_thread_id="thread-1",
                    managed_turn_id="turn-1",
                ),
            ),
            None,
        )

        cleanup_record = await service._store.get_outbox(
            f"managed-thread-progress-failure:{progress_send_record_id}"
        )
        assert cleanup_record is not None
        assert cleanup_record.operation == "edit"
        assert cleanup_record.message_id == 77
        assert (
            cleanup_record.text
            == "Status: this turn finished, but Telegram failed before the final reply was delivered. Please retry if needed."
        )
    finally:
        await service._bot.close()


@pytest.mark.anyio
async def test_telegram_service_failed_terminal_send_cancels_pending_progress_send(
    tmp_path: Path,
) -> None:
    config = _telegram_config(tmp_path)
    service = TelegramBotService(config, hub_root=tmp_path)

    progress_send_record_id = bound_chat_progress_send_record_id(
        surface_kind="telegram",
        surface_key="123:77",
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
    )
    await service._store.enqueue_outbox(
        TelegramOutboxRecord(
            record_id=progress_send_record_id,
            chat_id=123,
            thread_id=77,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="working",
            created_at="2026-01-01T00:00:00Z",
            operation="send",
            message_id=None,
        )
    )
    try:
        await service._handle_telegram_outbox_delivery(
            TelegramOutboxRecord(
                record_id="terminal:telegram:failed-pending-progress",
                chat_id=123,
                thread_id=77,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="final reply",
                created_at="2026-01-01T00:00:01Z",
                operation="send",
                message_id=None,
                delivery_metadata=build_bound_chat_progress_cleanup_metadata(
                    surface_kind="telegram",
                    surface_key="123:77",
                    managed_thread_id="thread-1",
                    managed_turn_id="turn-1",
                ),
            ),
            None,
        )

        assert await service._store.get_outbox(progress_send_record_id) is None
    finally:
        await service._bot.close()


@pytest.mark.anyio
async def test_telegram_service_failed_terminal_send_retires_stale_progress_updates(
    tmp_path: Path,
) -> None:
    config = _telegram_config(tmp_path)
    service = TelegramBotService(config, hub_root=tmp_path)

    progress_send_record_id = bound_chat_progress_send_record_id(
        surface_kind="telegram",
        surface_key="123:77",
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
    )
    stale_edit_operation_id = bound_chat_progress_edit_operation_id(
        surface_kind="telegram",
        surface_key="123:77",
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
    )
    stale_delete_record_id = bound_chat_progress_delete_record_id(
        surface_kind="telegram",
        surface_key="123:77",
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
    )
    PmaNotificationStore(tmp_path).record_notification(
        correlation_id="managed-thread-progress:thread-1:turn-1:telegram:stale",
        source_kind="managed_thread_live_progress",
        delivery_mode="bound",
        surface_kind="telegram",
        surface_key="123:77",
        delivery_record_id=progress_send_record_id,
        managed_thread_id="thread-1",
        context={"managed_turn_id": "turn-1"},
    )
    mark_bound_chat_progress_delivered(
        hub_root=tmp_path,
        delivery_record_id=progress_send_record_id,
        delivered_message_id="77",
    )
    await service._store.enqueue_outbox(
        TelegramOutboxRecord(
            record_id=f"{stale_edit_operation_id}:stale-edit",
            chat_id=123,
            thread_id=77,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="working 1",
            created_at="2026-01-01T00:00:00Z",
            operation="edit",
            message_id=77,
            operation_id=stale_edit_operation_id,
        )
    )
    await service._store.enqueue_outbox(
        TelegramOutboxRecord(
            record_id=stale_delete_record_id,
            chat_id=123,
            thread_id=77,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="",
            created_at="2026-01-01T00:00:01Z",
            operation="delete",
            message_id=77,
        )
    )

    try:
        await service._handle_telegram_outbox_delivery(
            TelegramOutboxRecord(
                record_id="terminal:telegram:failed-with-stale-progress",
                chat_id=123,
                thread_id=77,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="final reply",
                created_at="2026-01-01T00:00:02Z",
                operation="send",
                message_id=None,
                delivery_metadata=build_bound_chat_progress_cleanup_metadata(
                    surface_kind="telegram",
                    surface_key="123:77",
                    managed_thread_id="thread-1",
                    managed_turn_id="turn-1",
                ),
            ),
            None,
        )

        assert (
            await service._store.get_outbox(f"{stale_edit_operation_id}:stale-edit")
            is None
        )
        assert await service._store.get_outbox(stale_delete_record_id) is None
        failure_record = await service._store.get_outbox(
            f"managed-thread-progress-failure:{progress_send_record_id}"
        )
        assert failure_record is not None
        assert failure_record.operation == "edit"
    finally:
        await service._bot.close()


@pytest.mark.anyio
async def test_discord_service_failed_terminal_send_retires_stale_progress_updates(
    tmp_path: Path,
) -> None:
    config: DiscordBotConfig = discord_config(tmp_path)
    service = DiscordBotService(
        config,
        logger=logging.getLogger("test.discord.progress.failure_cleanup"),
        state_store=DiscordStateStore(config.state_file),
    )
    await service._store.initialize()

    progress_send_record_id = bound_chat_progress_send_record_id(
        surface_kind="discord",
        surface_key="channel-1",
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
    )
    stale_edit_operation_id = bound_chat_progress_edit_operation_id(
        surface_kind="discord",
        surface_key="channel-1",
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
    )
    stale_delete_record_id = bound_chat_progress_delete_record_id(
        surface_kind="discord",
        surface_key="channel-1",
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
    )
    PmaNotificationStore(tmp_path).record_notification(
        correlation_id="managed-thread-progress:thread-1:turn-1:discord:stale",
        source_kind="managed_thread_live_progress",
        delivery_mode="bound",
        surface_kind="discord",
        surface_key="channel-1",
        delivery_record_id=progress_send_record_id,
        managed_thread_id="thread-1",
        context={"managed_turn_id": "turn-1"},
    )
    mark_bound_chat_progress_delivered(
        hub_root=tmp_path,
        delivery_record_id=progress_send_record_id,
        delivered_message_id="progress-msg-1",
    )
    await service._store.enqueue_outbox(
        DiscordOutboxRecord(
            record_id=f"{stale_edit_operation_id}:stale-edit",
            channel_id="channel-1",
            message_id="progress-msg-1",
            operation="edit",
            payload_json={"content": "working 1"},
            created_at="2026-01-01T00:00:00Z",
            operation_id=stale_edit_operation_id,
        )
    )
    await service._store.enqueue_outbox(
        DiscordOutboxRecord(
            record_id=stale_delete_record_id,
            channel_id="channel-1",
            message_id="progress-msg-1",
            operation="delete",
            payload_json={},
            created_at="2026-01-01T00:00:01Z",
        )
    )
    try:
        await service._handle_discord_outbox_delivery(
            DiscordOutboxRecord(
                record_id="terminal:discord:failed-with-stale-progress",
                channel_id="channel-1",
                message_id=None,
                operation="send",
                payload_json={
                    "content": "final reply",
                    "_codex_autorunner_cleanup": build_bound_chat_progress_cleanup_metadata(
                        surface_kind="discord",
                        surface_key="channel-1",
                        managed_thread_id="thread-1",
                        managed_turn_id="turn-1",
                    ),
                },
                created_at="2026-01-01T00:00:02Z",
            ),
            None,
        )

        assert (
            await service._store.get_outbox(f"{stale_edit_operation_id}:stale-edit")
            is None
        )
        assert await service._store.get_outbox(stale_delete_record_id) is None
        failure_record = await service._store.get_outbox(
            f"managed-thread-progress-failure:{progress_send_record_id}"
        )
        assert failure_record is not None
        assert failure_record.operation == "edit"
        assert failure_record.message_id == "progress-msg-1"
    finally:
        await service._store.close()
        await service._rest.close()


@pytest.mark.anyio
async def test_telegram_run_polling_recovers_managed_thread_executions_on_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _telegram_config(tmp_path)
    service = TelegramBotService(config, hub_root=tmp_path)
    calls: list[str] = []

    async def _recover(_service: object) -> None:
        calls.append("recover")

    async def _run_chat_core() -> None:
        calls.append("run")

    async def _perform_hub_handshake() -> bool:
        return True

    monkeypatch.setattr(service, "_perform_hub_handshake", _perform_hub_handshake)
    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.service._recover_managed_thread_executions_on_startup_impl",
        _recover,
    )
    monkeypatch.setattr(service._chat_core, "run", _run_chat_core)

    await service.run_polling()

    assert calls == ["recover", "run"]
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
async def test_bound_live_progress_skips_malformed_bindings(
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
                    {"surface_kind": "telegram", "surface_key": "bad-key"},
                )(),
                type(
                    "Binding",
                    (),
                    {"surface_kind": "discord", "surface_key": "channel-1"},
                )(),
            ]

    monkeypatch.setattr(progress_module, "OrchestrationBindingStore", FakeBindingStore)
    session = build_bound_chat_live_progress_session(
        hub_root=tmp_path,
        raw_config={},
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        agent="codex",
        model="gpt-5",
    )

    assert session.surface_targets == (("discord", "channel-1"),)
    await session.close()


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
