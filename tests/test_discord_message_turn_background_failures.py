from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import codex_autorunner.integrations.discord.message_turns as discord_message_turns_module
import codex_autorunner.integrations.discord.service as discord_service_module
import codex_autorunner.integrations.discord.service_lifecycle as discord_service_lifecycle_module
from codex_autorunner.integrations.chat.dispatcher import build_dispatch_context
from codex_autorunner.integrations.discord.message_turns import (
    bind_discord_progress_task_context,
)
from codex_autorunner.integrations.discord.service import (
    DiscordBotService,
    DiscordMessageTurnResult,
)
from codex_autorunner.integrations.discord.state import DiscordStateStore
from tests.discord_message_turns_support import (
    _config,
    _FakeGateway,
    _FakeOutboxManager,
    _FakeRest,
    _message_create,
)

pytestmark = pytest.mark.slow


class _FakeIngress:
    async def submit_message(
        self,
        request,
        *,
        resolve_paused_flow_target,
        submit_flow_reply,
        submit_thread_message,
    ):
        _ = request, resolve_paused_flow_target, submit_flow_reply
        thread_result = await submit_thread_message(request)
        return SimpleNamespace(route="thread", thread_result=thread_result)


class _FakeThreadService:
    def __init__(self, *, execution_status: str = "running") -> None:
        self.execution_status = execution_status

    def get_thread_target(self, thread_target_id: str) -> Any:
        return SimpleNamespace(thread_target_id=thread_target_id)

    def get_latest_execution(self, thread_target_id: str) -> Any:
        return SimpleNamespace(
            execution_id="exec-1",
            status=self.execution_status,
            target_id=thread_target_id,
        )

    def get_running_execution(self, thread_target_id: str) -> Any:
        if self.execution_status == "running":
            return SimpleNamespace(
                execution_id="exec-1",
                status="running",
                target_id=thread_target_id,
            )
        return None

    def get_execution(self, thread_target_id: str, execution_id: str) -> Any:
        return SimpleNamespace(
            execution_id=execution_id,
            status=self.execution_status,
            target_id=thread_target_id,
        )


@pytest.mark.anyio
async def test_message_event_background_failure_cleans_up_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fail_execute(*args: Any, **kwargs: Any) -> DiscordMessageTurnResult:
        _ = args, kwargs
        raise Exception("background boom")

    monkeypatch.setattr(
        discord_message_turns_module,
        "build_surface_orchestration_ingress",
        lambda **_: _FakeIngress(),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (
            SimpleNamespace(),
            SimpleNamespace(thread_target_id="thread-1"),
        ),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_execute_discord_thread_message",
        _fail_execute,
    )

    try:
        event = service._chat_adapter.parse_message_event(
            _message_create("route via ingress")
        )
        assert event is not None
        await discord_message_turns_module.handle_message_event(
            service,
            event,
            build_dispatch_context(event),
            channel_id="channel-1",
            text="route via ingress",
            has_attachments=False,
            policy_result=None,
            log_event_fn=discord_service_module.log_event,
            build_ticket_flow_controller_fn=discord_service_module.build_ticket_flow_controller,
            ensure_worker_fn=discord_service_module.ensure_worker,
        )
        task_results = await asyncio.gather(
            *list(service._background_tasks), return_exceptions=True
        )
        assert task_results
        assert all(result is None for result in task_results)
        assert any(
            message["payload"]["content"] == "Received. Preparing turn..."
            for message in rest.channel_messages
        )
        assert any(
            message["message_id"] == "msg-1"
            for message in rest.deleted_channel_messages
        )
        assert any(
            message["payload"]["content"] == "Turn failed: background boom"
            for message in rest.channel_messages
        )
        assert service._discord_reusable_progress_messages == {}
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_event_pre_spawn_failure_cleans_up_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    monkeypatch.setattr(
        discord_message_turns_module,
        "build_surface_orchestration_ingress",
        lambda **_: _FakeIngress(),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (_ for _ in ()).throw(Exception("pre-spawn boom")),
    )

    try:
        event = service._chat_adapter.parse_message_event(
            _message_create("route via ingress")
        )
        assert event is not None
        await discord_message_turns_module.handle_message_event(
            service,
            event,
            build_dispatch_context(event),
            channel_id="channel-1",
            text="route via ingress",
            has_attachments=False,
            policy_result=None,
            log_event_fn=discord_service_module.log_event,
            build_ticket_flow_controller_fn=discord_service_module.build_ticket_flow_controller,
            ensure_worker_fn=discord_service_module.ensure_worker,
        )
        assert not service._background_tasks
        assert any(
            message["payload"]["content"] == "Received. Preparing turn..."
            for message in rest.channel_messages
        )
        assert any(
            message["message_id"] == "msg-1"
            for message in rest.deleted_channel_messages
        )
        assert any(
            message["payload"]["content"] == "Turn failed: pre-spawn boom"
            for message in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_event_cleanup_failure_does_not_send_false_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_execute(*args: Any, **kwargs: Any) -> DiscordMessageTurnResult:
        _ = args, kwargs
        return DiscordMessageTurnResult(
            final_message="handled by ingress",
            preview_message_id="msg-1",
        )

    async def _fail_flush_outbox_files(*args: Any, **kwargs: Any) -> None:
        _ = args, kwargs
        raise RuntimeError("outbox boom")

    monkeypatch.setattr(
        discord_message_turns_module,
        "build_surface_orchestration_ingress",
        lambda **_: _FakeIngress(),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_execute_discord_thread_message",
        _fake_execute,
    )
    service._flush_outbox_files = _fail_flush_outbox_files  # type: ignore[assignment]

    try:
        event = service._chat_adapter.parse_message_event(
            _message_create("route via ingress")
        )
        assert event is not None
        await discord_message_turns_module.handle_message_event(
            service,
            event,
            build_dispatch_context(event),
            channel_id="channel-1",
            text="route via ingress",
            has_attachments=False,
            policy_result=None,
            log_event_fn=discord_service_module.log_event,
            build_ticket_flow_controller_fn=discord_service_module.build_ticket_flow_controller,
            ensure_worker_fn=discord_service_module.ensure_worker,
        )
        await asyncio.gather(*list(service._background_tasks), return_exceptions=True)
        assert any(
            message["payload"]["content"] == "handled by ingress"
            for message in rest.channel_messages
        )
        assert not any(
            message["payload"]["content"].startswith("Turn failed:")
            for message in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_event_delivery_failure_retires_progress_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_execute(*args: Any, **kwargs: Any) -> DiscordMessageTurnResult:
        _ = args, kwargs
        await store.upsert_turn_progress_lease(
            lease_id="lease-1",
            managed_thread_id="thread-1",
            execution_id="exec-1",
            channel_id="channel-1",
            message_id="msg-1",
            state="active",
            progress_label="working",
        )
        return DiscordMessageTurnResult(
            final_message="handled by ingress",
            preview_message_id="msg-1",
            execution_id="exec-1",
        )

    async def _fail_delivery(*args: Any, **kwargs: Any) -> None:
        turn_result = kwargs.get("turn_result")
        if isinstance(turn_result, DiscordMessageTurnResult) and (
            turn_result.deferred_delivery or not turn_result.send_final_message
        ):
            return
        raise RuntimeError("delivery boom")

    monkeypatch.setattr(
        discord_message_turns_module,
        "build_surface_orchestration_ingress",
        lambda **_: _FakeIngress(),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (
            SimpleNamespace(),
            SimpleNamespace(thread_target_id="thread-1"),
        ),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_execute_discord_thread_message",
        _fake_execute,
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_deliver_discord_turn_result",
        _fail_delivery,
    )

    try:
        event = service._chat_adapter.parse_message_event(
            _message_create("route via ingress")
        )
        assert event is not None
        await discord_message_turns_module.handle_message_event(
            service,
            event,
            build_dispatch_context(event),
            channel_id="channel-1",
            text="route via ingress",
            has_attachments=False,
            policy_result=None,
            log_event_fn=discord_service_module.log_event,
            build_ticket_flow_controller_fn=discord_service_module.build_ticket_flow_controller,
            ensure_worker_fn=discord_service_module.ensure_worker,
        )
        await asyncio.gather(*list(service._background_tasks), return_exceptions=True)

        assert rest.edited_channel_messages
        retired = rest.edited_channel_messages[-1]
        assert retired["message_id"] == "msg-1"
        assert retired["payload"]["components"] == []
        assert "final reply was delivered" in (retired["payload"]["content"].lower())
        assert (
            await store.list_turn_progress_leases(
                managed_thread_id="thread-1",
                execution_id="exec-1",
            )
            == []
        )
        assert not any(
            message["payload"]["content"]
            == "Turn finished, but final reply delivery failed. Please retry."
            for message in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_background_task_done_reconciles_progress_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-1",
        managed_thread_id="thread-1",
        execution_id="exec-1",
        channel_id="channel-1",
        message_id="preview-1",
        state="active",
        progress_label="working",
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "build_discord_thread_orchestration_service",
        lambda _service: _FakeThreadService(execution_status="running"),
    )

    async def _boom() -> None:
        raise RuntimeError("queue worker crashed")

    try:
        task = service._spawn_task(_boom())
        bind_discord_progress_task_context(
            task,
            managed_thread_id="thread-1",
            lease_id="lease-1",
            channel_id="channel-1",
            message_id="preview-1",
            failure_note="Turn failed: queue worker crashed",
            orphaned=True,
        )
        await asyncio.gather(task, return_exceptions=True)
        while service._background_tasks:
            await asyncio.gather(
                *list(service._background_tasks),
                return_exceptions=True,
            )

        assert rest.edited_channel_messages
        assert "queue worker crashed" in (
            rest.edited_channel_messages[-1]["payload"]["content"].lower()
        )
        assert (
            await store.list_turn_progress_leases(
                managed_thread_id="thread-1",
                execution_id="exec-1",
            )
            == []
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_background_task_done_ignores_expected_progress_task_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-1",
        managed_thread_id="thread-1",
        execution_id="exec-1",
        channel_id="channel-1",
        message_id="preview-1",
        state="active",
        progress_label="working",
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "build_discord_thread_orchestration_service",
        lambda _service: _FakeThreadService(execution_status="running"),
    )

    blocker = asyncio.Event()

    async def _hang_forever() -> None:
        await blocker.wait()

    try:
        task = service._spawn_task(_hang_forever())
        bind_discord_progress_task_context(
            task,
            managed_thread_id="thread-1",
            execution_id="exec-1",
            lease_id="lease-1",
            channel_id="channel-1",
            message_id="preview-1",
            failure_note="Status: this progress message lost its worker.",
            orphaned=True,
        )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        while service._background_tasks:
            await asyncio.gather(
                *list(service._background_tasks),
                return_exceptions=True,
            )

        assert rest.edited_channel_messages == []
        leases = await store.list_turn_progress_leases(
            managed_thread_id="thread-1",
            execution_id="exec-1",
        )
        assert [lease.lease_id for lease in leases] == ["lease-1"]
    finally:
        blocker.set()
        await store.close()


@pytest.mark.anyio
async def test_background_task_done_reconciles_cancel_sensitive_progress_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-1",
        managed_thread_id="thread-1",
        execution_id="exec-1",
        channel_id="channel-1",
        message_id="preview-1",
        state="active",
        progress_label="working",
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "build_discord_thread_orchestration_service",
        lambda _service: _FakeThreadService(execution_status="running"),
    )

    blocker = asyncio.Event()

    async def _hang_forever() -> None:
        await blocker.wait()

    try:
        task = service._spawn_task(_hang_forever())
        bind_discord_progress_task_context(
            task,
            managed_thread_id="thread-1",
            execution_id="exec-1",
            lease_id="lease-1",
            channel_id="channel-1",
            message_id="preview-1",
            failure_note="Status: this progress message lost its queue worker.",
            orphaned=True,
            reconcile_on_cancel=True,
        )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        while service._background_tasks:
            await asyncio.gather(
                *list(service._background_tasks),
                return_exceptions=True,
            )

        assert rest.edited_channel_messages
        assert "lost its queue worker" in (
            rest.edited_channel_messages[-1]["payload"]["content"].lower()
        )
        assert (
            await store.list_turn_progress_leases(
                managed_thread_id="thread-1",
                execution_id="exec-1",
            )
            == []
        )
    finally:
        blocker.set()
        await store.close()


@pytest.mark.anyio
async def test_queued_delivery_preserves_progress_lease_until_terminal_delivery(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-1",
        managed_thread_id="thread-1",
        execution_id="exec-1",
        channel_id="channel-1",
        message_id="progress-1",
        state="active",
        progress_label="queued",
    )

    dispatch = SimpleNamespace(
        service=service,
        channel_id="channel-1",
        session_key="session-1",
        pending_compact_seed=None,
        agent="codex",
        model_override=None,
    )

    try:
        await discord_message_turns_module._deliver_discord_turn_result(
            dispatch,
            workspace_root=workspace,
            turn_result=DiscordMessageTurnResult(
                final_message="Queued (waiting for available worker...)",
                execution_id="exec-1",
                preserve_progress_lease=True,
            ),
        )

        assert any(
            message["payload"]["content"] == "Queued (waiting for available worker...)"
            for message in rest.channel_messages
        )
        leases = await store.list_turn_progress_leases(
            managed_thread_id="thread-1",
            execution_id="exec-1",
        )
        assert [lease.lease_id for lease in leases] == ["lease-1"]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_terminal_delivery_retire_progress_anchor_when_preview_delete_fails(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-1",
        managed_thread_id="thread-1",
        execution_id="exec-1",
        channel_id="channel-1",
        message_id="progress-1",
        state="active",
        progress_label="working",
    )

    async def _fail_delete(
        channel_id: str,
        message_id: str,
        *,
        record_id: str | None = None,
    ) -> bool:
        _ = (channel_id, message_id, record_id)
        return False

    service._delete_channel_message_safe = _fail_delete  # type: ignore[method-assign]
    dispatch = SimpleNamespace(
        service=service,
        channel_id="channel-1",
        session_key="session-1",
        pending_compact_seed=None,
        agent="codex",
        model_override=None,
    )
    supervision = SimpleNamespace(
        task_context={
            "managed_thread_id": "thread-1",
            "lease_id": "lease-1",
            "message_id": "progress-1",
            "execution_id": "exec-1",
        },
        set_message_id=lambda _value: None,
        set_execution_id=lambda _value: None,
        set_failure_note=lambda _value: None,
        clear_progress_tracking=lambda **_kwargs: None,
    )

    try:
        await discord_message_turns_module._deliver_discord_turn_result(
            dispatch,
            workspace_root=workspace,
            turn_result=DiscordMessageTurnResult(
                final_message="final response",
                preview_message_id="progress-1",
                execution_id="exec-1",
                send_final_message=True,
            ),
            supervision=supervision,
        )

        assert len(rest.channel_messages) == 1
        assert rest.channel_messages[0]["payload"]["content"] == "final response"
        assert rest.deleted_channel_messages == []
        assert rest.edited_channel_messages
        retired = rest.edited_channel_messages[-1]
        assert retired["message_id"] == "progress-1"
        assert "already completed" in retired["payload"]["content"].lower()
        assert retired["payload"]["components"] == []
        assert (
            await store.list_turn_progress_leases(
                managed_thread_id="thread-1",
                execution_id="exec-1",
            )
            == []
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_shutdown_timeout_reconciles_supervised_progress_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-1",
        managed_thread_id="thread-1",
        execution_id="exec-1",
        channel_id="channel-1",
        message_id="preview-1",
        state="active",
        progress_label="working",
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "build_discord_thread_orchestration_service",
        lambda _service: _FakeThreadService(execution_status="running"),
    )
    monkeypatch.setattr(
        discord_service_lifecycle_module,
        "_DISCORD_BACKGROUND_TASK_SHUTDOWN_GRACE_SECONDS",
        0.0,
    )

    blocker = asyncio.Event()

    async def _hang_forever() -> None:
        await blocker.wait()

    try:
        task = service._spawn_task(_hang_forever(), await_on_shutdown=True)
        bind_discord_progress_task_context(
            task,
            managed_thread_id="thread-1",
            execution_id="exec-1",
            lease_id="lease-1",
            channel_id="channel-1",
            message_id="preview-1",
            failure_note="Status: this progress message lost its worker.",
            shutdown_note=discord_message_turns_module._shutdown_progress_note(),
            orphaned=True,
        )

        await service._shutdown()

        assert rest.edited_channel_messages
        retired = rest.edited_channel_messages[-1]
        assert retired["message_id"] == "preview-1"
        assert retired["payload"]["components"] == []
        assert "interrupted during discord shutdown" in (
            retired["payload"]["content"].lower()
        )
        assert (
            await store.list_turn_progress_leases(
                managed_thread_id="thread-1",
                execution_id="exec-1",
            )
            == []
        )
    finally:
        blocker.set()
        await store.close()


@pytest.mark.anyio
async def test_startup_reconciles_orphaned_progress_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-1",
        managed_thread_id="thread-1",
        execution_id="exec-1",
        channel_id="channel-1",
        message_id="preview-1",
        state="active",
        progress_label="working",
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "build_discord_thread_orchestration_service",
        lambda _service: _FakeThreadService(execution_status="running"),
    )

    try:
        await service._reconcile_discord_progress_leases_on_startup()

        assert rest.edited_channel_messages
        assert "lost its discord worker during restart" in (
            rest.edited_channel_messages[-1]["payload"]["content"].lower()
        )
        assert await store.list_turn_progress_leases() == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_startup_reconciles_progress_leases_stuck_in_retiring_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-1",
        managed_thread_id="thread-1",
        execution_id="exec-1",
        channel_id="channel-1",
        message_id="preview-1",
        state="retiring",
        progress_label="working",
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "build_discord_thread_orchestration_service",
        lambda _service: _FakeThreadService(execution_status="running"),
    )

    try:
        await service._reconcile_discord_progress_leases_on_startup()

        assert rest.edited_channel_messages
        assert "lost its discord worker during restart" in (
            rest.edited_channel_messages[-1]["payload"]["content"].lower()
        )
        assert await store.list_turn_progress_leases() == []
    finally:
        await store.close()
