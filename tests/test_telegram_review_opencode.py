import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import codex_autorunner.integrations.telegram.handlers.commands.github as github_commands
from codex_autorunner.core.sse import SSEEvent
from codex_autorunner.integrations.chat.managed_thread_progress import (
    ProgressRuntimeState,
    apply_run_event_to_progress_tracker,
)
from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
    _RuntimeStub,
)
from codex_autorunner.integrations.telegram.progress_stream import TurnProgressTracker
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord


class _OpenCodeClientStub:
    def __init__(self, *, session_id: str) -> None:
        self._session_id = session_id
        self.create_session_calls: list[str] = []
        self.send_command_calls: list[
            tuple[str, str, str, Optional[str], Optional[str]]
        ] = []
        self.abort_calls: list[str] = []
        self.list_messages_calls: list[str] = []
        self.permission_replies: list[tuple[str, str]] = []
        self.question_replies: list[tuple[str, list[list[str]]]] = []
        self.question_rejections: list[str] = []
        self.messages_response: Any = []
        self.send_command_result: dict[str, Any] = {}
        self.stream_events_payloads: list[SSEEvent] = []

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
        return self.send_command_result

    async def abort(self, session_id: str) -> None:
        self.abort_calls.append(session_id)

    async def stream_events(
        self,
        *,
        directory: Optional[str] = None,
        ready_event: object = None,
        session_id: Optional[str] = None,
        paths: object = None,
    ):
        _ = (directory, session_id, paths)
        if ready_event is not None:
            ready_event.set()
        for event in self.stream_events_payloads:
            yield event

    async def list_messages(self, session_id: str, **_kwargs: Any) -> Any:
        self.list_messages_calls.append(session_id)
        return self.messages_response

    async def session_status(
        self, *, directory: Optional[str] = None
    ) -> dict[str, object]:
        _ = directory
        return {}

    async def providers(self, *, directory: Optional[str] = None) -> dict[str, object]:
        _ = directory
        return {}

    async def respond_permission(self, *, request_id: str, reply: str) -> None:
        self.permission_replies.append((request_id, reply))

    async def reply_question(
        self, request_id: str, *, answers: list[list[str]]
    ) -> None:
        self.question_replies.append((request_id, answers))

    async def reject_question(self, request_id: str) -> None:
        self.question_rejections.append(request_id)


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

    async def session_stall_timeout_seconds_for_workspace(
        self, _root: Path
    ) -> Optional[float]:
        return None


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
        deliver_result: bool = True,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace(
            concurrency=SimpleNamespace(max_parallel_turns=1, per_topic_queue=False),
            progress_stream=SimpleNamespace(
                enabled=False, max_actions=0, max_output_chars=0
            ),
            agent_turn_timeout_seconds={"codex": 28800.0, "opencode": 28800.0},
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
        self._intermediate: list[Optional[str]] = []
        self._delivery_delete_flags: list[bool] = []
        self._deleted: list[int] = []
        self._edited: list[tuple[int, int, str]] = []
        self._placeholder_counter = 200
        self._deliver_result = deliver_result

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
        self, chat_id: int, message_id: int, text: str
    ) -> bool:
        self._edited.append((chat_id, message_id, text))
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
        intermediate_response: Optional[str] = None,
        delete_placeholder_on_delivery: bool = True,
    ) -> bool:
        _ = (
            chat_id,
            thread_id,
            reply_to,
            placeholder_id,
        )
        self._intermediate.append(intermediate_response)
        self._delivery_delete_flags.append(delete_placeholder_on_delivery)
        self._delivered.append(response)
        return self._deliver_result

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

    async def _apply_run_event_to_progress(
        self, turn_key: tuple[str, str], run_event: object
    ) -> None:
        tracker = self._turn_progress_trackers.get(turn_key)
        if not isinstance(tracker, TurnProgressTracker):
            return
        apply_run_event_to_progress_tracker(
            tracker,
            run_event,
            runtime_state=ProgressRuntimeState(),
        )


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


@pytest.mark.integration
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
    client.messages_response = [
        {
            "info": {"id": "assistant-1", "role": "assistant"},
            "parts": [{"type": "text", "text": "Review output"}],
        }
    ]

    async def _fake_opencode_missing_env(
        *_args: object, **_kwargs: object
    ) -> list[str]:
        return []

    monkeypatch.setattr(
        github_commands, "opencode_missing_env", _fake_opencode_missing_env
    )

    await handler._handle_review(_message(), "", runtime)

    assert client.send_command_calls
    session_id, command, _args, _model, _agent = client.send_command_calls[0]
    assert session_id == "session-123"
    assert command == "review"
    assert client.list_messages_calls
    assert set(client.list_messages_calls) == {"session-123"}
    assert handler._delivered
    assert handler._delivered[-1]
    assert handler._delivery_delete_flags[-1] is True
    assert handler._deleted == []


@pytest.mark.integration
@pytest.mark.anyio
async def test_telegram_review_opencode_forwards_progress_summary(
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
    client.messages_response = [
        {
            "info": {"id": "assistant-1", "role": "assistant"},
            "parts": [{"type": "text", "text": "Review output"}],
        }
    ]

    async def _fake_opencode_missing_env(
        *_args: object, **_kwargs: object
    ) -> list[str]:
        return []

    monkeypatch.setattr(
        github_commands, "opencode_missing_env", _fake_opencode_missing_env
    )
    handler._render_turn_progress_summary = lambda _turn_key: (
        "done · agent opencode · model-x · 1s · step 3"
    )
    handler._config.agent_turn_timeout_seconds["opencode"] = 0

    await handler._handle_review(_message(), "", runtime)

    assert len(handler._delivered) >= 1, "Expected at least one delivered response"
    has_agent = any("agent opencode" in (resp or "") for resp in handler._delivered)
    has_model = any("model-x" in (resp or "") for resp in handler._delivered)
    has_step = any("step 3" in (resp or "") for resp in handler._delivered)
    assert has_agent and has_model and has_step, (
        f"Expected progress summary components not found in delivered responses: "
        f"{handler._delivered}"
    )


@pytest.mark.integration
@pytest.mark.anyio
async def test_telegram_review_opencode_tracks_thinking_and_tool_progress_via_harness(
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
    stream_finished = asyncio.Event()

    async def _fake_opencode_missing_env(
        *_args: object, **_kwargs: object
    ) -> list[str]:
        return []

    async def _start_turn_progress(
        turn_key: tuple[str, str],
        *,
        ctx: object,
        agent: str,
        model: Optional[str],
        label: str = "working",
    ) -> None:
        _ = ctx
        handler._turn_progress_trackers[turn_key] = TurnProgressTracker(
            started_at=0.0,
            agent=agent,
            model=model or "default",
            label=label,
            max_actions=10,
            max_output_chars=1000,
        )

    handler._start_turn_progress = _start_turn_progress

    monkeypatch.setattr(
        github_commands, "opencode_missing_env", _fake_opencode_missing_env
    )

    class _FakeHarness:
        def __init__(self, _supervisor: object) -> None:
            self.capabilities = frozenset({"event_streaming"})

        async def start_review(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            model: Optional[str],
            reasoning: Optional[str],
            *,
            approval_mode: Optional[str],
            sandbox_policy: Optional[object],
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
            return SimpleNamespace(turn_id="turn-1")

        def configure_turn_handlers(self, *_args: object, **_kwargs: object) -> None:
            return None

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        def allows_parallel_event_stream(self) -> bool:
            return True

        async def stream_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = (workspace_root, conversation_id, turn_id)
            yield {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "sessionID": "session-123",
                        "messageID": "assistant-1",
                        "properties": {
                            "messageID": "assistant-1",
                            "info": {"id": "assistant-1", "role": "assistant"},
                            "part": {
                                "id": "reason-1",
                                "type": "reasoning",
                                "messageID": "assistant-1",
                                "sessionID": "session-123",
                                "text": "checking review progress",
                            },
                        },
                    },
                }
            }
            yield {
                "message": {
                    "method": "message.part.updated",
                    "params": {
                        "sessionID": "session-123",
                        "messageID": "assistant-1",
                        "properties": {
                            "messageID": "assistant-1",
                            "info": {"id": "assistant-1", "role": "assistant"},
                            "part": {
                                "id": "tool-1",
                                "type": "tool",
                                "messageID": "assistant-1",
                                "sessionID": "session-123",
                                "tool": "shell",
                                "input": "git diff",
                                "state": {"status": "running"},
                            },
                        },
                    },
                }
            }
            stream_finished.set()

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
        ) -> SimpleNamespace:
            _ = (workspace_root, conversation_id, turn_id)
            await stream_finished.wait()
            return SimpleNamespace(
                status="ok",
                assistant_text="Review output",
                errors=[],
            )

        async def interrupt(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ) -> None:
            _ = (workspace_root, conversation_id, turn_id)

    monkeypatch.setattr(github_commands, "OpenCodeHarness", _FakeHarness)

    await handler._handle_review(_message(), "", runtime)

    assert handler._turn_progress_trackers
    progress_traces = [
        (
            tracker.last_thinking_trace.text if tracker.last_thinking_trace else "",
            tracker.last_tool_trace.text if tracker.last_tool_trace else "",
        )
        for tracker in handler._turn_progress_trackers.values()
        if isinstance(tracker, TurnProgressTracker)
    ]
    assert progress_traces
    assert any(
        "checking review progress" in thinking for thinking, _tool in progress_traces
    )
    assert any("shell" in tool for _thinking, tool in progress_traces)


@pytest.mark.anyio
async def test_finalize_codex_review_success_clears_interrupt_status_message(
    tmp_path: Path,
) -> None:
    record = TelegramTopicRecord(workspace_path=str(tmp_path))
    client = _OpenCodeClientStub(session_id="session-123")
    supervisor = _SupervisorStub(client)
    handler = _ReviewHandlerStub(record=record, supervisor=supervisor)
    runtime = SimpleNamespace(
        interrupt_requested=True,
        interrupt_message_id=333,
        interrupt_turn_id="turn-1",
    )
    turn_context = github_commands.ReviewTurnContext(
        placeholder_id=201,
        turn_key=None,
        turn_id="turn-1",
        turn_semaphore=asyncio.Semaphore(1),
        turn_started_at=None,
        turn_elapsed_seconds=None,
        queued=False,
        turn_slot_acquired=False,
        agent_handle=SimpleNamespace(turn_id="turn-1"),
    )
    result = SimpleNamespace(
        final_message="",
        agent_messages=[],
        errors=[],
        status="interrupted",
    )

    await handler._finalize_codex_review_success(
        _message(),
        record,
        "thread-1",
        result,
        turn_context,
        runtime,
    )

    assert handler._deleted == [333]
    assert handler._edited == []
    assert runtime.interrupt_message_id is None
    assert runtime.interrupt_turn_id is None


@pytest.mark.anyio
async def test_finalize_codex_review_success_keeps_interrupt_signal_when_delivery_fails(
    tmp_path: Path,
) -> None:
    record = TelegramTopicRecord(workspace_path=str(tmp_path))
    client = _OpenCodeClientStub(session_id="session-123")
    supervisor = _SupervisorStub(client)
    handler = _ReviewHandlerStub(
        record=record,
        supervisor=supervisor,
        deliver_result=False,
    )
    runtime = SimpleNamespace(
        interrupt_requested=True,
        interrupt_message_id=334,
        interrupt_turn_id="turn-2",
    )
    turn_context = github_commands.ReviewTurnContext(
        placeholder_id=201,
        turn_key=None,
        turn_id="turn-2",
        turn_semaphore=asyncio.Semaphore(1),
        turn_started_at=None,
        turn_elapsed_seconds=None,
        queued=False,
        turn_slot_acquired=False,
        agent_handle=SimpleNamespace(turn_id="turn-2"),
    )
    result = SimpleNamespace(
        final_message="",
        agent_messages=[],
        errors=[],
        status="interrupted",
    )

    await handler._finalize_codex_review_success(
        _message(),
        record,
        "thread-1",
        result,
        turn_context,
        runtime,
    )

    assert handler._deleted == []
    assert handler._edited == [(123, 334, "Interrupted.")]
    assert runtime.interrupt_message_id is None
    assert runtime.interrupt_turn_id is None
