import asyncio
import contextlib
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import anyio
import httpx
import pytest

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.core.orchestration.runtime_threads import (
    RUNTIME_THREAD_INTERRUPTED_ERROR,
    RUNTIME_THREAD_TIMEOUT_ERROR,
)
from codex_autorunner.core.pma_context import default_pma_prompt_state_path
from codex_autorunner.core.sse import format_sse
from codex_autorunner.integrations.app_server.client import (
    CodexAppServerDisconnected,
    CodexAppServerResponseError,
)
from codex_autorunner.integrations.app_server.threads import (
    PMA_KEY,
    PMA_OPENCODE_KEY,
    AppServerThreadRegistry,
)
from codex_autorunner.integrations.telegram.adapter import (
    TelegramDocument,
    TelegramMessage,
    TelegramPhotoSize,
    TelegramVoice,
)
from codex_autorunner.integrations.telegram.config import TelegramBotDefaults
from codex_autorunner.integrations.telegram.handlers import (
    messages as telegram_messages_module,
)
from codex_autorunner.integrations.telegram.handlers.commands import (
    build_command_specs,
)
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as execution_commands_module,
)
from codex_autorunner.integrations.telegram.handlers.commands import (
    workspace as workspace_commands_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.execution import (
    ExecutionCommands,
    _TurnRunResult,
)
from codex_autorunner.integrations.telegram.handlers.commands.workspace import (
    WorkspaceCommands,
)
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
    _RuntimeStub,
)
from codex_autorunner.integrations.telegram.handlers.messages import (
    handle_media_message,
)
from codex_autorunner.integrations.telegram.handlers.selections import SelectionState
from codex_autorunner.integrations.telegram.helpers import _format_help_text
from codex_autorunner.integrations.telegram.notifications import (
    TelegramNotificationHandlers,
)
from codex_autorunner.integrations.telegram.state import (
    TelegramTopicRecord,
    ThreadSummary,
)


class _RouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record


def test_sanitize_runtime_thread_result_error_preserves_sanitized_detail() -> None:
    assert (
        execution_commands_module._sanitize_runtime_thread_result_error(
            "backend exploded with private detail",
            public_error="Telegram PMA turn failed",
            timeout_error="Telegram PMA turn timed out",
            interrupted_error="Telegram PMA turn interrupted",
        )
        == "backend exploded with private detail"
    )


def test_sanitize_runtime_thread_result_error_maps_timeout_to_surface_timeout() -> None:
    assert (
        execution_commands_module._sanitize_runtime_thread_result_error(
            RUNTIME_THREAD_TIMEOUT_ERROR,
            public_error="Telegram PMA turn failed",
            timeout_error="Telegram PMA turn timed out",
            interrupted_error="Telegram PMA turn interrupted",
        )
        == "Telegram PMA turn timed out"
    )


def test_sanitize_runtime_thread_result_error_maps_interrupted_to_surface_interrupted() -> (
    None
):
    assert (
        execution_commands_module._sanitize_runtime_thread_result_error(
            RUNTIME_THREAD_INTERRUPTED_ERROR,
            public_error="Telegram PMA turn failed",
            timeout_error="Telegram PMA turn timed out",
            interrupted_error="Telegram PMA turn interrupted",
        )
        == "Telegram PMA turn interrupted"
    )


class _ExecutionStub(ExecutionCommands):
    def __init__(self, record: TelegramTopicRecord, hub_root: Path) -> None:
        self._logger = logging.getLogger("test")
        self._router = _RouterStub(record)
        self._hub_root = hub_root
        self._hub_supervisor = None
        self._hub_thread_registry = None
        self._turn_semaphore = asyncio.Semaphore(1)
        self._captured: dict[str, object] = {}
        self._config = SimpleNamespace(
            agent_turn_timeout_seconds={"codex": None, "opencode": None}
        )

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    def _ensure_turn_semaphore(self) -> asyncio.Semaphore:
        return self._turn_semaphore

    async def _prepare_turn_placeholder(
        self,
        message: TelegramMessage,
        *,
        placeholder_id: Optional[int],
        send_placeholder: bool,
        queued: bool,
    ) -> Optional[int]:
        return None

    async def _execute_codex_turn(
        self,
        message: TelegramMessage,
        runtime: object,
        record: TelegramTopicRecord,
        prompt_text: str,
        thread_id: Optional[str],
        key: str,
        turn_semaphore: asyncio.Semaphore,
        input_items: Optional[list[dict[str, object]]],
        *,
        placeholder_id: Optional[int],
        placeholder_text: str,
        send_failure_response: bool,
        allow_new_thread: bool,
        missing_thread_message: Optional[str],
        transcript_message_id: Optional[int],
        transcript_text: Optional[str],
        pma_thread_registry: Optional[object] = None,
        pma_thread_key: Optional[str] = None,
    ) -> _TurnRunResult:
        self._captured["prompt_text"] = prompt_text
        self._captured["workspace_path"] = record.workspace_path
        self._captured["input_items"] = input_items
        return _TurnRunResult(
            record=record,
            thread_id=thread_id,
            turn_id="turn-1",
            response="ok",
            placeholder_id=None,
            elapsed_seconds=0.0,
            token_usage=None,
            transcript_message_id=None,
            transcript_text=None,
        )

    def _effective_agent(self, _record: TelegramTopicRecord) -> str:
        return "codex"

    def _effective_agent_state(self, _record: TelegramTopicRecord) -> tuple[str, None]:
        return "codex", None


@pytest.mark.anyio
async def test_pma_prompt_routing_uses_hub_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    prompt_path = hub_root / ".codex-autorunner" / "pma" / "prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("PMA system prompt", encoding="utf-8")
    inbox_dir = hub_root / ".codex-autorunner" / "filebox" / "inbox"
    outbox_dir = hub_root / ".codex-autorunner" / "filebox" / "outbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    outbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / "input.txt").write_text("inbox", encoding="utf-8")
    (outbox_dir / "output.txt").write_text("outbox", encoding="utf-8")

    class _LifecycleStoreStub:
        def get_unprocessed(self, limit: int = 20) -> list:
            return []

    class _HubSupervisorStub:
        def __init__(self) -> None:
            self.hub_config = SimpleNamespace(pma=None)
            self.lifecycle_store = _LifecycleStoreStub()

        def list_repos(self) -> list:
            return []

    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None)
    handler = _ExecutionStub(record, hub_root)
    handler._hub_supervisor = _HubSupervisorStub()
    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=123,
        thread_id=None,
        from_user_id=456,
        text="hello",
        date=None,
        is_topic_message=False,
    )

    result = await handler._run_turn_and_collect_result(
        message,
        runtime=SimpleNamespace(),
        text_override=None,
        send_placeholder=False,
    )

    assert isinstance(result, _TurnRunResult)
    assert handler._captured["workspace_path"] == str(hub_root)
    prompt_text = handler._captured["prompt_text"]
    assert "<hub_snapshot>" in prompt_text
    assert "<user_message>" in prompt_text
    assert "hello" in prompt_text
    snapshot_text = prompt_text.split("<hub_snapshot>\n", 1)[1].split(
        "\n</hub_snapshot>", 1
    )[0]
    assert "PMA File Inbox:" in snapshot_text
    assert "- inbox: [input.txt]" in snapshot_text
    assert "- outbox: [output.txt]" in snapshot_text


@pytest.mark.anyio
async def test_pma_prompt_routing_preserves_native_input_items(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    image_path = hub_root / "image.png"
    image_path.write_bytes(b"png-bytes")

    class _LifecycleStoreStub:
        def get_unprocessed(self, limit: int = 20) -> list:
            return []

    class _HubSupervisorStub:
        def __init__(self) -> None:
            self.hub_config = SimpleNamespace(pma=None)
            self.lifecycle_store = _LifecycleStoreStub()

        def list_repos(self) -> list:
            return []

    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None)
    handler = _ExecutionStub(record, hub_root)
    handler._hub_supervisor = _HubSupervisorStub()
    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=123,
        thread_id=None,
        from_user_id=456,
        text="review this image",
        date=None,
        is_topic_message=False,
    )
    input_items = [
        {"type": "text", "text": "review this image"},
        {"type": "localImage", "path": str(image_path)},
    ]

    result = await handler._run_turn_and_collect_result(
        message,
        runtime=SimpleNamespace(),
        input_items=input_items,
        send_placeholder=False,
    )

    assert isinstance(result, _TurnRunResult)
    captured = handler._captured.get("input_items")
    assert captured == input_items


@pytest.mark.anyio
async def test_pma_managed_thread_turn_forwards_yolo_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None, agent="codex")
    handler = _ExecutionStub(record, tmp_path)
    handler._config = SimpleNamespace(
        root=tmp_path,
        defaults=TelegramBotDefaults(
            approval_mode="yolo",
            approval_policy="on-request",
            sandbox_policy="workspaceWrite",
            yolo_approval_policy="never",
            yolo_sandbox_policy="dangerFullAccess",
        ),
        agent_turn_timeout_seconds={"codex": None, "opencode": None},
    )
    handler._spawn_task = lambda coro: None
    handler._effective_policies = (
        lambda current_record: WorkspaceCommands._effective_policies(
            handler, current_record
        )
    )
    captured: dict[str, Any] = {}

    async def _fake_run(_handlers: Any, **kwargs: Any) -> _TurnRunResult:
        captured["approval_policy"] = kwargs["approval_policy"]
        captured["sandbox_policy"] = kwargs["sandbox_policy"]
        return _TurnRunResult(
            record=kwargs["record"],
            thread_id="managed-thread-1",
            turn_id="managed-turn-1",
            response="ok",
            placeholder_id=None,
            elapsed_seconds=0.0,
            token_usage=None,
            transcript_message_id=None,
            transcript_text=None,
        )

    monkeypatch.setattr(
        execution_commands_module, "_run_telegram_managed_thread_turn", _fake_run
    )
    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=123,
        thread_id=456,
        from_user_id=789,
        text="hello",
        date=None,
        is_topic_message=True,
    )

    result = await handler._run_turn_and_collect_result(
        message,
        runtime=SimpleNamespace(),
        text_override=None,
        send_placeholder=False,
    )

    assert isinstance(result, _TurnRunResult)
    assert captured["approval_policy"] == "never"
    assert captured["sandbox_policy"] == "dangerFullAccess"


@pytest.mark.anyio
async def test_pma_managed_thread_turn_forwards_non_yolo_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        agent="codex",
        approval_mode="read-only",
    )
    handler = _ExecutionStub(record, tmp_path)
    handler._config = SimpleNamespace(
        root=tmp_path,
        defaults=TelegramBotDefaults(
            approval_mode="yolo",
            approval_policy="on-request",
            sandbox_policy="workspaceWrite",
            yolo_approval_policy="never",
            yolo_sandbox_policy="dangerFullAccess",
        ),
        agent_turn_timeout_seconds={"codex": None, "opencode": None},
    )
    handler._spawn_task = lambda coro: None
    handler._effective_policies = (
        lambda current_record: WorkspaceCommands._effective_policies(
            handler, current_record
        )
    )
    captured: dict[str, Any] = {}

    async def _fake_run(_handlers: Any, **kwargs: Any) -> _TurnRunResult:
        captured["approval_policy"] = kwargs["approval_policy"]
        captured["sandbox_policy"] = kwargs["sandbox_policy"]
        return _TurnRunResult(
            record=kwargs["record"],
            thread_id="managed-thread-1",
            turn_id="managed-turn-1",
            response="ok",
            placeholder_id=None,
            elapsed_seconds=0.0,
            token_usage=None,
            transcript_message_id=None,
            transcript_text=None,
        )

    monkeypatch.setattr(
        execution_commands_module, "_run_telegram_managed_thread_turn", _fake_run
    )
    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=123,
        thread_id=456,
        from_user_id=789,
        text="hello",
        date=None,
        is_topic_message=True,
    )

    result = await handler._run_turn_and_collect_result(
        message,
        runtime=SimpleNamespace(),
        text_override=None,
        send_placeholder=False,
    )

    assert isinstance(result, _TurnRunResult)
    assert captured["approval_policy"] == "on-request"
    assert captured["sandbox_policy"] == "readOnly"


@pytest.mark.anyio
async def test_telegram_text_messages_route_through_orchestration_ingress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict[str, object] = {}

    class _RouterStub:
        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return TelegramTopicRecord(
                workspace_path=str(workspace),
                pma_enabled=False,
                agent="codex",
            )

        def runtime_for(self, _key: str) -> object:
            return SimpleNamespace()

    class _HandlerStub:
        def __init__(self) -> None:
            self._router = _RouterStub()
            self._logger = logging.getLogger("test")
            self._config = SimpleNamespace(trigger_mode="all")
            self._pending_questions = {}
            self._resume_options = {}
            self._bind_options = {}
            self._flow_run_options = {}
            self._agent_options = {}
            self._model_options = {}
            self._model_pending = {}
            self._review_commit_options = {}
            self._review_commit_subjects = {}
            self._pending_review_custom = {}
            self._ticket_flow_pause_targets = {}
            self._bot_username = None
            self._command_specs = {}

        async def _resolve_topic_key(
            self, chat_id: int, thread_id: Optional[int]
        ) -> str:
            return f"{chat_id}:{thread_id}"

        def _get_paused_ticket_flow(
            self, _workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            return None

        def _enqueue_topic_work(self, _key: str, work):  # type: ignore[no-untyped-def]
            asyncio.get_running_loop().create_task(work())

        def _wrap_placeholder_work(self, **kwargs):  # type: ignore[no-untyped-def]
            return kwargs["work"]

        async def _send_message(self, *_args, **_kwargs) -> None:
            return None

        def _handle_pending_resume(self, *_args, **_kwargs) -> bool:
            return False

        def _handle_pending_bind(self, *_args, **_kwargs) -> bool:
            return False

        async def _handle_pending_review_commit(self, *_args, **_kwargs) -> bool:
            return False

        async def _handle_pending_review_custom(self, *_args, **_kwargs) -> bool:
            return False

        async def _dismiss_review_custom_prompt(self, *_args, **_kwargs) -> None:
            return None

    class _IngressStub:
        async def submit_message(self, request, **kwargs):  # type: ignore[no-untyped-def]
            captured["request"] = request
            captured["callbacks"] = set(kwargs)
            return SimpleNamespace(route="thread", thread_result=None)

    monkeypatch.setattr(
        telegram_messages_module,
        "build_surface_orchestration_ingress",
        lambda **_: _IngressStub(),
    )

    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=111,
        thread_id=222,
        from_user_id=333,
        text="hello",
        date=None,
        is_topic_message=True,
    )
    await telegram_messages_module.handle_message_inner(_HandlerStub(), message)
    await asyncio.sleep(0)

    request = captured.get("request")
    assert request is not None
    assert request.surface_kind == "telegram"
    assert request.prompt_text == "hello"
    assert request.workspace_root == workspace
    assert captured["callbacks"] == {
        "resolve_paused_flow_target",
        "submit_flow_reply",
        "submit_thread_message",
    }


@pytest.mark.anyio
async def test_telegram_opencode_turn_routes_through_managed_thread_without_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        pma_enabled=False,
        agent="opencode",
    )
    handler = _ExecutionStub(record, tmp_path)
    captured: dict[str, object] = {}

    async def _fake_run(_handlers: Any, **kwargs: Any) -> _TurnRunResult:
        captured.update(kwargs)
        return _TurnRunResult(
            record=kwargs["record"],
            thread_id="managed-thread-1",
            turn_id="managed-turn-1",
            response="ok",
            placeholder_id=None,
            elapsed_seconds=0.0,
            token_usage=None,
            transcript_message_id=None,
            transcript_text=None,
        )

    async def _legacy_opencode_turn(*_args: Any, **_kwargs: Any) -> _TurnRunResult:
        raise AssertionError("legacy opencode execution path should not be used")

    monkeypatch.setattr(
        execution_commands_module, "_run_telegram_managed_thread_turn", _fake_run
    )
    monkeypatch.setattr(_ExecutionStub, "_execute_opencode_turn", _legacy_opencode_turn)
    handler._effective_agent = lambda _record: "opencode"
    handler._effective_policies = lambda _record: (None, None)
    handler._files_inbox_dir = lambda _workspace, _topic_key: tmp_path / "inbox"
    handler._files_outbox_pending_dir = (
        lambda _workspace, _topic_key: tmp_path / "outbox"
    )
    handler._files_topic_dir = lambda _workspace, _topic_key: tmp_path / "topic"
    handler._config.media = SimpleNamespace(max_file_bytes=1024)
    handler._config.defaults = SimpleNamespace(
        policies_for_mode=lambda _mode: (None, None)
    )
    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=123,
        thread_id=456,
        from_user_id=789,
        text="hello",
        date=None,
        is_topic_message=True,
    )

    result = await handler._run_turn_and_collect_result(
        message,
        runtime=SimpleNamespace(),
        text_override=None,
        send_placeholder=False,
    )

    assert isinstance(result, _TurnRunResult)
    assert captured["mode"] == "repo"
    assert captured["pma_enabled"] is False
    assert captured["execution_prompt"] == captured["prompt_text"]
    assert captured["record"] is record


@pytest.mark.anyio
async def test_pma_media_uses_hub_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None)
    sent: list[str] = []
    captured: dict[str, object] = {}

    class _MediaRouterStub:
        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return record

    class _MediaHandlerStub:
        def __init__(self) -> None:
            self._hub_root = hub_root
            self._router = _MediaRouterStub()
            self._logger = logging.getLogger("test")
            self._config = SimpleNamespace(
                media=SimpleNamespace(
                    enabled=True,
                    images=True,
                    voice=True,
                    files=True,
                    max_image_bytes=10_000_000,
                    max_voice_bytes=10_000_000,
                    max_file_bytes=10_000_000,
                ),
                ticket_flow_auto_resume=False,
            )
            self._ticket_flow_pause_targets = {}
            self._ticket_flow_bridge = SimpleNamespace(
                auto_resume_run=lambda *_, **__: None
            )
            self._bot_username = None

        async def _resolve_topic_key(
            self, chat_id: int, thread_id: Optional[int]
        ) -> str:
            return f"{chat_id}:{thread_id}"

        async def _send_message(
            self,
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int],
            reply_to: Optional[int],
        ) -> None:
            sent.append(text)

        def _get_paused_ticket_flow(
            self, _workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            return None

        async def _handle_file_message(
            self,
            message: TelegramMessage,
            runtime: object,
            record_arg: TelegramTopicRecord,
            candidate: object,
            caption_text: str,
            *,
            placeholder_id: Optional[int] = None,
        ) -> None:
            captured["workspace_path"] = record_arg.workspace_path
            captured["caption"] = caption_text
            captured["kind"] = "file"

    handler = _MediaHandlerStub()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=111,
        thread_id=222,
        from_user_id=333,
        text=None,
        date=None,
        is_topic_message=True,
        document=TelegramDocument(
            file_id="file-1",
            file_unique_id=None,
            file_name="notes.txt",
            mime_type="text/plain",
            file_size=10,
        ),
        caption="please review",
    )
    await handle_media_message(
        handler, message, runtime=object(), caption_text="please review"
    )

    assert not sent  # no "Topic not bound" error
    assert captured["workspace_path"] == str(hub_root)
    assert captured["caption"] == "please review"
    assert captured["kind"] == "file"


@pytest.mark.anyio
async def test_telegram_media_messages_route_through_orchestration_ingress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict[str, object] = {}

    class _RouterStub:
        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return TelegramTopicRecord(
                workspace_path=str(workspace),
                pma_enabled=False,
                agent="codex",
            )

    class _HandlerStub:
        def __init__(self) -> None:
            self._router = _RouterStub()
            self._logger = logging.getLogger("test")
            self._config = SimpleNamespace(
                media=SimpleNamespace(
                    enabled=True,
                    images=True,
                    voice=True,
                    files=True,
                    max_image_bytes=10_000_000,
                    max_voice_bytes=10_000_000,
                    max_file_bytes=10_000_000,
                )
            )
            self._ticket_flow_pause_targets = {}
            self._bot_username = None

        async def _resolve_topic_key(
            self, chat_id: int, thread_id: Optional[int]
        ) -> str:
            return f"{chat_id}:{thread_id}"

        def _get_paused_ticket_flow(
            self, _workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            return None

        async def _send_message(self, *_args, **_kwargs) -> None:
            return None

    class _IngressStub:
        async def submit_message(self, request, **kwargs):  # type: ignore[no-untyped-def]
            captured["request"] = request
            captured["callbacks"] = set(kwargs)
            return SimpleNamespace(route="thread", thread_result=None)

    monkeypatch.setattr(
        telegram_messages_module,
        "build_surface_orchestration_ingress",
        lambda **_: _IngressStub(),
    )

    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=111,
        thread_id=222,
        from_user_id=333,
        text=None,
        date=None,
        is_topic_message=True,
        document=TelegramDocument(
            file_id="file-1",
            file_unique_id=None,
            file_name="notes.txt",
            mime_type="text/plain",
            file_size=10,
        ),
        caption="please review",
    )
    await handle_media_message(
        _HandlerStub(), message, runtime=object(), caption_text="please review"
    )

    request = captured.get("request")
    assert request is not None
    assert request.surface_kind == "telegram"
    assert request.prompt_text == "please review"
    assert request.workspace_root == workspace


@pytest.mark.anyio
async def test_pma_voice_uses_hub_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None)
    sent: list[str] = []
    captured: dict[str, object] = {}

    class _VoiceRouterStub:
        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return record

    class _VoiceHandlerStub:
        def __init__(self) -> None:
            self._hub_root = hub_root
            self._router = _VoiceRouterStub()
            self._logger = logging.getLogger("test")
            self._config = SimpleNamespace(
                media=SimpleNamespace(
                    enabled=True,
                    images=True,
                    voice=True,
                    files=True,
                    max_image_bytes=10_000_000,
                    max_voice_bytes=10_000_000,
                    max_file_bytes=10_000_000,
                ),
                ticket_flow_auto_resume=False,
            )
            self._ticket_flow_pause_targets = {}
            self._ticket_flow_bridge = SimpleNamespace(
                auto_resume_run=lambda *_, **__: None
            )
            self._bot_username = None

        async def _resolve_topic_key(
            self, chat_id: int, thread_id: Optional[int]
        ) -> str:
            return f"{chat_id}:{thread_id}"

        async def _send_message(
            self,
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int],
            reply_to: Optional[int],
        ) -> None:
            sent.append(text)

        def _get_paused_ticket_flow(
            self, _workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            return None

        async def _handle_voice_message(
            self,
            message: TelegramMessage,
            runtime: object,
            record_arg: TelegramTopicRecord,
            candidate: object,
            caption_text: str,
            *,
            placeholder_id: Optional[int] = None,
        ) -> None:
            captured["workspace_path"] = record_arg.workspace_path
            captured["caption"] = caption_text
            captured["kind"] = "voice"

    handler = _VoiceHandlerStub()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=111,
        thread_id=222,
        from_user_id=333,
        text=None,
        date=None,
        is_topic_message=True,
        voice=TelegramVoice("voice-1", None, 3, "audio/ogg", 100),
        caption="voice note",
    )
    await handle_media_message(
        handler, message, runtime=object(), caption_text="voice note"
    )

    assert not sent  # no "Topic not bound" error
    assert captured["workspace_path"] == str(hub_root)
    assert captured["caption"] == "voice note"
    assert captured["kind"] == "voice"


@pytest.mark.anyio
async def test_pma_image_uses_hub_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None)
    sent: list[str] = []
    captured: dict[str, object] = {}

    class _ImageRouterStub:
        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return record

    class _ImageHandlerStub:
        def __init__(self) -> None:
            self._hub_root = hub_root
            self._router = _ImageRouterStub()
            self._logger = logging.getLogger("test")
            self._config = SimpleNamespace(
                media=SimpleNamespace(
                    enabled=True,
                    images=True,
                    voice=True,
                    files=True,
                    max_image_bytes=10_000_000,
                    max_voice_bytes=10_000_000,
                    max_file_bytes=10_000_000,
                ),
                ticket_flow_auto_resume=False,
            )
            self._ticket_flow_pause_targets = {}
            self._ticket_flow_bridge = SimpleNamespace(
                auto_resume_run=lambda *_, **__: None
            )
            self._bot_username = None

        async def _resolve_topic_key(
            self, chat_id: int, thread_id: Optional[int]
        ) -> str:
            return f"{chat_id}:{thread_id}"

        async def _send_message(
            self,
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int],
            reply_to: Optional[int],
        ) -> None:
            sent.append(text)

        def _get_paused_ticket_flow(
            self, _workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            return None

        async def _handle_image_message(
            self,
            message: TelegramMessage,
            runtime: object,
            record_arg: TelegramTopicRecord,
            candidate: object,
            caption_text: str,
            *,
            placeholder_id: Optional[int] = None,
        ) -> None:
            _ = message, runtime, candidate, placeholder_id
            captured["workspace_path"] = record_arg.workspace_path
            captured["caption"] = caption_text
            captured["kind"] = "image"

    handler = _ImageHandlerStub()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=111,
        thread_id=222,
        from_user_id=333,
        text=None,
        date=None,
        is_topic_message=True,
        photos=(TelegramPhotoSize("photo-1", None, 1024, 768, 100),),
        caption="please inspect",
    )
    await handle_media_message(
        handler, message, runtime=object(), caption_text="please inspect"
    )

    assert not sent  # no "Topic not bound" error
    assert captured["workspace_path"] == str(hub_root)
    assert captured["caption"] == "please inspect"
    assert captured["kind"] == "image"


@pytest.mark.anyio
async def test_message_routing_submits_thread_work_through_orchestration_ingress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        workspace_path=str(tmp_path / "workspace"),
        pma_enabled=False,
    )
    Path(record.workspace_path or "").mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    class _Router:
        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return record

        def runtime_for(self, _key: str) -> object:
            return object()

    class _Handler:
        def __init__(self) -> None:
            self._logger = logging.getLogger("test")
            self._router = _Router()
            self._bot_username = None
            self._config = SimpleNamespace(trigger_mode="all")
            self._pending_questions: dict[str, object] = {}
            self._resume_options: dict[str, object] = {}
            self._bind_options: dict[str, object] = {}
            self._flow_run_options: dict[str, object] = {}
            self._agent_options: dict[str, object] = {}
            self._model_options: dict[str, object] = {}
            self._model_pending: dict[str, object] = {}
            self._review_commit_options: dict[str, object] = {}
            self._review_commit_subjects: dict[str, object] = {}
            self._pending_review_custom: dict[str, object] = {}
            self._ticket_flow_pause_targets: dict[str, str] = {}
            self._ticket_flow_bridge = SimpleNamespace(
                auto_resume_run=lambda *args, **kwargs: None
            )
            self._command_specs: dict[str, object] = {}
            self._last_task: Optional[asyncio.Task[None]] = None

        async def _resolve_topic_key(
            self, chat_id: int, thread_id: Optional[int]
        ) -> str:
            return f"{chat_id}:{thread_id}"

        def _handle_pending_resume(
            self, key: str, text: str, *, user_id: Optional[int]
        ) -> bool:
            _ = key, text, user_id
            return False

        def _handle_pending_bind(
            self, key: str, text: str, *, user_id: Optional[int]
        ) -> bool:
            _ = key, text, user_id
            return False

        async def _handle_pending_review_commit(
            self, message: TelegramMessage, runtime: object, key: str, text: str
        ) -> bool:
            _ = message, runtime, key, text
            return False

        async def _handle_pending_review_custom(
            self,
            key: str,
            message: TelegramMessage,
            runtime: object,
            command: object,
            raw_text: str,
            raw_caption: str,
        ) -> bool:
            _ = key, message, runtime, command, raw_text, raw_caption
            return False

        async def _dismiss_review_custom_prompt(
            self, message: TelegramMessage, pending: object
        ) -> None:
            _ = message, pending

        def _get_paused_ticket_flow(
            self, workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            _ = workspace_root, preferred_run_id
            return None

        async def _handle_normal_message(
            self,
            message: TelegramMessage,
            runtime: object,
            *,
            text_override: Optional[str] = None,
            placeholder_id: Optional[int] = None,
        ) -> None:
            _ = runtime, placeholder_id
            captured["handled_text"] = text_override
            captured["message_id"] = message.message_id

        def _wrap_placeholder_work(
            self,
            *,
            chat_id: int,
            placeholder_id: Optional[int],
            work: object,
        ):
            _ = chat_id, placeholder_id
            return work

        def _enqueue_topic_work(self, key: str, work: object) -> None:
            _ = key
            assert callable(work)
            self._last_task = asyncio.create_task(work())

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
            await submit_thread_message(request)
            return SimpleNamespace(route="thread", thread_result=None)

    monkeypatch.setattr(
        telegram_messages_module,
        "build_surface_orchestration_ingress",
        lambda **_: _FakeIngress(),
    )

    handler = _Handler()
    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=123,
        thread_id=456,
        from_user_id=789,
        text="route through ingress",
        date=None,
        is_topic_message=True,
    )

    await telegram_messages_module.handle_message_inner(handler, message)
    assert handler._last_task is not None
    await handler._last_task

    assert captured["surface_kind"] == "telegram"
    assert captured["prompt_text"] == "route through ingress"
    assert captured["handled_text"] == "route through ingress"
    assert captured["message_id"] == 10


class _TurnResult:
    def __init__(self) -> None:
        self.agent_messages = ["ok"]
        self.errors: list[str] = []
        self.status = "completed"
        self.token_usage = None


class _TurnHandle:
    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id

    async def wait(self, *_args: object, **_kwargs: object) -> _TurnResult:
        return _TurnResult()


class _PMARouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record

    async def set_active_thread(
        self, _chat_id: int, _thread_id: Optional[int], _active_thread_id: Optional[str]
    ) -> TelegramTopicRecord:
        return self._record

    async def update_topic(
        self, _chat_id: int, _thread_id: Optional[int], apply: object
    ) -> None:
        if callable(apply):
            apply(self._record)


class _PMAClientStub:
    def __init__(self) -> None:
        self.thread_start_calls: list[tuple[str, str]] = []
        self.turn_start_calls: list[str] = []

    async def thread_start(self, cwd: str, *, agent: str, **_kwargs: object) -> dict:
        self.thread_start_calls.append((cwd, agent))
        return {"thread_id": f"fresh-{len(self.thread_start_calls)}"}

    async def turn_start(
        self, thread_id: str, _prompt_text: str, **_kwargs: object
    ) -> _TurnHandle:
        self.turn_start_calls.append(thread_id)
        if thread_id == "stale":
            raise CodexAppServerResponseError(
                method="turn/start",
                code=-32600,
                message="thread not found: stale",
                data=None,
            )
        return _TurnHandle("turn-1")


class _PMAHandler(TelegramCommandHandlers):
    def __init__(
        self,
        record: TelegramTopicRecord,
        client: _PMAClientStub,
        hub_root: Path,
        registry: AppServerThreadRegistry,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace(
            concurrency=SimpleNamespace(max_parallel_turns=1, per_topic_queue=False),
            agent_turn_timeout_seconds={"codex": None, "opencode": None},
        )
        self._router = _PMARouterStub(record)
        self._turn_semaphore = asyncio.Semaphore(1)
        self._turn_contexts: dict[tuple[str, str], object] = {}
        self._turn_preview_text: dict[tuple[str, str], str] = {}
        self._turn_preview_updated_at: dict[tuple[str, str], float] = {}
        self._token_usage_by_thread: dict[str, dict[str, object]] = {}
        self._token_usage_by_turn: dict[str, dict[str, object]] = {}
        self._voice_config = None
        self._turn_progress_by_turn: dict[tuple[str, str], object] = {}
        self._turn_progress_by_topic: dict[str, object] = {}
        self._turn_progress_last_update: dict[tuple[str, str], float] = {}
        self._client = client
        self._hub_root = hub_root
        self._hub_thread_registry = registry
        self._bot_username = None

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    def _ensure_turn_semaphore(self) -> asyncio.Semaphore:
        return self._turn_semaphore

    async def _client_for_workspace(self, _workspace_path: str) -> _PMAClientStub:
        return self._client

    async def _find_thread_conflict(
        self, _thread_id: str, *, key: str
    ) -> Optional[str]:
        return None

    async def _refresh_workspace_id(
        self, _key: str, _record: TelegramTopicRecord
    ) -> Optional[str]:
        return None

    def _effective_policies(
        self, _record: TelegramTopicRecord
    ) -> tuple[Optional[str], Optional[Any]]:
        return None, None

    async def _handle_thread_conflict(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _verify_active_thread(
        self, _message: TelegramMessage, record: TelegramTopicRecord
    ) -> TelegramTopicRecord:
        return record

    def _maybe_append_whisper_disclaimer(
        self, prompt_text: str, *, transcript_text: Optional[str]
    ) -> str:
        return prompt_text

    async def _maybe_inject_github_context(
        self,
        prompt_text: str,
        _record: object,
        *,
        link_source_text: Optional[str] = None,
        allow_cross_repo: bool = False,
    ) -> tuple[str, bool]:
        _ = link_source_text, allow_cross_repo
        return prompt_text, False

    def _maybe_inject_car_context(self, prompt_text: str) -> tuple[str, bool]:
        return prompt_text, False

    def _maybe_inject_prompt_context(
        self,
        prompt_text: str,
        *,
        trigger_text: Optional[str] = None,
    ) -> tuple[str, bool]:
        _ = trigger_text
        return prompt_text, False

    def _maybe_inject_outbox_context(
        self,
        prompt_text: str,
        *,
        record: object,
        topic_key: str,
        has_file_context: bool = False,
        user_input_text: Optional[str] = None,
    ) -> tuple[str, bool]:
        _ = record, topic_key, has_file_context, user_input_text
        return prompt_text, False

    async def _prepare_turn_placeholder(
        self,
        _message: TelegramMessage,
        *,
        placeholder_id: Optional[int],
        send_placeholder: bool,
        queued: bool,
    ) -> Optional[int]:
        return None

    async def _send_message(
        self,
        _chat_id: int,
        _text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> None:
        return None

    async def _edit_message_text(
        self,
        _chat_id: int,
        _message_id: int,
        _text: str,
        *,
        thread_id: Optional[int] = None,
        reply_markup: Optional[object] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = False,
    ) -> None:
        return None

    async def _delete_message(
        self,
        _chat_id: int,
        _message_id: int,
        *,
        thread_id: Optional[int] = None,
    ) -> None:
        return None

    async def _finalize_voice_transcript(
        self,
        _chat_id: int,
        _transcript_message_id: Optional[int],
        _transcript_text: Optional[str],
    ) -> None:
        return None

    async def _deliver_turn_response(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        placeholder_id: Optional[int],
        response: str,
        delete_placeholder_on_delivery: bool = True,
    ) -> bool:
        _ = (
            chat_id,
            thread_id,
            reply_to,
            placeholder_id,
            response,
            delete_placeholder_on_delivery,
        )
        return True

    async def _start_turn_progress(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: object,
        agent: Optional[str],
        model: Optional[str],
        label: str,
    ) -> None:
        return None

    def _clear_turn_progress(self, _turn_key: tuple[str, str]) -> None:
        return None

    def _turn_key(
        self, thread_id: Optional[str], turn_id: Optional[str]
    ) -> Optional[tuple[str, str]]:
        if thread_id and turn_id:
            return (thread_id, turn_id)
        return None

    def _register_turn_context(
        self, turn_key: tuple[str, str], turn_id: str, ctx: object
    ) -> bool:
        self._turn_contexts[turn_key] = ctx
        return True

    def _clear_thinking_preview(self, _turn_key: tuple[str, str]) -> None:
        return None

    async def _require_thread_workspace(
        self,
        _message: TelegramMessage,
        _workspace_path: str,
        _thread: object,
        *,
        action: str,
    ) -> bool:
        return True

    def _format_turn_metrics(self, *_args: object, **_kwargs: object) -> Optional[str]:
        return None


class _ManagedThreadPMAHandler(_PMAHandler):
    def __init__(
        self,
        record: TelegramTopicRecord,
        hub_root: Path,
    ) -> None:
        super().__init__(
            record,
            client=_PMAClientStub(),
            hub_root=hub_root,
            registry=AppServerThreadRegistry(hub_root / "threads.json"),
        )
        self._config = SimpleNamespace(
            root=hub_root,
            concurrency=SimpleNamespace(max_parallel_turns=1, per_topic_queue=False),
            agent_turn_timeout_seconds={"codex": None, "opencode": None},
            metrics_mode="separate",
            progress_stream=SimpleNamespace(
                enabled=True,
                max_actions=10,
                max_output_chars=120,
                min_edit_interval_seconds=0.0,
            ),
        )
        self._sent: list[str] = []
        self._edited: list[dict[str, Any]] = []
        self._deleted: list[dict[str, Any]] = []
        self._placeholders: list[dict[str, Any]] = []
        self._next_placeholder_id = 900
        self._spawned_tasks: set[asyncio.Task[object]] = set()
        self._turn_progress_trackers: dict[tuple[str, str], object] = {}
        self._turn_progress_rendered: dict[tuple[str, str], str] = {}
        self._turn_progress_updated_at: dict[tuple[str, str], float] = {}
        self._turn_progress_tasks: dict[tuple[str, str], asyncio.Task[object]] = {}
        self._turn_progress_heartbeat_tasks: dict[
            tuple[str, str], asyncio.Task[object]
        ] = {}
        self._turn_progress_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._pending_context_usage: dict[tuple[str, str], int] = {}

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> None:
        _ = thread_id, reply_to
        self._sent.append(text)

    async def _prepare_turn_placeholder(
        self,
        _message: TelegramMessage,
        *,
        placeholder_id: Optional[int],
        send_placeholder: bool,
        queued: bool,
    ) -> Optional[int]:
        if placeholder_id is None and send_placeholder:
            placeholder_id = self._next_placeholder_id
            self._next_placeholder_id += 1
            self._placeholders.append({"message_id": placeholder_id, "queued": queued})
        return placeholder_id

    async def _edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        thread_id: Optional[int] = None,
        reply_markup: Optional[object] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = False,
    ) -> None:
        _ = parse_mode, disable_web_page_preview
        self._edited.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "thread_id": thread_id,
                "reply_markup": reply_markup,
            }
        )

    async def _delete_message(
        self,
        chat_id: int,
        message_id: int,
        *,
        thread_id: Optional[int] = None,
    ) -> None:
        self._deleted.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "thread_id": thread_id,
            }
        )

    async def _deliver_turn_response(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        placeholder_id: Optional[int],
        response: str,
        delete_placeholder_on_delivery: bool = True,
    ) -> bool:
        _ = (
            chat_id,
            thread_id,
            reply_to,
            placeholder_id,
            delete_placeholder_on_delivery,
        )
        self._sent.append(response)
        return True

    def _touch_cache_timestamp(self, _cache_name: str, _key: object) -> None:
        return None

    async def _flush_outbox_files(
        self,
        _record: TelegramTopicRecord,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> None:
        _ = chat_id, thread_id, reply_to
        return None

    def _format_turn_metrics_text(
        self,
        token_usage: Optional[dict[str, Any]],
        elapsed_seconds: Optional[float],
    ) -> Optional[str]:
        _ = token_usage, elapsed_seconds
        return None

    def _metrics_mode(self) -> str:
        return "separate"

    async def _start_turn_progress(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: object,
        agent: Optional[str],
        model: Optional[str],
        label: str,
    ) -> None:
        await TelegramNotificationHandlers._start_turn_progress(
            self,
            turn_key,
            ctx=ctx,
            agent=agent,
            model=model,
            label=label,
        )

    def _clear_turn_progress(self, turn_key: tuple[str, str]) -> None:
        TelegramNotificationHandlers._clear_turn_progress(self, turn_key)

    def _clear_thinking_preview(self, turn_key: tuple[str, str]) -> None:
        self._turn_preview_text.pop(turn_key, None)
        self._turn_preview_updated_at.pop(turn_key, None)
        self._clear_turn_progress(turn_key)

    async def _emit_progress_edit(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Optional[object] = None,
        now: Optional[float] = None,
        force: bool = False,
        render_mode: str = "live",
    ) -> None:
        await TelegramNotificationHandlers._emit_progress_edit(
            self,
            turn_key,
            ctx=ctx,
            now=now,
            force=force,
            render_mode=render_mode,
        )

    async def _schedule_progress_edit(self, turn_key: tuple[str, str]) -> None:
        await TelegramNotificationHandlers._schedule_progress_edit(self, turn_key)

    async def _ensure_turn_progress_lock(
        self, turn_key: tuple[str, str]
    ) -> asyncio.Lock:
        return await TelegramNotificationHandlers._ensure_turn_progress_lock(
            self,
            turn_key,
        )

    async def _delayed_progress_edit(
        self, turn_key: tuple[str, str], delay: float
    ) -> None:
        await TelegramNotificationHandlers._delayed_progress_edit(
            self,
            turn_key,
            delay,
        )

    async def _turn_progress_heartbeat(self, turn_key: tuple[str, str]) -> None:
        await TelegramNotificationHandlers._turn_progress_heartbeat(self, turn_key)

    async def _apply_run_event_to_progress(
        self,
        turn_key: tuple[str, str],
        run_event: object,
    ) -> None:
        await TelegramNotificationHandlers._apply_run_event_to_progress(
            self,
            turn_key,
            run_event,
        )

    def _spawn_task(self, coro):  # type: ignore[no-untyped-def]
        task = asyncio.create_task(coro)
        self._spawned_tasks.add(task)
        task.add_done_callback(self._spawned_tasks.discard)
        return task


@pytest.mark.anyio
async def test_managed_thread_queue_worker_wraps_execution_with_typing_indicator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    queued_started = object()
    begin_calls = 0

    class _Handler:
        def __init__(self) -> None:
            self._logger = logging.getLogger("test")
            self._spawned_tasks: set[asyncio.Task[object]] = set()

        def _spawn_task(self, coro):  # type: ignore[no-untyped-def]
            task = asyncio.create_task(coro)
            self._spawned_tasks.add(task)
            task.add_done_callback(self._spawned_tasks.discard)
            return task

        async def _begin_typing_indicator(
            self, chat_id: int, thread_id: Optional[int]
        ) -> None:
            events.append(("begin", chat_id, thread_id))

        async def _end_typing_indicator(
            self, chat_id: int, thread_id: Optional[int]
        ) -> None:
            events.append(("end", chat_id, thread_id))

        async def _send_message(
            self,
            chat_id: int,
            text: str,
            *,
            thread_id: Optional[int],
            reply_to: Optional[int],
        ) -> None:
            _ = reply_to
            events.append(("send", chat_id, thread_id, text))

        async def _flush_outbox_files(
            self,
            record: TelegramTopicRecord,
            *,
            chat_id: int,
            thread_id: Optional[int],
            reply_to: Optional[int],
        ) -> None:
            _ = record, reply_to
            events.append(("flush", chat_id, thread_id))

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
        events.append("finalize")
        return {"status": "ok", "assistant_text": "queued telegram reply"}

    monkeypatch.setattr(
        execution_commands_module,
        "begin_next_queued_runtime_thread_execution",
        _fake_begin_next,
    )
    monkeypatch.setattr(
        execution_commands_module.ManagedThreadTurnCoordinator,
        "run_started_execution",
        _fake_run_started_execution,
    )

    handler = _Handler()
    execution_commands_module._ensure_telegram_managed_thread_queue_worker(
        handler,
        orchestration_service=_OrchestrationServiceStub(),
        managed_thread_id="managed-thread-1",
        surface_key="telegram:-1001:101",
        record=TelegramTopicRecord(workspace_path=str(tmp_path), pma_enabled=True),
        chat_id=-1001,
        thread_id=101,
    )

    spawned = list(handler._spawned_tasks)
    assert len(spawned) == 1
    await asyncio.gather(*spawned)

    assert events == [
        ("begin", -1001, 101),
        "finalize",
        ("send", -1001, 101, "queued telegram reply"),
        ("flush", -1001, 101),
        ("end", -1001, 101),
    ]


@pytest.mark.anyio
async def test_pma_managed_thread_turn_edits_placeholder_with_live_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
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

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

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
                input_items,
            )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="telegram-backend-turn-1",
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
                assistant_text="telegram managed final reply",
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
                            "delta": "thinking through telegram pma",
                        },
                    }
                },
            )
            yield format_sse(
                "app-server",
                {
                    "message": {
                        "method": "item/agentMessage/delta",
                        "params": {"delta": "partial telegram managed reply"},
                    }
                },
            )
            stream_finished.set()

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="stream this telegram prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(message, runtime=_RuntimeStub())

    assert handler._placeholders
    assert handler._edited
    edited_texts = [str(edit["text"]) for edit in handler._edited]
    assert len(edited_texts) >= 2
    assert len(set(edited_texts)) >= 2
    assert "telegram managed final reply" in handler._sent

    remaining_tasks = list(handler._spawned_tasks)
    for task in remaining_tasks:
        if not task.done():
            task.cancel()
    for task in remaining_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_pma_managed_opencode_turn_edits_placeholder_with_thinking_and_tool_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="opencode",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
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

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

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
                input_items,
            )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="telegram-backend-turn-1",
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
                assistant_text="telegram opencode managed final reply",
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
            yield {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "sessionID": "telegram-backend-thread-1",
                        "messageID": "assistant-1",
                        "properties": {
                            "messageID": "assistant-1",
                            "info": {"id": "assistant-1", "role": "assistant"},
                            "part": {
                                "id": "reason-1",
                                "type": "reasoning",
                                "messageID": "assistant-1",
                                "sessionID": "telegram-backend-thread-1",
                                "text": "thinking through telegram opencode progress",
                            },
                        },
                    },
                }
            }
            yield {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "sessionID": "telegram-backend-thread-1",
                        "messageID": "assistant-1",
                        "properties": {
                            "messageID": "assistant-1",
                            "info": {"id": "assistant-1", "role": "assistant"},
                            "part": {
                                "id": "tool-1",
                                "type": "tool",
                                "messageID": "assistant-1",
                                "sessionID": "telegram-backend-thread-1",
                                "tool": "shell",
                                "input": "rg AGENTS.md",
                                "state": {"status": "running"},
                            },
                        },
                    },
                }
            }
            stream_finished.set()

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "opencode": AgentDescriptor(
                id="opencode",
                name="OpenCode",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="stream this telegram prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(message, runtime=_RuntimeStub())

    assert handler._placeholders
    assert handler._edited
    edited_texts = [str(edit["text"]) for edit in handler._edited]
    assert len(edited_texts) >= 2
    assert any(
        "thinking through telegram opencode progress" in text for text in edited_texts
    )
    assert any("shell" in text for text in edited_texts)
    assert "telegram opencode managed final reply" in handler._sent

    remaining_tasks = list(handler._spawned_tasks)
    for task in remaining_tasks:
        if not task.done():
            task.cancel()
    for task in remaining_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_pma_managed_thread_turn_recovers_if_wait_disconnects_after_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
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

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

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
                input_items,
            )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="telegram-backend-turn-1",
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
            raise CodexAppServerDisconnected("Reconnecting... 2/5")

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
                        "method": "message.completed",
                        "params": {
                            "text": "telegram completed reply survives",
                            "info": {"id": "msg-1", "role": "assistant"},
                        },
                    }
                },
            )
            yield format_sse(
                "app-server",
                {
                    "message": {
                        "method": "turn/completed",
                        "params": {"status": "completed"},
                    }
                },
            )
            stream_finished.set()

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="stream this telegram prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(message, runtime=_RuntimeStub())

    assert "telegram completed reply survives" in handler._sent
    assert not any("turn failed" in sent.lower() for sent in handler._sent)
    assert not any("disconnected" in sent.lower() for sent in handler._sent)

    remaining_tasks = list(handler._spawned_tasks)
    for task in remaining_tasks:
        if not task.done():
            task.cancel()
    for task in remaining_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_pma_text_messages_route_repeated_messages_through_managed_thread_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
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

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

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
                model,
                reasoning,
                approval_mode,
                sandbox_policy,
                input_items,
            )
            turn_id = f"telegram-backend-turn-{len(self.turn_prompts) + 1}"
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
            if turn_id == "telegram-backend-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="ok",
                    assistant_text="first telegram orchestration reply",
                    errors=[],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="second telegram orchestration reply",
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
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    first_message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="first orchestration prompt",
        date=None,
        is_topic_message=True,
    )
    second_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="second orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    first_task = asyncio.create_task(
        handler._handle_normal_message(first_message, runtime=_RuntimeStub())
    )
    try:
        await first_started.wait()
        await handler._handle_normal_message(second_message, runtime=_RuntimeStub())
        release_first.set()
        await first_task
        with anyio.fail_after(5):
            while "second telegram orchestration reply" not in handler._sent:
                await anyio.sleep(0.05)

        assert "Queued (waiting for available worker...)" in handler._sent
        assert "first telegram orchestration reply" in handler._sent
        assert "second telegram orchestration reply" in handler._sent

        thread_store = execution_commands_module.PmaThreadStore(tmp_path)
        threads = thread_store.list_threads(limit=10)
        assert len(threads) == 1
        turns = thread_store.list_turns(threads[0]["managed_thread_id"], limit=10)
        assert len(turns) == 2
        assert [turn["status"] for turn in turns] == ["ok", "ok"]
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task
        remaining_tasks = list(handler._spawned_tasks)
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        for task in remaining_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.anyio
async def test_pma_followup_turn_without_new_thread_reuses_managed_thread_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)

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
            self.start_calls: list[tuple[str, str]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

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
            self.start_calls.append((conversation_id, prompt))
            turn_id = f"telegram-backend-turn-{len(self.start_calls)}"
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id=turn_id,
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
            _ = workspace_root, timeout
            assert conversation_id == "telegram-backend-thread-1"
            assert isinstance(turn_id, str)
            return SimpleNamespace(
                status="ok",
                assistant_text=f"reply for {turn_id}",
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
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    first_message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="first pma orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(first_message, runtime=_RuntimeStub())

    assert handler._hub_thread_registry.get_thread_id(PMA_KEY) == (
        "telegram-backend-thread-1"
    )

    followup_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="/compact",
        date=None,
        is_topic_message=True,
    )

    outcome = await handler._run_turn_and_collect_result(
        followup_message,
        runtime=_RuntimeStub(),
        text_override="compact summary prompt",
        record=record,
        allow_new_thread=False,
        missing_thread_message="No active thread to compact. Use /new to start one.",
        send_placeholder=False,
    )

    assert isinstance(outcome, _TurnRunResult)
    assert outcome.thread_id == "telegram-backend-thread-1"
    assert handler._hub_thread_registry.get_thread_id(PMA_KEY) == (
        "telegram-backend-thread-1"
    )
    assert [call[0] for call in harness.start_calls] == [
        "telegram-backend-thread-1",
        "telegram-backend-thread-1",
    ]


@pytest.mark.anyio
async def test_resolve_telegram_managed_thread_reuses_archived_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=str(workspace),
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)

    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=frozenset({"durable_threads"}),
                make_harness=lambda _ctx: object(),
            )
        },
    )

    orchestration_service = (
        execution_commands_module._build_telegram_thread_orchestration_service(handler)
    )
    current_thread = orchestration_service.create_thread_target(
        "codex",
        workspace.resolve(),
        repo_id="repo-1",
        display_name="telegram:test-topic",
    )
    orchestration_service.upsert_binding(
        surface_kind="telegram",
        surface_key="telegram:-1001:101",
        thread_target_id=current_thread.thread_target_id,
        agent_id="codex",
        repo_id="repo-1",
        mode="pma",
    )
    orchestration_service.archive_thread_target(current_thread.thread_target_id)

    _service, resolved = (
        await execution_commands_module._resolve_telegram_managed_thread(
            handler,
            surface_key="telegram:-1001:101",
            workspace_root=workspace.resolve(),
            agent="codex",
            repo_id="repo-1",
            mode="pma",
            pma_enabled=True,
            allow_new_thread=False,
        )
    )

    assert resolved is not None
    assert resolved.thread_target_id == current_thread.thread_target_id
    assert resolved.lifecycle_status == "active"


@pytest.mark.anyio
async def test_resolve_telegram_managed_thread_ignores_backend_thread_id_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            raise AssertionError(
                "runtime instance lookup should be skipped in PMA mode"
            )

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            calls.append((thread_target_id, dict(kwargs)))
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                agent_id="codex",
                agent_profile=None,
                workspace_root=str(workspace),
                lifecycle_status="active",
                backend_thread_id=None,
            )

        def create_thread_target(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("create should not be used in this test")

        def upsert_binding(self, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="pma"),
            SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                agent_profile=None,
                workspace_root=str(workspace),
                lifecycle_status="archived",
                backend_thread_id="legacy-backend",
            ),
        ),
    )

    _service, resolved = (
        await execution_commands_module._resolve_telegram_managed_thread(
            SimpleNamespace(_logger=logging.getLogger("test")),
            surface_key="telegram:-1001:101",
            workspace_root=workspace.resolve(),
            agent="codex",
            repo_id="repo-1",
            mode="pma",
            pma_enabled=True,
            backend_thread_id="stale-backend",
            allow_new_thread=False,
        )
    )

    assert resolved is not None
    assert resolved.thread_target_id == "thread-1"
    assert calls == [
        (
            "thread-1",
            {
                "backend_thread_id": None,
                "backend_runtime_instance_id": None,
            },
        )
    ]


@pytest.mark.anyio
async def test_pma_native_input_items_route_through_managed_thread_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"png-bytes")
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

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

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
                turn_id="telegram-backend-turn-1",
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
                assistant_text="telegram managed attachment reply",
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
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="review this image",
        date=None,
        is_topic_message=True,
    )
    input_items = [
        {"type": "text", "text": "review this image"},
        {"type": "localImage", "path": str(image_path)},
    ]

    await handler._handle_normal_message(
        message,
        runtime=_RuntimeStub(),
        input_items=input_items,
    )

    assert captured_input_items and isinstance(captured_input_items[0], list)
    items = captured_input_items[0] or []
    assert items and items[0].get("type") == "text"
    assert "<user_message>" in str(items[0].get("text") or "")
    assert any(item.get("type") == "localImage" for item in items[1:])
    assert "telegram managed attachment reply" in handler._sent

    thread_store = execution_commands_module.PmaThreadStore(tmp_path)
    threads = thread_store.list_threads(limit=10)
    assert len(threads) == 1
    turns = thread_store.list_turns(threads[0]["managed_thread_id"], limit=10)
    assert len(turns) == 1
    assert turns[0]["status"] == "ok"


@pytest.mark.anyio
async def test_pma_interrupt_uses_managed_thread_orchestration_for_text_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
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
            self.interrupt_calls: list[tuple[Path, str, Optional[str]]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

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
                input_items,
            )
            turn_id = "telegram-backend-turn-1"
            if release_first.is_set():
                turn_id = "telegram-backend-turn-2"
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
            if turn_id == "telegram-backend-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="interrupted",
                    assistant_text="",
                    errors=[],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="unexpected queued reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            self.interrupt_calls.append((workspace_root, conversation_id, turn_id))
            release_first.set()

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="interruptible orchestration prompt",
        date=None,
        is_topic_message=True,
    )
    queued_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="queued orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    first_task = asyncio.create_task(
        handler._handle_normal_message(message, runtime=_RuntimeStub())
    )
    try:
        await first_started.wait()
        await handler._handle_normal_message(queued_message, runtime=_RuntimeStub())
        await handler._process_interrupt(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            runtime=_RuntimeStub(),
            message_id=99,
        )
        with anyio.fail_after(2):
            while (
                "Interrupted active PMA turn. Cancelled 1 queued PMA turn(s)."
                not in handler._sent
            ):
                await anyio.sleep(0.05)
        await first_task

        assert harness.interrupt_calls == [
            (tmp_path, "telegram-backend-thread-1", "telegram-backend-turn-1")
        ]
        assert (
            "Interrupted active PMA turn. Cancelled 1 queued PMA turn(s)."
            in handler._sent
        )
        assert "unexpected queued reply" not in handler._sent

        thread_store = execution_commands_module.PmaThreadStore(tmp_path)
        threads = thread_store.list_threads(limit=10)
        assert len(threads) == 1
        turns = thread_store.list_turns(threads[0]["managed_thread_id"], limit=10)
        assert len(turns) == 2
        assert [turn["status"] for turn in turns] == ["interrupted", "interrupted"]
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task
        remaining_tasks = list(handler._spawned_tasks)
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        for task in remaining_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.anyio
async def test_pma_interrupt_recovers_missing_backend_thread_for_text_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
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
            self.interrupt_calls: list[tuple[Path, str, Optional[str]]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="telegram-backend-thread-1")

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
                input_items,
            )
            turn_id = "telegram-backend-turn-1"
            if release_first.is_set():
                turn_id = "telegram-backend-turn-2"
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
            if turn_id == "telegram-backend-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="error",
                    assistant_text="",
                    errors=["stale backend thread"],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="unexpected queued reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            self.interrupt_calls.append((workspace_root, conversation_id, turn_id))
            raise CodexAppServerResponseError(
                method="turn/interrupt",
                code=-32600,
                message="thread not found: telegram-backend-thread-1",
                data=None,
            )

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="interruptible orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    first_task = asyncio.create_task(
        handler._handle_normal_message(message, runtime=_RuntimeStub())
    )
    try:
        await first_started.wait()
        await handler._process_interrupt(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            runtime=_RuntimeStub(),
            message_id=99,
        )
        with anyio.fail_after(2):
            while not any(
                "Recovered stale PMA session" in sent for sent in handler._sent
            ):
                await anyio.sleep(0.05)
        release_first.set()
        await first_task

        assert harness.interrupt_calls == [
            (tmp_path, "telegram-backend-thread-1", "telegram-backend-turn-1")
        ]
        assert any(
            "Recovered stale PMA session after backend thread was lost." in sent
            for sent in handler._sent
        )
        thread_store = execution_commands_module.PmaThreadStore(tmp_path)
        threads = thread_store.list_threads(limit=10)
        assert len(threads) == 1
        turns = thread_store.list_turns(threads[0]["managed_thread_id"], limit=10)
        assert turns[0]["status"] == "interrupted"
        assert turns[0]["error"] is None
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task
        remaining_tasks = list(handler._spawned_tasks)
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        for task in remaining_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.anyio
async def test_repo_text_turns_use_orchestration_binding_and_preserve_thread_continuity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="codex",
        active_thread_id="stale-active-thread",
        thread_ids=["stale-active-thread"],
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)

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
            self.start_calls: list[tuple[str, str]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            assert workspace_root == tmp_path

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            assert workspace_root == tmp_path
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="repo-backend-thread-1")

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
            _ = model, reasoning, approval_mode, sandbox_policy, input_items
            assert workspace_root == tmp_path
            self.start_calls.append((conversation_id, prompt))
            turn_id = f"repo-backend-turn-{len(self.start_calls)}"
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
            _ = workspace_root, timeout
            assert conversation_id == "repo-backend-thread-1"
            assert isinstance(turn_id, str)
            return SimpleNamespace(
                status="ok",
                assistant_text=f"reply for {turn_id}",
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
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    first_message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="first repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )
    second_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="second repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(first_message, runtime=_RuntimeStub())
    await handler._handle_normal_message(second_message, runtime=_RuntimeStub())

    assert harness.start_calls == [
        ("repo-backend-thread-1", "first repo orchestration prompt"),
        ("repo-backend-thread-1", "second repo orchestration prompt"),
    ]
    assert handler._client.thread_start_calls == []
    assert record.active_thread_id == "repo-backend-thread-1"
    assert record.thread_ids[0] == "repo-backend-thread-1"
    assert "reply for repo-backend-turn-1" in handler._sent
    assert "reply for repo-backend-turn-2" in handler._sent

    orchestration_service = (
        execution_commands_module._build_telegram_thread_orchestration_service(handler)
    )
    binding = orchestration_service.get_binding(
        surface_kind="telegram",
        surface_key="-1001:101",
    )
    assert binding is not None
    assert binding.mode == "repo"
    thread = orchestration_service.get_thread_target(binding.thread_target_id)
    assert thread is not None
    assert thread.backend_thread_id == "repo-backend-thread-1"


@pytest.mark.anyio
async def test_repo_media_turns_preserve_input_items_via_orchestration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"png-bytes")

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
            self.input_items: Optional[list[dict[str, Any]]] = None

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="repo-media-thread-1")

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
            self.input_items = input_items
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="repo-media-turn-1",
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
                assistant_text="repo media orchestration reply",
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
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="caption text",
        date=None,
        is_topic_message=True,
    )
    input_items = [
        {"type": "text", "text": "caption text"},
        {"type": "localImage", "path": str(image_path)},
    ]

    result = await handler._run_turn_and_collect_result(
        message,
        runtime=_RuntimeStub(),
        text_override="caption text",
        input_items=input_items,
        send_placeholder=False,
    )

    assert isinstance(result, _TurnRunResult)
    assert result.response == "repo media orchestration reply"
    assert harness.input_items == input_items
    assert handler._client.thread_start_calls == []
    assert record.active_thread_id == "repo-media-thread-1"


@pytest.mark.anyio
async def test_repo_interrupt_uses_orchestration_binding_for_text_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
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
            self.interrupt_calls: list[tuple[Path, str, Optional[str]]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="repo-backend-thread-1")

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
                input_items,
            )
            turn_id = "repo-backend-turn-1"
            if release_first.is_set():
                turn_id = "repo-backend-turn-2"
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
            if turn_id == "repo-backend-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="interrupted",
                    assistant_text="",
                    errors=[],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="unexpected queued repo reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            self.interrupt_calls.append((workspace_root, conversation_id, turn_id))
            release_first.set()

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    first_message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="interruptible repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )
    queued_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="queued repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    first_task = asyncio.create_task(
        handler._handle_normal_message(first_message, runtime=_RuntimeStub())
    )
    try:
        await first_started.wait()
        await handler._handle_normal_message(queued_message, runtime=_RuntimeStub())
        await handler._process_interrupt(
            chat_id=first_message.chat_id,
            thread_id=first_message.thread_id,
            reply_to=first_message.message_id,
            runtime=_RuntimeStub(),
            message_id=99,
        )
        with anyio.fail_after(2):
            while (
                "Interrupted active turn. Cancelled 1 queued turn(s)."
                not in handler._sent
            ):
                await anyio.sleep(0.05)
        await first_task

        assert handler._client.thread_start_calls == []
        assert harness.interrupt_calls == [
            (tmp_path, "repo-backend-thread-1", "repo-backend-turn-1")
        ]
        assert "Interrupted active turn. Cancelled 1 queued turn(s)." in handler._sent
        assert "unexpected queued repo reply" not in handler._sent
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task
        remaining_tasks = list(handler._spawned_tasks)
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        for task in remaining_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.anyio
async def test_repo_message_ingress_callback_reaches_orchestrated_thread_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="codex",
    )

    class _RepoIngressHandler(_ManagedThreadPMAHandler):
        def __init__(self, record: TelegramTopicRecord, hub_root: Path) -> None:
            super().__init__(record, hub_root)
            self._router.runtime_for = lambda _key: _RuntimeStub()  # type: ignore[attr-defined]
            self._pending_questions = {}
            self._resume_options = {}
            self._bind_options = {}
            self._flow_run_options = {}
            self._agent_options = {}
            self._model_options = {}
            self._model_pending = {}
            self._review_commit_options = {}
            self._review_commit_subjects = {}
            self._pending_review_custom = {}
            self._ticket_flow_pause_targets = {}
            self._bot_username = None
            self._command_specs = {}
            self._config.trigger_mode = "all"

        def _get_paused_ticket_flow(
            self, _workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            _ = preferred_run_id
            return None

        def _enqueue_topic_work(self, _key: str, work):  # type: ignore[no-untyped-def]
            asyncio.get_running_loop().create_task(work())

        def _wrap_placeholder_work(self, **kwargs):  # type: ignore[no-untyped-def]
            return kwargs["work"]

        def _handle_pending_resume(self, *_args, **_kwargs) -> bool:
            return False

        def _handle_pending_bind(self, *_args, **_kwargs) -> bool:
            return False

        async def _handle_pending_review_commit(self, *_args, **_kwargs) -> bool:
            return False

        async def _handle_pending_review_custom(self, *_args, **_kwargs) -> bool:
            return False

        async def _dismiss_review_custom_prompt(self, *_args, **_kwargs) -> None:
            return None

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

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="repo-ingress-thread-1")

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
                input_items,
            )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="repo-ingress-turn-1",
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
                assistant_text="repo ingress orchestration reply",
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

    handler = _RepoIngressHandler(record, tmp_path)
    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    class _IngressStub:
        async def submit_message(self, request, **kwargs):  # type: ignore[no-untyped-def]
            await kwargs["submit_thread_message"](request)
            return SimpleNamespace(route="thread", thread_result=None)

    monkeypatch.setattr(
        telegram_messages_module,
        "build_surface_orchestration_ingress",
        lambda **_: _IngressStub(),
    )

    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=111,
        thread_id=222,
        from_user_id=333,
        text="hello from ingress",
        date=None,
        is_topic_message=True,
    )

    await telegram_messages_module.handle_message_inner(handler, message)
    with anyio.fail_after(2):
        while "repo ingress orchestration reply" not in handler._sent:
            await anyio.sleep(0.05)

    assert "repo ingress orchestration reply" in handler._sent
    orchestration_service = (
        execution_commands_module._build_telegram_thread_orchestration_service(handler)
    )
    binding = orchestration_service.get_binding(
        surface_kind="telegram",
        surface_key="111:222",
    )
    assert binding is not None
    assert binding.mode == "repo"


@pytest.mark.anyio
async def test_repo_message_ingress_callback_reaches_hermes_orchestrated_thread_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="hermes",
    )

    class _RepoIngressHandler(_ManagedThreadPMAHandler):
        def __init__(self, record: TelegramTopicRecord, hub_root: Path) -> None:
            super().__init__(record, hub_root)
            self._router.runtime_for = lambda _key: _RuntimeStub()  # type: ignore[attr-defined]
            self._pending_questions = {}
            self._resume_options = {}
            self._bind_options = {}
            self._flow_run_options = {}
            self._agent_options = {}
            self._model_options = {}
            self._model_pending = {}
            self._review_commit_options = {}
            self._review_commit_subjects = {}
            self._pending_review_custom = {}
            self._ticket_flow_pause_targets = {}
            self._bot_username = None
            self._command_specs = {}
            self._config.trigger_mode = "all"

        def _get_paused_ticket_flow(
            self, _workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            _ = preferred_run_id
            return None

        def _enqueue_topic_work(self, _key: str, work):  # type: ignore[no-untyped-def]
            asyncio.get_running_loop().create_task(work())

        def _wrap_placeholder_work(self, **kwargs):  # type: ignore[no-untyped-def]
            return kwargs["work"]

        def _handle_pending_resume(self, *_args, **_kwargs) -> bool:
            return False

        def _handle_pending_bind(self, *_args, **_kwargs) -> bool:
            return False

        async def _handle_pending_review_commit(self, *_args, **_kwargs) -> bool:
            return False

        async def _handle_pending_review_custom(self, *_args, **_kwargs) -> bool:
            return False

        async def _dismiss_review_custom_prompt(self, *_args, **_kwargs) -> None:
            return None

    class _FakeHarness:
        display_name = "Hermes"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
                "approvals",
                "active_thread_discovery",
            }
        )

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-hermes-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="hermes-repo-thread-1")

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
                input_items,
            )
            return SimpleNamespace(
                conversation_id=conversation_id,
                turn_id="hermes-repo-turn-1",
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
                assistant_text="hermes repo ingress reply",
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

    handler = _RepoIngressHandler(record, tmp_path)
    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "hermes": AgentDescriptor(
                id="hermes",
                name="Hermes",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    class _IngressStub:
        async def submit_message(self, request, **kwargs):  # type: ignore[no-untyped-def]
            await kwargs["submit_thread_message"](request)
            return SimpleNamespace(route="thread", thread_result=None)

    monkeypatch.setattr(
        telegram_messages_module,
        "build_surface_orchestration_ingress",
        lambda **_: _IngressStub(),
    )

    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=111,
        thread_id=222,
        from_user_id=333,
        text="hello from hermes ingress",
        date=None,
        is_topic_message=True,
    )

    await telegram_messages_module.handle_message_inner(handler, message)
    with anyio.fail_after(2):
        while "hermes repo ingress reply" not in handler._sent:
            await anyio.sleep(0.05)

    assert "hermes repo ingress reply" in handler._sent
    orchestration_service = (
        execution_commands_module._build_telegram_thread_orchestration_service(handler)
    )
    binding = orchestration_service.get_binding(
        surface_kind="telegram",
        surface_key="111:222",
    )
    assert binding is not None
    assert binding.agent_id == "hermes"
    assert binding.mode == "repo"


@pytest.mark.anyio
async def test_repo_interrupt_uses_orchestration_binding_for_hermes_text_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="hermes",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class _FakeHarness:
        display_name = "Hermes"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
                "approvals",
                "active_thread_discovery",
            }
        )

        def __init__(self) -> None:
            self.interrupt_calls: list[tuple[Path, str, Optional[str]]] = []

        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            _ = workspace_root
            return "runtime-hermes-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            return SimpleNamespace(id="hermes-fresh-1")

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
                input_items,
            )
            turn_id = "hermes-turn-1"
            if release_first.is_set():
                turn_id = "hermes-turn-2"
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
            if turn_id == "hermes-turn-1":
                first_started.set()
                await release_first.wait()
                return SimpleNamespace(
                    status="interrupted",
                    assistant_text="",
                    errors=[],
                )
            return SimpleNamespace(
                status="ok",
                assistant_text="unexpected hermes queued reply",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
        ) -> None:
            self.interrupt_calls.append((workspace_root, conversation_id, turn_id))
            release_first.set()

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield ""

    harness = _FakeHarness()
    monkeypatch.setattr(
        execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "hermes": AgentDescriptor(
                id="hermes",
                name="Hermes",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    first_message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="interruptible hermes prompt",
        date=None,
        is_topic_message=True,
    )
    queued_message = TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="queued hermes prompt",
        date=None,
        is_topic_message=True,
    )

    first_task = asyncio.create_task(
        handler._handle_normal_message(first_message, runtime=_RuntimeStub())
    )
    try:
        await first_started.wait()
        await handler._handle_normal_message(queued_message, runtime=_RuntimeStub())
        await handler._process_interrupt(
            chat_id=first_message.chat_id,
            thread_id=first_message.thread_id,
            reply_to=first_message.message_id,
            runtime=_RuntimeStub(),
            message_id=99,
        )
        with anyio.fail_after(2):
            while (
                "Interrupted active turn. Cancelled 1 queued turn(s)."
                not in handler._sent
            ):
                await anyio.sleep(0.05)
        await first_task

        assert handler._client.thread_start_calls == []
        assert harness.interrupt_calls == [
            (tmp_path, "hermes-fresh-1", "hermes-turn-1")
        ]
        assert "Interrupted active turn. Cancelled 1 queued turn(s)." in handler._sent
        assert "unexpected hermes queued reply" not in handler._sent
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first_task
        remaining_tasks = list(handler._spawned_tasks)
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        for task in remaining_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.anyio
async def test_pma_missing_thread_resets_registry_and_recovers(tmp_path: Path) -> None:
    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    registry.set_thread_id(PMA_KEY, "stale")
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        model="gpt-5.1-codex-max",
    )
    client = _PMAClientStub()
    handler = _PMAHandler(record, client, tmp_path, registry)
    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=10587,
        from_user_id=42,
        text="hello",
        date=None,
        is_topic_message=True,
    )

    result = await handler._run_turn_and_collect_result(
        message,
        runtime=_RuntimeStub(),
        send_placeholder=False,
    )

    assert isinstance(result, _TurnRunResult)
    assert client.turn_start_calls[0] == "stale"
    assert client.turn_start_calls[-1] != "stale"
    assert registry.get_thread_id(PMA_KEY) != "stale"


class _PMAWorkspaceRouter:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record


def _write_prompt_state_sessions(hub_root: Path, *keys: str) -> Path:
    state_path = default_pma_prompt_state_path(hub_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-03-20T00:00:00Z",
                "sessions": {key: {"version": 1} for key in keys},
            }
        ),
        encoding="utf-8",
    )
    return state_path


class _PMAWorkspaceHandler(WorkspaceCommands):
    def __init__(
        self,
        record: TelegramTopicRecord,
        registry: AppServerThreadRegistry,
        *,
        hub_root: Optional[Path] = None,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace(require_topics=False)
        self._router = _PMAWorkspaceRouter(record)
        self._hub_thread_registry = registry
        self._hub_root = hub_root
        self._sent: list[str] = []
        self._record = record

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        reply_markup: Optional[object] = None,
    ) -> None:
        self._sent.append(text)

    def _pma_registry_key(
        self, record: "TelegramTopicRecord", message: Optional[TelegramMessage] = None
    ) -> str:
        from codex_autorunner.integrations.app_server.threads import pma_base_key

        agent = self._effective_agent(record)
        base_key = pma_base_key(agent)

        require_topics = getattr(self._config, "require_topics", False)
        if require_topics and message is not None:
            topic_key = f"{message.chat_id}:{message.thread_id or 'root'}"
            return f"{base_key}.{topic_key}"
        return base_key

    def _effective_agent(self, record: Optional["TelegramTopicRecord"]) -> str:
        if record and record.agent:
            return record.agent
        return "codex"


class _NewtRouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record
        self.update_snapshots: list[dict[str, object]] = []

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record

    async def update_topic(
        self, _chat_id: int, _thread_id: Optional[int], apply: object
    ) -> TelegramTopicRecord:
        if callable(apply):
            apply(self._record)
        self.update_snapshots.append(self._record.to_dict())
        return self._record


class _NewtClientStub:
    runtime_instance_id = "runtime-test-1"

    async def start(self) -> None:
        return None

    async def thread_start(self, workspace_path: str, *, agent: str) -> dict[str, str]:
        _ = agent
        return {"thread_id": "new-thread-id", "workspace_path": workspace_path}


class _NewtSupervisorStub:
    def __init__(self) -> None:
        self.client = _NewtClientStub()

    async def get_client(self, _workspace_root: Path) -> _NewtClientStub:
        return self.client


class _NewtEventsStub:
    async def stream(self, conversation_id: str, turn_id: str):
        _ = conversation_id, turn_id
        if False:
            yield ""


class _NewtHandler(WorkspaceCommands):
    def __init__(self, record: TelegramTopicRecord, *, hub_root: Path) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace()
        self._router = _NewtRouterStub(record)
        self._hub_root = hub_root
        self._sent: list[str] = []
        self.app_server_supervisor = _NewtSupervisorStub()
        self.app_server_events = _NewtEventsStub()

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        reply_markup: Optional[object] = None,
    ) -> None:
        self._sent.append(text)

    async def _client_for_workspace(self, _workspace_path: str) -> _NewtClientStub:
        return _NewtClientStub()

    def _canonical_workspace_root(
        self, workspace_path: Optional[str]
    ) -> Optional[Path]:
        if not workspace_path:
            return None
        return Path(workspace_path).expanduser().resolve()

    def _workspace_id_for_path(self, _workspace_path: str) -> Optional[str]:
        return None


@pytest.mark.anyio
async def test_archive_uses_shared_fresh_start_and_resets_topic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace_root),
        active_thread_id="thread-active",
        thread_ids=["thread-active", "thread-old"],
        rollout_path=str(workspace_root / "rollout.md"),
        pending_compact_seed="seed",
        pending_compact_seed_thread_id="thread-old",
    )
    record.thread_summaries = {
        "thread-active": ThreadSummary(
            user_preview="active",
            last_used_at="2026-03-19T00:00:00Z",
        )
    }
    handler = _NewtHandler(record, hub_root=hub_root)

    monkeypatch.setattr(
        "codex_autorunner.core.archive.resolve_workspace_archive_target",
        lambda workspace_root, **_kwargs: SimpleNamespace(
            base_repo_root=hub_root,
            base_repo_id="base",
            workspace_repo_id="repo",
            worktree_of="base",
            source_path="repo",
        ),
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.archive.archive_workspace_for_fresh_start",
        lambda **kwargs: (
            calls.append(kwargs)
            or SimpleNamespace(
                snapshot_id=None,
                archived_paths=(),
                archived_thread_ids=("managed-thread-1",),
            )
        ),
    )
    reset_calls: list[dict[str, object]] = []

    async def _fake_reset_telegram_thread_binding(
        *_args: object, **kwargs: object
    ) -> tuple[bool, str]:
        reset_calls.append(dict(kwargs))
        return True, "thread-fresh"

    monkeypatch.setattr(
        execution_commands_module,
        "_reset_telegram_thread_binding",
        _fake_reset_telegram_thread_binding,
    )

    await handler._handle_archive(
        TelegramMessage(
            update_id=1,
            message_id=2,
            chat_id=10,
            thread_id=20,
            from_user_id=30,
            text="/archive",
            date=None,
            is_topic_message=True,
        )
    )

    assert calls
    assert calls[0]["hub_root"] == hub_root
    assert calls[0]["worktree_repo_id"] == "repo"
    assert reset_calls
    assert reset_calls[0]["mode"] == "repo"
    assert record.active_thread_id is None
    assert record.thread_ids == []
    assert record.thread_summaries == {}
    assert record.rollout_path is None
    assert record.pending_compact_seed is None
    assert record.pending_compact_seed_thread_id is None
    assert handler._sent
    assert "Workspace CAR state was already clean." in handler._sent[-1]
    assert "Archived 1 managed thread." in handler._sent[-1]


@pytest.mark.anyio
async def test_archive_without_hub_root_does_not_substitute_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    record = TelegramTopicRecord(workspace_path=str(workspace_root))
    handler = _NewtHandler(record, hub_root=tmp_path / "unused")
    handler._hub_root = None

    monkeypatch.setattr(
        "codex_autorunner.core.archive.resolve_workspace_archive_target",
        lambda workspace_root, **_kwargs: SimpleNamespace(
            base_repo_root=workspace_root,
            base_repo_id="base",
            workspace_repo_id="repo",
            worktree_of="base",
            source_path="repo",
        ),
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.archive.archive_workspace_for_fresh_start",
        lambda **kwargs: (
            calls.append(kwargs)
            or SimpleNamespace(
                snapshot_id="snap-1",
                archived_paths=("tickets",),
                archived_thread_ids=(),
            )
        ),
    )

    await handler._handle_archive(
        TelegramMessage(
            update_id=1,
            message_id=2,
            chat_id=10,
            thread_id=20,
            from_user_id=30,
            text="/archive",
            date=None,
            is_topic_message=True,
        )
    )

    assert calls
    assert calls[0]["hub_root"] is None


def _patch_newt_branch_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def _fake_reset(repo_root: Path, branch_name: str) -> str:
        calls.append({"repo_root": repo_root, "branch_name": branch_name})
        return "master"

    monkeypatch.setattr(
        workspace_commands_module, "reset_branch_from_origin_main", _fake_reset
    )
    return calls


@pytest.mark.anyio
async def test_sync_telegram_thread_binding_archives_after_lost_backend_recovery() -> (
    None
):
    workspace = Path("/tmp/telegram-recovery-workspace").resolve()
    calls: list[tuple[str, str]] = []

    class _FakeThreadService:
        async def stop_thread(self, thread_target_id: str) -> Any:
            calls.append(("stop", thread_target_id))
            return SimpleNamespace(recovered_lost_backend=True)

        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id))
            assert workspace_root == workspace
            return "runtime-test-1"

        def archive_thread_target(self, thread_target_id: str) -> None:
            calls.append(("archive", thread_target_id))

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            calls.append(("create", agent))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-2",
                agent_id=agent,
                workspace_root=str(workspace_root),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"])))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="repo"),
            SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                workspace_root=str(workspace),
            ),
        ),
    )
    try:
        (
            _service,
            thread,
        ) = await execution_commands_module._sync_telegram_thread_binding(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="codex",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            backend_thread_id="backend-2",
            mode="repo",
            pma_enabled=False,
            replace_existing=True,
        )
    finally:
        monkeypatch.undo()

    assert thread.thread_target_id == "thread-2"
    assert calls == [
        ("stop", "thread-1"),
        ("archive", "thread-1"),
        ("resolve", "codex"),
        ("create", "codex"),
        ("bind", "thread-2"),
    ]


@pytest.mark.anyio
async def test_sync_telegram_thread_binding_ignores_backend_id_in_pma_mode() -> None:
    workspace = Path("/tmp/telegram-sync-pma-workspace").resolve()
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            raise AssertionError(
                "runtime instance lookup should be skipped in PMA mode"
            )

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            calls.append((thread_target_id, dict(kwargs)))
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                agent_id="codex",
                workspace_root=str(workspace),
            )

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            calls.append(("create", dict(kwargs)))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-2",
                agent_id=agent,
                workspace_root=str(workspace_root),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"])))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="pma"),
            SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                workspace_root=str(workspace),
            ),
        ),
    )
    try:
        (
            _service,
            thread,
        ) = await execution_commands_module._sync_telegram_thread_binding(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="codex",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            backend_thread_id="backend-2",
            mode="pma",
            pma_enabled=True,
            replace_existing=False,
        )
    finally:
        monkeypatch.undo()

    assert thread.thread_target_id == "thread-1"
    assert calls == [
        (
            "thread-1",
            {
                "backend_runtime_instance_id": None,
            },
        ),
        ("bind", "thread-1"),
    ]


@pytest.mark.anyio
async def test_sync_telegram_thread_binding_allows_missing_runtime_instance() -> None:
    workspace = Path("/tmp/telegram-runtime-missing-workspace").resolve()
    calls: list[tuple[str, str, object]] = []

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            assert workspace_root == workspace
            return None

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            metadata = kwargs.get("metadata") or {}
            calls.append(
                (
                    "create",
                    agent,
                    metadata.get("backend_runtime_instance_id"),
                )
            )
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-2",
                agent_id=agent,
                workspace_root=str(workspace_root),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            None,
            None,
        ),
    )
    try:
        (
            _service,
            thread,
        ) = await execution_commands_module._sync_telegram_thread_binding(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="opencode",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            backend_thread_id="backend-2",
            mode="repo",
            pma_enabled=False,
        )
    finally:
        monkeypatch.undo()

    assert thread.thread_target_id == "thread-2"
    assert calls == [
        ("resolve", "opencode", str(workspace)),
        ("create", "opencode", None),
        ("bind", "thread-2", "repo"),
    ]


@pytest.mark.anyio
async def test_resolve_telegram_managed_thread_allows_missing_runtime_instance() -> (
    None
):
    workspace = Path("/tmp/telegram-runtime-missing-resolve-workspace").resolve()
    calls: list[tuple[str, str, object]] = []

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            assert workspace_root == workspace
            return None

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            metadata = kwargs.get("metadata") or {}
            calls.append(
                (
                    "create",
                    agent,
                    metadata.get("backend_runtime_instance_id"),
                )
            )
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-3",
                agent_id=agent,
                workspace_root=str(workspace_root),
                backend_thread_id=None,
                lifecycle_status="active",
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            None,
            None,
        ),
    )
    try:
        (
            _service,
            thread,
        ) = await execution_commands_module._resolve_telegram_managed_thread(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="opencode",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            mode="repo",
            pma_enabled=False,
            backend_thread_id="backend-3",
            allow_new_thread=True,
        )
    finally:
        monkeypatch.undo()

    assert thread.thread_target_id == "thread-3"
    assert calls == [
        ("resolve", "opencode", str(workspace)),
        ("create", "opencode", None),
        ("bind", "thread-3", "repo"),
    ]


@pytest.mark.anyio
async def test_sync_telegram_thread_binding_rejects_rebind_when_runtime_missing() -> (
    None
):
    workspace = Path("/tmp/telegram-runtime-missing-rebind-workspace").resolve()
    calls: list[tuple[str, str, object]] = []

    current_thread = SimpleNamespace(
        thread_target_id="thread-existing",
        agent_id="opencode",
        workspace_root=str(workspace),
        backend_thread_id="backend-old",
    )

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            return None

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            calls.append(
                (
                    "resume",
                    thread_target_id,
                    kwargs.get("backend_thread_id"),
                )
            )
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                agent_id="opencode",
                workspace_root=str(workspace),
                backend_thread_id=kwargs.get("backend_thread_id"),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            None,
            current_thread,
        ),
    )
    try:
        _service, thread = (
            await execution_commands_module._sync_telegram_thread_binding(
                handlers,
                surface_key="topic-1",
                workspace_root=workspace,
                agent="opencode",
                repo_id="repo-1",
                resource_kind="repo",
                resource_id="repo-1",
                backend_thread_id="backend-new",
                mode="repo",
                pma_enabled=False,
            )
        )
    finally:
        monkeypatch.undo()

    assert thread.thread_target_id == "thread-existing"
    assert thread.backend_thread_id == "backend-old"
    assert calls == [
        ("resolve", "opencode", str(workspace)),
        ("resume", "thread-existing", "backend-old"),
        ("bind", "thread-existing", "repo"),
    ]


@pytest.mark.anyio
async def test_resolve_telegram_managed_thread_rejects_rebind_when_runtime_missing() -> (
    None
):
    workspace = Path("/tmp/telegram-runtime-missing-resolve-rebind-workspace").resolve()
    calls: list[tuple[str, str, object]] = []

    thread = SimpleNamespace(
        thread_target_id="thread-existing",
        agent_id="opencode",
        agent_profile=None,
        workspace_root=str(workspace),
        backend_thread_id="backend-old",
        lifecycle_status="active",
    )

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            return None

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            None,
            thread,
        ),
    )
    try:
        _service, resolved_thread = (
            await execution_commands_module._resolve_telegram_managed_thread(
                handlers,
                surface_key="topic-1",
                workspace_root=workspace,
                agent="opencode",
                repo_id="repo-1",
                resource_kind="repo",
                resource_id="repo-1",
                mode="repo",
                pma_enabled=False,
                backend_thread_id="backend-new",
                allow_new_thread=True,
            )
        )
    finally:
        monkeypatch.undo()

    assert resolved_thread.thread_target_id == "thread-existing"
    assert resolved_thread.backend_thread_id == "backend-old"
    assert calls == [
        ("resolve", "opencode", str(workspace)),
        ("bind", "thread-existing", "repo"),
    ]


@pytest.mark.anyio
async def test_resolve_telegram_managed_thread_keeps_requested_backend_thread_id_for_new_thread() -> (
    None
):
    workspace = Path("/tmp/telegram-runtime-missing-resolve-new-workspace").resolve()
    other_workspace = Path("/tmp/telegram-runtime-missing-existing-workspace").resolve()
    calls: list[tuple[str, str, object]] = []

    thread = SimpleNamespace(
        thread_target_id="thread-existing",
        agent_id="opencode",
        agent_profile=None,
        workspace_root=str(other_workspace),
        backend_thread_id="backend-old",
        lifecycle_status="active",
    )

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            return None

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            calls.append(("create", agent, kwargs.get("backend_thread_id")))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-new",
                agent_id=agent,
                agent_profile=None,
                workspace_root=str(workspace_root),
                backend_thread_id=kwargs.get("backend_thread_id"),
                lifecycle_status="active",
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            None,
            thread,
        ),
    )
    try:
        _service, resolved_thread = (
            await execution_commands_module._resolve_telegram_managed_thread(
                handlers,
                surface_key="topic-1",
                workspace_root=workspace,
                agent="opencode",
                repo_id="repo-1",
                resource_kind="repo",
                resource_id="repo-1",
                mode="repo",
                pma_enabled=False,
                backend_thread_id="backend-new",
                allow_new_thread=True,
            )
        )
    finally:
        monkeypatch.undo()

    assert resolved_thread.thread_target_id == "thread-new"
    assert resolved_thread.backend_thread_id == "backend-new"
    assert calls == [
        ("resolve", "opencode", str(workspace)),
        ("create", "opencode", "backend-new"),
        ("bind", "thread-new", "repo"),
    ]


async def test_reset_telegram_thread_binding_archives_after_lost_backend_recovery() -> (
    None
):
    workspace = Path("/tmp/telegram-reset-workspace").resolve()
    calls: list[tuple[str, str]] = []

    class _FakeThreadService:
        async def stop_thread(self, thread_target_id: str) -> Any:
            calls.append(("stop", thread_target_id))
            return SimpleNamespace(recovered_lost_backend=True)

        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id))
            assert workspace_root == workspace
            return "runtime-test-1"

        def archive_thread_target(self, thread_target_id: str) -> None:
            calls.append(("archive", thread_target_id))

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            calls.append(("create", agent))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-2",
                agent_id=agent,
                workspace_root=str(workspace_root),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"])))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="pma"),
            SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                workspace_root=str(workspace),
            ),
        ),
    )
    try:
        had_previous, new_thread_id = (
            await execution_commands_module._reset_telegram_thread_binding(
                handlers,
                surface_key="topic-1",
                workspace_root=workspace,
                agent="codex",
                repo_id="repo-1",
                resource_kind="repo",
                resource_id="repo-1",
                mode="pma",
                pma_enabled=True,
            )
        )
    finally:
        monkeypatch.undo()

    assert had_previous is True
    assert new_thread_id == "thread-2"
    assert calls == [
        ("stop", "thread-1"),
        ("archive", "thread-1"),
        ("create", "codex"),
        ("bind", "thread-2"),
    ]


@pytest.mark.anyio
async def test_resume_thread_by_id_rebinds_managed_thread_before_topic_mirror_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Path("/tmp/telegram-resume-managed-thread").resolve()
    record = TelegramTopicRecord(
        agent="codex",
        workspace_path=str(workspace),
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
    )
    resolve_calls: list[dict[str, Any]] = []

    class _RouterStub:
        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return record

        async def update_topic(
            self, _chat_id: int, _thread_id: Optional[int], apply
        ) -> TelegramTopicRecord:
            apply(record)
            return record

    class _StoreStub:
        async def update_topic(self, _key: str, apply) -> TelegramTopicRecord:
            apply(record)
            return record

    class _ClientStub:
        async def thread_resume(self, thread_id: str) -> dict[str, Any]:
            return {
                "thread_id": thread_id,
                "agent": "codex",
                "cwd": str(workspace),
                "path": str(workspace),
                "thread": {
                    "id": thread_id,
                    "path": str(workspace),
                    "agent": "codex",
                },
            }

    class _ResumeHandler(WorkspaceCommands):
        def __init__(self) -> None:
            self._logger = logging.getLogger("test")
            self._router = _RouterStub()
            self._store = _StoreStub()
            self._resume_options: dict[str, SelectionState] = {}
            self._config = SimpleNamespace(
                root=workspace,
                defaults=SimpleNamespace(policies_for_mode=lambda _mode: (None, None)),
            )
            self._managed_thread_rebound = False
            self.apply_sync_flags: list[bool] = []
            self.sent: list[str] = []

        async def _resolve_topic_key(
            self, _chat_id: int, _thread_id: Optional[int]
        ) -> str:
            return "123:root"

        async def _client_for_workspace(self, _workspace_path: str) -> _ClientStub:
            return _ClientStub()

        async def _refresh_workspace_id(
            self, _key: str, _record: TelegramTopicRecord
        ) -> Optional[str]:
            return None

        async def _find_thread_conflict(
            self, _thread_id: str, *, key: str
        ) -> Optional[str]:
            _ = key
            return None

        async def _finalize_selection(
            self, _key: str, _callback: object, text: str
        ) -> None:
            self.sent.append(text)

        async def _apply_thread_result(
            self,
            chat_id: int,
            thread_id: Optional[int],
            result: Any,
            *,
            active_thread_id: Optional[str] = None,
            overwrite_defaults: bool = False,
            sync_binding: bool = True,
        ) -> TelegramTopicRecord:
            _ = chat_id, thread_id, result, overwrite_defaults
            assert self._managed_thread_rebound is True
            self.apply_sync_flags.append(sync_binding)
            record.active_thread_id = active_thread_id
            if active_thread_id:
                record.thread_ids = [active_thread_id]
            return record

    async def _fake_resolve_telegram_managed_thread(
        _handlers: Any, **kwargs: Any
    ) -> tuple[Any, Any]:
        resolve_calls.append(kwargs)
        handler._managed_thread_rebound = True
        return object(), SimpleNamespace(thread_target_id="managed-thread-2")

    monkeypatch.setattr(
        execution_commands_module,
        "_resolve_telegram_managed_thread",
        _fake_resolve_telegram_managed_thread,
    )

    handler = _ResumeHandler()
    await handler._resume_thread_by_id("123:root", "backend-thread-2")

    assert resolve_calls == [
        {
            "surface_key": "123:root",
            "workspace_root": workspace,
            "agent": "codex",
            "agent_profile": None,
            "repo_id": "repo-1",
            "resource_kind": "repo",
            "resource_id": "repo-1",
            "mode": "repo",
            "pma_enabled": False,
            "backend_thread_id": "backend-thread-2",
            "allow_new_thread": True,
        }
    ]
    assert handler.apply_sync_flags == [False]
    assert record.active_thread_id == "backend-thread-2"
    assert record.thread_ids == ["backend-thread-2"]


@pytest.mark.anyio
async def test_apply_compact_summary_uses_shared_lifecycle_before_topic_mirror_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = TelegramTopicRecord(
        agent="codex",
        workspace_path=str(workspace),
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        active_thread_id="backend-old",
    )
    lifecycle_calls: list[tuple[str, Any, Any]] = []
    compact_seed_calls: list[tuple[str, str]] = []

    class _RouterStub:
        async def update_topic(
            self, _chat_id: int, _thread_id: Optional[int], apply
        ) -> TelegramTopicRecord:
            apply(record)
            return record

    class _ThreadService:
        async def stop_thread(self, thread_target_id: str) -> Any:
            lifecycle_calls.append(("stop", thread_target_id, None))
            return SimpleNamespace(recovered_lost_backend=False)

        def archive_thread_target(self, thread_target_id: str) -> None:
            lifecycle_calls.append(("archive", thread_target_id, None))

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            lifecycle_calls.append(("create", agent, kwargs.get("backend_thread_id")))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="managed-new",
                agent_id=agent,
                workspace_root=str(workspace_root),
                backend_thread_id=kwargs.get("backend_thread_id"),
                lifecycle_status="active",
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            lifecycle_calls.append(
                ("bind", kwargs["thread_target_id"], kwargs.get("mode"))
            )

    class _CompactHandler(TelegramCommandHandlers):
        def __init__(self) -> None:
            self._logger = logging.getLogger("test")
            self._router = _RouterStub()
            self._config = SimpleNamespace(
                root=tmp_path,
                defaults=SimpleNamespace(policies_for_mode=lambda _mode: (None, None)),
            )
            self.sync_binding_flags: list[bool] = []
            self._spawn_task = lambda coro: None

        def _resolve_workspace_path(
            self, _record: TelegramTopicRecord, allow_pma: bool = False
        ) -> tuple[Optional[str], Optional[str]]:
            _ = allow_pma
            return str(workspace), None

        async def _resolve_topic_key(
            self, _chat_id: int, _thread_id: Optional[int]
        ) -> str:
            return "123:root"

    def _fake_set_thread_compact_seed(
        self, managed_thread_id: str, compact_seed: Optional[str], **_kwargs: Any
    ) -> None:
        compact_seed_calls.append((managed_thread_id, str(compact_seed or "")))

    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _ThreadService(),
            SimpleNamespace(thread_target_id="managed-old", mode="repo"),
            SimpleNamespace(
                thread_target_id="managed-old",
                agent_id="codex",
                workspace_root=str(workspace),
                lifecycle_status="active",
            ),
        ),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.pma_thread_store.PmaThreadStore.set_thread_compact_seed",
        _fake_set_thread_compact_seed,
    )

    handler = _CompactHandler()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=123,
        thread_id=None,
        from_user_id=456,
        text="/compact",
        date=None,
        is_topic_message=False,
    )

    success, error = await handler._apply_compact_summary(
        message,
        record,
        "summary text",
    )

    assert success is True
    assert error is None
    assert lifecycle_calls == [
        ("stop", "managed-old", None),
        ("archive", "managed-old", None),
        ("create", "codex", None),
        ("bind", "managed-new", "repo"),
    ]
    assert compact_seed_calls == [("managed-new", "summary text")]
    assert handler.sync_binding_flags == []
    assert record.active_thread_id is None
    assert record.pending_compact_seed_thread_id == "managed-new"
    assert "summary text" in (record.pending_compact_seed or "")


@pytest.mark.anyio
async def test_repo_managed_thread_turn_matches_pending_compact_seed_by_managed_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_input_items: list[dict[str, Any]] | None = None

    class _Coordinator:
        async def submit_execution(
            self,
            request: Any,
            *,
            client_request_id: Optional[str],
            sandbox_policy: Optional[Any],
            begin_execution: Any = None,
        ) -> Any:
            nonlocal captured_input_items
            _ = client_request_id, sandbox_policy, begin_execution
            captured_input_items = request.input_items
            started_execution = SimpleNamespace(
                thread=SimpleNamespace(
                    thread_target_id="managed-new",
                    backend_thread_id=None,
                ),
                execution=SimpleNamespace(status="queued"),
            )
            return SimpleNamespace(started_execution=started_execution, queued=True)

    class _Handler:
        def __init__(self) -> None:
            self._logger = logging.getLogger("test")
            self._router = SimpleNamespace(update_topic=_update_topic)

        async def _prepare_turn_placeholder(
            self,
            _message: TelegramMessage,
            *,
            placeholder_id: Optional[int],
            send_placeholder: bool,
            queued: bool,
        ) -> Optional[int]:
            _ = send_placeholder, queued
            return placeholder_id

        def _effective_agent_profile(
            self, _record: TelegramTopicRecord
        ) -> Optional[str]:
            return None

        def _effective_runtime_agent(self, _record: TelegramTopicRecord) -> str:
            return "codex"

    async def _update_topic(
        _chat_id: int, _thread_id: Optional[int], apply: Any
    ) -> TelegramTopicRecord:
        apply(record)
        return record

    async def _fake_resolve_managed_thread(*_args: Any, **_kwargs: Any) -> Any:
        return object(), SimpleNamespace(
            thread_target_id="managed-new",
            backend_thread_id=None,
        )

    monkeypatch.setattr(
        execution_commands_module,
        "_resolve_telegram_managed_thread",
        _fake_resolve_managed_thread,
    )
    monkeypatch.setattr(
        execution_commands_module,
        "_build_telegram_managed_thread_coordinator",
        lambda *_args, **_kwargs: _Coordinator(),
    )
    monkeypatch.setattr(
        execution_commands_module,
        "_ensure_telegram_managed_thread_queue_worker",
        lambda *_args, **_kwargs: None,
    )

    handler = _Handler()
    record = TelegramTopicRecord(
        workspace_path="/tmp/workspace",
        pending_compact_seed="summary seed",
        pending_compact_seed_thread_id="managed-new",
    )
    result = await execution_commands_module._run_telegram_managed_thread_turn(
        handler,
        message=TelegramMessage(
            update_id=1,
            message_id=2,
            chat_id=123,
            thread_id=456,
            from_user_id=789,
            text="hello",
            date=None,
            is_topic_message=True,
        ),
        runtime=SimpleNamespace(),
        record=record,
        topic_key="123:456",
        prompt_text="hello",
        input_items=[{"type": "text", "text": "original"}],
        send_placeholder=False,
        send_failure_response=False,
        transcript_message_id=None,
        transcript_text=None,
        placeholder_id=99,
        mode="repo",
        pma_enabled=False,
        execution_prompt="runtime prompt",
    )

    assert isinstance(result, execution_commands_module._TurnRunResult)
    assert result.response == "Queued (waiting for available worker...)"
    assert captured_input_items is not None
    assert captured_input_items[0] == {"type": "text", "text": "summary seed"}
    assert captured_input_items[1] == {"type": "text", "text": "runtime prompt"}
    assert record.pending_compact_seed is None
    assert record.pending_compact_seed_thread_id is None


@pytest.mark.anyio
async def test_sync_telegram_thread_binding_keeps_requested_backend_thread_id_for_replacement() -> (
    None
):
    workspace = Path("/tmp/telegram-sync-runtime-missing-workspace").resolve()
    calls: list[tuple[str, str, object]] = []

    class _FakeThreadService:
        async def stop_thread(self, thread_target_id: str) -> Any:
            calls.append(("stop", thread_target_id, None))
            return SimpleNamespace(recovered_lost_backend=False)

        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            return None

        def archive_thread_target(self, thread_target_id: str) -> None:
            calls.append(("archive", thread_target_id, None))

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            calls.append(("create", agent, kwargs.get("backend_thread_id")))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-2",
                agent_id=agent,
                workspace_root=str(workspace_root),
                backend_thread_id=kwargs.get("backend_thread_id"),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="repo"),
            SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                workspace_root=str(workspace),
                backend_thread_id="backend-old",
            ),
        ),
    )
    try:
        _service, current_thread = (
            await execution_commands_module._sync_telegram_thread_binding(
                handlers,
                surface_key="topic-1",
                workspace_root=workspace,
                agent="codex",
                repo_id="repo-1",
                resource_kind="repo",
                resource_id="repo-1",
                backend_thread_id="backend-new",
                mode="repo",
                pma_enabled=False,
                replace_existing=True,
            )
        )
    finally:
        monkeypatch.undo()

    assert current_thread.thread_target_id == "thread-2"
    assert current_thread.backend_thread_id == "backend-new"
    assert calls == [
        ("stop", "thread-1", None),
        ("archive", "thread-1", None),
        ("resolve", "codex", str(workspace)),
        ("create", "codex", "backend-new"),
        ("bind", "thread-2", "repo"),
    ]


@pytest.mark.anyio
async def test_pma_new_resets_session(tmp_path: Path) -> None:
    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    registry.set_thread_id(PMA_OPENCODE_KEY, "old-thread")
    state_path = _write_prompt_state_sessions(tmp_path, PMA_OPENCODE_KEY)
    record = TelegramTopicRecord(
        pma_enabled=True, workspace_path=None, agent="opencode"
    )
    handler = _PMAWorkspaceHandler(record, registry, hub_root=tmp_path)
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-2002,
        thread_id=333,
        from_user_id=99,
        text="/new",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_new(message)

    assert registry.get_thread_id(PMA_OPENCODE_KEY) is None
    sessions = json.loads(state_path.read_text(encoding="utf-8")).get("sessions", {})
    assert PMA_OPENCODE_KEY not in sessions
    assert (
        handler._sent
        and handler._sent[-1]
        == "Started a fresh PMA session for `opencode` (new thread ready)."
    )


@pytest.mark.anyio
async def test_pma_new_resets_managed_binding_when_runtime_threads_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None, agent="codex")
    handler = _PMAWorkspaceHandler(record, registry, hub_root=tmp_path)
    handler._config = SimpleNamespace(require_topics=False, root=tmp_path)
    handler._spawn_task = lambda coro: None
    calls: list[dict[str, object]] = []

    async def _fake_reset_telegram_thread_binding(
        _handlers: Any, **kwargs: Any
    ) -> tuple[bool, str]:
        calls.append(kwargs)
        return True, "thread-2"

    monkeypatch.setattr(
        execution_commands_module,
        "_reset_telegram_thread_binding",
        _fake_reset_telegram_thread_binding,
    )
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-2002,
        thread_id=333,
        from_user_id=99,
        text="/new",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_new(message)

    assert calls
    assert calls[-1]["surface_key"] == "-2002:333"
    assert calls[-1]["mode"] == "pma"
    assert calls[-1]["pma_enabled"] is True
    assert (
        handler._sent
        and handler._sent[-1]
        == "Started a fresh PMA session for `codex` (new thread ready)."
    )


@pytest.mark.anyio
async def test_pma_new_resets_scoped_key_when_require_topics_enabled(
    tmp_path: Path,
) -> None:
    from codex_autorunner.integrations.app_server.threads import pma_topic_scoped_key

    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    scoped_key = pma_topic_scoped_key(
        agent="opencode",
        chat_id=-2002,
        thread_id=333,
        topic_key_fn=lambda c, t: f"{c}:{t or 'root'}",
    )
    registry.set_thread_id(scoped_key, "old-scoped-thread")
    state_path = _write_prompt_state_sessions(tmp_path, scoped_key)

    record = TelegramTopicRecord(
        pma_enabled=True, workspace_path=None, agent="opencode"
    )
    handler = _PMAWorkspaceHandlerWithScopedKey(
        record, registry, hub_root=tmp_path, require_topics=True
    )
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-2002,
        thread_id=333,
        from_user_id=99,
        text="/new",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_new(message)

    assert registry.get_thread_id(scoped_key) is None
    sessions = json.loads(state_path.read_text(encoding="utf-8")).get("sessions", {})
    assert scoped_key not in sessions
    assert (
        handler._sent
        and handler._sent[-1]
        == "Started a fresh PMA session for `opencode` (new thread ready)."
    )


@pytest.mark.anyio
async def test_pma_reset_resets_scoped_key_when_require_topics_enabled(
    tmp_path: Path,
) -> None:
    from codex_autorunner.integrations.app_server.threads import pma_topic_scoped_key

    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    scoped_key = pma_topic_scoped_key(
        agent="codex",
        chat_id=-1001,
        thread_id=42,
        topic_key_fn=lambda c, t: f"{c}:{t or 'root'}",
    )
    registry.set_thread_id(scoped_key, "old-scoped-thread")
    state_path = _write_prompt_state_sessions(tmp_path, scoped_key)

    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None, agent="codex")
    handler = _PMAWorkspaceHandlerWithScopedKey(
        record, registry, hub_root=tmp_path, require_topics=True
    )
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-1001,
        thread_id=42,
        from_user_id=99,
        text="/reset",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_reset(message)

    assert registry.get_thread_id(scoped_key) is None
    sessions = json.loads(state_path.read_text(encoding="utf-8")).get("sessions", {})
    assert scoped_key not in sessions
    assert (
        handler._sent
        and handler._sent[-1] == "Reset PMA thread state (fresh state) for `codex`."
    )


class _PMAWorkspaceHandlerWithScopedKey(WorkspaceCommands):
    def __init__(
        self,
        record: TelegramTopicRecord,
        registry: AppServerThreadRegistry,
        *,
        hub_root: Optional[Path] = None,
        require_topics: bool = False,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace(require_topics=require_topics)
        self._router = _PMAWorkspaceRouter(record)
        self._hub_thread_registry = registry
        self._hub_root = hub_root
        self._sent: list[str] = []
        self._record = record

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        reply_markup: Optional[object] = None,
    ) -> None:
        self._sent.append(text)

    def _pma_registry_key(
        self, record: "TelegramTopicRecord", message: Optional[TelegramMessage] = None
    ) -> str:
        from codex_autorunner.integrations.app_server.threads import pma_base_key

        agent = (
            self._effective_agent(record)
            if hasattr(self, "_effective_agent")
            else record.agent or "codex"
        )
        base_key = pma_base_key(agent)

        require_topics = getattr(self._config, "require_topics", False)
        if require_topics and message is not None:
            topic_key = f"{message.chat_id}:{message.thread_id or 'root'}"
            return f"{base_key}.{topic_key}"
        return base_key

    def _effective_agent(self, record: Optional["TelegramTopicRecord"]) -> str:
        if record and record.agent:
            return record.agent
        return "codex"


@pytest.mark.anyio
async def test_newt_branch_name_includes_chat_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "repo"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        thread_ids=["old-thread"],
        active_thread_id="old-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)
    message = TelegramMessage(
        update_id=100,
        message_id=200,
        chat_id=-7777,
        thread_id=333,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert branch_calls[0]["repo_root"] == workspace.resolve()
    expected_branch = (
        "thread-chat-7777-thread-333-"
        f"{hashlib.sha256(str(workspace.resolve()).encode('utf-8')).hexdigest()[:10]}"
    )
    assert branch_calls[0]["branch_name"] == expected_branch


@pytest.mark.anyio
async def test_newt_runs_hub_setup_commands_for_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "repo"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        repo_id="base-repo",
        workspace_path=str(workspace),
        thread_ids=["old-thread"],
        active_thread_id="old-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)

    class _HubSupervisorStub:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def run_setup_commands_for_workspace(
            self, workspace_path: Path, *, repo_id_hint: Optional[str] = None
        ) -> int:
            self.calls.append(
                {"workspace_path": workspace_path, "repo_id_hint": repo_id_hint}
            )
            return 2

    hub_supervisor = _HubSupervisorStub()
    handler._hub_supervisor = hub_supervisor  # type: ignore[attr-defined]
    message = TelegramMessage(
        update_id=100,
        message_id=200,
        chat_id=-7777,
        thread_id=333,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert hub_supervisor.calls == [
        {"workspace_path": workspace.resolve(), "repo_id_hint": "base-repo"}
    ]
    assert any("Ran 2 setup command(s)." in text for text in handler._sent)


@pytest.mark.anyio
async def test_newt_infers_base_repo_from_worktree_id_when_missing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "worktrees" / "base-repo--thread-chat-7777-thread-333"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        thread_ids=["old-thread"],
        active_thread_id="old-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)
    message = TelegramMessage(
        update_id=100,
        message_id=200,
        chat_id=-7777,
        thread_id=333,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert branch_calls[0]["repo_root"] == workspace.resolve()
    expected_branch = (
        "thread-chat-7777-thread-333-"
        f"{hashlib.sha256(str(workspace.resolve()).encode('utf-8')).hexdigest()[:10]}"
    )
    assert branch_calls[0]["branch_name"] == expected_branch
    assert all("Failed to reset branch" not in text for text in handler._sent)


@pytest.mark.anyio
async def test_newt_infers_base_repo_from_legacy_wt_worktree_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "worktrees" / "codex-autorunner-wt-1"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        thread_ids=["old-thread"],
        active_thread_id="old-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)
    message = TelegramMessage(
        update_id=100,
        message_id=200,
        chat_id=-7777,
        thread_id=333,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert branch_calls[0]["repo_root"] == workspace.resolve()
    expected_branch = (
        "thread-chat-7777-thread-333-"
        f"{hashlib.sha256(str(workspace.resolve()).encode('utf-8')).hexdigest()[:10]}"
    )
    assert branch_calls[0]["branch_name"] == expected_branch


@pytest.mark.anyio
async def test_newt_prefers_longest_manifest_base_match_for_worktree_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "worktrees" / "ml--infra--thread--chat-7777-thread-333"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        thread_ids=["old-thread"],
        active_thread_id="old-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)
    message = TelegramMessage(
        update_id=100,
        message_id=200,
        chat_id=-7777,
        thread_id=333,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert branch_calls[0]["repo_root"] == workspace.resolve()
    expected_branch = (
        "thread-chat-7777-thread-333-"
        f"{hashlib.sha256(str(workspace.resolve()).encode('utf-8')).hexdigest()[:10]}"
    )
    assert branch_calls[0]["branch_name"] == expected_branch


@pytest.mark.anyio
async def test_newt_thread_fallback_and_workspace_state_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "repo"
    workspace.mkdir(parents=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        workspace_id="stale-workspace-id",
        active_thread_id="stale-thread",
        thread_ids=["stale-thread"],
        thread_summaries={"stale-thread": ThreadSummary(user_preview="stale")},
        rollout_path="old-rollout",
        pending_compact_seed="old-seed",
        pending_compact_seed_thread_id="old-seed-thread",
    )
    handler = _NewtHandler(record, hub_root=hub_root)
    branch_calls = _patch_newt_branch_reset(monkeypatch)
    message = TelegramMessage(
        update_id=909,
        message_id=808,
        chat_id=-123456,
        thread_id=None,
        from_user_id=42,
        text="/newt",
        date=None,
        is_topic_message=False,
    )

    await handler._handle_newt(message)

    assert len(branch_calls) == 1
    assert branch_calls[0]["repo_root"] == workspace.resolve()
    expected_branch = (
        "thread-chat-123456-msg-808-upd-909-"
        f"{hashlib.sha256(str(workspace.resolve()).encode('utf-8')).hexdigest()[:10]}"
    )
    assert branch_calls[0]["branch_name"] == expected_branch

    # First topic update is /newt state reset before new thread is attached.
    reset_snapshot = handler._router.update_snapshots[0]
    assert reset_snapshot["workspace_id"] == "stale-workspace-id"
    assert reset_snapshot["active_thread_id"] is None
    assert reset_snapshot["thread_ids"] == []
    assert reset_snapshot["thread_summaries"] == {}
    assert reset_snapshot["rollout_path"] is None
    assert reset_snapshot["pending_compact_seed"] is None
    assert reset_snapshot["pending_compact_seed_thread_id"] is None
    assert handler._sent
    assert (
        handler._sent[-1]
        .splitlines()[0]
        .startswith("Reset branch `thread-chat-123456-msg-808-upd-909-")
    )
    assert handler._sent[-1].splitlines()[1].startswith("Started new thread ")


@pytest.mark.anyio
async def test_pma_resume_uses_hub_root(tmp_path: Path) -> None:
    """Test that /resume works for PMA topics by using hub root."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None, agent="codex")

    class _ResumeClientStub:
        async def thread_list(self, cursor: Optional[str] = None, limit: int = 100):
            return {
                "entries": [
                    {
                        "id": "thread-1",
                        "workspace_path": str(hub_root),
                        "rollout_path": None,
                        "preview": {"user": "Test", "assistant": "Response"},
                    }
                ],
                "cursor": None,
            }

    class _ResumeRouterStub:
        def __init__(self, record: TelegramTopicRecord) -> None:
            self._record = record

        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return self._record

    class _ResumeHandler(WorkspaceCommands):
        def __init__(self, record: TelegramTopicRecord, hub_root: Path) -> None:
            self._logger = logging.getLogger("test")
            self._config = SimpleNamespace()
            self._router = _ResumeRouterStub(record)
            self._hub_root = hub_root
            self._resume_options: dict[str, SelectionState] = {}
            self._sent: list[str] = []

            async def _store_load():
                return SimpleNamespace(topics={})

            async def _store_update_topic(k, f):
                return None

            self._store = SimpleNamespace(
                load=_store_load,
                update_topic=_store_update_topic,
            )

        async def _resolve_topic_key(
            self, chat_id: int, thread_id: Optional[int]
        ) -> str:
            return f"{chat_id}:{thread_id}"

        async def _send_message(
            self,
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int],
            reply_to: Optional[int],
            reply_markup: Optional[object] = None,
        ) -> None:
            self._sent.append(text)

        async def _client_for_workspace(self, workspace_path: str):
            return _ResumeClientStub()

    handler = _ResumeHandler(record, hub_root)
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-2002,
        thread_id=333,
        from_user_id=99,
        text="/resume",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_resume(message, "")

    # Should not send "Topic not bound" error - PMA should use hub root
    assert not any("Topic not bound" in msg for msg in handler._sent)


class _OpencodeResumeClientMissingSession:
    async def get_session(self, session_id: str) -> dict[str, object]:
        request = httpx.Request("GET", f"http://opencode.local/session/{session_id}")
        response = httpx.Response(
            404,
            request=request,
            json={"error": {"message": f"session not found: {session_id}"}},
        )
        raise httpx.HTTPStatusError(
            f"Client error '404 Not Found' for url '{request.url}'",
            request=request,
            response=response,
        )


class _OpencodeResumeSupervisorStub:
    def __init__(self, client: _OpencodeResumeClientMissingSession) -> None:
        self._client = client

    async def get_client(self, _root: Path) -> _OpencodeResumeClientMissingSession:
        return self._client


class _OpencodeResumeRouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record

    async def update_topic(
        self, _chat_id: int, _thread_id: Optional[int], apply: object
    ) -> TelegramTopicRecord:
        if callable(apply):
            apply(self._record)
        return self._record


class _OpencodeResumeStoreStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def update_topic(self, _key: str, apply: object) -> None:
        if callable(apply):
            apply(self._record)


class _OpencodeResumeHandler(WorkspaceCommands):
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._logger = logging.getLogger("test")
        self._router = _OpencodeResumeRouterStub(record)
        self._store = _OpencodeResumeStoreStub(record)
        self._resume_options: dict[str, SelectionState] = {}
        self._config = SimpleNamespace()
        self._opencode_supervisor = _OpencodeResumeSupervisorStub(
            _OpencodeResumeClientMissingSession()
        )
        self.answers: list[str] = []
        self.final_messages: list[str] = []

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _answer_callback(self, _callback: object, text: str) -> None:
        self.answers.append(text)

    async def _finalize_selection(
        self, _key: str, _callback: object, text: str
    ) -> None:
        self.final_messages.append(text)

    async def _find_thread_conflict(
        self, _thread_id: str, *, key: str
    ) -> Optional[str]:
        return None

    def _canonical_workspace_root(
        self, workspace_path: Optional[str]
    ) -> Optional[Path]:
        if not workspace_path:
            return None
        return Path(workspace_path).expanduser().resolve()


@pytest.mark.anyio
async def test_resume_opencode_missing_session_clears_stale_topic_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    stale_session = "session-stale"
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        agent="opencode",
        active_thread_id=stale_session,
        thread_ids=[stale_session, "session-live"],
    )
    record.thread_summaries[stale_session] = ThreadSummary(
        user_preview="stale",
    )
    handler = _OpencodeResumeHandler(record)
    key = await handler._resolve_topic_key(-1001, 77)

    await handler._resume_opencode_thread_by_id(key, stale_session)

    assert record.active_thread_id is None
    assert stale_session not in record.thread_ids
    assert stale_session not in record.thread_summaries
    assert handler.answers == []
    assert any("Thread no longer exists." in text for text in handler.final_messages)


class _PmaTargetsRouterStub:
    def __init__(self, record: Optional[TelegramTopicRecord]) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> Optional[TelegramTopicRecord]:
        return self._record

    async def ensure_topic(
        self, _chat_id: int, _thread_id: Optional[int]
    ) -> TelegramTopicRecord:
        if self._record is None:
            self._record = TelegramTopicRecord()
        return self._record

    async def update_topic(
        self, _chat_id: int, _thread_id: Optional[int], apply: object
    ) -> TelegramTopicRecord:
        if self._record is None:
            self._record = TelegramTopicRecord()
        if callable(apply):
            apply(self._record)
        return self._record


class _PmaTargetsHandler(TelegramCommandHandlers):
    def __init__(
        self,
        *,
        hub_root: Path,
        record: Optional[TelegramTopicRecord],
        pma_enabled: bool = True,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._hub_root = hub_root
        self._hub_supervisor = SimpleNamespace(
            hub_config=SimpleNamespace(pma=SimpleNamespace(enabled=pma_enabled))
        )
        self._router = _PmaTargetsRouterStub(record)
        self.sent: list[str] = []

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> None:
        self.sent.append(text)


class _McpClientStub:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def request(self, _method: str, _params: dict[str, object]) -> object:
        raise self._exc


class _McpHandler(TelegramCommandHandlers):
    def __init__(self, record: TelegramTopicRecord, client: _McpClientStub) -> None:
        self._logger = logging.getLogger("test")
        self._router = _PMARouterStub(record)
        self._client = client
        self.sent: list[str] = []

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _client_for_workspace(self, _workspace_path: str) -> _McpClientStub:
        return self._client

    async def _refresh_workspace_id(
        self, _key: str, record: TelegramTopicRecord
    ) -> Optional[str]:
        return record.workspace_id

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> None:
        self.sent.append(text)


def _make_pma_message(
    *, chat_id: int = -1001, thread_id: Optional[int] = 55
) -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=chat_id,
        thread_id=thread_id,
        from_user_id=99,
        text="/pma",
        date=None,
        is_topic_message=thread_id is not None,
    )


@pytest.mark.anyio
async def test_mcp_lists_failure_message_on_app_server_response_error() -> None:
    record = TelegramTopicRecord(
        workspace_path="/tmp/workspace",
        workspace_id="workspace-1",
    )
    handler = _McpHandler(
        record,
        _McpClientStub(
            CodexAppServerResponseError(
                method="mcpServerStatus/list",
                code=-32601,
                message="method not found",
                data=None,
            )
        ),
    )
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-1001,
        thread_id=55,
        from_user_id=99,
        text="/mcp",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_mcp(message, "", _RuntimeStub())

    assert handler.sent == [
        "Failed to list MCP servers; check logs for details. (conversation -1001:55)"
    ]


@pytest.mark.anyio
async def test_pma_on_enables_mode(tmp_path: Path) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        repo_id="repo-1",
        workspace_path=str(tmp_path / "repo"),
        workspace_id="workspace-1",
        active_thread_id="thread-1",
    )
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "on", _RuntimeStub())

    assert record.pma_enabled is True
    assert (
        handler.sent[-1]
        == "PMA mode enabled. Use /pma off to exit. Previous binding saved."
    )


@pytest.mark.anyio
async def test_pma_off_disables_mode(tmp_path: Path) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        pma_prev_repo_id="repo-2",
        pma_prev_workspace_path=str(tmp_path / "repo"),
        pma_prev_workspace_id="workspace-2",
        pma_prev_active_thread_id="thread-2",
    )
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "off", _RuntimeStub())

    assert record.pma_enabled is False
    assert (
        handler.sent[-1]
        == f"PMA mode disabled. Restored binding to {tmp_path / 'repo'}."
    )


@pytest.mark.anyio
async def test_pma_status_reports_disabled_mode(tmp_path: Path) -> None:
    record = TelegramTopicRecord(pma_enabled=False)
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "status", _RuntimeStub())

    assert handler.sent[-1] == "PMA mode: disabled\nCurrent workspace: unbound"


@pytest.mark.anyio
async def test_pma_status_reports_bound_workspace_when_disabled(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    record = TelegramTopicRecord(pma_enabled=False, workspace_path=str(workspace))
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "status", _RuntimeStub())

    assert handler.sent[-1] == f"PMA mode: disabled\nCurrent workspace: {workspace}"


@pytest.mark.anyio
async def test_pma_on_is_idempotent_when_already_enabled(tmp_path: Path) -> None:
    record = TelegramTopicRecord(pma_enabled=True)
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "on", _RuntimeStub())

    assert (
        handler.sent[-1]
        == "PMA mode is already enabled for this topic. Use /pma off to exit."
    )


@pytest.mark.anyio
async def test_pma_without_subcommand_reports_status(tmp_path: Path) -> None:
    record = TelegramTopicRecord(pma_enabled=False)
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "", _RuntimeStub())

    assert handler.sent[-1] == "PMA mode: disabled\nCurrent workspace: unbound"


@pytest.mark.anyio
async def test_pma_targets_subcommand_uses_usage_text(tmp_path: Path) -> None:
    handler = _PmaTargetsHandler(
        hub_root=tmp_path / "hub", record=TelegramTopicRecord()
    )
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "targets", _RuntimeStub())

    assert handler.sent[-1] == "Usage:\n/pma [on|off|status]"


@pytest.mark.anyio
async def test_require_topics_uses_scoped_pma_registry_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_autorunner.integrations.app_server.threads import pma_topic_scoped_key

    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    hub_root = tmp_path
    registry = AppServerThreadRegistry(hub_root / "threads.json")
    handler = _PMAHandler(record, _PMAClientStub(), hub_root, registry)
    handler._config = SimpleNamespace(
        root=hub_root,
        concurrency=SimpleNamespace(max_parallel_turns=1, per_topic_queue=False),
        agent_turn_timeout_seconds={"codex": None, "opencode": None},
        require_topics=True,
    )
    handler._pma_registry_key(record, None)

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="test",
        date=None,
        is_topic_message=True,
    )

    pma_key = handler._pma_registry_key(record, message)
    assert pma_key == "pma.-1001:101"

    registry.set_thread_id(pma_key, "test-thread-id")
    assert registry.get_thread_id(pma_key) == "test-thread-id"

    def mock_topic_key(chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id or 'root'}"

    expected_key = pma_topic_scoped_key(
        agent="codex",
        chat_id=-1001,
        thread_id=101,
        topic_key_fn=mock_topic_key,
    )
    assert pma_key == expected_key


def test_pma_registry_key_matches_logical_hermes_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_autorunner.integrations.app_server.threads import pma_base_key

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

    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="hermes-m4-pma",
    )
    handler = _PMAHandler(
        record, _PMAClientStub(), tmp_path, AppServerThreadRegistry(tmp_path / "t.json")
    )
    expected = pma_base_key("hermes", "m4-pma")
    assert handler._pma_registry_key(record, None) == expected

    record_split = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="hermes",
        agent_profile="m4-pma",
    )
    handler_split = _PMAHandler(
        record_split,
        _PMAClientStub(),
        tmp_path,
        AppServerThreadRegistry(tmp_path / "t2.json"),
    )
    assert handler_split._pma_registry_key(record_split, None) == expected
    assert expected != pma_base_key("hermes-m4-pma")


class _HelpHandlersStub:
    async def _noop(self, *args: object, **kwargs: object) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        if name.startswith("_handle_"):
            return self._noop
        raise AttributeError(name)


def test_help_text_mentions_pma_mode() -> None:
    specs = build_command_specs(_HelpHandlersStub())
    text = _format_help_text(specs)
    assert "/pma - PMA mode" in text
