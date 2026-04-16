import pytest

from codex_autorunner.integrations.telegram.adapter import (
    TelegramCallbackQuery,
    encode_agent_callback,
    encode_agent_profile_callback,
    encode_bind_callback,
    encode_cancel_callback,
    encode_effort_callback,
    encode_flow_callback,
    encode_flow_run_callback,
    encode_model_callback,
    encode_page_callback,
    encode_question_custom_callback,
    encode_question_done_callback,
    encode_question_option_callback,
    encode_resume_callback,
    encode_review_commit_callback,
    encode_update_callback,
    encode_update_confirm_callback,
)
from codex_autorunner.integrations.telegram.handlers.callbacks import handle_callback
from codex_autorunner.integrations.telegram.types import CompactState, SelectionState


class _HandlerStub:
    def __init__(self) -> None:
        self._resume_options: dict[str, SelectionState] = {}
        self._bind_options: dict[str, SelectionState] = {}
        self._flow_run_options: dict[str, SelectionState] = {}
        self._compact_pending: dict[str, CompactState] = {}
        self.calls: list[tuple[str, object]] = []

    async def _resolve_topic_key(self, chat_id: int, thread_id: object) -> str:
        return f"{chat_id}:{thread_id}"

    async def _answer_callback(
        self, _callback: TelegramCallbackQuery, text: str
    ) -> None:
        self.calls.append(("answer", text))

    async def _selection_resume_thread_by_id(
        self, key: str, thread_id: str, _callback: TelegramCallbackQuery
    ) -> None:
        self.calls.append(("resume", key, thread_id))

    async def _selection_bind_topic_by_repo_id(
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

    async def _handle_flow_run_callback(
        self, key: str, _callback: TelegramCallbackQuery, parsed: object
    ) -> None:
        self.calls.append(("flow-run", key, parsed))

    async def _handle_agent_callback(
        self, key: str, _callback: TelegramCallbackQuery, parsed: object
    ) -> None:
        self.calls.append(("agent", key, parsed))

    async def _handle_agent_profile_callback(
        self, key: str, _callback: TelegramCallbackQuery, parsed: object
    ) -> None:
        self.calls.append(("agent-profile", key, parsed))

    async def _handle_model_callback(
        self, key: str, _callback: TelegramCallbackQuery, parsed: object
    ) -> None:
        self.calls.append(("model", key, parsed))

    async def _handle_effort_callback(
        self, key: str, _callback: TelegramCallbackQuery, parsed: object
    ) -> None:
        self.calls.append(("effort", key, parsed))

    async def _handle_update_callback(
        self, key: str, _callback: TelegramCallbackQuery, parsed: object
    ) -> None:
        self.calls.append(("update", key, parsed))

    async def _handle_update_confirm_callback(
        self, key: str, _callback: TelegramCallbackQuery, parsed: object
    ) -> None:
        self.calls.append(("update-confirm", key, parsed))

    async def _handle_review_commit_callback(
        self, key: str, _callback: TelegramCallbackQuery, parsed: object
    ) -> None:
        self.calls.append(("review-commit", key, parsed))

    async def _handle_selection_cancel(
        self, key: str, parsed: object, _callback: TelegramCallbackQuery
    ) -> None:
        self.calls.append(("selection-cancel", key, parsed))

    async def _handle_selection_page(
        self, key: str, parsed: object, _callback: TelegramCallbackQuery
    ) -> None:
        self.calls.append(("page", key, parsed))

    async def _handle_queue_cancel_callback(
        self, _callback: TelegramCallbackQuery, kind: str
    ) -> None:
        self.calls.append(("queue-cancel", kind))

    async def _handle_queue_interrupt_send_callback(
        self, _callback: TelegramCallbackQuery, kind: str
    ) -> None:
        self.calls.append(("queue-interrupt-send", kind))


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
async def test_handle_callback_resume_selection_rejects_other_user() -> None:
    handlers = _HandlerStub()
    key = await handlers._resolve_topic_key(10, 11)
    handlers._resume_options[key] = SelectionState(
        items=[("thread_1", "One")],
        requester_user_id="99",
    )
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
async def test_handle_callback_bind_selection_rejects_other_user() -> None:
    handlers = _HandlerStub()
    key = await handlers._resolve_topic_key(12, None)
    handlers._bind_options[key] = SelectionState(
        items=[("repo_1", "Repo")],
        requester_user_id="99",
    )
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
    assert handlers.calls == [("answer", "Selection expired")]


@pytest.mark.anyio
async def test_handle_callback_compact_selection_expired() -> None:
    handlers = _HandlerStub()
    callback = TelegramCallbackQuery(
        update_id=3,
        callback_id="cb3",
        from_user_id=4,
        data="compact:apply",
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
        data="compact:apply",
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


@pytest.mark.anyio
async def test_handle_callback_flow_run_action() -> None:
    handlers = _HandlerStub()
    key = await handlers._resolve_topic_key(42, None)
    handlers._flow_run_options[key] = SelectionState(items=[("run-1", "Run 1")])
    callback = TelegramCallbackQuery(
        update_id=9,
        callback_id="cb9",
        from_user_id=10,
        data=encode_flow_run_callback("run-1"),
        message_id=13,
        chat_id=42,
        thread_id=None,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls
    assert handlers.calls[0][0] == "flow-run"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("data", "expected_call"),
    [
        (encode_agent_callback("codex"), "agent"),
        (encode_agent_profile_callback("fast"), "agent-profile"),
        (encode_model_callback("gpt-5.4"), "model"),
        (encode_effort_callback("high"), "effort"),
        (encode_update_callback("web"), "update"),
        (encode_update_confirm_callback("yes"), "update-confirm"),
        (encode_review_commit_callback("abc123"), "review-commit"),
        (encode_cancel_callback("agent"), "selection-cancel"),
        (encode_page_callback("agent", 1), "page"),
    ],
)
async def test_handle_callback_routes_extended_selection_callbacks(
    data: str, expected_call: str
) -> None:
    handlers = _HandlerStub()
    callback = TelegramCallbackQuery(
        update_id=12,
        callback_id="cb12",
        from_user_id=11,
        data=data,
        message_id=16,
        chat_id=50,
        thread_id=51,
    )

    await handle_callback(handlers, callback)

    assert handlers.calls
    assert handlers.calls[0][0] == expected_call
    assert handlers.calls[0][1] == "50:51"


@pytest.mark.anyio
async def test_handle_callback_queue_cancel_action() -> None:
    handlers = _HandlerStub()
    callback = TelegramCallbackQuery(
        update_id=10,
        callback_id="cb10",
        from_user_id=10,
        data=encode_cancel_callback("queue_cancel:123"),
        message_id=14,
        chat_id=42,
        thread_id=None,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls == [("queue-cancel", "queue_cancel:123")]


@pytest.mark.anyio
async def test_handle_callback_queue_interrupt_send_action() -> None:
    handlers = _HandlerStub()
    callback = TelegramCallbackQuery(
        update_id=11,
        callback_id="cb11",
        from_user_id=10,
        data=encode_cancel_callback("queue_interrupt_send:123"),
        message_id=15,
        chat_id=42,
        thread_id=None,
    )
    await handle_callback(handlers, callback)
    assert handlers.calls == [("queue-interrupt-send", "queue_interrupt_send:123")]
