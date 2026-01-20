import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

import codex_autorunner.integrations.telegram.handlers.commands.github as github_commands
from codex_autorunner.agents.opencode.runtime import OpenCodeTurnOutput
from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
    _RuntimeStub,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord


class _OpenCodeClientStub:
    def __init__(self, *, session_id: str) -> None:
        self._session_id = session_id
        self.create_session_calls: list[str] = []
        self.send_command_calls: list[
            tuple[str, str, str, Optional[str], Optional[str]]
        ] = []
        self.abort_calls: list[str] = []

    async def create_session(
        self, *, directory: Optional[str] = None
    ) -> dict[str, str]:
        if directory:
            self.create_session_calls.append(directory)
        return {"sessionId": self._session_id}

    async def send_command(
        self,
        session_id: str,
        *,
        command: str,
        arguments: Optional[str] = None,
        model: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> None:
        self.send_command_calls.append(
            (session_id, command, arguments or "", model, agent)
        )

    async def abort(self, session_id: str) -> None:
        self.abort_calls.append(session_id)


class _SupervisorStub:
    def __init__(self, client: _OpenCodeClientStub) -> None:
        self._client = client
        self.started: list[str] = []
        self.finished: list[str] = []

    async def get_client(self, _root: Path) -> _OpenCodeClientStub:
        return self._client

    async def mark_turn_started(self, root: Path) -> None:
        self.started.append(str(root))

    async def mark_turn_finished(self, root: Path) -> None:
        self.finished.append(str(root))


class _RouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> Optional[TelegramTopicRecord]:
        return self._record

    async def set_active_thread(
        self, _chat_id: int, _thread_id: Optional[int], active_thread_id: Optional[str]
    ) -> Optional[TelegramTopicRecord]:
        if active_thread_id is None:
            self._record.active_thread_id = None
        return self._record

    async def update_topic(
        self, _chat_id: int, _thread_id: Optional[int], apply: object
    ) -> Optional[TelegramTopicRecord]:
        if callable(apply):
            apply(self._record)
        return self._record


class _ReviewHandlerStub(TelegramCommandHandlers):
    def __init__(
        self,
        *,
        record: TelegramTopicRecord,
        supervisor: _SupervisorStub,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace(
            concurrency=SimpleNamespace(max_parallel_turns=1, per_topic_queue=False),
            progress_stream=SimpleNamespace(
                enabled=False, max_actions=0, max_output_chars=0
            ),
        )
        self._router = _RouterStub(record)
        self._opencode_supervisor = supervisor
        self._turn_semaphore = asyncio.Semaphore(1)
        self._turn_contexts: dict[tuple[str, str], object] = {}
        self._turn_progress_trackers: dict[tuple[str, str], object] = {}
        self._turn_progress_rendered: dict[tuple[str, str], object] = {}
        self._turn_progress_updated_at: dict[tuple[str, str], float] = {}
        self._turn_progress_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._turn_progress_heartbeat_tasks: dict[
            tuple[str, str], asyncio.Task[None]
        ] = {}
        self._turn_preview_text: dict[tuple[str, str], str] = {}
        self._turn_preview_updated_at: dict[tuple[str, str], float] = {}
        self._token_usage_by_turn: dict[str, dict[str, object]] = {}
        self._token_usage_by_thread: dict[str, dict[str, object]] = {}
        self._pending_review_custom: dict[str, dict[str, object]] = {}
        self._review_commit_options: dict[str, object] = {}
        self._review_commit_subjects: dict[str, object] = {}
        self._sent_messages: list[str] = []
        self._delivered: list[str] = []
        self._deleted: list[int] = []
        self._placeholder_counter = 200

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _refresh_workspace_id(
        self, _key: str, _record: TelegramTopicRecord
    ) -> None:
        return None

    async def _find_thread_conflict(
        self, _thread_id: str, *, key: str
    ) -> Optional[str]:
        return None

    async def _handle_thread_conflict(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _verify_active_thread(
        self, _message: TelegramMessage, record: TelegramTopicRecord
    ) -> TelegramTopicRecord:
        return record

    def _canonical_workspace_root(
        self, workspace_path: Optional[str]
    ) -> Optional[Path]:
        if not workspace_path:
            return None
        return Path(workspace_path).resolve()

    def _ensure_turn_semaphore(self) -> asyncio.Semaphore:
        return self._turn_semaphore

    def _effective_policies(self, _record: TelegramTopicRecord) -> tuple[None, None]:
        return None, None

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        reply_markup: Optional[dict[str, object]] = None,
    ) -> None:
        _ = (thread_id, reply_to, reply_markup)
        self._sent_messages.append(text)

    async def _send_placeholder(
        self,
        _chat_id: int,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        text: str,
        reply_markup: Optional[dict[str, object]] = None,
    ) -> int:
        _ = (thread_id, reply_to, text, reply_markup)
        self._placeholder_counter += 1
        return self._placeholder_counter

    async def _edit_message_text(
        self, _chat_id: int, _message_id: int, _text: str
    ) -> bool:
        return True

    async def _delete_message(self, _chat_id: int, message_id: int) -> bool:
        self._deleted.append(message_id)
        return True

    async def _deliver_turn_response(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        placeholder_id: Optional[int],
        response: str,
    ) -> bool:
        _ = (chat_id, thread_id, reply_to, placeholder_id)
        self._delivered.append(response)
        return True

    def _format_turn_metrics_text(
        self,
        _token_usage: Optional[dict[str, object]],
        _elapsed_seconds: Optional[float],
    ) -> Optional[str]:
        return None

    def _metrics_mode(self) -> str:
        return "separate"

    async def _send_turn_metrics(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def _append_metrics_to_placeholder(
        self, *_args: object, **_kwargs: object
    ) -> bool:
        return True

    def _turn_key(
        self, thread_id: Optional[str], turn_id: Optional[str]
    ) -> Optional[tuple[str, str]]:
        if not thread_id or not turn_id:
            return None
        return (thread_id, turn_id)

    def _register_turn_context(
        self, turn_key: tuple[str, str], _turn_id: str, ctx: object
    ) -> bool:
        existing = self._turn_contexts.get(turn_key)
        if existing and existing is not ctx:
            return False
        self._turn_contexts[turn_key] = ctx
        return True

    async def _start_turn_progress(self, *_args: object, **_kwargs: object) -> None:
        return None

    def _clear_thinking_preview(self, _turn_key: tuple[str, str]) -> None:
        return None

    def _clear_turn_progress(self, _turn_key: tuple[str, str]) -> None:
        return None

    async def _flush_outbox_files(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _note_progress_context_usage(
        self, *_args: object, **_kwargs: object
    ) -> None:
        return None

    async def _schedule_progress_edit(self, _turn_key: tuple[str, str]) -> None:
        return None


def _message() -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=123,
        thread_id=5,
        from_user_id=42,
        text="/review",
        date=None,
        is_topic_message=True,
    )


@pytest.mark.anyio
async def test_ensure_thread_id_creates_opencode_session(tmp_path: Path) -> None:
    record = TelegramTopicRecord(workspace_path=str(tmp_path), agent="opencode")
    client = _OpenCodeClientStub(session_id="session-abc")
    supervisor = _SupervisorStub(client)
    handler = _ReviewHandlerStub(record=record, supervisor=supervisor)
    thread_id = await handler._ensure_thread_id(_message(), record)
    assert thread_id == "session-abc"
    assert record.active_thread_id == "session-abc"
    assert record.thread_ids[0] == "session-abc"
    assert client.create_session_calls == [str(tmp_path.resolve())]


@pytest.mark.anyio
async def test_telegram_review_opencode_sends_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = TelegramTopicRecord(
        workspace_path=str(tmp_path),
        agent="opencode",
        active_thread_id="session-123",
        thread_ids=["session-123"],
    )
    client = _OpenCodeClientStub(session_id="session-123")
    supervisor = _SupervisorStub(client)
    handler = _ReviewHandlerStub(record=record, supervisor=supervisor)
    runtime = _RuntimeStub()
    calls: dict[str, str] = {}

    async def _fake_collect_opencode_output(
        _client: object,
        *,
        session_id: str,
        workspace_path: str,
        **_kwargs: object,
    ) -> OpenCodeTurnOutput:
        calls["session_id"] = session_id
        calls["workspace_path"] = workspace_path
        return OpenCodeTurnOutput(text="Review output", error=None)

    async def _fake_opencode_missing_env(
        *_args: object, **_kwargs: object
    ) -> list[str]:
        return []

    monkeypatch.setattr(
        github_commands, "collect_opencode_output", _fake_collect_opencode_output
    )
    monkeypatch.setattr(
        github_commands, "opencode_missing_env", _fake_opencode_missing_env
    )

    await handler._handle_review(_message(), "", runtime)

    assert client.send_command_calls
    session_id, command, _args, _model, _agent = client.send_command_calls[0]
    assert session_id == "session-123"
    assert command == "review"
    assert calls["session_id"] == "session-123"
    assert calls["workspace_path"] == str(tmp_path.resolve())
    assert handler._delivered
    assert handler._delivered[-1]
