import asyncio
import contextlib
import logging
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from tests import discord_message_turns_support as support

import codex_autorunner.integrations.discord.managed_thread_routing as discord_managed_thread_routing_module
from codex_autorunner.core.orchestration import SQLiteManagedThreadDeliveryEngine
from codex_autorunner.core.orchestration.managed_thread_delivery import (
    ManagedThreadDeliveryState,
)
from codex_autorunner.core.ports.run_event import (
    ApprovalRequested,
    Completed,
    ToolCall,
)
from codex_autorunner.integrations.chat.managed_thread_progress_projector import (
    ManagedThreadProgressProjector,
)
from codex_autorunner.integrations.chat.progress_primitives import TurnProgressTracker
from codex_autorunner.integrations.discord.errors import (
    DiscordPermanentError,
    DiscordTransientError,
)

pytestmark = pytest.mark.slow


class _TransientEditProgressRest(support._FakeRest):
    def __init__(self) -> None:
        super().__init__()
        self.edit_attempts = 0

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _ = channel_id, message_id, payload
        self.edit_attempts += 1
        raise DiscordTransientError("simulated transient progress edit failure")


class _UnknownMessageEditProgressRest(support._FakeRest):
    def __init__(self) -> None:
        super().__init__()
        self.edit_attempts = 0

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _ = channel_id, message_id, payload
        self.edit_attempts += 1
        raise DiscordPermanentError(
            "Discord API request failed for PATCH /channels/channel-1/messages/msg-1: "
            'status=404 body=\'{"message": "Unknown Message", "code": 10008}\''
        )


@pytest.mark.asyncio
async def test_orchestrated_turn_pending_durable_delivery_keeps_direct_final_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    thread = SimpleNamespace(thread_target_id="thread-1")

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    class _Service:
        def __init__(self) -> None:
            self._config = support._config(tmp_path)
            self._store = _Store()
            self._rest = support._FakeRest()
            self._logger = logging.getLogger(__name__)

        async def _send_channel_message(
            self, channel_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            return await self._rest.create_channel_message(
                channel_id=channel_id,
                payload=payload,
            )

        def _resolve_agent_state(self, binding: Any) -> tuple[str, Optional[str]]:
            _ = binding
            return "hermes", None

        def _runtime_agent_for_binding(self, binding: Any) -> str:
            _ = binding
            return "hermes"

    async def _fake_run_managed_surface_turn(
        _request: Any,
        *,
        config: Any,
    ) -> support.discord_message_turns_module.DiscordMessageTurnResult:
        finalized = support.managed_thread_turns_module.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="fixture reply",
            error=None,
            managed_thread_id="thread-1",
            managed_turn_id="exec-1",
            backend_thread_id="backend-1",
            token_usage={"input_tokens": 1, "output_tokens": 1},
        )
        return await config.on_finalized(
            SimpleNamespace(
                durable_delivery_performed=False,
                durable_delivery_pending=True,
            ),
            finalized,
        )

    monkeypatch.setattr(
        support.discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "_build_discord_managed_thread_coordinator",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "run_managed_surface_turn",
        _fake_run_managed_surface_turn,
    )

    result = await support.discord_message_turns_module._run_discord_orchestrated_turn_for_message(
        _Service(),
        workspace_root=tmp_path,
        prompt_text="echo hello world",
        input_items=None,
        source_message_id=None,
        agent="hermes",
        model_override=None,
        reasoning_effort=None,
        session_key="session-1",
        orchestrator_channel_key="channel-1",
        managed_thread_surface_key=None,
        mode="pma",
        pma_enabled=True,
        execution_prompt="<user_message>\necho hello world\n</user_message>\n",
        public_execution_error="Discord PMA turn failed",
        timeout_error="Discord PMA turn timed out",
        interrupted_error="Discord PMA turn interrupted",
        approval_mode="never",
        sandbox_policy="dangerFullAccess",
        max_actions=12,
        min_edit_interval_seconds=1.0,
        heartbeat_interval_seconds=2.0,
    )

    assert result.send_final_message is True
    assert result.delivery_visibility_pending is True
    assert "fixture reply" in result.final_message


@pytest.mark.asyncio
async def test_spawn_discord_progress_background_task_uses_service_tracking() -> None:
    class _Service:
        def __init__(self) -> None:
            self.spawn_kwargs: list[bool] = []

        def _spawn_task(
            self,
            coro: Any,
            *,
            await_on_shutdown: bool = False,
        ) -> asyncio.Task[Any]:
            self.spawn_kwargs.append(await_on_shutdown)
            return asyncio.create_task(coro)

    async def _noop() -> None:
        return None

    service = _Service()
    task = support.discord_message_turns_module._spawn_discord_progress_background_task(
        service,
        _noop(),
        managed_thread_id="thread-1",
        lease_id="lease-1",
        channel_id="channel-1",
        message_id="msg-1",
        await_on_shutdown=True,
    )

    try:
        await task
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert service.spawn_kwargs == [True]
    assert getattr(task, "_discord_progress_task_context", None) == {
        "managed_thread_id": "thread-1",
        "lease_id": "lease-1",
        "channel_id": "channel-1",
        "message_id": "msg-1",
    }


@pytest.mark.anyio
async def test_apply_discord_progress_run_event_tracks_shared_semantic_phase_order() -> (
    None
):
    tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="default",
        label="working",
        max_actions=8,
        max_output_chars=400,
    )
    projector = ManagedThreadProgressProjector(
        tracker,
        min_render_interval_seconds=1.0,
        heartbeat_interval_seconds=2.0,
    )
    projector.mark_queued()
    projector.mark_working()
    edits: list[dict[str, Any]] = []

    async def _edit_progress(**kwargs: Any) -> None:
        edits.append(kwargs)

    for event in (
        ApprovalRequested(
            timestamp="2026-03-15T00:00:00Z",
            request_id="req-1",
            description="Need approval to run tests",
            context={},
        ),
        ToolCall(
            timestamp="2026-03-15T00:00:01Z",
            tool_name="exec",
            tool_input={"cmd": "pytest -q"},
        ),
        Completed(
            timestamp="2026-03-15T00:00:02Z",
            final_message="tests passed",
        ),
    ):
        await support.discord_message_turns_module._apply_discord_progress_run_event(
            projector,
            event,
            edit_progress=_edit_progress,
        )

    assert projector.phase_sequence() == (
        "queued",
        "working",
        "approval",
        "progress",
        "terminal",
    )
    assert len(edits) == 3


@pytest.mark.anyio
async def test_reconcile_progress_lease_retries_when_retire_edit_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = support.DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_turn_progress_lease(
        lease_id="lease-1",
        managed_thread_id="thread-1",
        execution_id="exec-1",
        channel_id="channel-1",
        message_id="msg-1",
        state="active",
        progress_label="running",
    )

    fake_orchestration_service = SimpleNamespace(
        get_thread_target=lambda _thread_id: SimpleNamespace(
            thread_target_id="thread-1"
        ),
        get_latest_execution=lambda _thread_id: SimpleNamespace(
            execution_id="exec-1",
            status="ok",
        ),
        get_running_execution=lambda _thread_id: None,
        get_execution=lambda _thread_id, _execution_id: SimpleNamespace(
            execution_id="exec-1",
            status="ok",
        ),
    )
    monkeypatch.setattr(
        discord_managed_thread_routing_module,
        "build_discord_thread_orchestration_service",
        lambda _service: fake_orchestration_service,
    )

    service = SimpleNamespace(
        _store=store,
        _rest=support._EditFailingProgressRest(),
        _config=support._config(tmp_path),
        _logger=logging.getLogger("test"),
    )

    try:
        reconciled = await support.discord_progress_leases_module.reconcile_discord_turn_progress_leases(
            service,
            lease_id="lease-1",
        )
        assert reconciled == 0

        retained = await store.get_turn_progress_lease(lease_id="lease-1")
        assert retained is not None
        assert retained.state == "retiring"

        service._rest = support._FakeRest()
        reconciled = await support.discord_progress_leases_module.reconcile_discord_turn_progress_leases(
            service,
            lease_id="lease-1",
        )
        assert reconciled == 1

        retired = await store.get_turn_progress_lease(lease_id="lease-1")
        assert retired is None
        assert service._rest.edited_channel_messages == [
            {
                "channel_id": "channel-1",
                "message_id": "msg-1",
                "payload": {
                    "content": "Status: this turn already completed.",
                    "components": [],
                },
            }
        ]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_deliver_result_reconciles_stale_sibling_progress_leases(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = support.DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_turn_progress_lease(
        lease_id="lease-stale",
        managed_thread_id="thread-1",
        execution_id=None,
        channel_id="channel-1",
        message_id="100",
        state="pending",
        progress_label="working",
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-current",
        managed_thread_id="thread-1",
        execution_id="exec-2",
        channel_id="channel-1",
        message_id="200",
        state="active",
        progress_label="working",
    )

    monkeypatch.setattr(
        discord_managed_thread_routing_module,
        "build_discord_thread_orchestration_service",
        lambda _service: SimpleNamespace(
            get_thread_target=lambda _thread_id: SimpleNamespace(
                thread_target_id="thread-1"
            ),
            get_latest_execution=lambda _thread_id: SimpleNamespace(
                execution_id="exec-2",
                status="ok",
            ),
            get_running_execution=lambda _thread_id: None,
            get_execution=lambda _thread_id, execution_id: SimpleNamespace(
                execution_id=execution_id,
                status="ok",
            ),
        ),
    )

    rest = support._FakeRest()
    service = support.DiscordBotService(
        support._config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=support._FakeGateway([]),
        state_store=store,
        outbox_manager=support._FakeOutboxManager(),
    )
    supervision = support.discord_message_turns_module._DiscordTurnExecutionSupervision(
        service,
        channel_id="channel-1",
    )
    supervision.set_managed_thread_id("thread-1")
    supervision.set_lease_id("lease-current")
    supervision.set_message_id("200")
    supervision.set_execution_id("exec-2")
    dispatch = SimpleNamespace(
        service=service,
        channel_id="channel-1",
        session_key="session-1",
        pending_compact_seed=None,
        agent="codex",
        model_override=None,
    )

    try:
        await support.discord_message_turns_module._deliver_discord_turn_result(
            dispatch,
            workspace_root=workspace,
            turn_result=support.DiscordMessageTurnResult(
                final_message="done",
                preview_message_id="200",
                execution_id="exec-2",
            ),
            supervision=supervision,
        )

        assert await store.list_turn_progress_leases(managed_thread_id="thread-1") == []
        assert any(
            deleted["message_id"] == "200" for deleted in rest.deleted_channel_messages
        )
        assert any(
            edited["message_id"] == "100"
            and "failed before execution started"
            in edited["payload"]["content"].lower()
            for edited in rest.edited_channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_deliver_result_reconciles_stale_siblings_without_final_message(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = support.DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_turn_progress_lease(
        lease_id="lease-stale",
        managed_thread_id="thread-1",
        execution_id=None,
        channel_id="channel-1",
        message_id="100",
        state="pending",
        progress_label="working",
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-current",
        managed_thread_id="thread-1",
        execution_id="exec-2",
        channel_id="channel-1",
        message_id="200",
        state="active",
        progress_label="working",
    )

    monkeypatch.setattr(
        discord_managed_thread_routing_module,
        "build_discord_thread_orchestration_service",
        lambda _service: SimpleNamespace(
            get_thread_target=lambda _thread_id: SimpleNamespace(
                thread_target_id="thread-1"
            ),
            get_latest_execution=lambda _thread_id: SimpleNamespace(
                execution_id="exec-2",
                status="interrupted",
            ),
            get_running_execution=lambda _thread_id: None,
            get_execution=lambda _thread_id, execution_id: SimpleNamespace(
                execution_id=execution_id,
                status="interrupted",
            ),
        ),
    )

    rest = support._FakeRest()
    service = support.DiscordBotService(
        support._config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=support._FakeGateway([]),
        state_store=store,
        outbox_manager=support._FakeOutboxManager(),
    )
    supervision = support.discord_message_turns_module._DiscordTurnExecutionSupervision(
        service,
        channel_id="channel-1",
    )
    supervision.set_managed_thread_id("thread-1")
    supervision.set_lease_id("lease-current")
    supervision.set_message_id("200")
    supervision.set_execution_id("exec-2")
    dispatch = SimpleNamespace(
        service=service,
        channel_id="channel-1",
        session_key="session-1",
        pending_compact_seed=None,
        agent="codex",
        model_override=None,
    )

    try:
        await support.discord_message_turns_module._deliver_discord_turn_result(
            dispatch,
            workspace_root=workspace,
            turn_result=support.DiscordMessageTurnResult(
                final_message="Message received. Switching to it now...",
                execution_id="exec-2",
                send_final_message=False,
            ),
            supervision=supervision,
        )

        assert await store.list_turn_progress_leases(managed_thread_id="thread-1") == []
        assert rest.channel_messages == []
        assert any(
            edited["message_id"] == "100"
            and "failed before execution started"
            in edited["payload"]["content"].lower()
            for edited in rest.edited_channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_deliver_result_marks_pending_durable_delivery_direct_surface_delivered_after_visible_final_send(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = support.DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = support._FakeRest()
    service = support.DiscordBotService(
        support._config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=support._FakeGateway([]),
        state_store=store,
        outbox_manager=support._FakeOutboxManager(),
    )
    engine = SQLiteManagedThreadDeliveryEngine(tmp_path)
    finalized = support.managed_thread_turns_module.ManagedThreadFinalizationResult(
        status="ok",
        assistant_text="fixture reply",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="exec-1",
        backend_thread_id="backend-1",
    )
    intent = support.managed_thread_turns_module.build_managed_thread_delivery_intent(
        finalized,
        surface=support.managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Discord",
            surface_kind="discord",
            surface_key="channel-1",
        ),
        transport_target={"channel_id": "channel-1"},
    )
    record = engine.create_intent(intent).record
    patched = engine._ledger.patch_delivery(
        record.delivery_id,
        state=ManagedThreadDeliveryState.RETRY_SCHEDULED,
    )
    assert patched is not None
    claim = engine.claim_delivery(record.delivery_id)
    assert claim is not None
    dispatch = SimpleNamespace(
        service=service,
        channel_id="channel-1",
        session_key="session-1",
        pending_compact_seed=None,
        agent="codex",
        model_override=None,
    )

    try:
        await support.discord_message_turns_module._deliver_discord_turn_result(
            dispatch,
            workspace_root=workspace,
            turn_result=support.DiscordMessageTurnResult(
                final_message="done",
                execution_id="exec-1",
                send_final_message=True,
                delivery_visibility_pending=True,
                durable_delivery_id=record.delivery_id,
                durable_delivery_claim_token=claim.claim_token,
            ),
        )

        delivered = engine._ledger.get_delivery(record.delivery_id)
        assert delivered is not None
        assert delivered.state is ManagedThreadDeliveryState.DIRECT_SURFACE_DELIVERED
        assert len(rest.channel_messages) == 1
    finally:
        await store.close()


@pytest.mark.anyio
async def test_deliver_result_keeps_newer_pending_sibling_progress_lease(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = support.DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_turn_progress_lease(
        lease_id="lease-current",
        managed_thread_id="thread-1",
        execution_id="exec-2",
        channel_id="channel-1",
        message_id="200",
        state="active",
        progress_label="working",
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-newer",
        managed_thread_id="thread-1",
        execution_id=None,
        channel_id="channel-1",
        message_id="300",
        state="pending",
        progress_label="working",
    )

    monkeypatch.setattr(
        discord_managed_thread_routing_module,
        "build_discord_thread_orchestration_service",
        lambda _service: SimpleNamespace(
            get_thread_target=lambda _thread_id: SimpleNamespace(
                thread_target_id="thread-1"
            ),
            get_latest_execution=lambda _thread_id: SimpleNamespace(
                execution_id="exec-2",
                status="ok",
            ),
            get_running_execution=lambda _thread_id: None,
            get_execution=lambda _thread_id, execution_id: SimpleNamespace(
                execution_id=execution_id,
                status="ok",
            ),
        ),
    )

    rest = support._FakeRest()
    service = support.DiscordBotService(
        support._config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=support._FakeGateway([]),
        state_store=store,
        outbox_manager=support._FakeOutboxManager(),
    )
    supervision = support.discord_message_turns_module._DiscordTurnExecutionSupervision(
        service,
        channel_id="channel-1",
    )
    supervision.set_managed_thread_id("thread-1")
    supervision.set_lease_id("lease-current")
    supervision.set_message_id("200")
    supervision.set_execution_id("exec-2")
    dispatch = SimpleNamespace(
        service=service,
        channel_id="channel-1",
        session_key="session-1",
        pending_compact_seed=None,
        agent="codex",
        model_override=None,
    )

    try:
        await support.discord_message_turns_module._deliver_discord_turn_result(
            dispatch,
            workspace_root=workspace,
            turn_result=support.DiscordMessageTurnResult(
                final_message="done",
                preview_message_id="200",
                execution_id="exec-2",
            ),
            supervision=supervision,
        )

        remaining = await store.list_turn_progress_leases(managed_thread_id="thread-1")
        assert [lease.lease_id for lease in remaining] == ["lease-newer"]
        assert rest.edited_channel_messages == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_deliver_result_keeps_newer_queued_sibling_progress_lease_with_execution_id(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = support.DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_turn_progress_lease(
        lease_id="lease-current",
        managed_thread_id="thread-1",
        execution_id="exec-2",
        channel_id="channel-1",
        message_id="200",
        state="active",
        progress_label="working",
    )
    await store.upsert_turn_progress_lease(
        lease_id="lease-newer",
        managed_thread_id="thread-1",
        execution_id="exec-3",
        channel_id="channel-1",
        message_id="300",
        state="active",
        progress_label="queued",
    )

    def _get_execution(_thread_id: str, execution_id: str) -> SimpleNamespace:
        status = "queued" if execution_id == "exec-3" else "ok"
        return SimpleNamespace(
            execution_id=execution_id,
            status=status,
        )

    monkeypatch.setattr(
        discord_managed_thread_routing_module,
        "build_discord_thread_orchestration_service",
        lambda _service: SimpleNamespace(
            get_thread_target=lambda _thread_id: SimpleNamespace(
                thread_target_id="thread-1"
            ),
            get_latest_execution=lambda _thread_id: SimpleNamespace(
                execution_id="exec-3",
                status="queued",
            ),
            get_running_execution=lambda _thread_id: None,
            get_execution=_get_execution,
        ),
    )

    rest = support._FakeRest()
    service = support.DiscordBotService(
        support._config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=support._FakeGateway([]),
        state_store=store,
        outbox_manager=support._FakeOutboxManager(),
    )
    supervision = support.discord_message_turns_module._DiscordTurnExecutionSupervision(
        service,
        channel_id="channel-1",
    )
    supervision.set_managed_thread_id("thread-1")
    supervision.set_lease_id("lease-current")
    supervision.set_message_id("200")
    supervision.set_execution_id("exec-2")
    dispatch = SimpleNamespace(
        service=service,
        channel_id="channel-1",
        session_key="session-1",
        pending_compact_seed=None,
        agent="codex",
        model_override=None,
    )

    try:
        await support.discord_message_turns_module._deliver_discord_turn_result(
            dispatch,
            workspace_root=workspace,
            turn_result=support.DiscordMessageTurnResult(
                final_message="done",
                preview_message_id="200",
                execution_id="exec-2",
            ),
            supervision=supervision,
        )

        remaining = await store.list_turn_progress_leases(managed_thread_id="thread-1")
        assert [lease.lease_id for lease in remaining] == ["lease-newer"]
        assert rest.edited_channel_messages == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_deliver_result_keeps_newer_reused_progress_message_lease(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = support.DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_turn_progress_lease(
        lease_id="lease-current",
        managed_thread_id="thread-1",
        execution_id="exec-2",
        channel_id="channel-1",
        message_id="200",
        state="active",
        progress_label="working",
    )
    # Reusing the same progress message for a newer queued execution replaces the
    # stored lease row but must not be retired by the older turn's delivery.
    await store.upsert_turn_progress_lease(
        lease_id="lease-newer",
        managed_thread_id="thread-1",
        execution_id="exec-3",
        channel_id="channel-1",
        message_id="200",
        state="active",
        progress_label="queued",
    )

    def _get_execution(_thread_id: str, execution_id: str) -> SimpleNamespace:
        status = "queued" if execution_id == "exec-3" else "ok"
        return SimpleNamespace(
            execution_id=execution_id,
            status=status,
        )

    monkeypatch.setattr(
        discord_managed_thread_routing_module,
        "build_discord_thread_orchestration_service",
        lambda _service: SimpleNamespace(
            get_thread_target=lambda _thread_id: SimpleNamespace(
                thread_target_id="thread-1"
            ),
            get_latest_execution=lambda _thread_id: SimpleNamespace(
                execution_id="exec-3",
                status="queued",
            ),
            get_running_execution=lambda _thread_id: None,
            get_execution=_get_execution,
        ),
    )

    rest = support._FakeRest()
    service = support.DiscordBotService(
        support._config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=support._FakeGateway([]),
        state_store=store,
        outbox_manager=support._FakeOutboxManager(),
    )
    supervision = support.discord_message_turns_module._DiscordTurnExecutionSupervision(
        service,
        channel_id="channel-1",
    )
    supervision.set_managed_thread_id("thread-1")
    supervision.set_lease_id("lease-current")
    supervision.set_message_id("200")
    supervision.set_execution_id("exec-2")
    dispatch = SimpleNamespace(
        service=service,
        channel_id="channel-1",
        session_key="session-1",
        pending_compact_seed=None,
        agent="codex",
        model_override=None,
    )

    try:
        await support.discord_message_turns_module._deliver_discord_turn_result(
            dispatch,
            workspace_root=workspace,
            turn_result=support.DiscordMessageTurnResult(
                final_message="done",
                preview_message_id="200",
                execution_id="exec-2",
            ),
            supervision=supervision,
        )

        remaining = await store.list_turn_progress_leases(managed_thread_id="thread-1")
        assert [lease.lease_id for lease in remaining] == ["lease-newer"]
        assert rest.edited_channel_messages == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_orchestrated_turn_interrupt_send_falls_back_when_progress_ack_edit_is_transient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    thread = SimpleNamespace(thread_target_id="thread-1")
    started_execution = SimpleNamespace(
        execution=SimpleNamespace(status="running"),
        thread=thread,
    )

    class _Rest(support._FakeRest):
        async def edit_channel_message(
            self, *, channel_id: str, message_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            _ = channel_id, message_id, payload
            raise DiscordTransientError("transient edit failure")

    rest = _Rest()

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    class _Service:
        def __init__(self) -> None:
            self._config = support._config(tmp_path)
            self._store = _Store()
            self._rest = rest
            self._logger = logging.getLogger(__name__)

        async def _send_channel_message(
            self, channel_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            return await rest.create_channel_message(
                channel_id=channel_id,
                payload=payload,
            )

        def _register_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _clear_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _resolve_agent_state(self, binding: Any) -> tuple[str, Optional[str]]:
            _ = binding
            return "codex", None

        def _runtime_agent_for_binding(self, binding: Any) -> str:
            _ = binding
            return "codex"

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return started_execution

    async def _fake_finalize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _ = args, kwargs
        return {"status": "interrupted", "error": "Discord PMA turn interrupted"}

    async def _fake_complete(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return SimpleNamespace(finalized=await _fake_finalize())

    monkeypatch.setattr(
        support.discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    service = _Service()
    support.discord_progress_leases_module.request_discord_turn_progress_reuse(
        service,
        thread_target_id="thread-1",
        source_message_id="m-2",
        acknowledgement="Message received. Switching to it now...",
    )

    result = await support.discord_message_turns_module._run_discord_orchestrated_turn_for_message(
        service,
        workspace_root=tmp_path,
        prompt_text="first prompt",
        source_message_id="m-1",
        agent="codex",
        model_override=None,
        reasoning_effort=None,
        session_key="session-1",
        orchestrator_channel_key="channel-1",
        managed_thread_surface_key=None,
        mode="pma",
        pma_enabled=True,
        execution_prompt="<user_message>\nfirst prompt\n</user_message>\n",
        public_execution_error="Discord PMA turn failed",
        timeout_error="Discord PMA turn timed out",
        interrupted_error="Discord PMA turn interrupted",
        approval_mode="never",
        sandbox_policy="dangerFullAccess",
        max_actions=12,
        min_edit_interval_seconds=1.0,
        heartbeat_interval_seconds=2.0,
    )

    assert result.send_final_message is True
    assert result.final_message == "Message received. Switching to it now..."
    assert len(rest.channel_messages) == 1
    assert service._discord_turn_progress_reuse_requests == {}
    assert service._discord_reusable_progress_messages == {}


@pytest.mark.asyncio
async def test_orchestrated_turn_ignores_transient_progress_edit_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    rest = _TransientEditProgressRest()
    thread = SimpleNamespace(thread_target_id="thread-1")
    started_execution = SimpleNamespace(
        execution=SimpleNamespace(status="running"),
        thread=thread,
    )

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    class _Service:
        def __init__(self) -> None:
            self._config = support._config(tmp_path)
            self._store = _Store()
            self._rest = rest
            self._logger = logging.getLogger(__name__)

        async def _send_channel_message(
            self, channel_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            return await rest.create_channel_message(
                channel_id=channel_id,
                payload=payload,
            )

        def _register_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _clear_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _resolve_agent_state(self, binding: Any) -> tuple[str, Optional[str]]:
            _ = binding
            return "codex", None

        def _runtime_agent_for_binding(self, binding: Any) -> str:
            _ = binding
            return "codex"

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return started_execution

    async def _fake_complete(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        await asyncio.sleep(0.03)
        return SimpleNamespace(
            finalized={
                "status": "ok",
                "assistant_text": "done",
                "token_usage": None,
            }
        )

    monkeypatch.setattr(
        support.discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def _capture_exception(
        _loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        loop_errors.append(context)

    loop.set_exception_handler(_capture_exception)
    try:
        result = await support.discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            _Service(),
            workspace_root=tmp_path,
            prompt_text="hi",
            input_items=None,
            source_message_id=None,
            agent="codex",
            model_override=None,
            reasoning_effort=None,
            session_key="s1",
            orchestrator_channel_key="channel-1",
            managed_thread_surface_key=None,
            mode="pma",
            pma_enabled=True,
            execution_prompt="<user_message>\nhi\n</user_message>\n",
            public_execution_error="err",
            timeout_error="timeout",
            interrupted_error="interrupt",
            approval_mode="never",
            sandbox_policy="dangerFullAccess",
            max_actions=12,
            min_edit_interval_seconds=0.0,
            heartbeat_interval_seconds=0.01,
        )
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert result.final_message == "done"
    assert rest.edit_attempts >= 1
    assert loop_errors == []


@pytest.mark.asyncio
async def test_orchestrated_turn_ignores_unknown_message_progress_edit_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    rest = _UnknownMessageEditProgressRest()
    thread = SimpleNamespace(thread_target_id="thread-1")
    started_execution = SimpleNamespace(
        execution=SimpleNamespace(status="running"),
        thread=thread,
    )

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    class _Service:
        def __init__(self) -> None:
            self._config = support._config(tmp_path)
            self._store = _Store()
            self._rest = rest
            self._logger = logging.getLogger(__name__)

        async def _send_channel_message(
            self, channel_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            return await rest.create_channel_message(
                channel_id=channel_id,
                payload=payload,
            )

        def _register_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _clear_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _resolve_agent_state(self, binding: Any) -> tuple[str, Optional[str]]:
            _ = binding
            return "codex", None

        def _runtime_agent_for_binding(self, binding: Any) -> str:
            _ = binding
            return "codex"

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return started_execution

    async def _fake_complete(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        await asyncio.sleep(0.03)
        return SimpleNamespace(
            finalized={
                "status": "ok",
                "assistant_text": "done",
                "token_usage": None,
            }
        )

    monkeypatch.setattr(
        support.discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def _capture_exception(
        _loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        loop_errors.append(context)

    loop.set_exception_handler(_capture_exception)
    try:
        result = await support.discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            _Service(),
            workspace_root=tmp_path,
            prompt_text="hi",
            input_items=None,
            source_message_id=None,
            agent="codex",
            model_override=None,
            reasoning_effort=None,
            session_key="s1",
            orchestrator_channel_key="channel-1",
            managed_thread_surface_key=None,
            mode="pma",
            pma_enabled=True,
            execution_prompt="<user_message>\nhi\n</user_message>\n",
            public_execution_error="err",
            timeout_error="timeout",
            interrupted_error="interrupt",
            approval_mode="never",
            sandbox_policy="dangerFullAccess",
            max_actions=12,
            min_edit_interval_seconds=0.0,
            heartbeat_interval_seconds=0.01,
        )
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert result.final_message == "done"
    assert rest.edit_attempts >= 1
    assert loop_errors == []
