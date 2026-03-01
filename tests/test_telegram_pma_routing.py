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
from codex_autorunner.core.pma_delivery_targets import (
    PmaDeliveryTargetsStore,
    target_key,
)
from codex_autorunner.core.pma_target_refs import parse_pma_target_ref
from codex_autorunner.integrations.app_server.client import (
    CodexAppServerResponseError,
)
from codex_autorunner.integrations.telegram.adapter import (
    TelegramDocument,
    TelegramMessage,
    TelegramVoice,
)
from codex_autorunner.integrations.telegram.handlers.commands import (
    build_command_specs,
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
from codex_autorunner.integrations.telegram.state import (
    TelegramTopicRecord,
    ThreadSummary,
)
from codex_autorunner.surfaces.cli import pma_cli


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
    async def thread_start(self, workspace_path: str, *, agent: str) -> dict[str, str]:
        return {"thread_id": "new-thread-id", "workspace_path": workspace_path}


class _NewtHandler(WorkspaceCommands):
    def __init__(self, record: TelegramTopicRecord, *, hub_root: Path) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace()
        self._router = _NewtRouterStub(record)
        self._hub_root = hub_root
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
    assert branch_calls[0]["branch_name"] == "thread-chat-7777-thread-333"


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
    assert any("Setup commands run: 2" in text for text in handler._sent)


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
    assert branch_calls[0]["branch_name"] == "thread-chat-7777-thread-333"
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
    assert branch_calls[0]["branch_name"] == "thread-chat-7777-thread-333"


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
    assert branch_calls[0]["branch_name"] == "thread-chat-7777-thread-333"


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
    assert branch_calls[0]["branch_name"] == "thread-chat-123456-msg-808-upd-909"

    # First topic update is /newt state reset before new thread is attached.
    reset_snapshot = handler._router.update_snapshots[0]
    assert reset_snapshot["workspace_id"] == "stale-workspace-id"
    assert reset_snapshot["active_thread_id"] is None
    assert reset_snapshot["thread_ids"] == []
    assert reset_snapshot["thread_summaries"] == {}
    assert reset_snapshot["rollout_path"] is None
    assert reset_snapshot["pending_compact_seed"] is None
    assert reset_snapshot["pending_compact_seed_thread_id"] is None


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


def _targets_state_without_updated_at(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": state.get("version"),
        "targets": state.get("targets"),
        "last_delivery_by_target": state.get("last_delivery_by_target"),
        "active_target_key": state.get("active_target_key"),
    }


@pytest.mark.parametrize(
    ("ref", "expected_key"),
    [
        ("here", "chat:telegram:-1001:55"),
        ("web", "web"),
        (
            "local:.codex-autorunner/pma/deliveries.jsonl",
            "local:.codex-autorunner/pma/deliveries.jsonl",
        ),
        ("telegram:-100123", "chat:telegram:-100123"),
        ("telegram:-100123:777", "chat:telegram:-100123:777"),
        ("discord:99887766", "chat:discord:99887766"),
        ("chat:telegram:-100123:777", "chat:telegram:-100123:777"),
        ("chat:discord:99887766", "chat:discord:99887766"),
        ("telegram:abc", None),
        ("discord:", None),
        ("chat:discord:123:456", None),
    ],
)
def test_pma_target_ref_matrix_telegram_matches_canonical_parser(
    tmp_path: Path, ref: str, expected_key: Optional[str]
) -> None:
    handler = _PmaTargetsHandler(
        hub_root=tmp_path / "hub", record=TelegramTopicRecord()
    )
    message = _make_pma_message(chat_id=-1001, thread_id=55)
    parsed = handler._parse_pma_target_ref(message, ref)
    canonical = parse_pma_target_ref(ref, here_target=handler._pma_here_target(message))
    assert parsed == canonical
    if expected_key is None:
        assert parsed is None
        return
    assert parsed is not None
    assert target_key(parsed) == expected_key


@pytest.mark.anyio
async def test_pma_target_add_list_rm_clear_mutates_store(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    handler = _PmaTargetsHandler(hub_root=hub_root, record=TelegramTopicRecord())
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "target add here", _RuntimeStub())
    await handler._handle_pma(message, "target add web", _RuntimeStub())
    await handler._handle_pma(
        message,
        "target add local:.codex-autorunner/pma/deliveries.jsonl",
        _RuntimeStub(),
    )
    await handler._handle_pma(message, "target add telegram:-2002:77", _RuntimeStub())
    await handler._handle_pma(message, "target add discord:99887766", _RuntimeStub())
    await handler._handle_pma(
        message, "target add chat:telegram:-2002:77", _RuntimeStub()
    )

    store = PmaDeliveryTargetsStore(hub_root)
    state = store.load()
    keys = {
        key
        for key in (target_key(target) for target in state["targets"])
        if isinstance(key, str)
    }
    assert keys == {
        "web",
        "local:.codex-autorunner/pma/deliveries.jsonl",
        "chat:discord:99887766",
        "chat:telegram:-1001:55",
        "chat:telegram:-2002:77",
    }

    await handler._handle_pma(message, "targets", _RuntimeStub())
    assert "web" in handler.sent[-1]
    assert "chat:telegram:-1001:55" in handler.sent[-1]
    assert "chat:discord:99887766" in handler.sent[-1]

    await handler._handle_pma(message, "target rm here", _RuntimeStub())
    state = store.load()
    keys = {
        key
        for key in (target_key(target) for target in state["targets"])
        if isinstance(key, str)
    }
    assert "chat:telegram:-1001:55" not in keys

    await handler._handle_pma(message, "target clear", _RuntimeStub())
    assert store.load()["targets"] == []


@pytest.mark.anyio
async def test_pma_target_active_show_and_set(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    handler = _PmaTargetsHandler(hub_root=hub_root, record=TelegramTopicRecord())
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "target add web", _RuntimeStub())
    await handler._handle_pma(message, "target add telegram:-2002:77", _RuntimeStub())

    await handler._handle_pma(message, "target active", _RuntimeStub())
    assert "Active PMA delivery target: (not set;" in handler.sent[-1]

    await handler._handle_pma(
        message, "target active set telegram:-2002:77", _RuntimeStub()
    )
    assert "Set active PMA delivery target: chat:telegram:-2002:77" in handler.sent[-1]

    await handler._handle_pma(message, "target active show", _RuntimeStub())
    assert "Active PMA delivery target: chat:telegram:-2002:77" in handler.sent[-1]

    await handler._handle_pma(
        message, "target active chat:telegram:-2002:77", _RuntimeStub()
    )
    assert (
        "PMA delivery target already active: chat:telegram:-2002:77" in handler.sent[-1]
    )

    await handler._handle_pma(
        message, "target active set chat:discord:42", _RuntimeStub()
    )
    assert "PMA delivery target not found: chat:discord:42" in handler.sent[-1]

    await handler._handle_pma(message, "target active set telegram:abc", _RuntimeStub())
    assert "Invalid target ref 'telegram:abc'." in handler.sent[-1]
    assert "/pma target active" in handler.sent[-1]


@pytest.mark.anyio
async def test_pma_target_add_invalid_ref_reports_usage(tmp_path: Path) -> None:
    handler = _PmaTargetsHandler(
        hub_root=tmp_path / "hub",
        record=TelegramTopicRecord(),
    )
    message = _make_pma_message()

    await handler._handle_pma(message, "target add telegram:abc", _RuntimeStub())
    assert "Invalid target ref" in handler.sent[-1]
    assert "/pma target add <ref>" in handler.sent[-1]


@pytest.mark.anyio
async def test_pma_target_ref_sequence_matches_cli_store_payload(
    tmp_path: Path,
) -> None:
    telegram_root = tmp_path / "telegram-hub"
    cli_root = tmp_path / "cli-hub"
    handler = _PmaTargetsHandler(hub_root=telegram_root, record=TelegramTopicRecord())
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    add_refs = [
        "web",
        "local:.codex-autorunner/pma/deliveries.jsonl",
        "telegram:-2002:77",
        "discord:99887766",
        "chat:telegram:-2002:77",
    ]
    remove_refs = ["telegram:-2002:77"]

    for ref in add_refs:
        await handler._handle_pma(message, f"target add {ref}", _RuntimeStub())
    for ref in remove_refs:
        await handler._handle_pma(message, f"target rm {ref}", _RuntimeStub())
    await handler._handle_pma(message, "target active discord:99887766", _RuntimeStub())
    await handler._handle_pma(message, "target active web", _RuntimeStub())

    telegram_state = PmaDeliveryTargetsStore(telegram_root).load()

    cli_store = PmaDeliveryTargetsStore(cli_root)
    for ref in add_refs:
        parsed = pma_cli._parse_pma_target_ref(ref)
        assert parsed is not None
        cli_store.add_target(parsed)
    for ref in remove_refs:
        parsed = pma_cli._parse_pma_target_ref(ref)
        assert parsed is not None
        cli_store.remove_target(parsed)
    parsed_discord_active = pma_cli._parse_pma_target_ref("discord:99887766")
    assert parsed_discord_active is not None
    cli_store.set_active_target(parsed_discord_active)
    parsed_web_active = pma_cli._parse_pma_target_ref("web")
    assert parsed_web_active is not None
    cli_store.set_active_target(parsed_web_active)
    cli_state = cli_store.load()

    assert _targets_state_without_updated_at(
        telegram_state
    ) == _targets_state_without_updated_at(cli_state)


@pytest.mark.anyio
async def test_pma_on_sets_here_target_and_replaces_single_target(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    store = PmaDeliveryTargetsStore(hub_root)
    store.set_targets([{"kind": "chat", "platform": "discord", "chat_id": "111"}])
    record = TelegramTopicRecord(
        pma_enabled=False,
        repo_id="repo-1",
        workspace_path=str(tmp_path / "repo"),
        workspace_id="workspace-1",
        active_thread_id="thread-1",
    )
    handler = _PmaTargetsHandler(hub_root=hub_root, record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "on", _RuntimeStub())

    assert record.pma_enabled is True
    assert store.load()["targets"] == [
        {
            "kind": "chat",
            "platform": "telegram",
            "chat_id": "-1001",
            "thread_id": "55",
            "conversation_key": "-1001:55",
        }
    ]


@pytest.mark.anyio
async def test_pma_on_keeps_multi_targets_and_adds_here(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaDeliveryTargetsStore(hub_root)
    store.set_targets(
        [
            {"kind": "chat", "platform": "discord", "chat_id": "111"},
            {
                "kind": "chat",
                "platform": "telegram",
                "chat_id": "-2002",
                "thread_id": "77",
            },
        ]
    )
    handler = _PmaTargetsHandler(
        hub_root=hub_root,
        record=TelegramTopicRecord(pma_enabled=False),
    )
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "on", _RuntimeStub())

    keys = {
        key
        for key in (target_key(target) for target in store.load()["targets"])
        if isinstance(key, str)
    }
    assert keys == {
        "chat:discord:111",
        "chat:telegram:-1001:55",
        "chat:telegram:-2002:77",
    }


@pytest.mark.anyio
async def test_pma_off_keeps_delivery_targets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaDeliveryTargetsStore(hub_root)
    store.set_targets([{"kind": "chat", "platform": "discord", "chat_id": "111"}])
    record = TelegramTopicRecord(
        pma_enabled=True,
        pma_prev_repo_id="repo-2",
        pma_prev_workspace_path=str(tmp_path / "repo"),
        pma_prev_workspace_id="workspace-2",
        pma_prev_active_thread_id="thread-2",
    )
    handler = _PmaTargetsHandler(hub_root=hub_root, record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "off", _RuntimeStub())

    assert record.pma_enabled is False
    assert store.load()["targets"] == [
        {"kind": "chat", "platform": "discord", "chat_id": "111"}
    ]


@pytest.mark.anyio
async def test_pma_status_does_not_mutate_targets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaDeliveryTargetsStore(hub_root)
    store.set_targets([{"kind": "chat", "platform": "discord", "chat_id": "111"}])
    record = TelegramTopicRecord(pma_enabled=False)
    handler = _PmaTargetsHandler(hub_root=hub_root, record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "status", _RuntimeStub())

    assert handler.sent[-1] == "PMA mode: disabled"
    assert store.load()["targets"] == [
        {"kind": "chat", "platform": "discord", "chat_id": "111"}
    ]


class _HelpHandlersStub:
    async def _noop(self, *args: object, **kwargs: object) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        if name.startswith("_handle_"):
            return self._noop
        raise AttributeError(name)


def test_help_text_mentions_pma_target_management() -> None:
    specs = build_command_specs(_HelpHandlersStub())
    text = _format_help_text(specs)
    assert "/pma - PMA mode and delivery targets" in text
