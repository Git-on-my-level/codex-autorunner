"""Telegram implementation of the generic chat transport interface.

This module stays in the Telegram adapter layer and delegates outbound work to
existing Telegram transport/outbox/progress primitives.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from ..chat.models import ChatAction, ChatInteractionRef, ChatMessageRef, ChatThreadRef
from ..chat.transport import ChatTransport
from .progress_stream import TurnProgressTracker, render_progress_text


class _TelegramTransportOwner(Protocol):
    _bot: Any

    async def _send_message_with_outbox(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        placeholder_id: Optional[int] = None,
    ) -> bool: ...

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
    ) -> None: ...

    async def _edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> bool: ...

    async def _delete_message(
        self, chat_id: int, message_id: Optional[int], thread_id: Optional[int] = None
    ) -> bool: ...

    async def _send_document(
        self,
        chat_id: int,
        data: bytes,
        *,
        filename: str,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        caption: Optional[str] = None,
    ) -> bool: ...

    async def _answer_callback(self, callback: Optional[Any], text: str) -> None: ...


class TelegramChatTransport(ChatTransport):
    """Chat transport delegating to existing Telegram service primitives.

    This is intentionally a thin wrapper during extraction: Telegram handlers
    still use the existing `_send_*` methods directly, and this transport is
    wired for new chat-core call sites.
    """

    def __init__(self, owner: _TelegramTransportOwner) -> None:
        self._owner = owner

    async def send_text(
        self,
        thread: ChatThreadRef,
        text: str,
        *,
        reply_to: Optional[ChatMessageRef] = None,
        parse_mode: Optional[str] = None,
    ) -> ChatMessageRef:
        del parse_mode  # Existing outbox path handles render/parse choices internally.
        chat_id = _require_int_id(thread.chat_id, kind="chat")
        ok = await self._owner._send_message_with_outbox(
            chat_id,
            text,
            thread_id=_optional_int_id(thread.thread_id),
            reply_to=_optional_message_id(reply_to),
        )
        if not ok:
            raise RuntimeError("telegram outbox delivery failed")
        # Outbox flow does not expose Telegram message_id synchronously.
        return ChatMessageRef(
            thread=thread,
            message_id=f"outbox:{secrets.token_hex(8)}",
        )

    async def edit_text(
        self,
        message: ChatMessageRef,
        text: str,
        *,
        actions: Sequence[ChatAction] = (),
    ) -> None:
        await self._owner._edit_message_text(
            _require_int_id(message.thread.chat_id, kind="chat"),
            _require_int_id(message.message_id, kind="message"),
            text,
            message_thread_id=_optional_int_id(message.thread.thread_id),
            reply_markup=_build_reply_markup(actions),
        )

    async def delete_message(self, message: ChatMessageRef) -> None:
        await self._owner._delete_message(
            _require_int_id(message.thread.chat_id, kind="chat"),
            _optional_int_id(message.message_id),
            _optional_int_id(message.thread.thread_id),
        )

    async def send_attachment(
        self,
        thread: ChatThreadRef,
        file_path: str,
        *,
        caption: Optional[str] = None,
        reply_to: Optional[ChatMessageRef] = None,
    ) -> ChatMessageRef:
        path = Path(file_path)
        ok = await self._owner._send_document(
            _require_int_id(thread.chat_id, kind="chat"),
            path.read_bytes(),
            filename=path.name,
            thread_id=_optional_int_id(thread.thread_id),
            reply_to=_optional_message_id(reply_to),
            caption=caption,
        )
        if not ok:
            raise RuntimeError("telegram attachment delivery failed")
        return ChatMessageRef(
            thread=thread,
            message_id=f"outbox:{secrets.token_hex(8)}",
        )

    async def present_actions(
        self,
        thread: ChatThreadRef,
        text: str,
        *,
        actions: Sequence[ChatAction],
        reply_to: Optional[ChatMessageRef] = None,
        parse_mode: Optional[str] = None,
    ) -> ChatMessageRef:
        await self._owner._send_message(
            _require_int_id(thread.chat_id, kind="chat"),
            text,
            thread_id=_optional_int_id(thread.thread_id),
            reply_to=_optional_message_id(reply_to),
            reply_markup=_build_reply_markup(actions),
            parse_mode=parse_mode,
        )
        return ChatMessageRef(
            thread=thread,
            message_id=f"message:{secrets.token_hex(8)}",
        )

    async def ack_interaction(
        self,
        interaction: ChatInteractionRef,
        *,
        text: Optional[str] = None,
    ) -> None:
        callback = _CallbackQueryStub(
            callback_id=interaction.interaction_id,
            chat_id=_optional_int_id(interaction.thread.chat_id),
            thread_id=_optional_int_id(interaction.thread.thread_id),
        )
        await self._owner._answer_callback(callback, text or "")

    @staticmethod
    def render_progress(
        tracker: TurnProgressTracker,
        *,
        max_length: int,
        now: Optional[float] = None,
    ) -> str:
        """Expose existing Telegram progress rendering behavior for chat-core."""

        return render_progress_text(tracker, max_length=max_length, now=now)


class _CallbackQueryStub:
    def __init__(
        self,
        *,
        callback_id: str,
        chat_id: Optional[int],
        thread_id: Optional[int],
    ) -> None:
        self.callback_id = callback_id
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.message_id: Optional[int] = None


def _build_reply_markup(actions: Sequence[ChatAction]) -> Optional[dict[str, Any]]:
    if not actions:
        return None
    keyboard = []
    for action in actions:
        callback_data = (
            action.payload if action.payload is not None else action.action_id
        )
        keyboard.append([{"text": action.label, "callback_data": callback_data}])
    return {"inline_keyboard": keyboard}


def _require_int_id(raw: str, *, kind: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{kind} id must be numeric for telegram: {raw!r}") from exc


def _optional_int_id(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _optional_message_id(message: Optional[ChatMessageRef]) -> Optional[int]:
    if message is None:
        return None
    return _optional_int_id(message.message_id)
