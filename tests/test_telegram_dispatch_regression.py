from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import pytest

from codex_autorunner.integrations.telegram.adapter import (
    TelegramCallbackQuery,
    TelegramMessage,
    TelegramUpdate,
    encode_resume_callback,
)
from codex_autorunner.integrations.telegram.dispatch import (
    DispatchContext,
    _build_context,
    _operation_identity,
    dispatch_update,
)


def _make_message(
    *,
    update_id: int = 1,
    message_id: int = 10,
    chat_id: int = 100,
    thread_id: Optional[int] = None,
    from_user_id: int = 42,
    text: str = "hello",
) -> TelegramMessage:
    return TelegramMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        thread_id=thread_id,
        from_user_id=from_user_id,
        text=text,
        date=None,
        is_topic_message=False,
    )


def _make_callback(
    *,
    update_id: int = 1,
    callback_id: str = "cb-1",
    chat_id: int = 100,
    thread_id: Optional[int] = None,
    from_user_id: int = 42,
    data: str = "resume:thread-1",
) -> TelegramCallbackQuery:
    return TelegramCallbackQuery(
        update_id=update_id,
        callback_id=callback_id,
        from_user_id=from_user_id,
        data=data,
        message_id=10,
        chat_id=chat_id,
        thread_id=thread_id,
    )


class _DispatchHandlerStub:
    def __init__(self) -> None:
        self._allowlist = type(
            "Allowlist",
            (),
            {
                "allowed_chat_ids": {100},
                "allowed_user_ids": {42},
                "require_topic": False,
            },
        )()
        self._logger = logging.getLogger("test.dispatch.regression")
        self.resolved_keys: list[tuple[int, Optional[int]]] = []
        self.callbacks_handled: list[TelegramCallbackQuery] = []
        self.messages_handled: list[TelegramMessage] = []
        self.enqueued_work: list[tuple[str, Any]] = []
        self.dedup_checks: list[tuple[str, int]] = []
        self.operations_registered: list[dict[str, Any]] = []
        self.operation_states: list[tuple[Optional[str], str]] = []
        self.bypass_results: list[TelegramMessage] = []
        self.typing_begins: list[tuple[int, Optional[int]]] = []
        self.typing_ends: list[tuple[int, Optional[int]]] = []

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        self.resolved_keys.append((chat_id, thread_id))
        if thread_id is not None:
            return f"{chat_id}:{thread_id}"
        return f"{chat_id}:root"

    async def _should_process_update(self, key: str, update_id: int) -> bool:
        self.dedup_checks.append((key, update_id))
        return True

    async def _register_accepted_chat_operation(self, **kwargs: Any) -> bool:
        self.operations_registered.append(kwargs)
        return True

    async def _handle_callback(self, callback: TelegramCallbackQuery) -> None:
        self.callbacks_handled.append(callback)

    async def _handle_message(self, message: TelegramMessage) -> None:
        self.messages_handled.append(message)

    def _enqueue_topic_work(
        self,
        key: str,
        work: Any,
        *,
        force_queue: bool = False,
        item_id: Optional[str] = None,
    ) -> None:
        self.enqueued_work.append((key, work))
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(work())
        except Exception:
            pass

    def _should_bypass_topic_queue(self, message: TelegramMessage) -> bool:
        self.bypass_results.append(message)
        return False

    async def _mark_chat_operation_state(
        self,
        operation_id: Optional[str],
        *,
        state: Any,
        **changes: Any,
    ) -> None:
        self.operation_states.append((operation_id, str(state)))

    def _with_chat_operation(self, operation_id: Optional[str], work: Any) -> Any:
        return work

    async def _begin_typing_indicator(
        self, chat_id: int, thread_id: Optional[int]
    ) -> None:
        self.typing_begins.append((chat_id, thread_id))

    async def _end_typing_indicator(
        self, chat_id: int, thread_id: Optional[int]
    ) -> None:
        self.typing_ends.append((chat_id, thread_id))

    @property
    def _router(self) -> Any:
        return type("Router", (), {"runtime_for": lambda self_, key: None})()


@pytest.mark.anyio
async def test_dispatch_message_enqueues_and_handles() -> None:
    handlers = _DispatchHandlerStub()
    update = TelegramUpdate(
        update_id=1,
        message=_make_message(),
        callback=None,
    )

    await dispatch_update(handlers, update)

    assert len(handlers.enqueued_work) == 1
    key, _work = handlers.enqueued_work[0]
    assert key == "100:root"
    await asyncio.sleep(0.05)
    assert len(handlers.messages_handled) == 1
    assert handlers.messages_handled[0].text == "hello"


@pytest.mark.anyio
async def test_dispatch_callback_enqueues_and_handles() -> None:
    handlers = _DispatchHandlerStub()
    update = TelegramUpdate(
        update_id=2,
        message=None,
        callback=_make_callback(data=encode_resume_callback("thread-1")),
    )

    await dispatch_update(handlers, update)

    assert len(handlers.enqueued_work) == 1
    await asyncio.sleep(0.05)
    assert len(handlers.callbacks_handled) == 1


@pytest.mark.anyio
async def test_dispatch_rejects_disallowed_chat() -> None:
    handlers = _DispatchHandlerStub()
    update = TelegramUpdate(
        update_id=3,
        message=_make_message(chat_id=999),
        callback=None,
    )

    await dispatch_update(handlers, update)

    assert len(handlers.messages_handled) == 0
    assert len(handlers.callbacks_handled) == 0
    assert len(handlers.enqueued_work) == 0


@pytest.mark.anyio
async def test_dispatch_rejects_disallowed_user() -> None:
    handlers = _DispatchHandlerStub()
    update = TelegramUpdate(
        update_id=4,
        message=_make_message(from_user_id=999),
        callback=None,
    )

    await dispatch_update(handlers, update)

    assert len(handlers.messages_handled) == 0
    assert len(handlers.enqueued_work) == 0


@pytest.mark.anyio
async def test_dispatch_skips_duplicate_update() -> None:
    handlers = _DispatchHandlerStub()

    async def _reject_second(key: str, update_id: int) -> bool:
        return update_id != 5

    handlers._should_process_update = _reject_second  # type: ignore[assignment]

    update = TelegramUpdate(
        update_id=5,
        message=_make_message(update_id=5),
        callback=None,
    )

    await dispatch_update(handlers, update)

    assert len(handlers.messages_handled) == 0
    assert len(handlers.enqueued_work) == 0


@pytest.mark.anyio
async def test_dispatch_registers_operation() -> None:
    handlers = _DispatchHandlerStub()
    update = TelegramUpdate(
        update_id=6,
        message=_make_message(update_id=6),
        callback=None,
    )

    await dispatch_update(handlers, update)

    assert len(handlers.operations_registered) == 1
    reg = handlers.operations_registered[0]
    assert reg["operation_id"] == "telegram:update:6"
    assert reg["kind"] == "message"


@pytest.mark.anyio
async def test_build_context_extracts_message_fields() -> None:
    handlers = _DispatchHandlerStub()
    update = TelegramUpdate(
        update_id=7,
        message=_make_message(chat_id=100, thread_id=200, from_user_id=42),
        callback=None,
    )

    ctx = await _build_context(handlers, update)

    assert ctx.chat_id == 100
    assert ctx.thread_id == 200
    assert ctx.user_id == 42
    assert ctx.message_id == 10
    assert ctx.topic_key == "100:200"


@pytest.mark.anyio
async def test_build_context_extracts_callback_fields() -> None:
    handlers = _DispatchHandlerStub()
    update = TelegramUpdate(
        update_id=8,
        message=None,
        callback=_make_callback(chat_id=100, thread_id=200),
    )

    ctx = await _build_context(handlers, update)

    assert ctx.chat_id == 100
    assert ctx.thread_id == 200
    assert ctx.user_id == 42
    assert ctx.topic_key == "100:200"


@pytest.mark.anyio
async def test_operation_identity_message_uses_update_id() -> None:
    update = TelegramUpdate(
        update_id=10,
        message=_make_message(update_id=10),
        callback=None,
    )
    op_id, surface_key, kind = _operation_identity(update)
    assert op_id == "telegram:update:10"
    assert surface_key == "telegram:update:10"
    assert kind == "message"


@pytest.mark.anyio
async def test_operation_identity_callback_uses_update_id() -> None:
    update = TelegramUpdate(
        update_id=11,
        message=None,
        callback=_make_callback(update_id=11),
    )
    op_id, surface_key, kind = _operation_identity(update)
    assert op_id == "telegram:update:11"
    assert kind == "callback"


def test_dispatch_context_is_frozen() -> None:
    ctx = DispatchContext(
        chat_id=1,
        user_id=2,
        thread_id=3,
        message_id=4,
        is_topic=True,
        is_edited=False,
        topic_key="1:3",
    )
    with pytest.raises(AttributeError):
        ctx.chat_id = 99  # type: ignore[misc]


@pytest.mark.anyio
async def test_dispatch_duplicate_operation_not_inserted() -> None:
    handlers = _DispatchHandlerStub()

    async def _always_reject(**kwargs: Any) -> bool:
        return False

    handlers._register_accepted_chat_operation = _always_reject  # type: ignore[assignment]

    update = TelegramUpdate(
        update_id=20,
        message=_make_message(update_id=20),
        callback=None,
    )

    await dispatch_update(handlers, update)

    assert len(handlers.messages_handled) == 0
    assert len(handlers.enqueued_work) == 0


@pytest.mark.anyio
async def test_dispatch_resolves_topic_key() -> None:
    handlers = _DispatchHandlerStub()
    update = TelegramUpdate(
        update_id=30,
        message=_make_message(update_id=30, chat_id=100, thread_id=200),
        callback=None,
    )

    await dispatch_update(handlers, update)

    assert (100, 200) in handlers.resolved_keys


@pytest.mark.anyio
async def test_dispatch_checks_dedup() -> None:
    handlers = _DispatchHandlerStub()
    update = TelegramUpdate(
        update_id=40,
        message=_make_message(update_id=40),
        callback=None,
    )

    await dispatch_update(handlers, update)

    assert ("100:root", 40) in handlers.dedup_checks
