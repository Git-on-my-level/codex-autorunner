from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import anyio
import pytest

import codex_autorunner.agents.registry as agent_registry_module
import codex_autorunner.integrations.chat.managed_thread_turns as managed_thread_turns_module
import codex_autorunner.integrations.discord.managed_thread_routing as discord_managed_thread_routing_module
import codex_autorunner.integrations.discord.message_turns as discord_message_turns_module
import codex_autorunner.integrations.discord.progress_leases as discord_progress_leases_module
import codex_autorunner.integrations.discord.service as discord_service_module
from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.context_awareness import (
    CAR_AWARENESS_BLOCK,
    PROMPT_WRITING_HINT,
)
from codex_autorunner.core.filebox import (
    inbox_dir,
    outbox_dir,
    outbox_pending_dir,
    outbox_sent_dir,
)
from codex_autorunner.core.managed_thread_identity import (
    FILE_CHAT_OPENCODE_PREFIX,
    FILE_CHAT_PREFIX,
    PMA_KEY,
    normalize_feature_key,
    pma_base_key,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
    RUN_EVENT_DELTA_TYPE_USER_MESSAGE,
    Completed,
    Failed,
    OutputDelta,
    TokenUsage,
)
from codex_autorunner.core.sse import format_sse
from codex_autorunner.core.utils import canonicalize_path
from codex_autorunner.integrations.app_server.client import (
    CodexAppServerDisconnected,
)
from codex_autorunner.integrations.chat.collaboration_policy import (
    build_discord_collaboration_policy,
)
from codex_autorunner.integrations.chat.compaction import build_compact_seed_prompt
from codex_autorunner.integrations.chat.dispatcher import build_dispatch_context
from codex_autorunner.integrations.chat.managed_thread_surface_kernel import (
    build_managed_thread_terminal_delivery_hooks,
)
from codex_autorunner.integrations.chat.managed_thread_turns import (
    ManagedThreadExecutionHooks,
    ManagedThreadQueueWorkerHooks,
)
from codex_autorunner.integrations.discord.managed_thread_routing import (
    _build_discord_runner_hooks,
)
from codex_autorunner.integrations.discord.service import (
    DISCORD_QUEUED_PLACEHOLDER_TEXT as QUEUED_PLACEHOLDER_TEXT,
)
from codex_autorunner.integrations.discord.service import (
    DiscordBotService,
    DiscordMessageTurnResult,
)
from codex_autorunner.integrations.discord.state import DiscordStateStore
from tests.support.discord_turn_fakes import (
    _BaseDiscordFakeHarness,
    _build_discord_service,
    _config,
    _DeleteFailingProgressRest,
    _EditFailingProgressRest,
    _FailingChannelRest,
    _FailingProgressRest,
    _FakeCompactHubClient,
    _FakeGateway,
    _FakeOutboxManager,
    _FakeRest,
    _FakeVoiceService,
    _latest_interaction_completion_payload,
    _message_create,
    _patch_harness,
    _patch_streaming_harness,
    _pma_interaction,
)

pytestmark = pytest.mark.slow


def _discord_test_queue_worker_hooks(
    service: Any,
    *,
    channel_id: str,
    managed_thread_id: str,
    public_execution_error: str,
) -> ManagedThreadQueueWorkerHooks:
    """Match production queue-worker hooks for Discord queue-worker tests."""
    workspace_root = canonicalize_path(
        Path(getattr(getattr(service, "_config", None), "root", Path(".")))
    )
    runner_hooks = _build_discord_runner_hooks(
        service,
        channel_id=channel_id,
        managed_thread_id=managed_thread_id,
        workspace_root=workspace_root,
        public_execution_error=public_execution_error,
    )
    queue_hooks = runner_hooks.queue_worker_hooks()

    async def _send_success(record: Any, _context: Any) -> None:
        assistant_text = (
            managed_thread_turns_module.render_managed_thread_delivery_record_text(
                record
            )
        )
        formatted = (
            discord_message_turns_module.format_discord_message(assistant_text)
            if assistant_text
            else "(No response text returned.)"
        )
        chunks = discord_message_turns_module.chunk_discord_message(
            formatted,
            max_len=discord_message_turns_module.DISCORD_MAX_MESSAGE_LENGTH,
            with_numbering=False,
        )
        if not chunks:
            chunks = [formatted]
        base_record_id = (
            f"discord-queued:{record.managed_thread_id}:{record.managed_turn_id}"
        )
        for chunk_index, chunk in enumerate(chunks, start=1):
            record_id = (
                f"{base_record_id}:chunk:{chunk_index}"
                if len(chunks) > 1
                else base_record_id
            )
            await service._send_channel_message_safe(
                channel_id,
                {"content": chunk},
                record_id=record_id,
            )

    async def _send_failure(record: Any, _context: Any) -> None:
        await service._send_channel_message_safe(
            channel_id,
            {
                "content": (
                    f"Turn failed: {record.envelope.error_text or public_execution_error}"
                )
            },
            record_id=(
                f"discord-queued-error:{record.managed_thread_id}:{record.managed_turn_id}"
            ),
        )

    return ManagedThreadQueueWorkerHooks(
        durable_delivery=build_managed_thread_terminal_delivery_hooks(
            state_root=workspace_root,
            surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
                log_label="Discord",
                surface_kind="discord",
                surface_key=channel_id,
            ),
            adapter_key="discord",
            transport_target={"channel_id": channel_id},
            metadata={
                "managed_thread_id": managed_thread_id,
                "workspace_root": str(workspace_root),
            },
            send_success=_send_success,
            send_failure=_send_failure,
            cleanup=lambda record, context: asyncio.sleep(0),
        ),
        run_with_indicator=queue_hooks.run_with_indicator,
        execution_hooks=queue_hooks.execution_hooks,
    )


async def _collect_outbound_payloads(
    rest: _FakeRest, store: DiscordStateStore
) -> list[dict[str, Any]]:
    payloads = [msg["payload"] for msg in rest.channel_messages]
    payloads.extend(
        record.payload_json
        for record in await store.list_outbox()
        if isinstance(record.payload_json, dict)
    )
    return payloads


def test_sanitize_runtime_thread_result_error_preserves_sanitized_detail() -> None:
    assert (
        discord_message_turns_module._sanitize_runtime_thread_result_error(
            "backend exploded with private detail",
            public_error="Discord PMA turn failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
        )
        == "backend exploded with private detail"
    )


def test_sanitize_runtime_thread_result_error_maps_timeout_to_surface_timeout() -> None:
    from codex_autorunner.core.orchestration.runtime_threads import (
        RUNTIME_THREAD_TIMEOUT_ERROR,
    )

    assert (
        discord_message_turns_module._sanitize_runtime_thread_result_error(
            RUNTIME_THREAD_TIMEOUT_ERROR,
            public_error="Discord PMA turn failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
        )
        == "Discord PMA turn timed out"
    )


def test_sanitize_runtime_thread_result_error_maps_interrupted_to_surface_interrupted() -> (
    None
):
    from codex_autorunner.core.orchestration.runtime_threads import (
        RUNTIME_THREAD_INTERRUPTED_ERROR,
    )

    assert (
        discord_message_turns_module._sanitize_runtime_thread_result_error(
            RUNTIME_THREAD_INTERRUPTED_ERROR,
            public_error="Discord PMA turn failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
        )
        == "Discord PMA turn interrupted"
    )


def test_sanitize_runtime_thread_result_error_returns_public_error_for_empty_detail() -> (
    None
):
    assert (
        discord_message_turns_module._sanitize_runtime_thread_result_error(
            "",
            public_error="Discord PMA turn failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
        )
        == "Discord PMA turn failed"
    )


def test_resolve_discord_turn_policies_prefers_binding_preset() -> None:
    assert discord_message_turns_module._resolve_discord_turn_policies(
        {"approval_mode": "read-only"},
        default_approval_policy="never",
        default_sandbox_policy="dangerFullAccess",
    ) == ("on-request", "readOnly")


def test_resolve_discord_turn_policies_maps_safe_to_prompted_workspace_write() -> None:
    assert discord_message_turns_module._resolve_discord_turn_policies(
        {"approval_mode": "safe"},
        default_approval_policy="never",
        default_sandbox_policy="dangerFullAccess",
    ) == ("on-request", "workspaceWrite")


def test_resolve_discord_turn_policies_prefers_explicit_binding_values() -> None:
    assert discord_message_turns_module._resolve_discord_turn_policies(
        {
            "approval_mode": "full-access",
            "approval_policy": "on-failure",
            "sandbox_policy": "workspaceWrite",
        },
        default_approval_policy="never",
        default_sandbox_policy="dangerFullAccess",
    ) == ("on-failure", "workspaceWrite")


@pytest.mark.asyncio
async def test_queue_worker_start_clears_stale_queued_progress_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()

    class _Service:
        def __init__(self) -> None:
            self._config = _config(tmp_path)
            self._store = store
            self._rest = rest
            self._logger = logging.getLogger(__name__)
            self.approval_contexts: list[tuple[str, str]] = []

        def _register_discord_turn_approval_context(
            self,
            *,
            started_execution: Any,
            channel_id: str,
        ) -> None:
            execution = getattr(started_execution, "execution", None)
            execution_id = str(getattr(execution, "execution_id", "") or "").strip()
            self.approval_contexts.append((channel_id, execution_id))

        def _clear_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        async def _delete_channel_message_safe(
            self,
            channel_id: str,
            message_id: str,
            *,
            record_id: Optional[str] = None,
        ) -> bool:
            _ = record_id
            await rest.delete_channel_message(
                channel_id=channel_id,
                message_id=message_id,
            )
            return True

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.managed_thread_routing.build_managed_thread_surface_queue_execution_hooks",
        lambda **kwargs: ManagedThreadExecutionHooks(
            on_execution_started=lambda started_execution: _run_queue_execution_start(
                kwargs, started_execution
            ),
            on_execution_finished=kwargs["base_hooks"].on_execution_finished,
        ),
    )

    await store.upsert_turn_progress_lease(
        lease_id="lease-1",
        managed_thread_id="thread-1",
        execution_id="turn-1",
        channel_id="channel-1",
        message_id="queued-msg-1",
        source_message_id="source-1",
        state="active",
        progress_label="queued",
    )
    service = _Service()
    runner_hooks = _build_discord_runner_hooks(
        service,
        channel_id="channel-1",
        managed_thread_id="thread-1",
        workspace_root=tmp_path,
        public_execution_error="Discord PMA turn failed",
    )
    started_execution = SimpleNamespace(
        execution=SimpleNamespace(execution_id="turn-1")
    )

    try:
        assert runner_hooks.queue_execution_hooks is not None
        await runner_hooks.queue_execution_hooks.on_execution_started(started_execution)

        assert service.approval_contexts == [("channel-1", "turn-1")]
        assert rest.deleted_channel_messages == [
            {"channel_id": "channel-1", "message_id": "queued-msg-1"}
        ]
        assert (
            await store.list_turn_progress_leases(
                managed_thread_id="thread-1",
                execution_id="turn-1",
            )
            == []
        )
    finally:
        await store.close()


async def _run_queue_execution_start(
    kwargs: dict[str, Any],
    started_execution: Any,
) -> None:
    base_hooks = kwargs["base_hooks"]
    on_execution_started = getattr(base_hooks, "on_execution_started", None)
    if on_execution_started is not None:
        result = on_execution_started(started_execution)
        if asyncio.iscoroutine(result):
            await result
    on_progress_session_started = kwargs.get("on_progress_session_started")
    if on_progress_session_started is not None:
        result = on_progress_session_started(started_execution)
        if asyncio.iscoroutine(result):
            await result


@pytest.mark.asyncio
async def test_run_managed_thread_turn_for_message_uses_binding_policies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_run_discord_orchestrated_turn_for_message(
        *args: Any, **kwargs: Any
    ):
        _ = args
        captured.update(kwargs)
        return DiscordMessageTurnResult(final_message="ok")

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {"approval_mode": "safe"}

    service = SimpleNamespace(
        _config=_config(tmp_path),
        _store=_Store(),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_run_discord_orchestrated_turn_for_message",
        _fake_run_discord_orchestrated_turn_for_message,
    )

    result = await discord_message_turns_module.run_managed_thread_turn_for_message(
        service,
        workspace_root=tmp_path,
        prompt_text="hello",
        input_items=None,
        agent="codex",
        model_override=None,
        reasoning_effort=None,
        session_key="session-1",
        orchestrator_channel_key="channel-1",
    )

    assert result.final_message == "ok"
    assert captured["approval_mode"] == "on-request"
    assert captured["sandbox_policy"] == "workspaceWrite"


async def _run_interrupt_send_turn_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    rest: _FakeRest | None = None,
    finalize_result: dict[str, Any] | None = None,
    send_channel_message_raises: bool = False,
    spawn_task: bool = True,
    progress_source_message_id: str = "m-2",
    prompt_text: str = "first prompt",
    source_message_id: str = "m-1",
    session_key: str = "session-1",
    pre_run: Any | None = None,
) -> tuple[Any, DiscordMessageTurnResult, _FakeRest]:
    if rest is None:
        rest = _FakeRest()
    if finalize_result is None:
        finalize_result = {
            "status": "interrupted",
            "error": "Discord PMA turn interrupted",
        }
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
            self._config = _config(tmp_path)
            self._store = _Store()
            self._rest = rest
            self._logger = logging.getLogger(__name__)
            if spawn_task:
                self._spawn_task = asyncio.create_task

        async def _send_channel_message(
            self, channel_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            if send_channel_message_raises:
                raise RuntimeError("progress send failed")
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
        return finalize_result

    async def _fake_complete(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return SimpleNamespace(finalized=await _fake_finalize())

    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    service = _Service()
    discord_progress_leases_module.request_discord_turn_progress_reuse(
        service,
        thread_target_id="thread-1",
        source_message_id=progress_source_message_id,
        acknowledgement="Message received. Switching to it now...",
    )
    if pre_run is not None:
        await pre_run(service)
    result = (
        await discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            service,
            workspace_root=tmp_path,
            prompt_text=prompt_text,
            source_message_id=source_message_id,
            agent="codex",
            model_override=None,
            reasoning_effort=None,
            session_key=session_key,
            orchestrator_channel_key="channel-1",
            managed_thread_surface_key=None,
            mode="pma",
            pma_enabled=True,
            execution_prompt=f"<user_message>\n{prompt_text}\n</user_message>\n",
            public_execution_error="Discord PMA turn failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
            approval_mode="never",
            sandbox_policy="dangerFullAccess",
            max_actions=12,
            min_edit_interval_seconds=1.0,
            heartbeat_interval_seconds=2.0,
        )
    )
    return service, result, rest


@pytest.mark.asyncio
async def test_orchestrated_turn_interrupt_send_hands_off_progress_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, result, rest = await _run_interrupt_send_turn_harness(
        monkeypatch,
        tmp_path,
        spawn_task=True,
    )

    assert result.send_final_message is False
    assert result.preview_message_id is None
    assert len(rest.channel_messages) == 1
    assert rest.edited_channel_messages[-1]["message_id"] == "msg-1"
    assert (
        rest.edited_channel_messages[-1]["payload"]["content"]
        == "Message received. Switching to it now..."
    )
    assert (
        discord_message_turns_module._claim_discord_reusable_progress_message(
            service,
            thread_target_id="thread-1",
            source_message_id="m-2",
        )
        == "msg-1"
    )


@pytest.mark.asyncio
async def test_orchestrated_turn_interrupt_send_reuses_existing_progress_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _stash(service: Any) -> None:
        discord_message_turns_module._stash_discord_reusable_progress_message(
            service,
            thread_target_id="thread-1",
            source_message_id="m-2",
            channel_id="channel-1",
            message_id="preview-1",
        )

    service, result, rest = await _run_interrupt_send_turn_harness(
        monkeypatch,
        tmp_path,
        spawn_task=True,
        finalize_result={
            "status": "ok",
            "assistant_text": "updated reply",
            "token_usage": None,
        },
        prompt_text="next prompt",
        source_message_id="m-2",
        session_key="session-2",
        pre_run=_stash,
    )

    assert result.preview_message_id == "preview-1"
    assert result.final_message == "updated reply"
    assert rest.channel_messages == []
    assert rest.edited_channel_messages
    assert rest.edited_channel_messages[0]["message_id"] == "preview-1"


@pytest.mark.asyncio
async def test_orchestrated_turn_interrupt_send_acknowledges_when_progress_message_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, result, _ = await _run_interrupt_send_turn_harness(
        monkeypatch,
        tmp_path,
        send_channel_message_raises=True,
    )

    assert result.send_final_message is True
    assert result.final_message == "Message received. Switching to it now..."
    assert service._discord_turn_progress_reuse_requests == {}
    assert service._discord_reusable_progress_messages == {}


@pytest.mark.asyncio
async def test_orchestrated_turn_interrupt_send_falls_back_when_progress_ack_edit_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _EditFailingRest(_FakeRest):
        async def edit_channel_message(
            self, *, channel_id: str, message_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            _ = channel_id, message_id, payload
            raise RuntimeError("edit failed")

    service, result, rest = await _run_interrupt_send_turn_harness(
        monkeypatch,
        tmp_path,
        rest=_EditFailingRest(),
    )

    assert result.send_final_message is True
    assert result.final_message == "Message received. Switching to it now..."
    assert len(rest.channel_messages) == 1
    assert service._discord_turn_progress_reuse_requests == {}
    assert service._discord_reusable_progress_messages == {}


@pytest.mark.asyncio
async def test_orchestrated_turn_placeholder_sent_before_runtime_begin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    call_order: list[str] = []
    rest = _FakeRest()
    thread = SimpleNamespace(thread_target_id="thread-1")
    started_execution = SimpleNamespace(
        execution=SimpleNamespace(status="running"),
        thread=thread,
    )

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        call_order.append("begin")
        return started_execution

    async def _fake_finalize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _ = args, kwargs
        call_order.append("finalize")
        return {"status": "ok", "assistant_text": "ok", "token_usage": None}

    async def _fake_complete(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return SimpleNamespace(finalized=await _fake_finalize())

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    class _Service:
        def __init__(self) -> None:
            self._config = _config(tmp_path)
            self._store = _Store()
            self._rest = rest
            self._logger = logging.getLogger(__name__)

        async def _send_channel_message(
            self, channel_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            call_order.append("placeholder")
            return await rest.create_channel_message(
                channel_id=channel_id,
                payload=payload,
            )

        async def _delete_channel_message_safe(
            self,
            channel_id: str,
            message_id: str,
            *,
            record_id: Optional[str] = None,
        ) -> None:
            _ = record_id
            await rest.delete_channel_message(
                channel_id=channel_id,
                message_id=message_id,
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

    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    service = _Service()
    await discord_message_turns_module._run_discord_orchestrated_turn_for_message(
        service,
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
        min_edit_interval_seconds=1.0,
        heartbeat_interval_seconds=2.0,
    )

    assert call_order == ["placeholder", "begin", "finalize"]


@pytest.mark.asyncio
async def test_orchestrated_turn_begin_failure_deletes_progress_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rest = _FakeRest()
    thread = SimpleNamespace(thread_target_id="thread-1")

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        raise RuntimeError("simulated begin failure")

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    class _Service:
        def __init__(self) -> None:
            self._config = _config(tmp_path)
            self._store = _Store()
            self._rest = rest

        async def _send_channel_message(
            self, channel_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            return await rest.create_channel_message(
                channel_id=channel_id,
                payload=payload,
            )

        async def _delete_channel_message_safe(
            self,
            channel_id: str,
            message_id: str,
            *,
            record_id: Optional[str] = None,
        ) -> None:
            _ = record_id
            await rest.delete_channel_message(
                channel_id=channel_id,
                message_id=message_id,
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

    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "begin_runtime_thread_execution",
        _fake_begin,
    )

    service = _Service()
    with pytest.raises(RuntimeError, match="simulated begin failure"):
        await discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            service,
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
            min_edit_interval_seconds=1.0,
            heartbeat_interval_seconds=2.0,
        )

    assert len(rest.channel_messages) == 1
    assert rest.deleted_channel_messages
    assert rest.deleted_channel_messages[-1]["message_id"] == "msg-1"


@pytest.mark.asyncio
async def test_orchestrated_turn_submission_timeout_deletes_progress_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rest = _FakeRest()
    thread = SimpleNamespace(thread_target_id="thread-1")
    submit_started = asyncio.Event()

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    class _Service:
        def __init__(self) -> None:
            self._config = _config(tmp_path)
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

        async def _delete_channel_message_safe(
            self,
            channel_id: str,
            message_id: str,
            *,
            record_id: Optional[str] = None,
        ) -> None:
            _ = record_id
            await rest.delete_channel_message(
                channel_id=channel_id,
                message_id=message_id,
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

    async def _hanging_submit(self, *args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        submit_started.set()
        await asyncio.Future()

    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "DISCORD_PMA_SUBMISSION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        managed_thread_turns_module.ManagedThreadTurnCoordinator,
        "submit_execution",
        _hanging_submit,
    )

    service = _Service()
    with pytest.raises(
        RuntimeError,
        match="Turn failed to start in time. Please retry.",
    ):
        await discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            service,
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
            min_edit_interval_seconds=1.0,
            heartbeat_interval_seconds=2.0,
        )

    assert submit_started.is_set()
    assert len(rest.channel_messages) == 1
    assert rest.deleted_channel_messages == []
    assert rest.edited_channel_messages
    assert rest.edited_channel_messages[-1]["message_id"] == "msg-1"
    assert "failed" in rest.edited_channel_messages[-1]["payload"]["content"].lower()
    assert (
        "Turn failed to start in time. Please retry."
        in rest.edited_channel_messages[-1]["payload"]["content"]
    )
    assert rest.edited_channel_messages[-1]["payload"]["components"] == []


@pytest.mark.asyncio
async def test_orchestrated_turn_queued_updates_placeholder_skips_finalize(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    finalized_called = False
    rest = _FakeRest()
    thread = SimpleNamespace(thread_target_id="thread-1")
    started_execution = SimpleNamespace(
        execution=SimpleNamespace(status="queued", execution_id="turn-2"),
        thread=thread,
    )
    queue_worker_calls = 0

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return started_execution

    async def _fake_finalize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal finalized_called
        finalized_called = True
        _ = args, kwargs
        return {"status": "ok", "assistant_text": "nope", "token_usage": None}

    def _fake_ensure_queue(*args: Any, **kwargs: Any) -> None:
        nonlocal queue_worker_calls
        queue_worker_calls += 1

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    class _Service:
        def __init__(self) -> None:
            self._config = _config(tmp_path)
            self._store = _Store()
            self._rest = rest
            self._logger = logging.getLogger(__name__)
            self._spawn_task = asyncio.create_task
            self._logger = logging.getLogger(__name__)

        async def _send_channel_message(
            self, channel_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            return await rest.create_channel_message(
                channel_id=channel_id,
                payload=payload,
            )

        async def _delete_channel_message_safe(
            self,
            channel_id: str,
            message_id: str,
            *,
            record_id: Optional[str] = None,
        ) -> None:
            _ = record_id
            await rest.delete_channel_message(
                channel_id=channel_id,
                message_id=message_id,
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

    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "complete_managed_thread_execution",
        _fake_finalize,
    )
    monkeypatch.setattr(
        managed_thread_turns_module.ManagedThreadTurnCoordinator,
        "ensure_queue_worker",
        lambda *args, **kwargs: _fake_ensure_queue(),
    )

    service = _Service()
    result = (
        await discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            service,
            workspace_root=tmp_path,
            prompt_text="hi",
            input_items=None,
            source_message_id="m-2",
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
            min_edit_interval_seconds=1.0,
            heartbeat_interval_seconds=2.0,
        )
    )

    assert not finalized_called
    assert queue_worker_calls == 1
    assert "Queued" in (result.final_message or "")
    assert result.send_final_message is False
    assert rest.edited_channel_messages
    assert "queued" in rest.edited_channel_messages[-1]["payload"]["content"].lower()
    buttons = rest.edited_channel_messages[-1]["payload"]["components"][0]["components"]
    assert [button["label"] for button in buttons] == [
        "Cancel",
        "Interrupt + Send",
    ]
    assert buttons[1]["custom_id"].startswith("qis:")


@pytest.mark.anyio
async def test_message_create_personal_bound_channel_runs_without_collaboration_policy(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    captured: list[dict[str, Any]] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        input_items: Optional[list[dict[str, Any]]] = None,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        suppress_managed_thread_delivery: bool = False,
        chat_ux_snapshot: Any = None,
    ) -> DiscordMessageTurnResult:
        captured.append(
            {
                "workspace_root": workspace_root,
                "prompt_text": prompt_text,
                "agent": agent,
                "session_key": session_key,
                "orchestrator_channel_key": orchestrator_channel_key,
            }
        )
        return DiscordMessageTurnResult(final_message="Done from fake turn")

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._run_agent_turn_for_message = _fake_run_turn.__get__(  # type: ignore[assignment]
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured
        assert captured[0]["workspace_root"] == workspace.resolve()
        assert "ship it" in captured[0]["prompt_text"]
        assert CAR_AWARENESS_BLOCK not in captured[0]["prompt_text"]
        assert captured[0]["agent"] == "codex"
        assert captured[0]["session_key"].startswith(
            f"{FILE_CHAT_PREFIX}discord.channel-1."
        )
        assert (
            normalize_feature_key(captured[0]["session_key"])
            == captured[0]["session_key"]
        )
        assert captured[0]["orchestrator_channel_key"] == "channel-1"
        assert any(
            "Done from fake turn" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_session_compact_finishes_interaction_when_finalize_fails(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [],
        config_kwargs=dict(max_message_length=120),
    )

    orchestration_service = service._discord_thread_service()
    current_thread = orchestration_service.create_thread_target(
        "codex",
        workspace.resolve(),
        display_name="discord:channel-1",
    )
    service._attach_discord_thread_binding(
        channel_id="channel-1",
        thread_target_id=current_thread.thread_target_id,
        agent="codex",
        repo_id=None,
        pma_enabled=False,
    )

    async def _fake_run_turn(
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        suppress_managed_thread_delivery: bool = False,
        chat_ux_snapshot: Any = None,
    ) -> DiscordMessageTurnResult:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        assert suppress_managed_thread_delivery is True
        return DiscordMessageTurnResult(
            final_message="summary text",
            preview_message_id=None,
        )

    async def _failing_flush(*, workspace_root: Path, channel_id: str) -> None:
        _ = workspace_root, channel_id
        raise RuntimeError("outbox boom")

    service._run_agent_turn_for_message = _fake_run_turn  # type: ignore[assignment]
    service._flush_outbox_files = _failing_flush  # type: ignore[assignment]

    try:
        await service._handle_car_compact(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
        )
        payload = _latest_interaction_completion_payload(rest)
        assert payload["content"] == (
            "Compaction summary posted, but finalizing the fresh thread failed."
        )
        assert rest.channel_messages
        assert (
            rest.channel_messages[-1]["payload"]["content"]
            == "Compaction summary posted, but finalizing the fresh thread failed."
        )
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert "summary text" in binding["pending_compact_seed"]
        expected_compact_key = service._build_message_session_key(
            channel_id="channel-1",
            workspace_root=workspace.resolve(),
            pma_enabled=False,
            agent="codex",
        )
        assert binding["pending_compact_session_key"] == expected_compact_key
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_session_compact_restores_previous_thread_when_seed_save_fails(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [],
        config_kwargs=dict(max_message_length=120),
    )

    orchestration_service = service._discord_thread_service()
    current_thread = orchestration_service.create_thread_target(
        "codex",
        workspace.resolve(),
        display_name="discord:channel-1",
    )
    service._attach_discord_thread_binding(
        channel_id="channel-1",
        thread_target_id=current_thread.thread_target_id,
        agent="codex",
        repo_id=None,
        pma_enabled=False,
    )

    async def _fake_run_turn(
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        **_kwargs: Any,
    ) -> DiscordMessageTurnResult:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        return DiscordMessageTurnResult(
            final_message="summary text",
            preview_message_id=None,
        )

    async def _failing_set_pending_compact_seed(
        *, channel_id: str, seed_text: str, session_key: str
    ) -> None:
        _ = channel_id, seed_text, session_key
        raise RuntimeError("sqlite locked")

    service._run_agent_turn_for_message = _fake_run_turn  # type: ignore[assignment]
    store.set_pending_compact_seed = _failing_set_pending_compact_seed  # type: ignore[assignment]

    try:
        await service._handle_car_compact(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
        )
        payload = _latest_interaction_completion_payload(rest)
        assert payload["content"] == (
            "Compaction summary generated, but saving the compacted context failed. Restored the previous thread; please retry."
        )
        assert rest.channel_messages
        assert (
            rest.channel_messages[-1]["payload"]["content"]
            == "Compaction summary generated, but saving the compacted context failed. Restored the previous thread; please retry."
        )

        restored_thread = orchestration_service.get_thread_target(
            current_thread.thread_target_id
        )
        assert restored_thread is not None
        assert restored_thread.lifecycle_status == "active"

        active_threads = orchestration_service.list_thread_targets(
            lifecycle_status="active"
        )
        assert [thread.thread_target_id for thread in active_threads] == [
            current_thread.thread_target_id
        ]

        archived_threads = orchestration_service.list_thread_targets(
            lifecycle_status="archived"
        )
        assert len(archived_threads) == 1
        assert archived_threads[0].thread_target_id != current_thread.thread_target_id

        binding = orchestration_service.get_binding(
            surface_kind="discord",
            surface_key="channel-1",
        )
        assert binding is not None
        assert binding.thread_target_id == current_thread.thread_target_id

        state_binding = await store.get_binding(channel_id="channel-1")
        assert state_binding is not None
        assert state_binding["pending_compact_seed"] is None
        assert state_binding["pending_compact_session_key"] is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_session_compact_keeps_previous_thread_when_summary_is_blank(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [],
        config_kwargs=dict(max_message_length=120),
    )

    orchestration_service = service._discord_thread_service()
    current_thread = orchestration_service.create_thread_target(
        "codex",
        workspace.resolve(),
        display_name="discord:channel-1",
    )
    service._attach_discord_thread_binding(
        channel_id="channel-1",
        thread_target_id=current_thread.thread_target_id,
        agent="codex",
        repo_id=None,
        pma_enabled=False,
    )

    async def _fake_run_turn(
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        **_kwargs: Any,
    ) -> DiscordMessageTurnResult:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        return DiscordMessageTurnResult(
            final_message="",
            preview_message_id="preview-1",
        )

    service._run_agent_turn_for_message = _fake_run_turn  # type: ignore[assignment]

    try:
        await service._handle_car_compact(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
        )
        payload = _latest_interaction_completion_payload(rest)
        assert payload["content"] == (
            "Compaction returned an empty summary. Kept the current session active; please retry."
        )
        assert rest.channel_messages
        assert (
            rest.channel_messages[-1]["payload"]["content"]
            == "Compaction returned an empty summary. Kept the current session active; please retry."
        )
        assert rest.deleted_channel_messages == [
            {
                "channel_id": "channel-1",
                "message_id": "preview-1",
            }
        ]

        binding = orchestration_service.get_binding(
            surface_kind="discord",
            surface_key="channel-1",
        )
        assert binding is not None
        assert binding.thread_target_id == current_thread.thread_target_id

        restored_thread = orchestration_service.get_thread_target(
            current_thread.thread_target_id
        )
        assert restored_thread is not None
        assert restored_thread.lifecycle_status == "active"

        active_threads = orchestration_service.list_thread_targets(
            lifecycle_status="active"
        )
        assert [thread.thread_target_id for thread in active_threads] == [
            current_thread.thread_target_id
        ]

        state_binding = await store.get_binding(channel_id="channel-1")
        assert state_binding is not None
        assert state_binding["pending_compact_seed"] is None
        assert state_binding["pending_compact_session_key"] is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_session_compact_surfaces_empty_summary_without_transcript_fallback(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [],
        config_kwargs=dict(max_message_length=120),
    )

    orchestration_service = service._discord_thread_service()
    current_thread = orchestration_service.create_thread_target(
        "codex",
        workspace.resolve(),
        display_name="discord:channel-1",
    )
    service._attach_discord_thread_binding(
        channel_id="channel-1",
        thread_target_id=current_thread.thread_target_id,
        agent="codex",
        repo_id=None,
        pma_enabled=False,
    )

    async def _fake_run_turn(
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        **_kwargs: Any,
    ) -> DiscordMessageTurnResult:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        return DiscordMessageTurnResult(
            final_message="",
            preview_message_id=None,
        )

    service._hub_client = _FakeCompactHubClient(
        orchestration_service,
        transcript_entries=[
            {"preview": "User: earlier question\nAssistant: earlier answer"},
            {"preview": "User: latest question\nAssistant: latest answer"},
        ],
    )
    service._run_agent_turn_for_message = _fake_run_turn  # type: ignore[assignment]

    try:
        await service._handle_car_compact(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
        )
        payload = _latest_interaction_completion_payload(rest)
        expected_msg = "Compaction returned an empty summary. Kept the current session active; please retry."
        assert payload["content"] == expected_msg
        assert rest.channel_messages
        assert rest.channel_messages[-1]["payload"]["content"] == expected_msg

        binding = orchestration_service.get_binding(
            surface_kind="discord",
            surface_key="channel-1",
        )
        assert binding is not None
        assert binding.thread_target_id == current_thread.thread_target_id

        restored_thread = orchestration_service.get_thread_target(
            current_thread.thread_target_id
        )
        assert restored_thread is not None
        assert restored_thread.lifecycle_status == "active"

        state_binding = await store.get_binding(channel_id="channel-1")
        assert state_binding is not None
        assert state_binding["pending_compact_seed"] is None
        assert state_binding["pending_compact_session_key"] is None

        assert not service._hub_client.transcript_requests
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_event_submits_through_surface_orchestration_ingress(
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
    captured: dict[str, object] = {}
    turn_started = asyncio.Event()
    release_turn = asyncio.Event()

    async def _fake_run_turn(**kwargs: Any) -> DiscordMessageTurnResult:
        captured["session_key"] = kwargs["session_key"]
        turn_started.set()
        await release_turn.wait()
        return DiscordMessageTurnResult(
            final_message="handled by ingress",
            preview_message_id="msg-1",
        )

    class _FakeIngress:
        async def submit_message(
            self,
            request,
            *,
            resolve_paused_flow_target,
            submit_flow_reply,
            submit_thread_message,
        ):
            _ = resolve_paused_flow_target, submit_flow_reply
            captured["surface_kind"] = request.surface_kind
            captured["prompt_text"] = request.prompt_text
            thread_result = await submit_thread_message(request)
            return SimpleNamespace(route="thread", thread_result=thread_result)

    monkeypatch.setattr(
        discord_message_turns_module,
        "build_surface_orchestration_ingress",
        lambda **_: _FakeIngress(),
    )
    service._run_agent_turn_for_message = _fake_run_turn  # type: ignore[assignment]

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
        await asyncio.wait_for(turn_started.wait(), timeout=1)
        assert captured["surface_kind"] == "discord"
        assert captured["prompt_text"] == "route via ingress"
        assert isinstance(captured["session_key"], str)
        assert any(
            message["payload"]["content"] == "Received. Preparing turn..."
            for message in rest.channel_messages
        )
        release_turn.set()
        await asyncio.gather(*list(service._background_tasks), return_exceptions=True)
        assert any(
            message["payload"]["content"] == "handled by ingress"
            for message in rest.channel_messages
        )
    finally:
        release_turn.set()
        await store.close()


async def test_message_create_after_compact_uses_pending_seed_and_clears_it(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("please continue"))],
    )

    session_key = service._build_message_session_key(
        channel_id="channel-1",
        workspace_root=workspace.resolve(),
        pma_enabled=False,
        agent="codex",
    )
    await store.set_pending_compact_seed(
        channel_id="channel-1",
        seed_text=build_compact_seed_prompt("previous summary"),
        session_key=session_key,
    )

    captured: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        suppress_managed_thread_delivery: bool = False,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured.append(prompt_text)
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured
        assert "Context from previous conversation:" in captured[0]
        assert "previous summary" in captured[0]
        assert "please continue" in captured[0]
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding["pending_compact_seed"] is None
        assert binding["pending_compact_session_key"] is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_flushes_pending_outbox_files_after_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pending_dir = outbox_pending_dir(workspace.resolve())
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending_file = pending_dir / "result.txt"
    pending_file.write_text("artifact payload\n", encoding="utf-8")

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert rest.attachment_messages
        assert any(
            item["filename"] == "result.txt" for item in rest.attachment_messages
        )
        sent_files = list(outbox_sent_dir(workspace.resolve()).glob("result*.txt"))
        assert sent_files
        assert sent_files[0].read_text(encoding="utf-8") == "artifact payload\n"
        assert not pending_file.exists()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_flushes_root_outbox_files_after_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root_outbox = outbox_dir(workspace.resolve())
    root_outbox.mkdir(parents=True, exist_ok=True)
    root_file = root_outbox / "root-result.txt"
    root_file.write_text("root artifact payload\n", encoding="utf-8")

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert rest.attachment_messages
        assert any(
            item["filename"] == "root-result.txt" for item in rest.attachment_messages
        )
        sent_files = list(outbox_sent_dir(workspace.resolve()).glob("root-result*.txt"))
        assert sent_files
        assert sent_files[0].read_text(encoding="utf-8") == "root artifact payload\n"
        assert not root_file.exists()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_flush_outbox_skips_symlink_outside_pending(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pending_dir = outbox_pending_dir(workspace.resolve())
    pending_dir.mkdir(parents=True, exist_ok=True)
    external_file = tmp_path / "secret.txt"
    external_file.write_text("do-not-leak\n", encoding="utf-8")
    symlink_path = pending_dir / "leak.txt"
    try:
        symlink_path.symlink_to(external_file)
    except OSError:
        pytest.skip("symlinks not supported in this test environment")

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert rest.attachment_messages == []
        assert symlink_path.exists()
        assert external_file.read_text(encoding="utf-8") == "do-not-leak\n"
        assert not (outbox_sent_dir(workspace.resolve()) / "leak.txt").exists()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_flush_outbox_preserves_root_file_when_pending_symlink_points_to_it(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root_outbox = outbox_dir(workspace.resolve())
    root_outbox.mkdir(parents=True, exist_ok=True)
    pending_dir = outbox_pending_dir(workspace.resolve())
    pending_dir.mkdir(parents=True, exist_ok=True)

    root_file = root_outbox / "result.txt"
    root_file.write_text("artifact payload\n", encoding="utf-8")
    symlink_path = pending_dir / "result-link.txt"
    try:
        symlink_path.symlink_to(root_file)
    except OSError:
        pytest.skip("symlinks not supported in this test environment")

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert rest.attachment_messages
        assert len(rest.attachment_messages) == 1
        assert rest.attachment_messages[0]["filename"] == "result.txt"
        sent_files = list(outbox_sent_dir(workspace.resolve()).glob("result*.txt"))
        assert sent_files
        assert sent_files[0].read_text(encoding="utf-8") == "artifact payload\n"
        assert not root_file.exists()
        assert os.path.lexists(symlink_path)
        assert symlink_path.is_symlink()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_non_pma_injects_prompt_context_hints(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway(
        [("MESSAGE_CREATE", _message_create("please write a prompt for triage"))]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        assert CAR_AWARENESS_BLOCK not in captured_prompts[0]
        assert PROMPT_WRITING_HINT in captured_prompts[0]
        assert "please write a prompt for triage" in captured_prompts[0]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_non_pma_prompt_hint_ignores_reply_context_prompt_text(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    "please review this",
                    extra_payload={
                        "message_reference": {
                            "message_id": "parent-1",
                            "channel_id": "channel-1",
                        },
                        "referenced_message": {
                            "content": "write a prompt for this later",
                            "author": {"id": "user-2", "username": "alice"},
                        },
                    },
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert "please review this" in prompt
        assert "Replying to message" in prompt
        assert "write a prompt for this later" in prompt
        assert PROMPT_WRITING_HINT not in prompt
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_non_pma_injects_filebox_hint_for_outbox_keyword(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("outbox me"))],
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert "outbox me" in prompt
        assert str(inbox_dir(workspace.resolve())) in prompt
        assert str(outbox_dir(workspace.resolve())) in prompt
        assert str(outbox_pending_dir(workspace.resolve())) in prompt
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_non_pma_injects_filebox_hint_for_inbox_keyword(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("check inbox"))],
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert "check inbox" in prompt
        assert str(inbox_dir(workspace.resolve())) in prompt
        assert str(outbox_dir(workspace.resolve())) in prompt
        assert str(outbox_pending_dir(workspace.resolve())) in prompt
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_non_pma_uses_raw_message_for_github_link_source(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    user_text = (
        "please write a prompt for triage https://github.com/example/repo/issues/123"
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create(user_text))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured_link_source: list[Optional[str]] = []
    captured_prompt: list[str] = []

    async def _fake_maybe_inject_github_context(
        self,
        prompt_text: str,
        workspace_root: Path,
        *,
        link_source_text: Optional[str] = None,
        allow_cross_repo: bool = False,
    ) -> tuple[str, bool]:
        _ = (workspace_root, allow_cross_repo)
        captured_prompt.append(prompt_text)
        captured_link_source.append(link_source_text)
        return prompt_text, False

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        input_items: Optional[list[dict[str, Any]]] = None,
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        return "Done from fake turn"

    service._maybe_inject_github_context = _fake_maybe_inject_github_context.__get__(
        service, DiscordBotService
    )
    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_link_source == [user_text]
        assert captured_prompt
        assert CAR_AWARENESS_BLOCK not in captured_prompt[0]
        assert PROMPT_WRITING_HINT in captured_prompt[0]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_attachment_only_downloads_to_inbox_and_runs_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/file-1"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"attachment-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-1",
                            "filename": "report.txt",
                            "content_type": "text/plain",
                            "size": 16,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        input_items: Optional[list[dict[str, Any]]] = None,
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done with attachment"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert "Inbound Discord attachments:" in prompt
        assert "Outbox (pending):" in prompt
        inbox = inbox_dir(workspace.resolve())
        saved_files = [path for path in inbox.iterdir() if path.is_file()]
        assert len(saved_files) == 1
        assert saved_files[0].read_bytes() == b"attachment-bytes"
        assert str(saved_files[0]) in prompt
        assert str(outbox_pending_dir(workspace.resolve())) in prompt
        assert len(rest.download_requests) == 1
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_attachment_and_text_keeps_text_and_adds_file_context(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/file-2"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"image-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="Please analyze the screenshot.",
                    attachments=[
                        {
                            "id": "att-2",
                            "filename": "screen.png",
                            "content_type": "image/png",
                            "size": 11,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured_prompts: list[str] = []
    captured_input_items: list[Optional[list[dict[str, Any]]]] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        input_items: Optional[list[dict[str, Any]]] = None,
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        captured_input_items.append(input_items)
        return "Done with text+attachment"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert "Please analyze the screenshot." in prompt
        assert CAR_AWARENESS_BLOCK in prompt
        assert "Inbound Discord attachments:" in prompt
        assert "screen.png" in prompt
        assert captured_input_items and isinstance(captured_input_items[0], list)
        items = captured_input_items[0] or []
        assert items and items[0].get("type") == "text"
        assert any(item.get("type") == "localImage" for item in items[1:])
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_injects_car_context_for_car_trigger(
    tmp_path: Path,
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
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create("resume ticket flow in .codex-autorunner/"),
            )
        ]
    )
    captured_prompts: list[str] = []

    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_run_turn(
        self: DiscordBotService,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        assert CAR_AWARENESS_BLOCK in captured_prompts[0]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_audio_attachment_injects_transcript_context(
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
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/audio-1"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"voice-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-audio-1",
                            "filename": "voice-note.ogg",
                            "content_type": "audio/ogg",
                            "size": 11,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    fake_voice = _FakeVoiceService("Do we have whisper support?")
    monkeypatch.setattr(
        service,
        "_voice_service_for_workspace",
        lambda _workspace: (fake_voice, SimpleNamespace(provider="local_whisper")),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done with audio"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert "Inbound Discord attachments:" in prompt
        assert "Transcript: Do we have whisper support?" in prompt
        assert "Outbox (pending):" not in prompt
        assert fake_voice.calls
        assert fake_voice.calls[0]["audio_bytes"] == b"voice-bytes"
        assert fake_voice.calls[0]["client"] == "discord"
        assert fake_voice.calls[0]["filename"] == "voice-note.ogg"
        transcript_messages = [
            msg
            for msg in rest.channel_messages
            if msg["payload"].get("content", "") == "User:\nDo we have whisper support?"
        ]
        assert transcript_messages
        assert transcript_messages[0]["payload"].get("allowed_mentions") == {
            "parse": []
        }
        contents = [msg["payload"].get("content", "") for msg in rest.channel_messages]
        assert "Done with audio" in contents
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_audio_attachment_does_not_transcribe_when_voice_disabled(
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
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/audio-2"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"voice-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-audio-2",
                            "filename": "voice-note.ogg",
                            "content_type": "audio/ogg",
                            "size": 11,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, media_voice=False),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    fake_voice = _FakeVoiceService("ignored")
    monkeypatch.setattr(
        service,
        "_voice_service_for_workspace",
        lambda _workspace: (fake_voice, SimpleNamespace(provider="local_whisper")),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        assert "Transcript:" not in captured_prompts[0]
        assert "Outbox (pending):" not in captured_prompts[0]
        assert fake_voice.calls == []
        assert all(
            not msg["payload"].get("content", "").startswith("User:\n")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_audio_attachment_without_content_type_still_transcribes(
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
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/audio-missing-content-type"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"voice-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-audio-3",
                            "filename": "voice-note.ogg",
                            "size": 11,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    fake_voice = _FakeVoiceService("transcribed despite missing mime")
    monkeypatch.setattr(
        service,
        "_voice_service_for_workspace",
        lambda _workspace: (fake_voice, SimpleNamespace(provider="local_whisper")),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert "Transcript: transcribed despite missing mime" in prompt
        assert "Outbox (pending):" not in prompt
        assert fake_voice.calls
        assert fake_voice.calls[0]["audio_bytes"] == b"voice-bytes"
        assert fake_voice.calls[0]["filename"] == "voice-note.ogg"
        assert fake_voice.calls[0]["content_type"] == "audio/ogg"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_audio_attachment_with_generic_content_type_transcribes(
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
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/audio-generic-content-type"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"voice-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-audio-4",
                            "filename": "voice-message",
                            "content_type": "application/octet-stream",
                            "duration_secs": 8,
                            "size": 11,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    fake_voice = _FakeVoiceService("transcribed generic mime")
    monkeypatch.setattr(
        service,
        "_voice_service_for_workspace",
        lambda _workspace: (fake_voice, SimpleNamespace(provider="local_whisper")),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert "Transcript: transcribed generic mime" in prompt
        assert "Outbox (pending):" not in prompt
        assert fake_voice.calls
        assert fake_voice.calls[0]["audio_bytes"] == b"voice-bytes"
        assert fake_voice.calls[0]["content_type"] == "audio/ogg"
        assert str(fake_voice.calls[0]["filename"]).endswith(".ogg")
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_mixed_audio_and_file_attachment_keeps_outbox_hint(
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
        repo_id=None,
    )
    audio_url = "https://cdn.discordapp.com/attachments/audio-mixed"
    file_url = "https://cdn.discordapp.com/attachments/file-mixed"
    rest = _FakeRest()
    rest.attachment_data_by_url[audio_url] = b"voice-bytes"
    rest.attachment_data_by_url[file_url] = b"report-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-audio-mixed",
                            "filename": "voice-note.ogg",
                            "content_type": "audio/ogg",
                            "size": 11,
                            "url": audio_url,
                        },
                        {
                            "id": "att-file-mixed",
                            "filename": "report.txt",
                            "content_type": "text/plain",
                            "size": 12,
                            "url": file_url,
                        },
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    fake_voice = _FakeVoiceService("mixed transcript")
    monkeypatch.setattr(
        service,
        "_voice_service_for_workspace",
        lambda _workspace: (fake_voice, SimpleNamespace(provider="local_whisper")),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert "Transcript: mixed transcript" in prompt
        assert "Outbox (pending):" in prompt
        assert "voice-note.ogg" in prompt
        assert "report.txt" in prompt
        assert fake_voice.calls
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_video_attachment_does_not_transcribe(
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
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/video-1"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"video-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-video-1",
                            "filename": "clip.mp4",
                            "content_type": "video/mp4",
                            "size": 11,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    fake_voice = _FakeVoiceService("ignored for video")
    monkeypatch.setattr(
        service,
        "_voice_service_for_workspace",
        lambda _workspace: (fake_voice, SimpleNamespace(provider="local_whisper")),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        assert "Transcript:" not in captured_prompts[0]
        assert fake_voice.calls == []
    finally:
        await store.close()


def test_voice_service_for_workspace_uses_hub_config_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hub_config_path = tmp_path / "codex-autorunner.yml"
    hub_config_path.write_text("mode: hub\nversion: 2\n", encoding="utf-8")

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=SimpleNamespace(),
        outbox_manager=_FakeOutboxManager(),
    )
    service._hub_config_path = hub_config_path
    service._process_env = {"BASE": "1"}

    load_calls: list[tuple[Path, Optional[Path]]] = []

    def _fake_load_repo_config(
        start: Path, hub_path: Optional[Path] = None
    ) -> SimpleNamespace:
        load_calls.append((start, hub_path))
        return SimpleNamespace(
            voice={
                "enabled": False,
                "provider": "openai_whisper",
                "warn_on_remote_api": False,
            }
        )

    resolve_calls: list[tuple[Path, Optional[dict[str, str]]]] = []

    def _fake_resolve_env_for_root(
        root: Path, base_env: Optional[dict[str, str]] = None
    ) -> dict[str, str]:
        resolve_calls.append((root, base_env))
        return {
            "OPENAI_API_KEY": "workspace-key",
        }

    class _StubVoiceService:
        def __init__(
            self, _voice_config: Any, logger: Any = None, env: Optional[dict] = None
        ) -> None:
            _ = logger
            self.env = dict(env or {})

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            discord_service_module, "load_repo_config", _fake_load_repo_config
        )
        monkeypatch.setattr(
            discord_service_module,
            "resolve_env_for_root",
            _fake_resolve_env_for_root,
        )
        monkeypatch.setattr(discord_service_module, "VoiceService", _StubVoiceService)

        voice_service, voice_config = service._voice_service_for_workspace(workspace)
    finally:
        monkeypatch.undo()

    assert voice_service is not None
    assert voice_config is not None
    assert load_calls == [(workspace.resolve(), hub_config_path)]
    assert resolve_calls == [(workspace.resolve(), {"BASE": "1"})]
    assert voice_config.enabled is True
    assert voice_config.provider == "openai_whisper"
    assert voice_service.env.get("OPENAI_API_KEY") == "workspace-key"


def test_voice_service_for_workspace_caches_env_by_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=SimpleNamespace(),
        outbox_manager=_FakeOutboxManager(),
    )
    service._process_env = {"BASE": "1"}

    def _fake_load_repo_config(
        start: Path, hub_path: Optional[Path] = None
    ) -> SimpleNamespace:
        _ = start, hub_path
        return SimpleNamespace(
            voice={
                "enabled": True,
                "provider": "openai_whisper",
                "warn_on_remote_api": False,
            }
        )

    def _fake_resolve_env_for_root(
        root: Path, base_env: Optional[dict[str, str]] = None
    ) -> dict[str, str]:
        assert base_env == {"BASE": "1"}
        if root.resolve() == workspace_a.resolve():
            return {"OPENAI_API_KEY": "key-a"}
        return {"OPENAI_API_KEY": "key-b"}

    class _StubVoiceService:
        def __init__(
            self, _voice_config: Any, logger: Any = None, env: Optional[dict] = None
        ) -> None:
            _ = logger
            self.env = dict(env or {})

    monkeypatch.setattr(
        discord_service_module, "load_repo_config", _fake_load_repo_config
    )
    monkeypatch.setattr(
        discord_service_module,
        "resolve_env_for_root",
        _fake_resolve_env_for_root,
    )
    monkeypatch.setattr(discord_service_module, "VoiceService", _StubVoiceService)

    voice_service_a, _voice_config_a = service._voice_service_for_workspace(workspace_a)
    voice_service_b, _voice_config_b = service._voice_service_for_workspace(workspace_b)
    voice_service_a_repeat, _voice_config_a_repeat = (
        service._voice_service_for_workspace(workspace_a)
    )

    assert voice_service_a is not None
    assert voice_service_b is not None
    assert voice_service_a is voice_service_a_repeat
    assert voice_service_a.env.get("OPENAI_API_KEY") == "key-a"
    assert voice_service_b.env.get("OPENAI_API_KEY") == "key-b"


def test_build_attachment_filename_uses_source_url_audio_suffix(tmp_path: Path) -> None:
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
    )
    attachment = SimpleNamespace(
        file_name=None,
        mime_type=None,
        source_url="https://cdn.discordapp.com/attachments/voice-message.opus?foo=bar",
    )

    file_name = service._build_attachment_filename(attachment, index=1)

    assert file_name.endswith(".opus")


def test_build_attachment_filename_does_not_infer_audio_suffix_for_video(
    tmp_path: Path,
) -> None:
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
    )
    attachment = SimpleNamespace(
        file_name="clip",
        mime_type="video/mp4",
        source_url="https://cdn.discordapp.com/attachments/clip",
        kind="video",
    )

    file_name = service._build_attachment_filename(attachment, index=1)

    assert not file_name.endswith(".m4a")
    assert Path(file_name).suffix == ""


@pytest.mark.anyio
async def test_message_create_streaming_turn_posts_progress_placeholder_and_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text="done from streaming turn",
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        send_indices = [
            idx for idx, op in enumerate(rest.message_ops) if op.get("op") == "send"
        ]
        edit_indices = [
            idx for idx, op in enumerate(rest.message_ops) if op.get("op") == "edit"
        ]
        assert send_indices
        assert edit_indices
        assert send_indices[0] < edit_indices[0]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_surfaces_fast_transient_thinking_and_tool_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            (
                0.05,
                format_sse(
                    "app-server",
                    {
                        "message": {
                            "method": "item/reasoning/summaryTextDelta",
                            "params": {
                                "itemId": "reason-1",
                                "delta": "checking Discord progress visibility",
                            },
                        }
                    },
                ),
            ),
            (
                0.05,
                format_sse(
                    "app-server",
                    {
                        "message": {
                            "method": "item/toolCall/start",
                            "params": {
                                "toolName": "exec",
                                "toolInput": {"command": "pwd"},
                            },
                        }
                    },
                ),
            ),
            (
                0.05,
                format_sse(
                    "app-server",
                    {
                        "message": {
                            "method": "item/agentMessage/delta",
                            "params": {"delta": "partial reply"},
                        }
                    },
                ),
            ),
        ],
        assistant_text="done from streaming turn",
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        progress_contents = [
            str(msg["payload"].get("content", ""))
            for msg in rest.edited_channel_messages
        ]
        assert any(
            "🧠 checking Discord progress visibility" in content
            for content in progress_contents
        )
        assert any("tool: exec" in content for content in progress_contents)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_opencode_streaming_turn_surfaces_thinking_and_tool_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_agent_state(channel_id="channel-1", agent="opencode")
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "sessionID": "discord-backend-thread-1",
                        "messageID": "assistant-1",
                        "properties": {
                            "messageID": "assistant-1",
                            "info": {"id": "assistant-1", "role": "assistant"},
                            "part": {
                                "id": "reason-1",
                                "type": "reasoning",
                                "messageID": "assistant-1",
                                "sessionID": "discord-backend-thread-1",
                                "text": "checking Discord progress visibility",
                            },
                        },
                    },
                }
            },
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "sessionID": "discord-backend-thread-1",
                        "messageID": "assistant-1",
                        "properties": {
                            "messageID": "assistant-1",
                            "info": {"id": "assistant-1", "role": "assistant"},
                            "part": {
                                "id": "tool-1",
                                "type": "tool",
                                "messageID": "assistant-1",
                                "sessionID": "discord-backend-thread-1",
                                "tool": "shell",
                                "input": "rg AGENTS.md",
                                "state": {"status": "running"},
                            },
                        },
                    },
                }
            },
        ],
        agent_id="opencode",
        assistant_text="done from opencode streaming turn",
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        progress_contents = [
            str(msg["payload"].get("content", ""))
            for msg in rest.edited_channel_messages
        ]
        assert any(
            "checking Discord progress visibility" in content
            for content in progress_contents
        )
        assert any("tool: shell" in content for content in progress_contents)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_skips_live_progress_when_parallel_streaming_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text="done from streaming turn",
        wait_for_stream=False,
        allow_parallel_event_stream=False,
    )

    try:
        await service.run_forever()
        send_indices = [
            idx for idx, op in enumerate(rest.message_ops) if op.get("op") == "send"
        ]
        edit_indices = [
            idx for idx, op in enumerate(rest.message_ops) if op.get("op") == "edit"
        ]
        assert send_indices
        assert edit_indices
        assert send_indices[0] < edit_indices[0]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_persists_full_output_in_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    full_output = (
        "This output line is intentionally longer than one hundred twenty characters "
        "so we can verify Discord progress keeps the full persistent intermediate turn."
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content=full_output,
                delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
            ),
        ],
        assistant_text="done from streaming turn",
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        assert rest.edited_channel_messages
        assert any(
            full_output in msg["payload"].get("content", "")
            for msg in rest.edited_channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_updates_token_usage_log_line_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="tokens used - input: 66, output: 158, reasoning: 0",
                delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
            ),
            OutputDelta(
                timestamp="2026-01-01T00:00:02Z",
                content="tokens used - input: 297, cached: 11853, output: 147, reasoning: 0",
                delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
            ),
        ],
        assistant_text="done from streaming turn",
        wait_for_stream=True,
    )
    try:
        await service.run_forever()
        assert rest.edited_channel_messages
        final_progress = str(
            rest.edited_channel_messages[-1]["payload"].get("content", "")
        )
        assert "tokens used - input: 297, cached: 11853, output: 147, reasoning: 0" in (
            final_progress
        )
        assert (
            "tokens used - input: 66, output: 158, reasoning: 0" not in final_progress
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_accumulates_segmented_intermediate_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="intermediate output one",
                delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
            ),
            OutputDelta(
                timestamp="2026-01-01T00:00:02Z",
                content="intermediate output two",
                delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
            ),
        ],
        assistant_text="done from streaming turn",
        wait_for_stream=True,
    )
    try:
        await service.run_forever()
        progress_contents = [
            str(msg["payload"].get("content", ""))
            for msg in rest.edited_channel_messages
        ]
        sent_contents = [
            str(msg["payload"].get("content", "")) for msg in rest.channel_messages
        ]
        assert any(
            "intermediate output one" in content
            and "intermediate output two" in content
            for content in progress_contents
        )
        assert "---" not in sent_contents
        assert any("done from streaming turn" in content for content in sent_contents)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_final_progress_omits_duplicate_terminal_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    final_text = "intermediate output two"
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="intermediate output one",
                delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
            ),
            OutputDelta(
                timestamp="2026-01-01T00:00:02Z",
                content=final_text,
                delta_type=RUN_EVENT_DELTA_TYPE_LOG_LINE,
            ),
        ],
        assistant_text=final_text,
        wait_for_stream=True,
    )
    try:
        await service.run_forever()
        progress_contents = [
            str(msg["payload"].get("content", ""))
            for msg in rest.edited_channel_messages
        ]
        sent_contents = [
            str(msg["payload"].get("content", "")) for msg in rest.channel_messages
        ]
        intermediate_messages = [
            content
            for content in progress_contents
            if "intermediate output one" in content
        ]
        assert intermediate_messages
        assert all(final_text not in content for content in intermediate_messages)
        assert "---" not in sent_contents
        assert any(final_text in content for content in sent_contents)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_turn_sends_only_final_attachment_when_long(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
        config_kwargs=dict(max_message_length=40, message_overflow="document"),
    )

    async def _fake_run_turn(**_kwargs: Any) -> DiscordMessageTurnResult:
        return DiscordMessageTurnResult(
            final_message=("final section " * 20).strip(),
            intermediate_message=("intermediate section " * 20).strip(),
            preview_message_id="preview-1",
        )

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _fake_run_turn)

    try:
        await service.run_forever()
        assert len(rest.attachment_messages) == 1
        assert rest.attachment_messages[0]["filename"] == "final-response.md"
        assert not any(
            msg["payload"].get("content", "") == "---" for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_turn_uses_progress_snapshot_when_final_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )

    async def _fake_run_turn(**_kwargs: Any) -> DiscordMessageTurnResult:
        return DiscordMessageTurnResult(
            final_message="",
            intermediate_message="progress snapshot output",
            preview_message_id="preview-1",
        )

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _fake_run_turn)

    try:
        await service.run_forever()
        assert any(
            "progress snapshot output" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            "(No response text returned.)" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_opencode_turn_sends_summary_before_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_agent_state(channel_id="channel-1", agent="opencode")
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_run_turn(**_kwargs: Any) -> DiscordMessageTurnResult:
        return DiscordMessageTurnResult(
            final_message="final output",
            intermediate_message="done · agent opencode · model-x · 1s · step 3",
            preview_message_id="preview-1",
        )

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _fake_run_turn)

    try:
        await service.run_forever()
        contents = [
            str(msg["payload"].get("content", "")) for msg in rest.channel_messages
        ]
        assert any(
            "final output\n\nagent opencode · model-x · 1s · step 3" in content
            for content in contents
        )
        assert not any(content.strip() == "---" for content in contents)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_opencode_turn_does_not_duplicate_summary_with_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_agent_state(channel_id="channel-1", agent="opencode")
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    rendered_summary = "agent opencode · model-x · 1s · step 3"

    async def _fake_run_turn(**_kwargs: Any) -> DiscordMessageTurnResult:
        return DiscordMessageTurnResult(
            final_message="",
            intermediate_message="done · agent opencode · model-x · 1s · step 3",
            preview_message_id="preview-1",
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _fake_run_turn)

    try:
        await service.run_forever()
        contents = [
            str(msg["payload"].get("content", "")) for msg in rest.channel_messages
        ]
        combined = "\n".join(contents)
        assert combined.count(rendered_summary) == 1
        assert "done · agent opencode · model-x · 1s · step 3" not in combined
        assert "(No response text returned.)" in combined
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_completion_sends_final_and_deletes_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    final_text = "done from streaming turn"
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text=final_text,
        wait_for_stream=True,
    )
    try:
        await service.run_forever()
        assert all(
            item["message_id"] == "msg-1" for item in rest.deleted_channel_messages
        )
        assert rest.edited_channel_messages
        assert rest.edited_channel_messages[-1]["payload"].get("components") == []
        assert any(
            final_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_keeps_components_cleared_after_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text="done from streaming turn",
        wait_for_stream=True,
    )
    try:
        await service.run_forever()
        assert rest.edited_channel_messages
        assert rest.edited_channel_messages[-1]["payload"].get("components") == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_ignores_late_failed_after_completed_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    final_text = "done before late failure"
    logged_events: list[dict[str, Any]] = []

    def _capture_log_event(
        logger: logging.Logger,
        level: int,
        event: str,
        *,
        exc: Optional[Exception] = None,
        **fields: Any,
    ) -> None:
        _ = logger
        logged_events.append({"level": level, "event": event, "exc": exc, **fields})

    monkeypatch.setattr(discord_service_module, "log_event", _capture_log_event)
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            Completed(timestamp="2026-01-01T00:00:02Z", final_message=final_text),
            Failed(timestamp="2026-01-01T00:00:03Z", error_message="Turn failed"),
        ],
        assistant_text=final_text,
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        assert any(
            final_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            "Turn failed:" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            event["event"] == "discord.turn.failed_late_ignored"
            for event in logged_events
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_ignores_late_failed_with_stream_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    streamed_text = "fallback streamed answer survives"
    logged_events: list[dict[str, Any]] = []

    def _capture_log_event(
        logger: logging.Logger,
        level: int,
        event: str,
        *,
        exc: Optional[Exception] = None,
        **fields: Any,
    ) -> None:
        _ = logger
        logged_events.append({"level": level, "event": event, "exc": exc, **fields})

    monkeypatch.setattr(discord_service_module, "log_event", _capture_log_event)
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content=streamed_text,
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            Completed(timestamp="2026-01-01T00:00:02Z", final_message=""),
            Failed(timestamp="2026-01-01T00:00:03Z", error_message="Turn failed"),
        ],
        assistant_text="",
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        assert any(
            streamed_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            "(No response text returned.)" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            "Turn failed:" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            event["event"] == "discord.turn.failed_late_ignored"
            for event in logged_events
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_ignores_commentary_when_stream_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    commentary_text = "draft release summary"
    final_text = "final release summary"
    _patch_streaming_harness(
        monkeypatch,
        [
            {
                "message": {
                    "method": "item/completed",
                    "params": {
                        "itemId": "item-1",
                        "item": {
                            "type": "agentMessage",
                            "text": commentary_text,
                            "phase": "commentary",
                        },
                    },
                }
            },
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content=final_text,
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            Completed(timestamp="2026-01-01T00:00:02Z", final_message=""),
        ],
        assistant_text="",
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        contents = [
            str(msg["payload"].get("content", "")) for msg in rest.channel_messages
        ]
        assert any(final_text in content for content in contents)
        assert not any(commentary_text in content for content in contents)
        assert not any(
            "(No response text returned.)" in content for content in contents
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_recovers_if_wait_disconnects_after_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    final_text = "completed answer survives reconnect"
    harness = _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content=final_text,
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            Completed(timestamp="2026-01-01T00:00:02Z", final_message=final_text),
        ],
        assistant_text="",
        wait_for_stream=True,
    )

    async def _disconnecting_wait_for_turn(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        await harness._stream_done.wait()
        raise CodexAppServerDisconnected("Reconnecting... 2/5")

    monkeypatch.setattr(harness, "wait_for_turn", _disconnecting_wait_for_turn)

    try:
        await service.run_forever()
        assert any(
            final_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            "Turn failed:" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            "(No response text returned.)" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert rest.deleted_channel_messages
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_recovers_if_wait_times_out_after_streamed_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    final_text = "completed answer survives timeout"
    harness = _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content=final_text,
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            Completed(timestamp="2026-01-01T00:00:02Z", final_message=""),
        ],
        assistant_text="",
        wait_for_stream=False,
    )

    async def _hanging_wait_for_turn(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        await asyncio.sleep(1.0)
        return SimpleNamespace(status="ok", assistant_text="", errors=[])

    monkeypatch.setattr(harness, "wait_for_turn", _hanging_wait_for_turn)
    monkeypatch.setattr(
        discord_message_turns_module,
        "DISCORD_PMA_IDLE_TIMEOUT_SECONDS",
        0.05,
    )

    try:
        await service.run_forever()
        assert any(
            final_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            "Turn failed:" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            "(No response text returned.)" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_failure_before_completion_still_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="partial output before failure",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        status="error",
        assistant_text="",
        errors=["Discord turn failed"],
        wait_for_stream=True,
    )
    try:
        await service.run_forever()
        assert any(
            "Turn failed:" in msg["payload"].get("content", "")
            and "Discord turn failed" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            "partial output before failure" in msg["payload"].get("content", "")
            and "Turn failed:" not in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_multi_chunk_deletes_preview_and_sends_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
        config_kwargs=dict(max_message_length=80),
    )
    final_text = "\n".join(
        [f"line {index} with enough content for chunking" for index in range(1, 20)]
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text=final_text,
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        assert all(
            item["message_id"] == "msg-1" for item in rest.deleted_channel_messages
        )
        final_contents = [
            msg["payload"].get("content", "") for msg in rest.channel_messages
        ]
        rendered = "\n".join(str(content) for content in final_contents)
        assert "line 1 with enough content for chunking" in rendered
        assert "line 19 with enough content for chunking" in rendered
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_appends_final_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    final_text = "done from streaming turn"
    _patch_streaming_harness(
        monkeypatch,
        [
            TokenUsage(
                timestamp="2026-01-01T00:00:01Z",
                usage={
                    "last": {
                        "totalTokens": 71173,
                        "inputTokens": 400,
                        "outputTokens": 245,
                    },
                    "modelContextWindow": 203352,
                },
            ),
        ],
        assistant_text=final_text,
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        final_content = ""
        final_candidates = [*rest.edited_channel_messages, *rest.channel_messages]
        for message in final_candidates:
            content = str(message.get("payload", {}).get("content", ""))
            if final_text in content:
                final_content = content
                break
        assert final_content
        assert "ctx 65%" in final_content
        assert "Token usage: total 71173 input 400 output 245" in final_content
        assert final_content.count("ctx 65%") == 1
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_uses_assistant_stream_when_final_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    streamed_text = "fallback streamed answer"
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content=streamed_text,
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            TokenUsage(
                timestamp="2026-01-01T00:00:02Z",
                usage={
                    "last": {
                        "totalTokens": 71173,
                        "inputTokens": 400,
                        "outputTokens": 245,
                    },
                    "modelContextWindow": 203352,
                },
            ),
        ],
        assistant_text="",
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        final_candidates = [*rest.edited_channel_messages, *rest.channel_messages]
        final_content = ""
        for message in final_candidates:
            content = str(message.get("payload", {}).get("content", ""))
            if (
                streamed_text in content
                and "Token usage: total 71173 input 400 output 245" in content
            ):
                final_content = content
                break
        assert final_content
        assert streamed_text in final_content
        assert "ctx 65%" in final_content
        assert "Token usage: total 71173 input 400 output 245" in final_content
        assert final_content.count("ctx 65%") == 1
        assert "(No response text returned.)" not in final_content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_empty_final_includes_text_fallback_with_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            TokenUsage(
                timestamp="2026-01-01T00:00:01Z",
                usage={
                    "last": {
                        "totalTokens": 71173,
                        "inputTokens": 400,
                        "outputTokens": 245,
                    },
                    "modelContextWindow": 203352,
                },
            ),
        ],
        assistant_text="",
        wait_for_stream=True,
    )
    try:
        await service.run_forever()
        final_candidates = [*rest.edited_channel_messages, *rest.channel_messages]
        final_content = ""
        for message in final_candidates:
            content = str(message.get("payload", {}).get("content", ""))
            if "Token usage: total 71173 input 400 output 245" in content:
                final_content = content
                break
        assert final_content
        assert "(No response text returned.)" in final_content
        assert "ctx 65%" in final_content
        assert final_content.count("ctx 65%") == 1
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_fallback_preserves_multichunk_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    expected_text = "go go \nnext line"
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="go ",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            OutputDelta(
                timestamp="2026-01-01T00:00:02Z",
                content="go go ",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            OutputDelta(
                timestamp="2026-01-01T00:00:03Z",
                content="\nnext line",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text="",
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        final_candidates = [*rest.edited_channel_messages, *rest.channel_messages]
        final_content = ""
        for message in final_candidates:
            content = str(message.get("payload", {}).get("content", ""))
            if expected_text in content:
                final_content = content
                break
        assert final_content
        assert expected_text in final_content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_fallback_preserves_whitespace_only_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    expected_text = "line 1\n\nline 2"
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="line 1",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            OutputDelta(
                timestamp="2026-01-01T00:00:02Z",
                content="\n\n",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            OutputDelta(
                timestamp="2026-01-01T00:00:03Z",
                content="line 2",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text="",
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        final_candidates = [*rest.edited_channel_messages, *rest.channel_messages]
        final_content = ""
        for message in final_candidates:
            content = str(message.get("payload", {}).get("content", ""))
            if expected_text in content:
                final_content = content
                break
        assert final_content
        assert expected_text in final_content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_fallback_handles_cumulative_deltas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    expected_text = "Hello world"
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="Hello",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            OutputDelta(
                timestamp="2026-01-01T00:00:02Z",
                content="Hello world",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text="",
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        final_candidates = [*rest.edited_channel_messages, *rest.channel_messages]
        final_content = ""
        for message in final_candidates:
            content = str(message.get("payload", {}).get("content", ""))
            if expected_text in content:
                final_content = content
                break
        assert final_content
        assert expected_text in final_content
        assert "HelloHello world" not in final_content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_ignores_user_message_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    secret = "SECRET PMA CONTEXT SHOULD NOT LEAK"
    visible = "assistant output"
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content=secret,
                delta_type=RUN_EVENT_DELTA_TYPE_USER_MESSAGE,
            ),
            OutputDelta(
                timestamp="2026-01-01T00:00:02Z",
                content=visible,
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text="done from streaming turn",
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        rendered_progress = [
            msg["payload"].get("content", "") for msg in rest.edited_channel_messages
        ]
        assert rendered_progress
        assert not any(secret in text for text in rendered_progress)
        assert any(visible in text for text in rendered_progress)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_progress_failures_are_best_effort_and_do_not_block_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
        rest=_FailingProgressRest(),
    )
    final_text = "final despite progress failures"
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text=final_text,
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        assert rest.send_attempts >= 2
        assert any(
            final_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_progress_edit_failures_are_best_effort_and_throttled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
        rest=_EditFailingProgressRest(),
    )
    final_text = "final despite edit failures"
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
            OutputDelta(
                timestamp="2026-01-01T00:00:02Z",
                content="still thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text=final_text,
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        assert 1 <= rest.edit_attempts <= 4
        assert all(
            item["message_id"] == "msg-1" for item in rest.deleted_channel_messages
        )
        assert any(
            final_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_enqueues_preview_delete_when_delete_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
        rest=_DeleteFailingProgressRest(),
    )
    final_text = "final with preview kept"
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text=final_text,
        wait_for_stream=True,
    )

    try:
        await service.run_forever()
        assert any(
            final_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        pending = await store.list_outbox()
        assert any(record.operation == "delete" for record in pending)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_exception_marks_progress_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )
    _patch_streaming_harness(
        monkeypatch,
        [
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        status="error",
        assistant_text="",
        errors=["Discord turn failed"],
        wait_for_stream=True,
        stream_exception=RuntimeError("boom"),
    )

    try:
        await service.run_forever()
        assert rest.edited_channel_messages
        assert any(
            "failed" in msg["payload"].get("content", "")
            for msg in rest.edited_channel_messages
        )
        assert any(
            "Turn failed: Discord turn failed" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert not any(
            "(No response text returned.)" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_silent_destination_ignores_plain_text_and_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            ("MESSAGE_CREATE", _message_create("hello team")),
            ("MESSAGE_CREATE", _message_create("!pwd", message_id="m-2")),
        ]
    )
    policy = build_discord_collaboration_policy(
        allowed_guild_ids=("guild-1",),
        allowed_channel_ids=(),
        allowed_user_ids=(),
        collaboration_raw={
            "destinations": [
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-1",
                    "mode": "silent",
                }
            ]
        },
    )
    service = DiscordBotService(
        _config(
            tmp_path,
            allowed_channel_ids=frozenset(),
            collaboration_policy=policy,
        ),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("silent destinations should not run turns")

    def _should_not_run_shell(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("silent destinations should not run shell commands")

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.car_handlers.shell_commands.subprocess.run",
        _should_not_run_shell,
    )

    try:
        with caplog.at_level(logging.INFO):
            await service.run_forever()
        contents = [
            payload.get("content", "")
            for payload in await _collect_outbound_payloads(rest, store)
        ]
        assert not any("hello team" in content for content in contents)
        assert not any("$ pwd" in content for content in contents)
        assert rest.attachment_messages == []
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            '"policy_outcome":"silent_destination"' in message
            and '"message_id":"m-1"' in message
            for message in messages
        )
        assert any(
            '"policy_outcome":"silent_destination"' in message
            and '"message_id":"m-2"' in message
            for message in messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_honors_shared_turn_policy_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
    )

    calls: list[tuple[str, str]] = []

    def _deny_policy(*, mode: str, context: Any) -> bool:
        calls.append((mode, context.text))
        return False

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("agent turn should not run when shared policy denies")

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.should_trigger_plain_text_turn",
        _deny_policy,
    )
    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        await service.run_forever()
        assert calls == [("always", "ship it")]
        assert rest.channel_messages == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_denied_by_user_allowlist_logs_policy_outcome(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    denied_payload = _message_create("hello", message_id="m-denied")
    denied_payload["author"] = {"id": "unauthorized", "bot": False}
    gateway = _FakeGateway([("MESSAGE_CREATE", denied_payload)])
    policy = build_discord_collaboration_policy(
        allowed_guild_ids=("guild-1",),
        allowed_channel_ids=("channel-1",),
        allowed_user_ids=("user-1",),
    )
    service = DiscordBotService(
        _config(
            tmp_path,
            allowed_channel_ids=frozenset({"channel-1"}),
            collaboration_policy=policy,
        ),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        with caplog.at_level(logging.INFO):
            await service.run_forever()
        assert rest.channel_messages == []
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            '"event":"discord.collaboration_policy.evaluated"' in message
            and '"policy_outcome":"denied_actor"' in message
            for message in messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_unbound_channel_stays_silent_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("hello team"))])
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset()),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("unbound plain-text messages should stay silent")

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        with caplog.at_level(logging.INFO):
            await service.run_forever()
        assert rest.channel_messages == []
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            '"event":"discord.message.unbound_plain_text_ignored"' in message
            for message in messages
        )
        assert any('"policy_outcome":"turn_started"' in message for message in messages)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_mentions_only_destination_requires_discord_mention(
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
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            ("MESSAGE_CREATE", _message_create("ship it")),
            ("MESSAGE_CREATE", _message_create("<@app-1> ship it", message_id="m-2")),
        ]
    )
    policy = build_discord_collaboration_policy(
        allowed_guild_ids=("guild-1",),
        allowed_channel_ids=(),
        allowed_user_ids=(),
        collaboration_raw={
            "default_plain_text_trigger": "mentions",
            "destinations": [
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-1",
                    "mode": "active",
                    "plain_text_trigger": "mentions",
                }
            ],
        },
    )
    service = DiscordBotService(
        _config(
            tmp_path,
            allowed_channel_ids=frozenset(),
            collaboration_policy=policy,
        ),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    prompts: list[str] = []

    async def _fake_run_turn(self: DiscordBotService, *args: Any, **kwargs: Any) -> str:
        _ = args
        prompts.append(str(kwargs["prompt_text"]))
        return "mention ok"

    monkeypatch.setattr(
        service,
        "_run_agent_turn_for_message",
        _fake_run_turn.__get__(service, DiscordBotService),
    )

    try:
        await service.run_forever()
        assert len(prompts) == 1
        assert prompts[0].endswith("<@app-1> ship it")
        assert any(
            "mention ok" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_command_only_destination_allows_bang_commands_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            ("MESSAGE_CREATE", _message_create("ship it")),
            ("MESSAGE_CREATE", _message_create("!pwd", message_id="m-2")),
        ]
    )
    policy = build_discord_collaboration_policy(
        allowed_guild_ids=("guild-1",),
        allowed_channel_ids=(),
        allowed_user_ids=(),
        collaboration_raw={
            "destinations": [
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-1",
                    "mode": "command_only",
                }
            ]
        },
    )
    service = DiscordBotService(
        _config(
            tmp_path,
            allowed_channel_ids=frozenset(),
            collaboration_policy=policy,
        ),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("plain-text should be disabled in command_only channels")

    shell_calls: list[str] = []

    async def _fake_bang_shell(
        *,
        channel_id: str,
        message_id: str,
        text: str,
        workspace_root: Path,
    ) -> None:
        _ = channel_id, message_id, workspace_root
        shell_calls.append(text)

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)
    monkeypatch.setattr(service, "_handle_bang_shell", _fake_bang_shell)

    try:
        with caplog.at_level(logging.INFO):
            await service.run_forever()
        assert shell_calls == ["!pwd"]
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            '"policy_outcome":"command_only_destination"' in message
            and '"message_id":"m-1"' in message
            for message in messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_ignores_slash_prefixed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("/car status"))],
    )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("slash-prefixed text should not run message turns")

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        await service.run_forever()
        assert rest.channel_messages == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_bang_shell_executes_in_bound_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "car").write_text("#!/bin/sh\n", encoding="utf-8")

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("!pwd"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    seen: dict[str, Any] = {}

    def _fake_shell_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args[0], 0, "/tmp/workspace\n", "")

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("bang-prefixed messages should bypass agent turn path")

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.car_handlers.shell_commands.subprocess.run",
        _fake_shell_run,
    )
    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        await service.run_forever()
        assert seen["args"][0] == ["bash", "-lc", "pwd"]
        assert seen["kwargs"]["cwd"] == workspace.resolve()
        path_entries = seen["kwargs"]["env"]["PATH"].split(os.pathsep)
        assert str(workspace.resolve()) in path_entries
        assert any(
            "$ pwd" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert any(
            "/tmp/workspace" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_bang_shell_honors_shell_disable_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("!ls"))],
        config_kwargs=dict(shell_enabled=False),
    )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("bang-prefixed shell command should not run agent turn")

    def _should_not_run_shell(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("shell execution should stay disabled")

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.car_handlers.shell_commands.subprocess.run",
        _should_not_run_shell,
    )

    try:
        await service.run_forever()
        assert any(
            "Shell commands are disabled" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_bang_shell_attaches_oversized_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("!echo long"))],
        config_kwargs=dict(shell_max_output_chars=12),
    )

    long_output = "1234567890abcdefghijklmnopqrstuvwxyz\n"

    def _fake_shell_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, long_output, "")

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.car_handlers.shell_commands.subprocess.run",
        _fake_shell_run,
    )

    try:
        await service.run_forever()
        assert any(
            "$ echo long" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert rest.attachment_messages
        assert rest.attachment_messages[0]["filename"].startswith("shell-output-")
        assert (
            long_output.encode("utf-8").rstrip(b"\n")
            in rest.attachment_messages[0]["data"]
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_in_pma_mode_uses_pma_session_key(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            ("INTERACTION_CREATE", _pma_interaction("on")),
            ("MESSAGE_CREATE", _message_create("plan next sprint")),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured: list[dict[str, Any]] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
        **_kwargs: Any,
    ) -> str:
        captured.append(
            {
                "workspace_root": workspace_root,
                "prompt_text": prompt_text,
                "agent": agent,
                "session_key": session_key,
                "orchestrator_channel_key": orchestrator_channel_key,
            }
        )
        return "PMA reply"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured
        assert captured[0]["session_key"] == PMA_KEY
        assert captured[0]["orchestrator_channel_key"] == "pma:channel-1"
        prompt_text = captured[0]["prompt_text"]
        assert "plan next sprint" in prompt_text and prompt_text != "plan next sprint"
        assert "<hub_snapshot>" in prompt_text
        assert any(
            "PMA reply" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_attachment_only_in_pma_mode_uses_hub_inbox_snapshot(
    tmp_path: Path,
) -> None:
    seed_hub_files(tmp_path, force=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
    )

    attachment_url = "https://cdn.discordapp.com/attachments/pma-zip-1"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"zip-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-zip-1",
                            "filename": "ticket-pack.zip",
                            "content_type": "application/zip",
                            "size": 9,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured_prompts: list[str] = []
    captured_workspaces: list[Path] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
        **_kwargs: Any,
    ) -> str:
        _ = (
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        captured_workspaces.append(workspace_root)
        return "PMA zip reply"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        assert captured_workspaces == [tmp_path.resolve()]

        hub_inbox = inbox_dir(tmp_path.resolve())
        hub_files = [path for path in hub_inbox.iterdir() if path.is_file()]
        assert len(hub_files) == 1
        assert hub_files[0].read_bytes() == b"zip-bytes"

        repo_inbox = inbox_dir(workspace.resolve())
        if repo_inbox.exists():
            assert [path for path in repo_inbox.iterdir() if path.is_file()] == []

        prompt = captured_prompts[0]
        assert "CAR:PMA_DOCS_GENERATED" in prompt
        assert any(
            "PMA zip reply" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_image_attachment_in_pma_mode_adds_native_local_image_item(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
    )

    attachment_url = "https://cdn.discordapp.com/attachments/pma-image-1"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"png-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="Please review image",
                    attachments=[
                        {
                            "id": "att-image-1",
                            "filename": "screen.png",
                            "content_type": "image/png",
                            "size": 9,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured_items: list[Optional[list[dict[str, Any]]]] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        input_items: Optional[list[dict[str, Any]]] = None,
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
        **_kwargs: Any,
    ) -> str:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_items.append(input_items)
        return "PMA image reply"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_items and isinstance(captured_items[0], list)
        items = captured_items[0] or []
        assert items and items[0].get("type") == "text"
        assert any(item.get("type") == "localImage" for item in items[1:])
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_in_pma_mode_falls_back_to_hub_root_when_binding_path_invalid(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(tmp_path / "missing-workspace"),
        repo_id=None,
    )
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
    )

    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("status please"))])
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured: list[dict[str, Any]] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
        **_kwargs: Any,
    ) -> str:
        captured.append(
            {
                "workspace_root": workspace_root,
                "prompt_text": prompt_text,
                "agent": agent,
                "session_key": session_key,
                "orchestrator_channel_key": orchestrator_channel_key,
            }
        )
        return "fallback root ok"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured
        assert captured[0]["workspace_root"] == tmp_path.resolve()
        assert captured[0]["session_key"] == PMA_KEY
        assert any(
            "fallback root ok" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


def test_build_message_session_key_is_registry_valid(tmp_path: Path) -> None:
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    codex_key = service._build_message_session_key(
        channel_id="channel-1",
        workspace_root=workspace,
        pma_enabled=False,
        agent="codex",
    )
    opencode_key = service._build_message_session_key(
        channel_id="channel-1",
        workspace_root=workspace,
        pma_enabled=False,
        agent="opencode",
    )

    assert codex_key.startswith(f"{FILE_CHAT_PREFIX}discord.channel-1.")
    assert opencode_key.startswith(f"{FILE_CHAT_OPENCODE_PREFIX}discord.channel-1.")
    assert normalize_feature_key(codex_key) == codex_key
    assert normalize_feature_key(opencode_key) == opencode_key


def test_build_message_session_key_pma_matches_logical_agent_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PMA keys use resolve_chat_agent_and_profile (same as web PMA), not runtime alias."""
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_registered_agents",
        lambda context=None: {
            "hermes-m4-pma": SimpleNamespace(name="Hermes M4 PMA"),
        },
    )
    hub_config = SimpleNamespace(
        agent_profiles=lambda agent_id: (
            {"m4-pma": SimpleNamespace(display_name="M4 PMA")}
            if agent_id == "hermes"
            else {}
        ),
        agent_binary=lambda agent_id, **kw: "hermes",
    )
    monkeypatch.setattr(
        "codex_autorunner.agents.registry._resolve_runtime_agent_config",
        lambda ctx: hub_config,
    )

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    expected = pma_base_key("hermes", "m4-pma")
    alias_key = service._build_message_session_key(
        channel_id="channel-1",
        workspace_root=workspace,
        pma_enabled=True,
        agent="hermes-m4-pma",
    )
    split_key = service._build_message_session_key(
        channel_id="channel-1",
        workspace_root=workspace,
        pma_enabled=True,
        agent="hermes",
        agent_profile="m4-pma",
    )
    assert alias_key == expected
    assert split_key == expected
    assert alias_key != pma_base_key("hermes-m4-pma")


@pytest.mark.anyio
async def test_message_create_denied_by_guild_allowlist(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("hello", guild_id="x"))])
    service = DiscordBotService(
        _config(
            tmp_path,
            allowed_guild_ids=frozenset({"guild-1"}),
            allowed_channel_ids=frozenset(),
        ),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert rest.channel_messages == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_resumes_paused_flow_run_in_repo_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("needs approval"))],
    )

    paused = SimpleNamespace(id="run-paused")
    reply_path = workspace / ".codex-autorunner" / "runs" / paused.id / "USER_REPLY.md"
    reply_path.parent.mkdir(parents=True, exist_ok=True)

    async def _fake_find_paused(_: Path):
        return paused

    def _fake_write_reply(_: Path, record: Any, text: str) -> Path:
        assert record is paused
        assert text == "needs approval"
        reply_path.write_text(text, encoding="utf-8")
        return reply_path

    class _FakeController:
        async def resume_flow(self, run_id: str):
            assert run_id == paused.id
            return SimpleNamespace(
                id=run_id,
                status=SimpleNamespace(is_terminal=lambda: False),
            )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("agent turn should not run while a paused flow is waiting")

    monkeypatch.setattr(service, "_find_paused_flow_run", _fake_find_paused)
    monkeypatch.setattr(service, "_write_user_reply", _fake_write_reply)
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_controller",
        lambda _: _FakeController(),
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.ensure_worker",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        await service.run_forever()
        assert any(
            "resumed paused run `run-paused`" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_bang_shell_resumes_paused_flow_run_in_repo_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("!pwd"))],
    )

    paused = SimpleNamespace(id="run-paused")
    reply_path = workspace / ".codex-autorunner" / "runs" / paused.id / "USER_REPLY.md"
    reply_path.parent.mkdir(parents=True, exist_ok=True)

    async def _fake_find_paused(_: Path):
        return paused

    def _fake_write_reply(_: Path, record: Any, text: str) -> Path:
        assert record is paused
        assert text == "!pwd"
        reply_path.write_text(text, encoding="utf-8")
        return reply_path

    class _FakeController:
        async def resume_flow(self, run_id: str):
            assert run_id == paused.id
            return SimpleNamespace(
                id=run_id,
                status=SimpleNamespace(is_terminal=lambda: False),
            )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("agent turn should not run while a paused flow is waiting")

    def _should_not_run_shell(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("bang shell should not bypass paused flow handling")

    monkeypatch.setattr(service, "_find_paused_flow_run", _fake_find_paused)
    monkeypatch.setattr(service, "_write_user_reply", _fake_write_reply)
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_controller",
        lambda _: _FakeController(),
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.ensure_worker",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.car_handlers.shell_commands.subprocess.run",
        _should_not_run_shell,
    )

    try:
        await service.run_forever()
        assert any(
            "resumed paused run `run-paused`" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_attachment_only_resumes_paused_flow_run(
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
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/paused-file-1"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"paused-attachment"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-paused-1",
                            "filename": "evidence.pdf",
                            "content_type": "application/pdf",
                            "size": 17,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    paused = SimpleNamespace(id="run-paused")
    reply_path = workspace / ".codex-autorunner" / "runs" / paused.id / "USER_REPLY.md"
    reply_path.parent.mkdir(parents=True, exist_ok=True)
    captured: dict[str, str] = {}

    async def _fake_find_paused(_: Path):
        return paused

    def _fake_write_reply(_: Path, record: Any, text: str) -> Path:
        assert record is paused
        captured["text"] = text
        reply_path.write_text(text, encoding="utf-8")
        return reply_path

    class _FakeController:
        async def resume_flow(self, run_id: str):
            assert run_id == paused.id
            return SimpleNamespace(
                id=run_id,
                status=SimpleNamespace(is_terminal=lambda: False),
            )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("agent turn should not run while a paused flow is waiting")

    monkeypatch.setattr(service, "_find_paused_flow_run", _fake_find_paused)
    monkeypatch.setattr(service, "_write_user_reply", _fake_write_reply)
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_controller",
        lambda _: _FakeController(),
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.ensure_worker",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        await service.run_forever()
        assert "Inbound Discord attachments:" in captured.get("text", "")
        assert "evidence.pdf" in captured.get("text", "")
        assert str(inbox_dir(workspace.resolve())) in captured.get("text", "")
        assert str(outbox_pending_dir(workspace.resolve())) in captured.get("text", "")
        assert len(rest.download_requests) == 1
        assert any(
            "resumed paused run `run-paused`" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_skips_inbox_reply_for_user_ticket_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("needs approval"))],
    )

    paused = SimpleNamespace(id="run-paused")

    async def _fake_find_paused(_: Path):
        return paused

    def _should_not_write_reply(*args: Any, **kwargs: Any) -> Path:  # pragma: no cover
        raise AssertionError("should not write USER_REPLY.md for user-ticket pauses")

    captured: dict[str, str] = {}

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: Optional[str] = None,
        orchestrator_channel_key: Optional[str] = None,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        input_items: Optional[list[dict[str, Any]]] = None,
        chat_ux_snapshot: Any = None,
        **_kwargs: Any,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
            input_items,
        )
        captured["prompt_text"] = prompt_text
        return f"thread:{prompt_text}"

    monkeypatch.setattr(service, "_find_paused_flow_run", _fake_find_paused)
    monkeypatch.setattr(
        service,
        "_is_user_ticket_pause",
        lambda _workspace_root, _record: True,
    )
    monkeypatch.setattr(service, "_write_user_reply", _should_not_write_reply)
    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert "needs approval" in captured.get("prompt_text", "")
        assert rest.channel_messages
        assert not any(
            "resumed paused run" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


def test_is_user_ticket_pause_detects_in_workspace_user_ticket(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ticket_path = workspace / "TICKET-490.md"
    ticket_path.write_text(
        "---\nagent: user\ndone: false\n---\n\nPlease do thing.\n",
        encoding="utf-8",
    )

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
    )
    paused = SimpleNamespace(
        state={
            "ticket_engine": {
                "reason_code": "user_pause",
                "current_ticket": ticket_path.name,
            }
        }
    )

    assert service._is_user_ticket_pause(workspace, paused) is True


@pytest.mark.anyio
async def test_message_create_sends_queued_notice_when_dispatch_queue_is_busy(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            ("MESSAGE_CREATE", _message_create("first message", message_id="m-1")),
            ("MESSAGE_CREATE", _message_create("second message", message_id="m-2")),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    turn_count = 0

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
        **_kwargs: Any,
    ) -> str:
        nonlocal turn_count
        turn_count += 1
        if turn_count == 1:
            first_started.set()
            await release_first.wait()
            return "first reply"
        return "second reply"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    async def _release_later() -> None:
        await first_started.wait()
        await asyncio.sleep(0.05)
        release_first.set()

    release_task = asyncio.create_task(_release_later())
    try:
        await asyncio.wait_for(service.run_forever(), timeout=5)
        payloads = await _collect_outbound_payloads(rest, store)
        queued_notice = next(
            (
                payload
                for payload in payloads
                if QUEUED_PLACEHOLDER_TEXT in payload.get("content", "")
            ),
            None,
        )
        contents = [payload.get("content", "") for payload in payloads]
        if queued_notice is not None:
            assert queued_notice.get("components")
            buttons = queued_notice["components"][0]["components"]
            assert [button["custom_id"] for button in buttons] == [
                "queue_cancel:m-2",
                "queue_interrupt_send:m-2",
            ]
        assert any("first reply" in content for content in contents)
        assert any("second reply" in content for content in contents)
    finally:
        if not release_task.done():
            release_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await release_task
        await store.close()


@pytest.mark.anyio
async def test_repo_message_create_routes_repeated_messages_through_orchestration_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_hub_files(tmp_path, force=True)
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
    await store.update_agent_state(channel_id="channel-1", agent="codex")

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create("first orchestration prompt", message_id="m-1"),
            ),
            (
                "MESSAGE_CREATE",
                _message_create("second orchestration prompt", message_id="m-2"),
            ),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class _FakeHarness(_BaseDiscordFakeHarness):
        def __init__(self) -> None:
            self.turn_prompts: list[str] = []
            self.turn_conversation_ids: list[str] = []

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            turn_id = f"backend-turn-{len(self.turn_prompts) + 1}"
            self.turn_prompts.append(prompt)
            self.turn_conversation_ids.append(conversation_id)
            return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, timeout
            if turn_id == "backend-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="ok",
                    assistant_text="first orchestration reply",
                    errors=[],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="second orchestration reply",
                errors=[],
            )

    harness = _FakeHarness()
    _patch_harness(monkeypatch, harness)

    async def _release_later() -> None:
        await first_started.wait()
        await asyncio.sleep(0.05)
        release_first.set()

    release_task = asyncio.create_task(_release_later())
    try:
        await asyncio.wait_for(service.run_forever(), timeout=5)
        with anyio.fail_after(2):
            while not any(
                "second orchestration reply" in msg["payload"].get("content", "")
                for msg in rest.channel_messages
            ):
                await anyio.sleep(0.05)

        contents = [
            payload.get("content", "")
            for payload in await _collect_outbound_payloads(rest, store)
        ]
        assert any("first orchestration reply" in content for content in contents)
        assert any("second orchestration reply" in content for content in contents)
        assert rest.typing_calls.count("channel-1") >= 2

        thread_store = PmaThreadStore(tmp_path)
        threads = thread_store.list_threads(limit=10)
        assert len(threads) == 1
        turns = thread_store.list_turns(threads[0]["managed_thread_id"], limit=10)
        assert len(turns) == 2
        assert [turn["status"] for turn in turns] == ["ok", "ok"]

        orchestration_service = service._discord_thread_service()
        binding = orchestration_service.get_binding(
            surface_kind="discord",
            surface_key="channel-1",
        )
        assert binding is not None
        assert binding.thread_target_id == threads[0]["managed_thread_id"]
        assert harness.turn_conversation_ids == [
            "backend-thread-1",
            "backend-thread-1",
        ]
    finally:
        if not release_task.done():
            release_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await release_task
        await store.close()


@pytest.mark.anyio
async def test_repo_message_create_routes_repeated_messages_through_orchestration_thread_for_hermes_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_hub_files(tmp_path, force=True)
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
    await store.update_agent_state(
        channel_id="channel-1",
        agent="hermes",
        agent_profile="m4-pma",
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create("first hermes orchestration prompt", message_id="m-1"),
            ),
            (
                "MESSAGE_CREATE",
                _message_create("second hermes orchestration prompt", message_id="m-2"),
            ),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class _FakeHarness(_BaseDiscordFakeHarness):
        display_name = "Hermes"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
                "approvals",
            }
        )
        _conversation_id = "hermes-backend-thread-1"

        def __init__(self) -> None:
            self.turn_prompts: list[str] = []
            self.turn_conversation_ids: list[str] = []

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            turn_id = f"hermes-backend-turn-{len(self.turn_prompts) + 1}"
            self.turn_prompts.append(prompt)
            self.turn_conversation_ids.append(conversation_id)
            return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, timeout
            if turn_id == "hermes-backend-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="ok",
                    assistant_text="first hermes orchestration reply",
                    errors=[],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="second hermes orchestration reply",
                errors=[],
            )

    harness = _FakeHarness()
    hub_config = SimpleNamespace(
        agent_profiles=lambda agent_id: (
            {"m4-pma": SimpleNamespace(display_name="M4 PMA")}
            if agent_id == "hermes"
            else {}
        ),
        agent_binary=lambda agent_id, **kw: agent_id,
    )
    monkeypatch.setattr(
        "codex_autorunner.agents.registry._resolve_runtime_agent_config",
        lambda ctx: hub_config,
    )
    _patch_harness(monkeypatch, harness, agent_id="hermes", agent_name="Hermes")

    async def _release_later() -> None:
        await first_started.wait()
        await asyncio.sleep(0.05)
        release_first.set()

    release_task = asyncio.create_task(_release_later())
    try:
        await asyncio.wait_for(service.run_forever(), timeout=5)
        with anyio.fail_after(2):
            while not any(
                "second hermes orchestration reply" in msg["payload"].get("content", "")
                for msg in rest.channel_messages
            ):
                await anyio.sleep(0.05)

        contents = [
            payload.get("content", "")
            for payload in await _collect_outbound_payloads(rest, store)
        ]
        assert any(
            "first hermes orchestration reply" in content for content in contents
        )
        assert any(
            "second hermes orchestration reply" in content for content in contents
        )

        orchestration_service = service._discord_thread_service()
        binding = orchestration_service.get_binding(
            surface_kind="discord",
            surface_key="channel-1",
        )
        assert binding is not None
        resolved_thread = orchestration_service.get_thread_target(
            binding.thread_target_id
        )
        assert resolved_thread is not None
        assert resolved_thread.agent_id == "hermes"
        assert resolved_thread.agent_profile == "m4-pma"

        thread_store = PmaThreadStore(tmp_path)
        turns = thread_store.list_turns(resolved_thread.thread_target_id, limit=10)
        assert len(turns) == 2
        assert [turn["status"] for turn in turns] == ["ok", "ok"]
        assert harness.turn_conversation_ids == [
            "hermes-backend-thread-1",
            "hermes-backend-thread-1",
        ]
    finally:
        if not release_task.done():
            release_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await release_task
        await store.close()


@pytest.mark.anyio
async def test_pma_message_create_streams_progress_before_terminal_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_hub_files(tmp_path, force=True)
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
    await store.update_pma_state(channel_id="channel-1", pma_enabled=True)
    await store.update_agent_state(channel_id="channel-1", agent="codex")

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create("stream this prompt", message_id="m-1"),
            ),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    stream_finished = asyncio.Event()

    class _FakeHarness(_BaseDiscordFakeHarness):
        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="backend-turn-1",
            )

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, turn_id, timeout
            await stream_finished.wait()
            return SimpleNamespace(
                status="ok",
                assistant_text="discord managed thread final reply",
                errors=[],
            )

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            yield format_sse(
                "app-server",
                {
                    "message": {
                        "method": "item/reasoning/summaryTextDelta",
                        "params": {
                            "itemId": "reason-1",
                            "delta": "thinking through the repo",
                        },
                    }
                },
            )
            await asyncio.sleep(1.05)
            yield format_sse(
                "app-server",
                {
                    "message": {
                        "method": "item/agentMessage/delta",
                        "params": {"delta": "partial discord managed reply"},
                    }
                },
            )
            stream_finished.set()

    harness = _FakeHarness()
    _patch_harness(monkeypatch, harness)

    try:
        await asyncio.wait_for(service.run_forever(), timeout=10)
        assert rest.edited_channel_messages
        assert any(
            "partial discord managed reply" in msg["payload"].get("content", "")
            for msg in rest.edited_channel_messages
        )
        assert any(
            msg["payload"].get("components") for msg in rest.edited_channel_messages
        )
        assert any(
            "discord managed thread final reply" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_message_create_routes_repeated_messages_through_managed_thread_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_hub_files(tmp_path, force=True)
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
    await store.update_pma_state(channel_id="channel-1", pma_enabled=True)
    await store.update_agent_state(channel_id="channel-1", agent="codex")

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create("first orchestration prompt", message_id="m-1"),
            ),
            (
                "MESSAGE_CREATE",
                _message_create("second orchestration prompt", message_id="m-2"),
            ),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class _FakeHarness(_BaseDiscordFakeHarness):
        def __init__(self) -> None:
            self.turn_prompts: list[str] = []
            self.waited_turns: list[str] = []

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            turn_id = f"backend-turn-{len(self.turn_prompts) + 1}"
            self.turn_prompts.append(prompt)
            return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, timeout
            assert isinstance(turn_id, str)
            self.waited_turns.append(turn_id)
            if turn_id == "backend-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="ok",
                    assistant_text="first orchestration reply",
                    errors=[],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="second orchestration reply",
                errors=[],
            )

    harness = _FakeHarness()
    _patch_harness(monkeypatch, harness)

    async def _release_later() -> None:
        await first_started.wait()
        await asyncio.sleep(0.05)
        release_first.set()

    release_task = asyncio.create_task(_release_later())
    try:
        await asyncio.wait_for(service.run_forever(), timeout=5)
        with anyio.fail_after(2):
            while not any(
                "second orchestration reply" in msg["payload"].get("content", "")
                for msg in rest.channel_messages
            ):
                await anyio.sleep(0.05)

        contents = [
            payload.get("content", "")
            for payload in await _collect_outbound_payloads(rest, store)
        ]
        assert any("first orchestration reply" in content for content in contents)
        assert any("second orchestration reply" in content for content in contents)

        thread_store = PmaThreadStore(tmp_path)
        threads = thread_store.list_threads(limit=10)
        assert len(threads) == 1
        turns = thread_store.list_turns(threads[0]["managed_thread_id"], limit=10)
        assert len(turns) == 2
        assert [turn["status"] for turn in turns] == ["ok", "ok"]
    finally:
        if not release_task.done():
            release_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await release_task
        await store.close()


@pytest.mark.anyio
async def test_pma_message_create_image_attachment_routes_through_managed_thread_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_hub_files(tmp_path, force=True)
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
    await store.update_pma_state(channel_id="channel-1", pma_enabled=True)
    await store.update_agent_state(channel_id="channel-1", agent="codex")

    attachment_url = "https://cdn.discordapp.com/attachments/pma-managed-image-1"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"png-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="Please review image",
                    message_id="m-1",
                    attachments=[
                        {
                            "id": "att-image-1",
                            "filename": "screen.png",
                            "content_type": "image/png",
                            "size": 9,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured_input_items: list[Optional[list[dict[str, Any]]]] = []

    class _FakeHarness(_BaseDiscordFakeHarness):
        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[Any],
            input_items: Optional[list[dict[str, Any]]] = None,
        ) -> SimpleNamespace:
            _ = (
                workspace_root,
                conversation_id,
                prompt,
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
            )
            captured_input_items.append(input_items)
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="backend-turn-1",
            )

    harness = _FakeHarness()
    _patch_harness(monkeypatch, harness)

    try:
        await service.run_forever()
        assert captured_input_items and isinstance(captured_input_items[0], list)
        items = captured_input_items[0] or []
        assert items and items[0].get("type") == "text"
        assert "<user_message>" in str(items[0].get("text") or "")
        assert any(item.get("type") == "localImage" for item in items[1:])

        thread_store = PmaThreadStore(tmp_path)
        threads = thread_store.list_threads(limit=10)
        assert len(threads) == 1
        turns = thread_store.list_turns(threads[0]["managed_thread_id"], limit=10)
        assert len(turns) == 1
        assert turns[0]["status"] == "ok"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_enqueues_outbox_when_channel_send_fails(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [("MESSAGE_CREATE", _message_create("ship it"))],
        rest=_FailingChannelRest(),
    )

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        chat_ux_snapshot: Any = None,
    ) -> str:
        return "queued reply"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        outbox = await store.list_outbox()
        assert outbox
        assert any(
            "queued reply" in item.payload_json.get("content", "") for item in outbox
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_discord_managed_thread_queue_worker_sends_placeholder_for_empty_reply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queued_started = SimpleNamespace()
    events: list[Any] = []
    begin_calls = 0

    class _Service:
        def __init__(self) -> None:
            self._spawned_tasks: list[asyncio.Task[Any]] = []
            self._config = SimpleNamespace(root=tmp_path)

        def _spawn_task(self, coro: Any) -> asyncio.Task[Any]:
            task = asyncio.create_task(coro)
            self._spawned_tasks.append(task)
            return task

        def _register_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _clear_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        async def _send_channel_message_safe(
            self,
            channel_id: str,
            payload: dict[str, Any],
            *,
            record_id: str,
        ) -> None:
            events.append((channel_id, payload, record_id))

    class _OrchestrationServiceStub:
        def get_running_execution(self, managed_thread_id: str) -> None:
            _ = managed_thread_id
            return None

    async def _fake_begin_next(
        orchestration_service: object,
        managed_thread_id: str,
    ) -> Optional[object]:
        nonlocal begin_calls
        _ = orchestration_service, managed_thread_id
        if begin_calls == 0:
            begin_calls += 1
            return queued_started
        return None

    async def _fake_run_started_execution(
        self: object,
        started: object,
        *,
        hooks: object = None,
        runtime_event_state: object = None,
    ) -> dict[str, object]:
        _ = self, hooks, runtime_event_state
        assert started is queued_started
        return {
            "status": "ok",
            "assistant_text": "",
            "managed_thread_id": "managed-thread-1",
            "managed_turn_id": "turn-1",
            "backend_thread_id": "backend-thread-1",
        }

    monkeypatch.setattr(
        discord_message_turns_module,
        "begin_next_queued_runtime_thread_execution",
        _fake_begin_next,
    )
    monkeypatch.setattr(
        managed_thread_turns_module.ManagedThreadTurnCoordinator,
        "run_started_execution",
        _fake_run_started_execution,
    )

    service = _Service()
    orchestration_service = _OrchestrationServiceStub()
    coordinator = (
        discord_message_turns_module._build_discord_managed_thread_coordinator(
            service=service,
            orchestration_service=orchestration_service,
            channel_id="channel-1",
            public_execution_error="Runtime thread failed",
            timeout_error="Runtime thread timed out",
            interrupted_error="Runtime thread interrupted",
            pma_enabled=True,
        )
    )
    coordinator.ensure_queue_worker(
        task_map=discord_message_turns_module._get_discord_thread_queue_task_map(
            service
        ),
        managed_thread_id="managed-thread-1",
        spawn_task=service._spawn_task,
        hooks=_discord_test_queue_worker_hooks(
            service,
            channel_id="channel-1",
            managed_thread_id="managed-thread-1",
            public_execution_error="Runtime thread failed",
        ),
        begin_next_execution=discord_message_turns_module.begin_next_queued_runtime_thread_execution,
    )

    await asyncio.gather(*service._spawned_tasks)

    assert events == [
        (
            "channel-1",
            {"content": "(No response text returned.)"},
            "discord-queued:managed-thread-1:turn-1",
        )
    ]


@pytest.mark.anyio
async def test_discord_managed_thread_queue_worker_formats_local_file_links(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queued_started = SimpleNamespace()
    events: list[Any] = []
    begin_calls = 0

    class _Service:
        def __init__(self) -> None:
            self._spawned_tasks: list[asyncio.Task[Any]] = []
            self._config = SimpleNamespace(root=tmp_path)

        def _spawn_task(self, coro: Any) -> asyncio.Task[Any]:
            task = asyncio.create_task(coro)
            self._spawned_tasks.append(task)
            return task

        def _register_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _clear_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        async def _send_channel_message_safe(
            self,
            channel_id: str,
            payload: dict[str, Any],
            *,
            record_id: str,
        ) -> None:
            events.append((channel_id, payload, record_id))

    class _OrchestrationServiceStub:
        def get_running_execution(self, managed_thread_id: str) -> None:
            _ = managed_thread_id
            return None

    async def _fake_begin_next(
        orchestration_service: object,
        managed_thread_id: str,
    ) -> Optional[object]:
        nonlocal begin_calls
        _ = orchestration_service, managed_thread_id
        if begin_calls == 0:
            begin_calls += 1
            return queued_started
        return None

    async def _fake_run_started_execution(
        self: object,
        started: object,
        *,
        hooks: object = None,
        runtime_event_state: object = None,
    ) -> dict[str, object]:
        _ = self, hooks, runtime_event_state
        assert started is queued_started
        return {
            "status": "ok",
            "assistant_text": (
                "Updated [archive_helpers.py](/Users/dazheng/worktree/src/archive_helpers.py) "
                "and kept [docs](https://example.com/docs)."
            ),
            "managed_thread_id": "managed-thread-1",
            "managed_turn_id": "turn-2",
            "backend_thread_id": "backend-thread-1",
        }

    monkeypatch.setattr(
        discord_message_turns_module,
        "begin_next_queued_runtime_thread_execution",
        _fake_begin_next,
    )
    monkeypatch.setattr(
        managed_thread_turns_module.ManagedThreadTurnCoordinator,
        "run_started_execution",
        _fake_run_started_execution,
    )

    service = _Service()
    orchestration_service = _OrchestrationServiceStub()
    coordinator = (
        discord_message_turns_module._build_discord_managed_thread_coordinator(
            service=service,
            orchestration_service=orchestration_service,
            channel_id="channel-1",
            public_execution_error="Runtime thread failed",
            timeout_error="Runtime thread timed out",
            interrupted_error="Runtime thread interrupted",
            pma_enabled=True,
        )
    )
    coordinator.ensure_queue_worker(
        task_map=discord_message_turns_module._get_discord_thread_queue_task_map(
            service
        ),
        managed_thread_id="managed-thread-1",
        spawn_task=service._spawn_task,
        hooks=_discord_test_queue_worker_hooks(
            service,
            channel_id="channel-1",
            managed_thread_id="managed-thread-1",
            public_execution_error="Runtime thread failed",
        ),
        begin_next_execution=discord_message_turns_module.begin_next_queued_runtime_thread_execution,
    )

    await asyncio.gather(*service._spawned_tasks)

    assert events == [
        (
            "channel-1",
            {
                "content": (
                    "Updated archive\\_helpers.py and kept "
                    "[docs](https://example.com/docs)."
                )
            },
            "discord-queued:managed-thread-1:turn-2",
        )
    ]


@pytest.mark.anyio
async def test_discord_managed_thread_coordinator_prefers_started_execution_error(
    tmp_path: Path,
) -> None:
    seed_hub_files(tmp_path, force=True)

    service = SimpleNamespace(
        _config=SimpleNamespace(root=tmp_path),
        _logger=logging.getLogger("test.discord.finalize"),
    )

    captured_result: dict[str, Any] = {}

    class _OrchestrationServiceStub:
        def get_thread_target(self, managed_thread_id: str) -> Any:
            _ = managed_thread_id
            return SimpleNamespace(backend_thread_id=None)

        def record_execution_result(
            self,
            managed_thread_id: str,
            managed_turn_id: str,
            *,
            status: str,
            assistant_text: str,
            error: Optional[str],
            backend_turn_id: Optional[str],
            transcript_turn_id: Optional[str],
        ) -> Any:
            _ = (
                managed_thread_id,
                managed_turn_id,
                assistant_text,
                backend_turn_id,
                transcript_turn_id,
            )
            captured_result["status"] = status
            captured_result["error"] = error
            return SimpleNamespace(status=status, error=error)

    started = SimpleNamespace(
        thread=SimpleNamespace(
            thread_target_id="managed-thread-1",
            backend_thread_id=None,
            agent_id="codex",
        ),
        execution=SimpleNamespace(
            execution_id="turn-1",
            status="error",
            backend_id=None,
            error=(
                "ACP subprocess emitted invalid JSON: b'  [tool] deliberating...\\n'"
            ),
        ),
        request=SimpleNamespace(
            message_text="What did we do in this session so far?",
            model=None,
            reasoning=None,
            kind="message",
        ),
        workspace_root=tmp_path,
        harness=object(),
    )

    orchestration_service = _OrchestrationServiceStub()
    coordinator = (
        discord_message_turns_module._build_discord_managed_thread_coordinator(
            service=service,
            orchestration_service=orchestration_service,
            channel_id="channel-1",
            public_execution_error="Discord PMA turn failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
            pma_enabled=True,
        )
    )

    result = await coordinator.run_started_execution(
        started,
        runtime_event_state=discord_message_turns_module.RuntimeThreadRunEventState(),
    )

    assert result.status == "error"
    assert "ACP subprocess emitted invalid JSON" in str(result.error)
    assert captured_result["status"] == "error"
    assert "ACP subprocess emitted invalid JSON" in str(captured_result["error"])


@pytest.mark.anyio
async def test_discord_managed_thread_coordinator_uses_started_execution_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _fake_finalize(**kwargs: Any) -> dict[str, Any]:
        seen["turn_preview"] = kwargs["turn_preview"]
        return {"status": "ok", "managed_turn_id": "turn-1"}

    monkeypatch.setattr(
        managed_thread_turns_module,
        "finalize_managed_thread_execution",
        _fake_finalize,
    )

    coordinator = (
        discord_message_turns_module._build_discord_managed_thread_coordinator(
            service=SimpleNamespace(
                _config=SimpleNamespace(root=tmp_path),
                _logger=logging.getLogger("test"),
            ),
            orchestration_service=SimpleNamespace(),
            channel_id="channel-1",
            public_execution_error="Discord PMA turn failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
            pma_enabled=True,
        )
    )

    started = SimpleNamespace(
        thread=SimpleNamespace(thread_target_id="managed-thread-1"),
        execution=SimpleNamespace(execution_id="turn-1"),
        request=SimpleNamespace(
            message_text="A later queued Discord prompt that should win."
        ),
        workspace_root=tmp_path,
        harness=object(),
    )

    await coordinator.run_started_execution(started)

    assert seen["turn_preview"] == discord_message_turns_module.truncate_for_discord(
        "A later queued Discord prompt that should win.",
        max_len=120,
    )


@pytest.mark.anyio
async def test_car_review_single_chunk_deletes_preview_and_sends_chunk_when_flush_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [],
        config_kwargs=dict(max_message_length=200),
    )

    async def _fake_run_turn(
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        managed_thread_surface_key: Optional[str] = None,
        suppress_managed_thread_delivery: bool = False,
        chat_ux_snapshot: Any = None,
    ) -> DiscordMessageTurnResult:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        return DiscordMessageTurnResult(
            final_message="single chunk review response",
            preview_message_id="preview-1",
        )

    async def _failing_flush_outbox_files(
        *, workspace_root: Path, channel_id: str
    ) -> None:
        _ = (workspace_root, channel_id)
        raise RuntimeError("simulated flush failure")

    logged_events: list[dict[str, Any]] = []

    def _capture_log_event(
        logger: logging.Logger,
        level: int,
        event: str,
        *,
        exc: Optional[Exception] = None,
        **fields: Any,
    ) -> None:
        _ = logger
        logged_events.append({"level": level, "event": event, "exc": exc, **fields})

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _fake_run_turn)
    monkeypatch.setattr(service, "_flush_outbox_files", _failing_flush_outbox_files)
    monkeypatch.setattr(discord_service_module, "log_event", _capture_log_event)

    try:
        await service._handle_car_review(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
            workspace_root=workspace.resolve(),
            options={},
        )
        assert rest.edited_channel_messages == []
        assert len(rest.deleted_channel_messages) == 1
        assert rest.deleted_channel_messages[0]["message_id"] == "preview-1"
        assert any(
            "single chunk review response" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert any(
            event["event"] == "discord.review.outbox_flush_failed"
            and event["level"] == logging.WARNING
            for event in logged_events
        )
        assert not any(
            event["event"] == "discord.review.preview_edit_failed"
            for event in logged_events
        )
        assert not any(
            event["event"] == "discord.review.preview_delete_failed"
            for event in logged_events
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_session_compact_reuses_preview_without_part_numbering(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [],
        config_kwargs=dict(max_message_length=120),
    )

    orchestration_service = service._discord_thread_service()
    current_thread = orchestration_service.create_thread_target(
        "codex",
        workspace.resolve(),
        display_name="discord:channel-1",
    )
    assert current_thread.backend_thread_id is None
    service._attach_discord_thread_binding(
        channel_id="channel-1",
        thread_target_id=current_thread.thread_target_id,
        agent="codex",
        repo_id=None,
        pma_enabled=False,
    )

    summary = "\n".join(
        [
            f"- compact summary detail line {idx} with enough content to wrap"
            for idx in range(1, 30)
        ]
    )

    async def _fake_run_turn(
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        **_kwargs: Any,
    ) -> DiscordMessageTurnResult:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        return DiscordMessageTurnResult(
            final_message=summary,
            preview_message_id="preview-1",
        )

    service._run_agent_turn_for_message = _fake_run_turn  # type: ignore[assignment]
    service._hub_client = _FakeCompactHubClient(orchestration_service)

    try:
        await service._handle_car_compact(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
        )
        payload = _latest_interaction_completion_payload(rest)
        assert payload["content"] == (
            "Compaction complete. Summary posted in the channel. Send your next message to continue this session."
        )
        assert rest.edited_channel_messages
        compact_preview_edit = rest.edited_channel_messages[-1]
        assert compact_preview_edit["message_id"] == "preview-1"
        assert "components" not in compact_preview_edit["payload"]
        assert rest.channel_messages

        for msg in rest.channel_messages:
            assert "components" not in msg["payload"]

        rendered_chunks = [compact_preview_edit["payload"].get("content", "")]
        rendered_chunks.extend(
            msg["payload"].get("content", "") for msg in rest.channel_messages
        )
        assert len(rendered_chunks) > 1
        assert any("Conversation Summary" in chunk for chunk in rendered_chunks)
        assert all(
            not re.match(r"^Part \d+/\d+$", (chunk.splitlines() or [""])[0].strip())
            for chunk in rendered_chunks
        )
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert "Context from previous conversation:" in binding["pending_compact_seed"]
        assert summary in binding["pending_compact_seed"]
        expected_compact_key = service._build_message_session_key(
            channel_id="channel-1",
            workspace_root=workspace.resolve(),
            pma_enabled=False,
            agent="codex",
        )
        assert binding["pending_compact_session_key"] == expected_compact_key
        active_binding = orchestration_service.get_binding(
            surface_kind="discord",
            surface_key="channel-1",
        )
        assert active_binding is not None
        replacement_thread = orchestration_service.get_thread_target(
            active_binding.thread_target_id
        )
        assert replacement_thread is not None
        assert replacement_thread.compact_seed == summary
        archived_thread = orchestration_service.get_thread_target(
            current_thread.thread_target_id
        )
        assert archived_thread is not None
        assert archived_thread.lifecycle_status == "archived"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_resolve_discord_thread_target_reuses_archived_managed_thread(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [],
        config_kwargs=dict(max_message_length=120),
    )

    try:
        orchestration_service = service._discord_thread_service()
        current_thread = orchestration_service.create_thread_target(
            "codex",
            workspace.resolve(),
            display_name="discord:channel-1",
        )
        service._attach_discord_thread_binding(
            channel_id="channel-1",
            thread_target_id=current_thread.thread_target_id,
            agent="codex",
            repo_id=None,
            pma_enabled=False,
        )
        orchestration_service.archive_thread_target(current_thread.thread_target_id)

        _service, resolved = discord_message_turns_module.resolve_discord_thread_target(
            service,
            channel_id="channel-1",
            workspace_root=workspace.resolve(),
            agent="codex",
            repo_id=None,
            resource_kind=None,
            resource_id=None,
            mode="repo",
            pma_enabled=False,
        )

        assert resolved.thread_target_id == current_thread.thread_target_id
        assert resolved.lifecycle_status == "active"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_session_compact_places_continue_button_on_last_chunk_without_preview(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [],
        config_kwargs=dict(max_message_length=120),
    )

    orchestration_service = service._discord_thread_service()
    current_thread = orchestration_service.create_thread_target(
        "codex",
        workspace.resolve(),
        display_name="discord:channel-1",
    )
    assert current_thread.backend_thread_id is None
    service._attach_discord_thread_binding(
        channel_id="channel-1",
        thread_target_id=current_thread.thread_target_id,
        agent="codex",
        repo_id=None,
        pma_enabled=False,
    )

    summary = "\n".join(
        [
            f"- compact summary detail line {idx} with enough content to wrap"
            for idx in range(1, 30)
        ]
    )

    async def _fake_run_turn(
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
        source_message_id: Optional[str] = None,
        **_kwargs: Any,
    ) -> DiscordMessageTurnResult:
        _ = (
            workspace_root,
            prompt_text,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        return DiscordMessageTurnResult(
            final_message=summary,
            preview_message_id=None,
        )

    service._run_agent_turn_for_message = _fake_run_turn  # type: ignore[assignment]
    service._hub_client = _FakeCompactHubClient(orchestration_service)

    try:
        await service._handle_car_compact(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
        )
        payload = _latest_interaction_completion_payload(rest)
        assert payload["content"] == (
            "Compaction complete. Summary posted in the channel. Send your next message to continue this session."
        )
        assert not rest.edited_channel_messages
        assert len(rest.channel_messages) > 1

        for msg in rest.channel_messages:
            assert "components" not in msg["payload"]

        rendered_chunks = [
            msg["payload"].get("content", "") for msg in rest.channel_messages
        ]
        assert any("Conversation Summary" in chunk for chunk in rendered_chunks)
        assert all(
            not re.match(r"^Part \d+/\d+$", (chunk.splitlines() or [""])[0].strip())
            for chunk in rendered_chunks
        )
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert "Context from previous conversation:" in binding["pending_compact_seed"]
        assert summary in binding["pending_compact_seed"]
        expected_compact_key = service._build_message_session_key(
            channel_id="channel-1",
            workspace_root=workspace.resolve(),
            pma_enabled=False,
            agent="codex",
        )
        assert binding["pending_compact_session_key"] == expected_compact_key
        active_binding = orchestration_service.get_binding(
            surface_kind="discord",
            surface_key="channel-1",
        )
        assert active_binding is not None
        replacement_thread = orchestration_service.get_thread_target(
            active_binding.thread_target_id
        )
        assert replacement_thread is not None
        assert replacement_thread.compact_seed == summary
        archived_thread = orchestration_service.get_thread_target(
            current_thread.thread_target_id
        )
        assert archived_thread is not None
        assert archived_thread.lifecycle_status == "archived"
    finally:
        await store.close()


def test_discord_harness_factory_accepts_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, Any]] = []

    def _fake_make_harness(ctx: Any) -> SimpleNamespace:
        agent_id = getattr(ctx, "_requested_agent_id", None)
        profile = getattr(ctx, "_requested_agent_profile", None)
        captured.append((agent_id, profile))
        return SimpleNamespace()

    descriptor = AgentDescriptor(
        id="hermes",
        name="Hermes",
        capabilities=("durable_threads",),
        make_harness=_fake_make_harness,
        healthcheck=lambda: True,
        runtime_kind="hermes",
    )
    monkeypatch.setattr(
        agent_registry_module,
        "get_registered_agents",
        lambda context=None: {"hermes": descriptor},
    )

    store = DiscordStateStore(tmp_path / "state.sqlite3")
    service = DiscordBotService(
        _config(tmp_path, max_message_length=100),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._discord_thread_orchestration_service = None
    service._discord_managed_thread_orchestration_service = None

    orch = discord_managed_thread_routing_module.build_discord_thread_orchestration_service(
        service
    )
    orch._harness_for_agent("hermes", "m4-pma")
    assert len(captured) == 1
    assert captured[0] == ("hermes", "m4-pma")


@pytest.mark.anyio
async def test_resolve_discord_thread_target_stores_agent_profile_in_metadata(
    tmp_path: Path,
) -> None:
    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [],
        config_kwargs=dict(max_message_length=120),
    )

    try:
        _orch, thread = discord_message_turns_module.resolve_discord_thread_target(
            service,
            channel_id="channel-1",
            workspace_root=workspace.resolve(),
            agent="hermes",
            agent_profile="m4-pma",
            repo_id=None,
            resource_kind=None,
            resource_id=None,
            mode="repo",
            pma_enabled=False,
        )
        assert thread.agent_id == "hermes"
        assert thread.agent_profile == "m4-pma"
    finally:
        await store.close()


async def test_resolve_discord_thread_target_reuses_legacy_hermes_runtime_alias_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = AgentDescriptor(
        id="hermes-m4-pma",
        name="Hermes M4 PMA",
        capabilities=("durable_threads",),
        make_harness=lambda _ctx: SimpleNamespace(),
        healthcheck=lambda: True,
        runtime_kind="hermes",
    )
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_registered_agents",
        lambda context=None: {
            "hermes-m4-pma": descriptor,
        },
    )
    monkeypatch.setattr(
        agent_registry_module,
        "get_registered_agents",
        lambda context=None: {
            "hermes-m4-pma": descriptor,
        },
    )
    hub_config = SimpleNamespace(
        agent_profiles=lambda agent_id: (
            {"m4-pma": SimpleNamespace(display_name="M4 PMA")}
            if agent_id == "hermes"
            else {}
        ),
        agent_binary=lambda agent_id, **kw: "hermes",
    )
    monkeypatch.setattr(
        "codex_autorunner.agents.registry._resolve_runtime_agent_config",
        lambda ctx: hub_config,
    )

    service, rest, store, workspace = await _build_discord_service(
        tmp_path,
        [],
        config_kwargs=dict(max_message_length=120),
    )

    try:
        orchestration_service = service._discord_thread_service()
        legacy_thread = orchestration_service.create_thread_target(
            "hermes-m4-pma",
            workspace.resolve(),
            display_name="discord:channel-1",
            metadata={"agent_profile": "m4-pma"},
        )
        service._attach_discord_thread_binding(
            channel_id="channel-1",
            thread_target_id=legacy_thread.thread_target_id,
            agent="hermes",
            agent_profile="m4-pma",
            repo_id=None,
            workspace_root=workspace.resolve(),
            pma_enabled=False,
        )

        _orch, resolved = discord_message_turns_module.resolve_discord_thread_target(
            service,
            channel_id="channel-1",
            workspace_root=workspace.resolve(),
            agent="hermes",
            agent_profile="m4-pma",
            repo_id=None,
            resource_kind=None,
            resource_id=None,
            mode="repo",
            pma_enabled=False,
        )

        assert resolved.thread_target_id == legacy_thread.thread_target_id
        assert resolved.agent_id == "hermes-m4-pma"
        assert resolved.agent_profile == "m4-pma"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_reset_discord_thread_binding_preserves_logical_hermes_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_registered_agents",
        lambda context=None: {
            "hermes-m4-pma": SimpleNamespace(name="Hermes M4 PMA"),
        },
    )
    hub_config = SimpleNamespace(
        agent_profiles=lambda agent_id: (
            {"m4-pma": SimpleNamespace(display_name="M4 PMA")}
            if agent_id == "hermes"
            else {}
        ),
        agent_binary=lambda agent_id, **kw: "hermes",
    )
    monkeypatch.setattr(
        "codex_autorunner.agents.registry._resolve_runtime_agent_config",
        lambda ctx: hub_config,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_agent_state(
        channel_id="channel-1",
        agent="hermes",
        agent_profile="m4-pma",
    )
    service = DiscordBotService(
        _config(tmp_path, max_message_length=120),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        orchestration_service = service._discord_thread_service()
        current_thread = orchestration_service.create_thread_target(
            "hermes",
            workspace.resolve(),
            display_name="discord:channel-1",
            metadata={"agent_profile": "m4-pma"},
        )
        service._attach_discord_thread_binding(
            channel_id="channel-1",
            thread_target_id=current_thread.thread_target_id,
            agent="hermes",
            agent_profile="m4-pma",
            repo_id=None,
            workspace_root=workspace.resolve(),
            pma_enabled=False,
        )

        (
            had_previous,
            replacement_thread_id,
        ) = await service._reset_discord_thread_binding(
            channel_id="channel-1",
            workspace_root=workspace.resolve(),
            agent="hermes",
            agent_profile="m4-pma",
            repo_id=None,
            resource_kind=None,
            resource_id=None,
            pma_enabled=False,
        )

        assert had_previous is True
        replacement_thread = orchestration_service.get_thread_target(
            replacement_thread_id
        )
        assert replacement_thread is not None
        assert replacement_thread.agent_id == "hermes"
        assert replacement_thread.agent_profile == "m4-pma"

        binding = orchestration_service.get_binding(
            surface_kind="discord",
            surface_key="channel-1",
        )
        assert binding is not None
        assert binding.thread_target_id == replacement_thread_id
        assert binding.agent_id == "hermes"
    finally:
        await store.close()
