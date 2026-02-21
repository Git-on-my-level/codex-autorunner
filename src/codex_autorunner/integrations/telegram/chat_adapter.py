"""Telegram implementation of the platform-agnostic chat adapter contract.

This module belongs to the Telegram adapter layer and maps Telegram-native
updates/operations to `integrations.chat` models and protocols.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from ..chat.adapter import ChatAdapter, SendAttachmentRequest, SendTextRequest
from ..chat.capabilities import ChatCapabilities
from ..chat.errors import ChatAdapterPermanentError
from ..chat.models import (
    ChatAction,
    ChatAttachment,
    ChatEvent,
    ChatInteractionEvent,
    ChatInteractionRef,
    ChatMessageEvent,
    ChatMessageRef,
    ChatThreadRef,
)
from ..chat.renderer import RenderedText, TextRenderer
from .adapter import (
    TelegramBotClient,
    TelegramCallbackQuery,
    TelegramMessage,
    TelegramUpdate,
    TelegramUpdatePoller,
)
from .constants import TELEGRAM_CALLBACK_DATA_LIMIT, TELEGRAM_MAX_MESSAGE_LENGTH
from .rendering import _format_telegram_html, _format_telegram_markdown

_TELEGRAM_PARSE_MODES = ("HTML", "Markdown", "MarkdownV2")


class TelegramTextRenderer(TextRenderer):
    """Telegram-aware text renderer for chat-core transport usage."""

    def render_text(
        self, text: str, *, parse_mode: Optional[str] = None
    ) -> RenderedText:
        if parse_mode == "HTML":
            return RenderedText(text=_format_telegram_html(text), parse_mode=parse_mode)
        if parse_mode in ("Markdown", "MarkdownV2"):
            return RenderedText(
                text=_format_telegram_markdown(text, parse_mode),
                parse_mode=parse_mode,
            )
        return RenderedText(text=text, parse_mode=parse_mode)

    def split_text(
        self,
        rendered: RenderedText,
        *,
        max_length: Optional[int] = None,
    ) -> tuple[RenderedText, ...]:
        limit = (
            TELEGRAM_MAX_MESSAGE_LENGTH
            if max_length is None or max_length <= 0
            else max_length
        )
        text = rendered.text
        if len(text) <= limit:
            return (rendered,)
        chunks = []
        start = 0
        while start < len(text):
            end = start + limit
            chunks.append(
                RenderedText(text=text[start:end], parse_mode=rendered.parse_mode)
            )
            start = end
        return tuple(chunks)


class TelegramChatAdapter(ChatAdapter):
    """Adapter wrapper that normalizes Telegram updates and delivery methods."""

    def __init__(
        self,
        bot: TelegramBotClient,
        *,
        poller: Optional[TelegramUpdatePoller] = None,
        renderer: Optional[TextRenderer] = None,
    ) -> None:
        self._bot = bot
        self._poller = poller or TelegramUpdatePoller(bot)
        self._renderer = renderer or TelegramTextRenderer()
        self._capabilities = ChatCapabilities(
            max_text_length=TELEGRAM_MAX_MESSAGE_LENGTH,
            max_caption_length=1024,
            max_callback_payload_bytes=TELEGRAM_CALLBACK_DATA_LIMIT,
            supports_threads=True,
            supports_message_edits=True,
            supports_message_delete=True,
            supports_attachments=True,
            supports_interactions=True,
            supported_parse_modes=_TELEGRAM_PARSE_MODES,
        )

    @property
    def platform(self) -> str:
        return "telegram"

    @property
    def capabilities(self) -> ChatCapabilities:
        return self._capabilities

    @property
    def renderer(self) -> TextRenderer:
        return self._renderer

    async def poll_events(
        self, *, timeout_seconds: float = 30.0
    ) -> Sequence[ChatEvent]:
        timeout = int(timeout_seconds)
        if timeout <= 0:
            timeout = 1
        updates = await self._poller.poll(timeout=timeout)
        events: list[ChatEvent] = []
        for update in updates:
            mapped = self._map_update(update)
            if mapped is not None:
                events.append(mapped)
        return tuple(events)

    async def send_text(self, request: SendTextRequest) -> ChatMessageRef:
        rendered = self._renderer.render_text(
            request.text, parse_mode=request.parse_mode
        )
        response = await self._bot.send_message(
            self._chat_id(request.thread),
            rendered.text,
            message_thread_id=self._thread_id(request.thread),
            reply_to_message_id=self._reply_to_id(request.reply_to),
            reply_markup=self._action_markup(request.actions),
            parse_mode=rendered.parse_mode,
        )
        message_id = self._message_id_from_response(response)
        return ChatMessageRef(thread=request.thread, message_id=message_id)

    async def edit_text(
        self,
        message: ChatMessageRef,
        text: str,
        *,
        actions: Sequence[ChatAction] = (),
    ) -> None:
        rendered = self._renderer.render_text(text)
        await self._bot.edit_message_text(
            self._chat_id(message.thread),
            self._required_int_id(message.message_id, kind="message"),
            rendered.text,
            message_thread_id=self._thread_id(message.thread),
            reply_markup=self._action_markup(actions),
            parse_mode=rendered.parse_mode,
        )

    async def delete_message(self, message: ChatMessageRef) -> None:
        await self._bot.delete_message(
            self._chat_id(message.thread),
            self._required_int_id(message.message_id, kind="message"),
            message_thread_id=self._thread_id(message.thread),
        )

    async def send_attachment(self, request: SendAttachmentRequest) -> ChatMessageRef:
        path = Path(request.file_path)
        payload = path.read_bytes()
        response = await self._bot.send_document(
            self._chat_id(request.thread),
            payload,
            filename=path.name,
            message_thread_id=self._thread_id(request.thread),
            reply_to_message_id=self._reply_to_id(request.reply_to),
            caption=request.caption,
        )
        message_id = self._message_id_from_response(response)
        return ChatMessageRef(thread=request.thread, message_id=message_id)

    async def send_file(self, request: SendAttachmentRequest) -> ChatMessageRef:
        """Back-compat alias for attachment delivery naming."""

        return await self.send_attachment(request)

    async def ack_interaction(
        self,
        interaction: ChatInteractionRef,
        *,
        text: Optional[str] = None,
    ) -> None:
        await self._bot.answer_callback_query(
            interaction.interaction_id,
            chat_id=self._optional_int_id(interaction.thread.chat_id),
            thread_id=self._thread_id(interaction.thread),
            text=text,
        )

    def _map_update(self, update: TelegramUpdate) -> Optional[ChatEvent]:
        if update.message is not None:
            return self._map_message(update.message)
        if update.callback is not None:
            return self._map_callback(update.callback)
        return None

    def _map_message(self, message: TelegramMessage) -> ChatMessageEvent:
        thread = self._thread_ref(message.chat_id, message.thread_id)
        content = message.text if message.text is not None else message.caption
        reply_to = None
        if message.reply_to_message_id is not None:
            reply_to = ChatMessageRef(
                thread=thread, message_id=str(message.reply_to_message_id)
            )
        attachments = self._message_attachments(message)
        return ChatMessageEvent(
            update_id=str(message.update_id),
            thread=thread,
            message=ChatMessageRef(thread=thread, message_id=str(message.message_id)),
            from_user_id=_string_or_none(message.from_user_id),
            text=content,
            is_edited=message.is_edited,
            reply_to=reply_to,
            attachments=attachments,
        )

    def _map_callback(
        self, callback: TelegramCallbackQuery
    ) -> Optional[ChatInteractionEvent]:
        if callback.chat_id is None:
            return None
        thread = self._thread_ref(callback.chat_id, callback.thread_id)
        message = None
        if callback.message_id is not None:
            message = ChatMessageRef(thread=thread, message_id=str(callback.message_id))
        return ChatInteractionEvent(
            update_id=str(callback.update_id),
            thread=thread,
            interaction=ChatInteractionRef(
                thread=thread,
                interaction_id=callback.callback_id,
            ),
            from_user_id=_string_or_none(callback.from_user_id),
            payload=callback.data,
            message=message,
        )

    def _message_attachments(
        self, message: TelegramMessage
    ) -> tuple[ChatAttachment, ...]:
        attachments: list[ChatAttachment] = []
        for photo in message.photos:
            attachments.append(
                ChatAttachment(
                    kind="photo",
                    file_id=photo.file_id,
                    file_name=None,
                    mime_type=None,
                    size_bytes=photo.file_size,
                )
            )
        if message.document is not None:
            attachments.append(
                ChatAttachment(
                    kind="document",
                    file_id=message.document.file_id,
                    file_name=message.document.file_name,
                    mime_type=message.document.mime_type,
                    size_bytes=message.document.file_size,
                )
            )
        if message.audio is not None:
            attachments.append(
                ChatAttachment(
                    kind="audio",
                    file_id=message.audio.file_id,
                    file_name=message.audio.file_name,
                    mime_type=message.audio.mime_type,
                    size_bytes=message.audio.file_size,
                )
            )
        if message.voice is not None:
            attachments.append(
                ChatAttachment(
                    kind="voice",
                    file_id=message.voice.file_id,
                    file_name=None,
                    mime_type=message.voice.mime_type,
                    size_bytes=message.voice.file_size,
                )
            )
        return tuple(attachments)

    def _thread_ref(self, chat_id: int, thread_id: Optional[int]) -> ChatThreadRef:
        return ChatThreadRef(
            platform=self.platform,
            chat_id=str(chat_id),
            thread_id=_string_or_none(thread_id),
        )

    def _chat_id(self, thread: ChatThreadRef) -> Any:
        numeric = self._optional_int_id(thread.chat_id)
        if numeric is not None:
            return numeric
        return thread.chat_id

    def _thread_id(self, thread: ChatThreadRef) -> Optional[int]:
        if thread.thread_id is None:
            return None
        return self._required_int_id(thread.thread_id, kind="thread")

    def _reply_to_id(self, reply_to: Optional[ChatMessageRef]) -> Optional[int]:
        if reply_to is None:
            return None
        return self._required_int_id(reply_to.message_id, kind="message")

    def _action_markup(self, actions: Sequence[ChatAction]) -> Optional[dict[str, Any]]:
        if not actions:
            return None
        return {
            "inline_keyboard": [
                [
                    {
                        "text": action.label,
                        "callback_data": (
                            action.payload if action.payload else action.action_id
                        ),
                    }
                ]
                for action in actions
            ]
        }

    def _message_id_from_response(self, response: dict[str, Any]) -> str:
        message_id = response.get("message_id")
        if isinstance(message_id, int):
            return str(message_id)
        raise ChatAdapterPermanentError("Telegram response missing message_id")

    def _required_int_id(self, value: str, *, kind: str) -> int:
        parsed = self._optional_int_id(value)
        if parsed is None:
            raise ChatAdapterPermanentError(f"invalid {kind} id: {value!r}")
        return parsed

    def _optional_int_id(self, value: Any) -> Optional[int]:
        try:
            if value is None:
                return None
            if isinstance(value, bool):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None


def _string_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)
