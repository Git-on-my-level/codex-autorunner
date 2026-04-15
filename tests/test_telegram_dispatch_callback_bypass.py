from __future__ import annotations

import logging
from typing import Any, Optional

import pytest

from codex_autorunner.core.orchestration import ChatOperationState
from codex_autorunner.integrations.telegram.adapter import (
    TelegramCallbackQuery,
    TelegramUpdate,
    encode_approval_callback,
    encode_cancel_callback,
    encode_flow_callback,
    encode_page_callback,
    encode_question_cancel_callback,
    encode_question_done_callback,
    encode_question_option_callback,
    encode_resume_callback,
)
from codex_autorunner.integrations.telegram.dispatch import (
    DispatchContext,
    _dispatch_callback,
)


def _callback_update(
    data: str,
    *,
    chat_id: int = 10,
    thread_id: Optional[int] = 20,
    update_id: int = 1,
) -> TelegramUpdate:
    return TelegramUpdate(
        update_id=update_id,
        message=None,
        callback=TelegramCallbackQuery(
            update_id=update_id,
            callback_id=f"cb-{update_id}",
            from_user_id=99,
            data=data,
            message_id=5,
            chat_id=chat_id,
            thread_id=thread_id,
        ),
    )


def _context(
    *,
    topic_key: Optional[str] = "10:20",
    operation_id: Optional[str] = None,
) -> DispatchContext:
    return DispatchContext(
        chat_id=10,
        user_id=99,
        thread_id=20,
        message_id=5,
        is_topic=True,
        is_edited=False,
        topic_key=topic_key,
        operation_id=operation_id,
    )


class _HandlersStub:
    def __init__(self) -> None:
        self.handle_callback_calls: list[TelegramCallbackQuery] = []
        self.enqueue_calls: list[tuple[str, Any]] = []
        self.mark_state_calls: list[tuple[Optional[str], ChatOperationState]] = []
        self._logger = logging.getLogger("test_dispatch")

    async def _handle_callback(self, callback: TelegramCallbackQuery) -> None:
        self.handle_callback_calls.append(callback)

    def _enqueue_topic_work(
        self,
        key: str,
        work: Any,
        *,
        force_queue: bool = False,
        item_id: Optional[str] = None,
    ) -> None:
        self.enqueue_calls.append((key, force_queue))

    async def _mark_chat_operation_state(
        self,
        operation_id: Optional[str],
        *,
        state: ChatOperationState,
        **changes: Any,
    ) -> None:
        self.mark_state_calls.append((operation_id, state))

    def _with_chat_operation(
        self,
        operation_id: Optional[str],
        work: Any,
    ) -> Any:
        return work

    async def _begin_typing_indicator(
        self, chat_id: Optional[int], thread_id: Optional[int]
    ) -> None:
        pass

    async def _end_typing_indicator(
        self, chat_id: Optional[int], thread_id: Optional[int]
    ) -> None:
        pass


@pytest.mark.anyio
async def test_interrupt_callback_bypasses_queue() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_cancel_callback("interrupt"))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == 1
    assert any(
        s == ChatOperationState.INTERRUPTING for _, s in handlers.mark_state_calls
    )


@pytest.mark.anyio
async def test_queue_cancel_callback_bypasses_queue() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_cancel_callback("queue_cancel:123"))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == 1


@pytest.mark.anyio
async def test_queue_interrupt_send_callback_bypasses_queue() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_cancel_callback("queue_interrupt_send:123"))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == 1
    assert any(
        s == ChatOperationState.INTERRUPTING for _, s in handlers.mark_state_calls
    )


@pytest.mark.anyio
async def test_approval_callback_bypasses_queue() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_approval_callback("accept", "req-1"))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == 1


@pytest.mark.anyio
async def test_question_option_callback_bypasses_queue() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_question_option_callback("req-1", 0, 1))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == 1


@pytest.mark.anyio
async def test_question_done_callback_bypasses_queue() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_question_done_callback("req-1"))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == 1


@pytest.mark.anyio
async def test_question_cancel_callback_bypasses_queue() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_question_cancel_callback("req-1"))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == 1


@pytest.mark.anyio
async def test_page_callback_bypasses_queue() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_page_callback("bind", 2))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == 1


@pytest.mark.anyio
async def test_flow_refresh_callback_bypasses_queue() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_flow_callback("refresh"))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == 1


@pytest.mark.anyio
async def test_cancel_selection_callback_bypasses_queue() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_cancel_callback("agent"))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == 1


@pytest.mark.anyio
async def test_resume_callback_enqueued() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_resume_callback("thread-1"))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 1
    assert handlers.enqueue_calls[0] == ("10:20", True)
    assert len(handlers.handle_callback_calls) == 0


@pytest.mark.anyio
async def test_flow_status_callback_enqueued() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_flow_callback("status", "run-1"))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 1
    assert len(handlers.handle_callback_calls) == 0


@pytest.mark.anyio
async def test_non_interrupt_cancel_uses_running_state() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_cancel_callback("queue_cancel:123"))
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    running_states = [
        s for _, s in handlers.mark_state_calls if s == ChatOperationState.RUNNING
    ]
    assert len(running_states) >= 1
    interrupting_states = [
        s for _, s in handlers.mark_state_calls if s == ChatOperationState.INTERRUPTING
    ]
    assert len(interrupting_states) == 0


@pytest.mark.anyio
async def test_unknown_callback_data_does_not_bypass() -> None:
    handlers = _HandlersStub()
    update = _callback_update("unknown_callback_type:value")
    ctx = _context()
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 1


@pytest.mark.anyio
async def test_no_topic_key_always_executes_directly() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_resume_callback("thread-1"))
    ctx = _context(topic_key=None)
    await _dispatch_callback(handlers, update, ctx)
    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == 1


@pytest.mark.anyio
async def test_control_callback_bypasses_while_normal_callbacks_queue() -> None:
    handlers = _HandlersStub()
    control_update = _callback_update(encode_cancel_callback("interrupt"), update_id=2)
    normal_update = _callback_update(encode_resume_callback("t-1"), update_id=3)
    ctx = _context()

    await _dispatch_callback(handlers, normal_update, ctx)
    assert len(handlers.enqueue_calls) == 1
    assert len(handlers.handle_callback_calls) == 0

    await _dispatch_callback(handlers, control_update, ctx)
    assert len(handlers.enqueue_calls) == 1
    assert len(handlers.handle_callback_calls) == 1
    assert handlers.handle_callback_calls[0].callback_id == "cb-2"


@pytest.mark.anyio
async def test_pagination_bypasses_while_resume_queued() -> None:
    handlers = _HandlersStub()
    page_update = _callback_update(encode_page_callback("bind", 3), update_id=10)
    resume_update = _callback_update(encode_resume_callback("t-1"), update_id=11)
    ctx = _context()

    await _dispatch_callback(handlers, resume_update, ctx)
    assert len(handlers.enqueue_calls) == 1

    await _dispatch_callback(handlers, page_update, ctx)
    assert len(handlers.enqueue_calls) == 1
    assert len(handlers.handle_callback_calls) == 1
    assert handlers.handle_callback_calls[0].callback_id == "cb-10"


@pytest.mark.anyio
async def test_approval_bypasses_while_queue_saturated() -> None:
    handlers = _HandlersStub()
    approval_update = _callback_update(
        encode_approval_callback("accept", "req-1"), update_id=20
    )
    resume_update = _callback_update(encode_resume_callback("t-1"), update_id=21)
    ctx = _context()

    await _dispatch_callback(handlers, resume_update, ctx)
    await _dispatch_callback(
        handlers,
        _callback_update(encode_resume_callback("t-2"), update_id=22),
        ctx,
    )
    assert len(handlers.enqueue_calls) == 2

    await _dispatch_callback(handlers, approval_update, ctx)
    assert len(handlers.enqueue_calls) == 2
    assert len(handlers.handle_callback_calls) == 1


@pytest.mark.anyio
async def test_multiple_control_callbacks_all_bypass() -> None:
    handlers = _HandlersStub()
    ctx = _context()

    control_datas = [
        encode_cancel_callback("interrupt"),
        encode_cancel_callback("queue_cancel:123"),
        encode_cancel_callback("queue_interrupt_send:456"),
        encode_page_callback("agent", 2),
        encode_flow_callback("refresh"),
        encode_cancel_callback("model"),
    ]

    for i, data in enumerate(control_datas):
        update = _callback_update(data, update_id=i + 100)
        await _dispatch_callback(handlers, update, ctx)

    assert len(handlers.enqueue_calls) == 0
    assert len(handlers.handle_callback_calls) == len(control_datas)


@pytest.mark.anyio
async def test_interrupt_produces_correct_state_sequence() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_cancel_callback("interrupt"))
    ctx = _context(operation_id="op-1")
    await _dispatch_callback(handlers, update, ctx)

    states = [s for _, s in handlers.mark_state_calls]
    assert ChatOperationState.INTERRUPTING in states
    assert ChatOperationState.COMPLETED in states
    assert states.index(ChatOperationState.INTERRUPTING) < states.index(
        ChatOperationState.COMPLETED
    )


@pytest.mark.anyio
async def test_long_running_callback_produces_acknowledged_before_enqueue() -> None:
    handlers = _HandlersStub()
    update = _callback_update(encode_resume_callback("thread-1"))
    ctx = _context(operation_id="op-2")
    await _dispatch_callback(handlers, update, ctx)

    assert len(handlers.enqueue_calls) == 1
    states = [s for _, s in handlers.mark_state_calls]
    assert ChatOperationState.ACKNOWLEDGED in states
    assert ChatOperationState.RUNNING not in states


@pytest.mark.anyio
async def test_control_callback_error_marks_failed() -> None:
    handlers = _HandlersStub()

    async def _fail(callback: TelegramCallbackQuery) -> None:
        raise RuntimeError("test failure")

    handlers._handle_callback = _fail  # type: ignore[assignment]

    update = _callback_update(encode_cancel_callback("interrupt"))
    ctx = _context(operation_id="op-3")

    with pytest.raises(RuntimeError, match="test failure"):
        await _dispatch_callback(handlers, update, ctx)

    states = [s for _, s in handlers.mark_state_calls]
    assert ChatOperationState.INTERRUPTING in states
    assert ChatOperationState.FAILED in states
