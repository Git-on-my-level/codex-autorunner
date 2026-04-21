import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from codex_autorunner.core.orchestration.runtime_threads import (
    RUNTIME_THREAD_INTERRUPTED_ERROR,
    RUNTIME_THREAD_TIMEOUT_ERROR,
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
    execution as execution_commands_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.execution import (
    ExecutionCommands,
    _TurnRunResult,
)
from codex_autorunner.integrations.telegram.handlers.commands.workspace import (
    WorkspaceCommands,
)
from codex_autorunner.integrations.telegram.handlers.media_ingress import (
    handle_media_message,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord


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

    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None)
    handler = _ExecutionStub(record, hub_root)
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
    assert "Hub Snapshot Availability:" in snapshot_text
    assert "status=hub_unavailable" in snapshot_text
    assert "Do not infer hub-root queue, thread, inbox, or automation state" in (
        snapshot_text
    )
    assert "PMA File Inbox:" not in snapshot_text


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
    handler._effective_policies = lambda current_record: (
        WorkspaceCommands._effective_policies(handler, current_record)
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
    handler._effective_policies = lambda current_record: (
        WorkspaceCommands._effective_policies(handler, current_record)
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

    import codex_autorunner.integrations.telegram.handlers.media_ingress as _mi_mod
    import codex_autorunner.integrations.telegram.handlers.surface_ingress as _si_mod

    monkeypatch.setattr(
        _si_mod, "build_surface_orchestration_ingress", lambda **_: _IngressStub()
    )
    monkeypatch.setattr(
        _mi_mod, "build_surface_orchestration_ingress", lambda **_: _IngressStub()
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
    handler._files_outbox_pending_dir = lambda _workspace, _topic_key: (
        tmp_path / "outbox"
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

    import codex_autorunner.integrations.telegram.handlers.media_ingress as _mi_mod

    monkeypatch.setattr(
        _mi_mod, "build_surface_orchestration_ingress", lambda **_: _IngressStub()
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

    import codex_autorunner.integrations.telegram.handlers.surface_ingress as _si_mod

    monkeypatch.setattr(
        _si_mod, "build_surface_orchestration_ingress", lambda **_: _FakeIngress()
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
