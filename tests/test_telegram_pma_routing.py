import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import httpx
import pytest

from codex_autorunner.core.app_server_threads import (
    PMA_KEY,
    PMA_OPENCODE_KEY,
    AppServerThreadRegistry,
)
from codex_autorunner.integrations.app_server.client import (
    CodexAppServerResponseError,
)
from codex_autorunner.integrations.telegram.adapter import (
    TelegramDocument,
    TelegramMessage,
    TelegramVoice,
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
from codex_autorunner.integrations.telegram.state import (
    TelegramTopicRecord,
    ThreadSummary,
)


class _RouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record


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


@pytest.mark.anyio
async def test_pma_prompt_routing_uses_hub_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    prompt_path = hub_root / ".codex-autorunner" / "pma" / "prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("PMA system prompt", encoding="utf-8")
    inbox_dir = prompt_path.parent / "inbox"
    outbox_dir = prompt_path.parent / "outbox"
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
        self, prompt_text: str, _record: object
    ) -> tuple[str, bool]:
        return prompt_text, False

    def _maybe_inject_car_context(self, prompt_text: str) -> tuple[str, bool]:
        return prompt_text, False

    def _maybe_inject_prompt_context(self, prompt_text: str) -> tuple[str, bool]:
        return prompt_text, False

    def _maybe_inject_outbox_context(
        self, prompt_text: str, *, record: object, topic_key: str
    ) -> tuple[str, bool]:
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
    ) -> bool:
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


class _PMAWorkspaceHandler(WorkspaceCommands):
    def __init__(
        self, record: TelegramTopicRecord, registry: AppServerThreadRegistry
    ) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace()
        self._router = _PMAWorkspaceRouter(record)
        self._hub_thread_registry = registry
        self._sent: list[str] = []

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


@pytest.mark.anyio
async def test_pma_new_resets_session(tmp_path: Path) -> None:
    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    registry.set_thread_id(PMA_OPENCODE_KEY, "old-thread")
    record = TelegramTopicRecord(
        pma_enabled=True, workspace_path=None, agent="opencode"
    )
    handler = _PMAWorkspaceHandler(record, registry)
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
    assert handler._sent and "PMA session reset" in handler._sent[-1]


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
    assert handler.answers and handler.answers[-1] == "Thread missing"
    assert any("Thread no longer exists." in text for text in handler.final_messages)
