import pytest

from codex_autorunner.integrations.telegram.adapter import (
    TelegramCallbackQuery,
    encode_bind_callback,
    encode_compact_callback,
    encode_flow_callback,
    encode_question_custom_callback,
    encode_question_done_callback,
    encode_question_option_callback,
    encode_resume_callback,
)
from codex_autorunner.integrations.telegram.handlers.callbacks import handle_callback
from codex_autorunner.integrations.telegram.types import CompactState, SelectionState


class _HandlerStub:
    def __init__(self) -> None:
        self._resume_options: dict[str, SelectionState] = {}
        self._bind_options: dict[str, SelectionState] = {}
        self._compact_pending: dict[str, CompactState] = {}
        self.calls: list[tuple[str, object]] = []

    async def _resolve_topic_key(self, chat_id: int, thread_id: object) -> str:
        return f"{chat_id}:{thread_id}"

    async def _answer_callback(
        self, _callback: TelegramCallbackQuery, text: str
    ) -> None:
        self.calls.append(("answer", text))

    async def _resume_thread_by_id(
        self, key: str, thread_id: str, _callback: TelegramCallbackQuery
    ) -> None:
        self.calls.append(("resume", key, thread_id))

    async def _bind_topic_by_repo_id(
        self, key: str, repo_id: str, _callback: TelegramCallbackQuery
    ) -> None:
        self.calls.append(("bind", key, repo_id))

    async def _handle_compact_callback(
        self, key: str, _callback: TelegramCallbackQuery, parsed: object
    ) -> None:
        state = self._compact_pending.get(key)
        if not state or _callback.message_id != state.message_id:
            await self._answer_callback(_callback, "Selection expired")
            return
        self.calls.append(("compact", key, parsed))

    async def _handle_question_callback(
        self, _callback: TelegramCallbackQuery, parsed: object
    ) -> None:
        self.calls.append(("question", parsed))

    async def _handle_flow_callback(
        self, _callback: TelegramCallbackQuery, parsed: object
    ) -> None:
        self.calls.append(("flow", parsed))


@pytest.mark.anyio
async def test_handle_callback_resume_selection_expired() -> None:
    handlers = _HandlerStub()
    callback = TelegramCallbackQuery(
        update_id=1,
        callback_id="cb1",
        from_user_id=2,
        data=encode_resume_callback("thread_1"),
        message_id=3,
        chat_id=10,
        thread_id=11,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls == [("answer", "Selection expired")]


@pytest.mark.anyio
async def test_handle_callback_resume_selection_ok() -> None:
    handlers = _HandlerStub()
    key = await handlers._resolve_topic_key(10, 11)
    handlers._resume_options[key] = SelectionState(items=[("thread_1", "One")])
    callback = TelegramCallbackQuery(
        update_id=1,
        callback_id="cb1",
        from_user_id=2,
        data=encode_resume_callback("thread_1"),
        message_id=3,
        chat_id=10,
        thread_id=11,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls == [("resume", key, "thread_1")]


@pytest.mark.anyio
async def test_handle_callback_bind_selection_ok() -> None:
    handlers = _HandlerStub()
    key = await handlers._resolve_topic_key(12, None)
    handlers._bind_options[key] = SelectionState(items=[("repo_1", "Repo")])
    callback = TelegramCallbackQuery(
        update_id=2,
        callback_id="cb2",
        from_user_id=3,
        data=encode_bind_callback("repo_1"),
        message_id=4,
        chat_id=12,
        thread_id=None,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls == [("bind", key, "repo_1")]


@pytest.mark.anyio
async def test_handle_callback_compact_selection_expired() -> None:
    handlers = _HandlerStub()
    callback = TelegramCallbackQuery(
        update_id=3,
        callback_id="cb3",
        from_user_id=4,
        data=encode_compact_callback("apply"),
        message_id=5,
        chat_id=15,
        thread_id=16,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls == [("answer", "Selection expired")]


@pytest.mark.anyio
async def test_handle_callback_compact_selection_ok() -> None:
    handlers = _HandlerStub()
    key = await handlers._resolve_topic_key(20, 21)
    handlers._compact_pending[key] = CompactState(
        summary_text="summary",
        display_text="summary",
        message_id=7,
        created_at="now",
    )
    callback = TelegramCallbackQuery(
        update_id=4,
        callback_id="cb4",
        from_user_id=5,
        data=encode_compact_callback("apply"),
        message_id=7,
        chat_id=20,
        thread_id=21,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls
    assert handlers.calls[0][0] == "compact"
    assert handlers.calls[0][1] == key
    assert handlers.calls[0][2] is not None


@pytest.mark.anyio
async def test_handle_callback_question() -> None:
    handlers = _HandlerStub()
    callback = TelegramCallbackQuery(
        update_id=5,
        callback_id="cb5",
        from_user_id=6,
        data=encode_question_option_callback("req-1", 0, 1),
        message_id=9,
        chat_id=33,
        thread_id=34,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls
    assert handlers.calls[0][0] == "question"


@pytest.mark.anyio
async def test_handle_callback_question_custom() -> None:
    handlers = _HandlerStub()
    callback = TelegramCallbackQuery(
        update_id=6,
        callback_id="cb6",
        from_user_id=7,
        data=encode_question_custom_callback("req-2"),
        message_id=10,
        chat_id=35,
        thread_id=36,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls
    assert handlers.calls[0][0] == "question"


@pytest.mark.anyio
async def test_handle_callback_question_done() -> None:
    handlers = _HandlerStub()
    callback = TelegramCallbackQuery(
        update_id=7,
        callback_id="cb7",
        from_user_id=8,
        data=encode_question_done_callback("req-3"),
        message_id=11,
        chat_id=37,
        thread_id=38,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls
    assert handlers.calls[0][0] == "question"


@pytest.mark.anyio
async def test_handle_callback_flow_action() -> None:
    handlers = _HandlerStub()
    callback = TelegramCallbackQuery(
        update_id=8,
        callback_id="cb8",
        from_user_id=9,
        data=encode_flow_callback("resume", "run-123"),
        message_id=12,
        chat_id=40,
        thread_id=41,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls
    assert handlers.calls[0][0] == "flow"
