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

import codex_autorunner.integrations.discord.message_turns as discord_message_turns_module
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
from codex_autorunner.integrations.app_server.client import (
    CodexAppServerDisconnected,
)
from codex_autorunner.integrations.app_server.threads import (
    FILE_CHAT_OPENCODE_PREFIX,
    FILE_CHAT_PREFIX,
    PMA_KEY,
    normalize_feature_key,
)
from codex_autorunner.integrations.chat.collaboration_policy import (
    CollaborationPolicy,
    build_discord_collaboration_policy,
)
from codex_autorunner.integrations.chat.compaction import build_compact_seed_prompt
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotMediaConfig,
    DiscordBotShellConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import (
    DISCORD_QUEUED_PLACEHOLDER_TEXT as QUEUED_PLACEHOLDER_TEXT,
)
from codex_autorunner.integrations.discord.service import (
    DiscordBotService,
    DiscordMessageTurnResult,
)
from codex_autorunner.integrations.discord.state import DiscordStateStore

pytestmark = pytest.mark.slow


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.original_interaction_edits: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []
        self.attachment_messages: list[dict[str, Any]] = []
        self.edited_channel_messages: list[dict[str, Any]] = []
        self.deleted_channel_messages: list[dict[str, Any]] = []
        self.typing_calls: list[str] = []
        self.message_ops: list[dict[str, Any]] = []
        self.download_requests: list[dict[str, Any]] = []
        self.attachment_data_by_url: dict[str, bytes] = {}

    async def create_interaction_response(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> None:
        self.interaction_responses.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )

    async def create_followup_message(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.followup_messages.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": dict(payload),
            }
        )
        return {"id": f"followup-{len(self.followup_messages)}"}

    async def edit_original_interaction_response(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.original_interaction_edits.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": dict(payload),
            }
        )
        return {"id": "original-response"}

    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.channel_messages.append(
            {"channel_id": channel_id, "payload": dict(payload)}
        )
        message = {"id": f"msg-{len(self.channel_messages)}"}
        self.message_ops.append(
            {
                "op": "send",
                "channel_id": channel_id,
                "payload": dict(payload),
                "message_id": message["id"],
            }
        )
        return message

    async def create_channel_message_with_attachment(
        self,
        *,
        channel_id: str,
        data: bytes,
        filename: str,
        caption: Optional[str] = None,
    ) -> dict[str, Any]:
        self.attachment_messages.append(
            {
                "channel_id": channel_id,
                "data": data,
                "filename": filename,
                "caption": caption,
            }
        )
        message = {"id": f"att-{len(self.attachment_messages)}"}
        self.message_ops.append(
            {
                "op": "send_attachment",
                "channel_id": channel_id,
                "filename": filename,
                "message_id": message["id"],
            }
        )
        return message

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.edited_channel_messages.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "payload": dict(payload),
            }
        )
        self.message_ops.append(
            {
                "op": "edit",
                "channel_id": channel_id,
                "message_id": message_id,
                "payload": dict(payload),
            }
        )
        return {"id": message_id}

    async def delete_channel_message(self, *, channel_id: str, message_id: str) -> None:
        self.deleted_channel_messages.append(
            {"channel_id": channel_id, "message_id": message_id}
        )
        self.message_ops.append(
            {
                "op": "delete",
                "channel_id": channel_id,
                "message_id": message_id,
            }
        )

    async def trigger_typing(self, *, channel_id: str) -> None:
        self.typing_calls.append(channel_id)

    async def download_attachment(
        self, *, url: str, max_size_bytes: Optional[int] = None
    ) -> bytes:
        self.download_requests.append({"url": url, "max_size_bytes": max_size_bytes})
        if url not in self.attachment_data_by_url:
            raise RuntimeError(f"no attachment fixture for {url}")
        return self.attachment_data_by_url[url]

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return commands


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


@pytest.mark.asyncio
async def test_orchestrated_turn_interrupt_send_hands_off_progress_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rest = _FakeRest()
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

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return started_execution

    async def _fake_finalize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _ = args, kwargs
        return {"status": "interrupted", "error": "Discord PMA turn interrupted"}

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
        "_finalize_discord_thread_execution",
        _fake_finalize,
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_ensure_discord_thread_queue_worker",
        lambda *args, **kwargs: None,
    )

    service = _Service()
    discord_message_turns_module.request_discord_turn_progress_reuse(
        service,
        thread_target_id="thread-1",
        source_message_id="m-2",
        acknowledgement="Message received. Switching to it now...",
    )

    result = (
        await discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            service,
            workspace_root=tmp_path,
            prompt_text="first prompt",
            source_message_id="m-1",
            agent="codex",
            model_override=None,
            reasoning_effort=None,
            session_key="session-1",
            orchestrator_channel_key="channel-1",
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
    rest = _FakeRest()
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

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return started_execution

    async def _fake_finalize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _ = args, kwargs
        return {
            "status": "ok",
            "assistant_text": "updated reply",
            "token_usage": None,
        }

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
        "_finalize_discord_thread_execution",
        _fake_finalize,
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_ensure_discord_thread_queue_worker",
        lambda *args, **kwargs: None,
    )

    service = _Service()
    discord_message_turns_module.request_discord_turn_progress_reuse(
        service,
        thread_target_id="thread-1",
        source_message_id="m-2",
        acknowledgement="Message received. Switching to it now...",
    )
    discord_message_turns_module._stash_discord_reusable_progress_message(
        service,
        thread_target_id="thread-1",
        source_message_id="m-2",
        channel_id="channel-1",
        message_id="preview-1",
    )

    result = (
        await discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            service,
            workspace_root=tmp_path,
            prompt_text="next prompt",
            source_message_id="m-2",
            agent="codex",
            model_override=None,
            reasoning_effort=None,
            session_key="session-2",
            orchestrator_channel_key="channel-1",
            mode="pma",
            pma_enabled=True,
            execution_prompt="<user_message>\nnext prompt\n</user_message>\n",
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
            self._rest = _FakeRest()

        async def _send_channel_message(
            self, channel_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            _ = channel_id, payload
            raise RuntimeError("progress send failed")

        def _register_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _clear_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return started_execution

    async def _fake_finalize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _ = args, kwargs
        return {"status": "interrupted", "error": "Discord PMA turn interrupted"}

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
        "_finalize_discord_thread_execution",
        _fake_finalize,
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_ensure_discord_thread_queue_worker",
        lambda *args, **kwargs: None,
    )

    service = _Service()
    discord_message_turns_module.request_discord_turn_progress_reuse(
        service,
        thread_target_id="thread-1",
        source_message_id="m-2",
        acknowledgement="Message received. Switching to it now...",
    )

    result = (
        await discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            service,
            workspace_root=tmp_path,
            prompt_text="first prompt",
            source_message_id="m-1",
            agent="codex",
            model_override=None,
            reasoning_effort=None,
            session_key="session-1",
            orchestrator_channel_key="channel-1",
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
    )

    assert result.send_final_message is True
    assert result.final_message == "Message received. Switching to it now..."
    assert service._discord_turn_progress_reuse_requests == {}
    assert service._discord_reusable_progress_messages == {}


class _FailingChannelRest(_FakeRest):
    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raise RuntimeError("simulated channel send failure")


class _FailingProgressRest(_FakeRest):
    def __init__(self) -> None:
        super().__init__()
        self.send_attempts = 0

    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.send_attempts += 1
        if self.send_attempts == 1:
            raise RuntimeError("simulated progress send failure")
        return await super().create_channel_message(
            channel_id=channel_id, payload=payload
        )

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raise RuntimeError("simulated progress edit failure")


class _EditFailingProgressRest(_FakeRest):
    def __init__(self) -> None:
        super().__init__()
        self.edit_attempts = 0

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.edit_attempts += 1
        raise RuntimeError("simulated progress edit failure")


class _DeleteFailingProgressRest(_FakeRest):
    async def delete_channel_message(self, *, channel_id: str, message_id: str) -> None:
        _ = (channel_id, message_id)
        raise RuntimeError("simulated progress delete failure")


class _FlakyEditProgressRest(_FakeRest):
    def __init__(self, *, fail_first_edits: int) -> None:
        super().__init__()
        self.edit_attempts = 0
        self.fail_first_edits = max(0, int(fail_first_edits))

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.edit_attempts += 1
        if self.edit_attempts <= self.fail_first_edits:
            raise RuntimeError("simulated transient progress edit failure")
        return await super().edit_channel_message(
            channel_id=channel_id,
            message_id=message_id,
            payload=payload,
        )


class _FakeGateway:
    def __init__(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self._events = events
        self.stopped = False

    async def run(self, on_dispatch) -> None:
        for event_type, payload in self._events:
            await on_dispatch(event_type, payload)
        await asyncio.sleep(0.05)

    async def stop(self) -> None:
        self.stopped = True


class _FakeOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        await asyncio.Event().wait()


class _StreamingFakeOrchestrator:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self._thread_by_key: dict[str, str] = {}

    def get_thread_id(self, session_key: str) -> Optional[str]:
        return self._thread_by_key.get(session_key)

    def set_thread_id(self, session_key: str, thread_id: str) -> None:
        self._thread_by_key[session_key] = thread_id

    async def run_turn(
        self,
        agent_id: str,
        state: Any,
        prompt: str,
        *,
        input_items: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_root: Optional[Path] = None,
    ):
        _ = (
            agent_id,
            state,
            prompt,
            input_items,
            model,
            reasoning,
            session_key,
            session_id,
            workspace_root,
        )
        for event in self._events:
            yield event


class _RaisingStreamingFakeOrchestrator(_StreamingFakeOrchestrator):
    def __init__(self, events: list[Any], exc: Exception) -> None:
        super().__init__(events)
        self._exc = exc

    async def run_turn(
        self,
        agent_id: str,
        state: Any,
        prompt: str,
        *,
        input_items: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_root: Optional[Path] = None,
    ):
        _ = (
            agent_id,
            state,
            prompt,
            input_items,
            model,
            reasoning,
            session_key,
            session_id,
            workspace_root,
        )
        for event in self._events:
            yield event
        raise self._exc


class _StreamingFakeHarness:
    display_name = "StreamingFake"
    capabilities = frozenset(
        {
            "durable_threads",
            "message_turns",
            "interrupt",
            "event_streaming",
        }
    )

    def __init__(
        self,
        events: list[Any],
        *,
        status: str = "ok",
        assistant_text: str = "done",
        errors: Optional[list[str]] = None,
        wait_for_stream: bool = False,
        stream_exception: Optional[Exception] = None,
        allow_parallel_event_stream: bool = True,
        allow_progress_event_stream: bool = True,
    ) -> None:
        self._events = events
        self._status = status
        self._assistant_text = assistant_text
        self._errors = list(errors or [])
        self._wait_for_stream = wait_for_stream
        self._stream_exception = stream_exception
        self._allow_parallel_event_stream = allow_parallel_event_stream
        self._allow_progress_event_stream = allow_progress_event_stream
        self._stream_done = asyncio.Event()

    async def ensure_ready(self, workspace_root: Path) -> None:
        _ = workspace_root

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def allows_parallel_event_stream(self) -> bool:
        return self._allow_parallel_event_stream

    def progress_event_stream(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        if not self._allow_progress_event_stream:

            async def _unsupported():
                if False:
                    yield None
                raise RuntimeError("progress event streaming disabled")

            return _unsupported()
        return self.stream_events(workspace_root, conversation_id, turn_id)

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> SimpleNamespace:
        _ = workspace_root, title
        return SimpleNamespace(id="backend-thread-1")

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> SimpleNamespace:
        _ = workspace_root
        return SimpleNamespace(id=conversation_id)

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
        return SimpleNamespace(
            conversation_id=conversation_id, turn_id="backend-turn-1"
        )

    async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        raise AssertionError("review mode should not be used in this test")

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
        if self._wait_for_stream:
            await self._stream_done.wait()
        return SimpleNamespace(
            status=self._status,
            assistant_text=self._assistant_text,
            errors=list(self._errors),
        )

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        _ = workspace_root, conversation_id, turn_id

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        _ = workspace_root, conversation_id, turn_id
        try:
            for event in self._events:
                if (
                    isinstance(event, tuple)
                    and len(event) == 2
                    and isinstance(event[0], (int, float))
                ):
                    delay = float(event[0])
                    payload = event[1]
                    if delay > 0:
                        await asyncio.sleep(delay)
                    yield payload
                    continue
                yield event
            if self._stream_exception is not None:
                raise self._stream_exception
        finally:
            self._stream_done.set()


def _patch_streaming_harness(
    monkeypatch: pytest.MonkeyPatch,
    events: list[Any],
    *,
    status: str = "ok",
    assistant_text: str = "done",
    errors: Optional[list[str]] = None,
    wait_for_stream: bool = False,
    stream_exception: Optional[Exception] = None,
    allow_parallel_event_stream: bool = True,
    allow_progress_event_stream: bool = True,
) -> _StreamingFakeHarness:
    harness = _StreamingFakeHarness(
        events,
        status=status,
        assistant_text=assistant_text,
        errors=errors,
        wait_for_stream=wait_for_stream,
        stream_exception=stream_exception,
        allow_parallel_event_stream=allow_parallel_event_stream,
        allow_progress_event_stream=allow_progress_event_stream,
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "get_registered_agents",
        lambda: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )
    return harness


class _FakeVoiceService:
    def __init__(self, transcript: str = "transcribed text") -> None:
        self.transcript = transcript
        self.calls: list[dict[str, Any]] = []

    async def transcribe_async(
        self, audio_bytes: bytes, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append({"audio_bytes": audio_bytes, **kwargs})
        return {"text": self.transcript}


def _config(
    root: Path,
    *,
    allowed_guild_ids: frozenset[str] = frozenset({"guild-1"}),
    allowed_channel_ids: frozenset[str] = frozenset({"channel-1"}),
    command_registration_enabled: bool = False,
    pma_enabled: bool = True,
    shell_enabled: bool = True,
    shell_timeout_ms: int = 120000,
    shell_max_output_chars: int = 3800,
    max_message_length: int = 2000,
    message_overflow: str = "split",
    media_enabled: bool = True,
    media_voice: bool = True,
    media_max_voice_bytes: int = 10 * 1024 * 1024,
    collaboration_policy: CollaborationPolicy | None = None,
) -> DiscordBotConfig:
    return DiscordBotConfig(
        root=root,
        enabled=True,
        bot_token_env="TOKEN_ENV",
        app_id_env="APP_ENV",
        bot_token="token",
        application_id="app-1",
        allowed_guild_ids=allowed_guild_ids,
        allowed_channel_ids=allowed_channel_ids,
        allowed_user_ids=frozenset(),
        command_registration=DiscordCommandRegistration(
            enabled=command_registration_enabled,
            scope="guild",
            guild_ids=("guild-1",),
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=max_message_length,
        message_overflow=message_overflow,
        pma_enabled=pma_enabled,
        shell=DiscordBotShellConfig(
            enabled=shell_enabled,
            timeout_ms=shell_timeout_ms,
            max_output_chars=shell_max_output_chars,
        ),
        media=DiscordBotMediaConfig(
            enabled=media_enabled,
            voice=media_voice,
            max_voice_bytes=media_max_voice_bytes,
        ),
        collaboration_policy=collaboration_policy,
    )


def _bind_interaction(path: str) -> dict[str, Any]:
    return {
        "id": "inter-bind",
        "token": "token-bind",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": "car",
            "options": [
                {
                    "type": 1,
                    "name": "bind",
                    "options": [{"type": 3, "name": "path", "value": path}],
                }
            ],
        },
    }


def _pma_interaction(subcommand: str) -> dict[str, Any]:
    return {
        "id": "inter-pma",
        "token": "token-pma",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": "pma",
            "options": [{"type": 1, "name": subcommand, "options": []}],
        },
    }


def _message_create(
    content: str = "",
    *,
    message_id: str = "m-1",
    guild_id: str = "guild-1",
    channel_id: str = "channel-1",
    attachments: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "guild_id": guild_id,
        "content": content,
        "author": {"id": "user-1", "bot": False},
        "attachments": attachments or [],
    }


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
    service = DiscordBotService(
        _config(tmp_path, max_message_length=120),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
        assert rest.original_interaction_edits
        assert (
            rest.original_interaction_edits[-1]["payload"]["content"]
            == "Compaction summary posted, but finalizing the fresh thread failed."
        )
        assert rest.channel_messages
        assert (
            rest.channel_messages[-1]["payload"]["content"]
            == "Compaction summary posted, but finalizing the fresh thread failed."
        )
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert "summary text" in binding["pending_compact_seed"]
        assert binding["pending_compact_session_key"]
        assert binding["pending_compact_session_key"] != current_thread.thread_target_id
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_session_compact_restores_previous_thread_when_seed_save_fails(
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
    service = DiscordBotService(
        _config(tmp_path, max_message_length=120),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
        assert rest.original_interaction_edits
        assert (
            rest.original_interaction_edits[-1]["payload"]["content"]
            == "Compaction summary generated, but saving the compacted context failed. Restored the previous thread; please retry."
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
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("route via ingress"))])
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    captured: dict[str, object] = {}

    async def _fake_run_turn(**kwargs: Any) -> DiscordMessageTurnResult:
        captured["session_key"] = kwargs["session_key"]
        return DiscordMessageTurnResult(final_message="handled by ingress")

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
        await service.run_forever()
        assert captured["surface_kind"] == "discord"
        assert captured["prompt_text"] == "route via ingress"
        assert isinstance(captured["session_key"], str)
        assert any(
            message["payload"]["content"] == "handled by ingress"
            for message in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_after_compact_uses_pending_seed_and_clears_it(
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
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("please continue"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
            "CODEX_AUTORUNNER_VOICE_ENABLED": "1",
            "CODEX_AUTORUNNER_VOICE_PROVIDER": "local_whisper",
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
    assert voice_config.provider == "local_whisper"
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
async def test_message_create_streaming_turn_uses_safe_progress_stream_when_parallel_streaming_is_disabled(
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
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content="thinking",
                delta_type=RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
            ),
        ],
        assistant_text="done from streaming turn",
        wait_for_stream=True,
        allow_parallel_event_stream=False,
        allow_progress_event_stream=True,
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
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
    final_text = "intermediate output two"
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
    service = DiscordBotService(
        _config(tmp_path, max_message_length=40, message_overflow="document"),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
    final_text = "done from streaming turn"
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
        assert len(rest.deleted_channel_messages) == 1
        assert rest.deleted_channel_messages[0]["message_id"] == "msg-1"
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
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
async def test_message_create_streaming_turn_recovers_if_wait_disconnects_after_completion(
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
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
async def test_message_create_streaming_turn_failure_before_completion_still_fails(
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
    service = DiscordBotService(
        _config(tmp_path, max_message_length=80),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
        assert len(rest.deleted_channel_messages) == 1
        assert rest.deleted_channel_messages[0]["message_id"] == "msg-1"
        final_sends = [
            op
            for op in rest.message_ops
            if op.get("op") == "send" and op.get("message_id") != "msg-1"
        ]
        assert len(final_sends) >= 2
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_appends_final_metrics(
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
        assert "agent codex" in final_content
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
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
        assert "agent codex" in final_content
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
        assert "agent codex" in final_content
        assert "ctx 65%" in final_content
        assert final_content.count("ctx 65%") == 1
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_fallback_preserves_multichunk_whitespace(
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
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
    rest = _FailingProgressRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
    rest = _EditFailingProgressRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
        assert 1 <= rest.edit_attempts <= 2
        assert len(rest.deleted_channel_messages) == 1
        assert rest.deleted_channel_messages[0]["message_id"] == "msg-1"
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
    rest = _DeleteFailingProgressRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
        "codex_autorunner.integrations.discord.service.subprocess.run",
        _should_not_run_shell,
    )

    try:
        with caplog.at_level(logging.INFO):
            await service.run_forever()
        assert rest.channel_messages == []
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
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("/car status"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
        "codex_autorunner.integrations.discord.service.subprocess.run",
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
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("!ls"))])
    service = DiscordBotService(
        _config(tmp_path, shell_enabled=False),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("bang-prefixed shell command should not run agent turn")

    def _should_not_run_shell(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("shell execution should stay disabled")

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.subprocess.run",
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
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("!echo long"))])
    service = DiscordBotService(
        _config(tmp_path, shell_max_output_chars=12),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    long_output = "1234567890abcdefghijklmnopqrstuvwxyz\n"

    def _fake_shell_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, long_output, "")

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.subprocess.run",
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
        assert "plan next sprint" in captured[0]["prompt_text"]
        assert captured[0]["prompt_text"] != "plan next sprint"
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
        assert "PMA File Inbox:" in prompt
        assert "next_action: process_uploaded_file" in prompt
        assert "ticket-pack.zip" in prompt
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
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("needs approval"))])
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
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("!pwd"))])
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
        "codex_autorunner.integrations.discord.service.subprocess.run",
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
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("needs approval"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
        input_items: Optional[list[dict[str, Any]]] = None,
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
        queued_notice = next(
            (
                msg["payload"]
                for msg in rest.channel_messages
                if QUEUED_PLACEHOLDER_TEXT in msg["payload"].get("content", "")
            ),
            None,
        )
        assert queued_notice is not None
        assert queued_notice.get("components")
        buttons = queued_notice["components"][0]["components"]
        assert [button["custom_id"] for button in buttons] == [
            "queue_cancel:m-2",
            "queue_interrupt_send:m-2",
        ]
        contents = [msg["payload"].get("content", "") for msg in rest.channel_messages]
        assert any(
            "Queued (waiting for available worker...)" in content
            for content in contents
        )
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

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        def __init__(self) -> None:
            self.turn_prompts: list[str] = []
            self.resume_conversation_calls: list[tuple[Path, str]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            self.resume_conversation_calls.append((workspace_root, conversation_id))
            return SimpleNamespace(id=conversation_id)

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
            turn_id = f"backend-turn-{len(self.turn_prompts) + 1}"
            self.turn_prompts.append(prompt)
            return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

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

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        discord_message_turns_module,
        "get_registered_agents",
        lambda: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

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

        contents = [msg["payload"].get("content", "") for msg in rest.channel_messages]
        assert any(
            "Queued (waiting for available worker...)" in content
            for content in contents
        )
        assert any("first orchestration reply" in content for content in contents)
        assert any("second orchestration reply" in content for content in contents)
        assert rest.typing_calls.count("channel-1") >= 2

        thread_store = discord_message_turns_module.PmaThreadStore(tmp_path)
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
        assert harness.resume_conversation_calls == [
            (workspace.resolve(), "backend-thread-1")
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

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

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

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

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

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

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
    monkeypatch.setattr(
        discord_message_turns_module,
        "get_registered_agents",
        lambda: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    try:
        await asyncio.wait_for(service.run_forever(), timeout=5)
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

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        def __init__(self) -> None:
            self.turn_prompts: list[str] = []
            self.waited_turns: list[str] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

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

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

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

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        discord_message_turns_module,
        "get_registered_agents",
        lambda: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

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

        contents = [msg["payload"].get("content", "") for msg in rest.channel_messages]
        assert any(
            "Queued (waiting for available worker...)" in content
            for content in contents
        )
        assert any("first orchestration reply" in content for content in contents)
        assert any("second orchestration reply" in content for content in contents)

        thread_store = discord_message_turns_module.PmaThreadStore(tmp_path)
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

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="backend-thread-1")

        async def resume_conversation(
            self, workspace_root: Path, conversation_id: str
        ) -> SimpleNamespace:
            _ = workspace_root
            return SimpleNamespace(id=conversation_id)

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

        async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise AssertionError("review mode should not be used in this test")

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: Optional[str],
            *,
            timeout: Optional[float] = None,
        ) -> SimpleNamespace:
            _ = workspace_root, conversation_id, turn_id, timeout
            return SimpleNamespace(
                status="ok",
                assistant_text="discord managed attachment reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        discord_message_turns_module,
        "get_registered_agents",
        lambda: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    try:
        await service.run_forever()
        assert captured_input_items and isinstance(captured_input_items[0], list)
        items = captured_input_items[0] or []
        assert items and items[0].get("type") == "text"
        assert "<user_message>" in str(items[0].get("text") or "")
        assert any(item.get("type") == "localImage" for item in items[1:])

        thread_store = discord_message_turns_module.PmaThreadStore(tmp_path)
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
    rest = _FailingChannelRest()
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
) -> None:
    queued_started = SimpleNamespace()
    events: list[Any] = []
    begin_calls = 0

    class _Service:
        def __init__(self) -> None:
            self._spawned_tasks: list[asyncio.Task[Any]] = []

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

    async def _fake_finalize(
        service: object,
        *,
        orchestration_service: object,
        started: object,
        channel_id: str,
        public_execution_error: str,
        timeout_error: str,
        interrupted_error: str,
    ) -> dict[str, object]:
        _ = (
            service,
            orchestration_service,
            channel_id,
            public_execution_error,
            timeout_error,
            interrupted_error,
        )
        assert started is queued_started
        return {
            "status": "ok",
            "assistant_text": "",
            "managed_turn_id": "turn-1",
        }

    monkeypatch.setattr(
        discord_message_turns_module,
        "begin_next_queued_runtime_thread_execution",
        _fake_begin_next,
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_finalize_discord_thread_execution",
        _fake_finalize,
    )

    service = _Service()
    discord_message_turns_module._ensure_discord_thread_queue_worker(
        service,
        orchestration_service=_OrchestrationServiceStub(),
        managed_thread_id="managed-thread-1",
        channel_id="channel-1",
        public_execution_error="Runtime thread failed",
        timeout_error="Runtime thread timed out",
        interrupted_error="Runtime thread interrupted",
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
) -> None:
    queued_started = SimpleNamespace()
    events: list[Any] = []
    begin_calls = 0

    class _Service:
        def __init__(self) -> None:
            self._spawned_tasks: list[asyncio.Task[Any]] = []

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

    async def _fake_finalize(
        service: object,
        *,
        orchestration_service: object,
        started: object,
        channel_id: str,
        public_execution_error: str,
        timeout_error: str,
        interrupted_error: str,
    ) -> dict[str, object]:
        _ = (
            service,
            orchestration_service,
            channel_id,
            public_execution_error,
            timeout_error,
            interrupted_error,
        )
        assert started is queued_started
        return {
            "status": "ok",
            "assistant_text": (
                "Updated [archive_helpers.py](/Users/dazheng/worktree/src/archive_helpers.py) "
                "and kept [docs](https://example.com/docs)."
            ),
            "managed_turn_id": "turn-2",
        }

    monkeypatch.setattr(
        discord_message_turns_module,
        "begin_next_queued_runtime_thread_execution",
        _fake_begin_next,
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_finalize_discord_thread_execution",
        _fake_finalize,
    )

    service = _Service()
    discord_message_turns_module._ensure_discord_thread_queue_worker(
        service,
        orchestration_service=_OrchestrationServiceStub(),
        managed_thread_id="managed-thread-1",
        channel_id="channel-1",
        public_execution_error="Runtime thread failed",
        timeout_error="Runtime thread timed out",
        interrupted_error="Runtime thread interrupted",
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
async def test_car_review_single_chunk_deletes_preview_and_sends_chunk_when_flush_fails(
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
    service = DiscordBotService(
        _config(tmp_path, max_message_length=200),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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
    service = DiscordBotService(
        _config(tmp_path, max_message_length=120),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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

    try:
        await service._handle_car_compact(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
        )
        assert rest.original_interaction_edits
        assert (
            rest.original_interaction_edits[-1]["payload"]["content"]
            == "Compaction complete. Summary posted in the channel. Send your next message to continue this session."
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
        assert binding["pending_compact_session_key"]
        assert binding["pending_compact_session_key"] != current_thread.thread_target_id
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
    service = DiscordBotService(
        _config(tmp_path, max_message_length=120),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
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

    try:
        await service._handle_car_compact(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
        )
        assert rest.original_interaction_edits
        assert (
            rest.original_interaction_edits[-1]["payload"]["content"]
            == "Compaction complete. Summary posted in the channel. Send your next message to continue this session."
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
        assert binding["pending_compact_session_key"]
        assert binding["pending_compact_session_key"] != current_thread.thread_target_id
        archived_thread = orchestration_service.get_thread_target(
            current_thread.thread_target_id
        )
        assert archived_thread is not None
        assert archived_thread.lifecycle_status == "archived"
    finally:
        await store.close()
