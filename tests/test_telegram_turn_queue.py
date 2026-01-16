import asyncio
import logging
from types import SimpleNamespace
from typing import Optional

import pytest

from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.constants import (
    PLACEHOLDER_TEXT,
    QUEUED_PLACEHOLDER_TEXT,
)
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
    _RuntimeStub,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord


class _TurnResult:
    def __init__(self) -> None:
        self.agent_messages = ["ok"]
        self.errors: list[str] = []
        self.status = "completed"
        self.token_usage = None


class _TurnHandle:
    def __init__(self, turn_id: str, wait_event: asyncio.Event) -> None:
        self.turn_id = turn_id
        self._wait_event = wait_event

    async def wait(self) -> _TurnResult:
        await self._wait_event.wait()
        return _TurnResult()


class _ClientStub:
    def __init__(
        self,
        *,
        turn_wait_events: Optional[list[asyncio.Event]] = None,
        turn_start_events: Optional[list[asyncio.Event]] = None,
        review_wait_events: Optional[list[asyncio.Event]] = None,
        review_start_events: Optional[list[asyncio.Event]] = None,
    ) -> None:
        self.turn_start_calls: list[tuple[str, str]] = []
        self.review_start_calls: list[tuple[str, str]] = []
        self._turn_wait_events = turn_wait_events or []
        self._turn_start_events = turn_start_events or []
        self._review_wait_events = review_wait_events or []
        self._review_start_events = review_start_events or []

    async def turn_start(
        self, thread_id: str, prompt_text: str, **_kwargs: object
    ) -> _TurnHandle:
        idx = len(self.turn_start_calls)
        self.turn_start_calls.append((thread_id, prompt_text))
        if idx < len(self._turn_start_events):
            self._turn_start_events[idx].set()
        wait_event = self._turn_wait_events[idx]
        return _TurnHandle(f"turn-{idx}", wait_event)

    async def review_start(self, thread_id: str, **_kwargs: object) -> _TurnHandle:
        idx = len(self.review_start_calls)
        self.review_start_calls.append((thread_id, "review"))
        if idx < len(self._review_start_events):
            self._review_start_events[idx].set()
        wait_event = self._review_wait_events[idx]
        return _TurnHandle(f"review-{idx}", wait_event)


class _RouterStub:
    def __init__(self, records: dict[str, TelegramTopicRecord]) -> None:
        self._records = records

    def get_topic(self, key: str) -> Optional[TelegramTopicRecord]:
        return self._records.get(key)

    def set_active_thread(
        self, chat_id: int, thread_id: Optional[int], active_thread_id: Optional[str]
    ) -> Optional[TelegramTopicRecord]:
        record = self._records.get(f"{chat_id}:{thread_id}")
        if record is not None and active_thread_id is not None:
            record.active_thread_id = active_thread_id
        return record

    def update_topic(
        self, chat_id: int, thread_id: Optional[int], apply: object
    ) -> None:
        record = self._records.get(f"{chat_id}:{thread_id}")
        if record is None:
            return
        if callable(apply):
            apply(record)


class _HandlerStub(TelegramCommandHandlers):
    def __init__(
        self,
        *,
        client: _ClientStub,
        max_parallel_turns: int,
        records: dict[str, TelegramTopicRecord],
        placeholder_events: Optional[dict[int, asyncio.Event]] = None,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace(
            concurrency=SimpleNamespace(
                max_parallel_turns=max_parallel_turns,
                per_topic_queue=False,
            )
        )
        self._router = _RouterStub(records)
        self._turn_semaphore = asyncio.Semaphore(max_parallel_turns)
        self._turn_contexts: dict[tuple[str, str], object] = {}
        self._turn_preview_text: dict[tuple[str, str], str] = {}
        self._turn_preview_updated_at: dict[tuple[str, str], float] = {}
        self._token_usage_by_thread: dict[str, dict[str, object]] = {}
        self._token_usage_by_turn: dict[str, dict[str, object]] = {}
        self._voice_config = None
        self._client = client
        self._placeholder_calls: list[dict[str, object]] = []
        self._placeholder_ids: dict[int, int] = {}
        self._edit_calls: list[tuple[int, str]] = []
        self._placeholder_events = placeholder_events or {}

    def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    def _ensure_turn_semaphore(self) -> asyncio.Semaphore:
        return self._turn_semaphore

    async def _client_for_workspace(self, _workspace_path: str) -> _ClientStub:
        return self._client

    def _find_thread_conflict(self, _thread_id: str, *, key: str) -> Optional[str]:
        return None

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

    def _effective_policies(self, _record: TelegramTopicRecord) -> tuple[None, None]:
        return None, None

    async def _send_placeholder(
        self,
        chat_id: int,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        text: str = PLACEHOLDER_TEXT,
        reply_markup: Optional[dict[str, object]] = None,
    ) -> int:
        call = {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "reply_to": reply_to,
            "text": text,
            "reply_markup": reply_markup,
        }
        self._placeholder_calls.append(call)
        if reply_to is not None:
            placeholder_id = 100 + len(self._placeholder_calls)
            self._placeholder_ids[reply_to] = placeholder_id
            event = self._placeholder_events.get(reply_to)
            if event is not None:
                event.set()
            return placeholder_id
        return 0

    async def _edit_message_text(
        self, _chat_id: int, message_id: int, text: str
    ) -> bool:
        self._edit_calls.append((message_id, text))
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

    def _clear_thinking_preview(self, turn_key: tuple[str, str]) -> None:
        self._turn_preview_text.pop(turn_key, None)
        self._turn_preview_updated_at.pop(turn_key, None)

    async def _start_turn_progress(
        self,
        _turn_key: tuple[str, str],
        *,
        ctx: object,
        model: Optional[str],
        label: str = "working",
    ) -> None:
        return None

    def _clear_turn_progress(self, _turn_key: tuple[str, str]) -> None:
        return None

    async def _send_message(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _deliver_turn_response(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def _send_turn_metrics(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def _delete_message(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def _flush_outbox_files(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _finalize_voice_transcript(
        self, *_args: object, **_kwargs: object
    ) -> None:
        return None


def _message(*, message_id: int, thread_id: int) -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=message_id,
        chat_id=10,
        thread_id=thread_id,
        from_user_id=2,
        text="hello",
        date=None,
        is_topic_message=True,
    )


def _record(thread_id: str) -> TelegramTopicRecord:
    return TelegramTopicRecord(
        workspace_path="/tmp",
        active_thread_id=thread_id,
        thread_ids=[thread_id],
    )


@pytest.mark.anyio
async def test_turn_placeholder_sent_while_queued() -> None:
    first_wait = asyncio.Event()
    second_wait = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    second_placeholder = asyncio.Event()

    client = _ClientStub(
        turn_wait_events=[first_wait, second_wait],
        turn_start_events=[first_started, second_started],
    )
    records = {
        "10:11": _record("thread-1"),
        "10:12": _record("thread-2"),
    }
    handler = _HandlerStub(
        client=client,
        max_parallel_turns=1,
        records=records,
        placeholder_events={2: second_placeholder},
    )

    runtime_one = _RuntimeStub()
    runtime_two = _RuntimeStub()
    task_one = asyncio.create_task(
        handler._run_turn_and_collect_result(
            _message(message_id=1, thread_id=11),
            runtime_one,
            record=records["10:11"],
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    task_two = asyncio.create_task(
        handler._run_turn_and_collect_result(
            _message(message_id=2, thread_id=12),
            runtime_two,
            record=records["10:12"],
        )
    )
    await asyncio.wait_for(second_placeholder.wait(), timeout=1.0)
    assert len(client.turn_start_calls) == 1

    second_call = next(
        call for call in handler._placeholder_calls if call["reply_to"] == 2
    )
    assert second_call["text"] == QUEUED_PLACEHOLDER_TEXT

    first_wait.set()
    await asyncio.wait_for(second_started.wait(), timeout=1.0)
    second_wait.set()
    await asyncio.gather(task_one, task_two)

    placeholder_id = handler._placeholder_ids[2]
    assert (placeholder_id, PLACEHOLDER_TEXT) in handler._edit_calls


@pytest.mark.anyio
async def test_turns_start_without_queue_when_parallelism_allows() -> None:
    first_wait = asyncio.Event()
    second_wait = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    client = _ClientStub(
        turn_wait_events=[first_wait, second_wait],
        turn_start_events=[first_started, second_started],
    )
    records = {
        "10:11": _record("thread-1"),
        "10:12": _record("thread-2"),
    }
    handler = _HandlerStub(
        client=client,
        max_parallel_turns=2,
        records=records,
    )

    runtime_one = _RuntimeStub()
    runtime_two = _RuntimeStub()
    task_one = asyncio.create_task(
        handler._run_turn_and_collect_result(
            _message(message_id=1, thread_id=11),
            runtime_one,
            record=records["10:11"],
        )
    )
    task_two = asyncio.create_task(
        handler._run_turn_and_collect_result(
            _message(message_id=2, thread_id=12),
            runtime_two,
            record=records["10:12"],
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await asyncio.wait_for(second_started.wait(), timeout=1.0)

    first_wait.set()
    second_wait.set()
    await asyncio.gather(task_one, task_two)

    texts = [call["text"] for call in handler._placeholder_calls]
    assert texts == [PLACEHOLDER_TEXT, PLACEHOLDER_TEXT]
    assert handler._edit_calls == []


@pytest.mark.anyio
async def test_review_placeholder_sent_while_queued() -> None:
    first_wait = asyncio.Event()
    second_wait = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    second_placeholder = asyncio.Event()

    client = _ClientStub(
        review_wait_events=[first_wait, second_wait],
        review_start_events=[first_started, second_started],
    )
    records = {
        "10:11": _record("thread-1"),
        "10:12": _record("thread-2"),
    }
    handler = _HandlerStub(
        client=client,
        max_parallel_turns=1,
        records=records,
        placeholder_events={2: second_placeholder},
    )

    runtime_one = _RuntimeStub()
    runtime_two = _RuntimeStub()
    message_one = _message(message_id=1, thread_id=11)
    message_two = _message(message_id=2, thread_id=12)

    task_one = asyncio.create_task(
        handler._start_review(
            message_one,
            runtime_one,
            record=records["10:11"],
            thread_id="thread-1",
            target={"type": "diff"},
            delivery="inline",
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    task_two = asyncio.create_task(
        handler._start_review(
            message_two,
            runtime_two,
            record=records["10:12"],
            thread_id="thread-2",
            target={"type": "diff"},
            delivery="inline",
        )
    )
    await asyncio.wait_for(second_placeholder.wait(), timeout=1.0)
    assert len(client.review_start_calls) == 1

    second_call = next(
        call for call in handler._placeholder_calls if call["reply_to"] == 2
    )
    assert second_call["text"] == QUEUED_PLACEHOLDER_TEXT

    first_wait.set()
    await asyncio.wait_for(second_started.wait(), timeout=1.0)
    second_wait.set()
    await asyncio.gather(task_one, task_two)

    placeholder_id = handler._placeholder_ids[2]
    assert (placeholder_id, PLACEHOLDER_TEXT) in handler._edit_calls


@pytest.mark.anyio
async def test_turn_cancel_releases_semaphore_on_race() -> None:
    client = _ClientStub(turn_wait_events=[asyncio.Event()])
    records = {"10:11": _record("thread-1")}
    handler = _HandlerStub(
        client=client,
        max_parallel_turns=1,
        records=records,
    )
    runtime = _RuntimeStub()
    runtime.queued_turn_cancel = None
    message = _message(message_id=1, thread_id=11)

    await handler._turn_semaphore.acquire()
    task = asyncio.create_task(
        handler._await_turn_slot(
            handler._turn_semaphore,
            runtime,
            message=message,
            placeholder_id=101,
            queued=True,
        )
    )
    for _ in range(100):
        if runtime.queued_turn_cancel is not None:
            break
        await asyncio.sleep(0)
    assert runtime.queued_turn_cancel is not None

    handler._turn_semaphore.release()
    runtime.queued_turn_cancel.set()
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result is False

    await asyncio.wait_for(handler._turn_semaphore.acquire(), timeout=1.0)
    handler._turn_semaphore.release()
