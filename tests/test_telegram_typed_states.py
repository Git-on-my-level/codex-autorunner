from __future__ import annotations

import logging
from pathlib import Path

import pytest

from codex_autorunner.integrations.telegram.adapter import (
    TelegramCallbackQuery,
    UpdateConfirmCallback,
)
from codex_autorunner.integrations.telegram.handlers.commands.execution import (
    ExecutionCommands,
    _TurnDeliveryState,
)
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
)
from codex_autorunner.integrations.telegram.handlers.selections import (
    TelegramSelectionHandlers,
)
from codex_autorunner.integrations.telegram.types import (
    CompactStatusState,
    TelegramNoticeContext,
    UpdateConfirmState,
)


class _ExecutionProgressHandler(ExecutionCommands):
    def __init__(self) -> None:
        self._turn_contexts = {("thread-1", "turn-1"): object()}
        self.cleared: list[tuple[str, tuple[str, str]]] = []

    def _render_turn_progress_summary(self, _turn_key: tuple[str, str]) -> str:
        return "done · agent codex · 1s"

    def _clear_thinking_preview(self, turn_key: tuple[str, str]) -> None:
        self.cleared.append(("thinking", turn_key))

    def _clear_turn_progress(self, turn_key: tuple[str, str]) -> None:
        self.cleared.append(("progress", turn_key))


class _ExecutionFallbackHandler(_ExecutionProgressHandler):
    _render_turn_progress_summary = None

    def _render_final_turn_progress(self, _turn_key: tuple[str, str]) -> str:
        return "Interrupted."


class _UpdateConfirmHandler(TelegramSelectionHandlers):
    def __init__(self, *, target: str | None) -> None:
        self._update_confirm_options = {"10:20": UpdateConfirmState(target=target)}
        self.answers: list[str] = []
        self.finalized: list[str] = []
        self.started: list[dict[str, object]] = []
        self.prompted = 0

    async def _answer_callback(
        self, _callback: TelegramCallbackQuery, text: str
    ) -> None:
        self.answers.append(text)

    async def _finalize_selection(
        self, _key: str, _callback: TelegramCallbackQuery, text: str
    ) -> None:
        self.finalized.append(text)

    async def _start_update(self, **kwargs: object) -> None:
        self.started.append(dict(kwargs))

    async def _prompt_update_selection_from_callback(
        self, _key: str, _callback: TelegramCallbackQuery, *, prompt: str = ""
    ) -> None:
        _ = prompt
        self.prompted += 1


class _CompactStatusHandler(TelegramCommandHandlers):
    def __init__(self, status_path: Path) -> None:
        self._logger = logging.getLogger("test")
        self._status_path = status_path
        self.edits: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []

    def _compact_status_path(self) -> Path:
        return self._status_path

    async def _edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: object = None,
        **_kwargs: object,
    ) -> bool:
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return True

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None,
        reply_to: int | None = None,
    ) -> None:
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "thread_id": thread_id,
                "reply_to": reply_to,
            }
        )


def _callback() -> TelegramCallbackQuery:
    return TelegramCallbackQuery(
        update_id=1,
        callback_id="cb-1",
        from_user_id=99,
        data="confirm",
        message_id=7,
        chat_id=10,
        thread_id=20,
    )


def test_turn_delivery_state_captures_rendered_progress_summary() -> None:
    handler = _ExecutionProgressHandler()
    state = _TurnDeliveryState()

    handler._finalize_turn_progress(("thread-1", "turn-1"), state)

    assert state.intermediate_response == "done · agent codex · 1s"
    assert ("thread-1", "turn-1") not in handler._turn_contexts
    assert handler.cleared == [
        ("thinking", ("thread-1", "turn-1")),
        ("progress", ("thread-1", "turn-1")),
    ]


def test_turn_delivery_state_falls_back_to_final_renderer() -> None:
    handler = _ExecutionFallbackHandler()
    state = _TurnDeliveryState()

    handler._finalize_turn_progress(("thread-1", "turn-1"), state)

    assert state.intermediate_response == "Interrupted."


def test_finalize_turn_progress_clears_state_even_if_summary_capture_fails() -> None:
    class _FailingExecutionProgressHandler(_ExecutionProgressHandler):
        def _render_turn_progress_summary(self, _turn_key: tuple[str, str]) -> str:
            raise RuntimeError("disk I/O error")

    handler = _FailingExecutionProgressHandler()
    state = _TurnDeliveryState()

    with pytest.raises(RuntimeError, match="disk I/O error"):
        handler._finalize_turn_progress(("thread-1", "turn-1"), state)

    assert ("thread-1", "turn-1") not in handler._turn_contexts
    assert handler.cleared == [
        ("thinking", ("thread-1", "turn-1")),
        ("progress", ("thread-1", "turn-1")),
    ]


def test_telegram_notice_context_round_trips_payload() -> None:
    context = TelegramNoticeContext(chat_id=10, thread_id=20, reply_to=30)

    assert TelegramNoticeContext.from_payload(context.to_payload()) == context
    assert TelegramNoticeContext.from_payload({"chat_id": True}) is None


def test_compact_status_state_round_trips_payload() -> None:
    state = CompactStatusState(
        status="error",
        message="failed",
        at=123.0,
        chat_id=10,
        thread_id=20,
        message_id=30,
        display_text="preview",
        error_detail="boom",
        started_at=100.0,
        notify_sent_at=150.0,
    )

    assert CompactStatusState.from_payload(state.to_payload()) == state


@pytest.mark.anyio
async def test_update_confirm_callback_uses_typed_target_state() -> None:
    handler = _UpdateConfirmHandler(target="web")

    await handler._handle_update_confirm_callback(
        "10:20",
        _callback(),
        UpdateConfirmCallback(decision="yes"),
    )

    assert handler.started == [
        {
            "chat_id": 10,
            "thread_id": 20,
            "update_target": "web",
            "callback": _callback(),
            "selection_key": "10:20",
        }
    ]
    assert handler.prompted == 0


@pytest.mark.anyio
async def test_maybe_send_compact_status_notice_marks_running_status_interrupted(
    tmp_path: Path,
) -> None:
    handler = _CompactStatusHandler(tmp_path / "compact-status.json")
    handler._write_compact_status(
        "running",
        "Applying summary...",
        chat_id=10,
        thread_id=20,
        message_id=30,
        display_text="Summary preview",
    )

    await handler._maybe_send_compact_status_notice()

    assert handler.edits == [
        {
            "chat_id": 10,
            "message_id": 30,
            "text": "Summary preview\n\nCompact apply interrupted by restart. Please retry.",
            "reply_markup": None,
        }
    ]
    status = handler._read_compact_status()
    assert status is not None
    assert status.status == "interrupted"
    assert status.message == "Compact apply interrupted by restart. Please retry."
    assert status.notify_sent_at is not None
