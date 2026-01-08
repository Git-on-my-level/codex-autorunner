import pytest

from codex_autorunner.integrations.telegram.adapter import (
    TelegramCallbackQuery,
    encode_bind_callback,
    encode_resume_callback,
)
from codex_autorunner.integrations.telegram.handlers.callbacks import handle_callback
from codex_autorunner.integrations.telegram.types import SelectionState


class _HandlerStub:
    def __init__(self) -> None:
        self._resume_options: dict[str, SelectionState] = {}
        self._bind_options: dict[str, SelectionState] = {}
        self.calls: list[tuple[str, object]] = []

    def _resolve_topic_key(self, chat_id: int, thread_id: object) -> str:
        return f"{chat_id}:{thread_id}"

    async def _answer_callback(self, _callback: TelegramCallbackQuery, text: str) -> None:
        self.calls.append(("answer", text))

    async def _resume_thread_by_id(
        self, key: str, thread_id: str, _callback: TelegramCallbackQuery
    ) -> None:
        self.calls.append(("resume", key, thread_id))

    async def _bind_topic_by_repo_id(
        self, key: str, repo_id: str, _callback: TelegramCallbackQuery
    ) -> None:
        self.calls.append(("bind", key, repo_id))


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
    key = handlers._resolve_topic_key(10, 11)
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
    key = handlers._resolve_topic_key(12, None)
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
