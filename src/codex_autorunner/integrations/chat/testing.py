"""Testing utilities for chat adapter contracts (adapter-layer test support)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional, Sequence

from .adapter import ChatAdapter, SendAttachmentRequest, SendTextRequest
from .capabilities import ChatCapabilities
from .models import (
    ChatAction,
    ChatAttachment,
    ChatEvent,
    ChatInteractionEvent,
    ChatInteractionRef,
    ChatMessageEvent,
    ChatMessageRef,
    ChatThreadRef,
)
from .renderer import RenderedText, TextRenderer


class FakeTextRenderer(TextRenderer):
    """Minimal renderer used by FakeChatAdapter."""

    def render_text(
        self, text: str, *, parse_mode: Optional[str] = None
    ) -> RenderedText:
        return RenderedText(text=text, parse_mode=parse_mode)

    def split_text(
        self,
        rendered: RenderedText,
        *,
        max_length: Optional[int] = None,
    ) -> tuple[RenderedText, ...]:
        if max_length is None or max_length <= 0 or len(rendered.text) <= max_length:
            return (rendered,)
        chunks = []
        start = 0
        while start < len(rendered.text):
            end = start + max_length
            chunks.append(
                RenderedText(
                    text=rendered.text[start:end],
                    parse_mode=rendered.parse_mode,
                )
            )
            start = end
        return tuple(chunks)


def serialize_chat_event(event: ChatEvent) -> dict[str, object]:
    """Serialize an event to a stable dict shape for contract testing."""

    payload = asdict(event)
    payload["_event_type"] = (
        "message" if isinstance(event, ChatMessageEvent) else "interaction"
    )
    return payload


def deserialize_chat_event(payload: dict[str, object]) -> ChatEvent:
    """Deserialize a chat event previously created by `serialize_chat_event`."""

    event_type = payload.get("_event_type")
    if event_type == "message":
        return ChatMessageEvent(
            update_id=str(payload["update_id"]),
            thread=_deserialize_thread(payload["thread"]),
            message=_deserialize_message_ref(payload["message"]),
            from_user_id=_string_or_none(payload.get("from_user_id")),
            text=_string_or_none(payload.get("text")),
            is_edited=bool(payload.get("is_edited", False)),
            reply_to=_deserialize_optional_message_ref(payload.get("reply_to")),
            attachments=tuple(
                _deserialize_attachment(item)
                for item in _coerce_list(payload.get("attachments"))
            ),
        )
    if event_type == "interaction":
        return ChatInteractionEvent(
            update_id=str(payload["update_id"]),
            thread=_deserialize_thread(payload["thread"]),
            interaction=_deserialize_interaction_ref(payload["interaction"]),
            from_user_id=_string_or_none(payload.get("from_user_id")),
            payload=_string_or_none(payload.get("payload")),
            message=_deserialize_optional_message_ref(payload.get("message")),
        )
    raise ValueError(f"Unknown event type: {event_type!r}")


class FakeChatAdapter(ChatAdapter):
    """In-memory adapter used to validate chat contract behavior in tests."""

    def __init__(
        self,
        *,
        platform: str = "fake",
        capabilities: Optional[ChatCapabilities] = None,
        renderer: Optional[TextRenderer] = None,
    ) -> None:
        self._platform = platform
        self._capabilities = capabilities or ChatCapabilities()
        self._renderer = renderer or FakeTextRenderer()
        self._next_message_id = 1
        self._events: list[ChatEvent] = []
        self.sent_text_requests: list[SendTextRequest] = []
        self.sent_attachment_requests: list[SendAttachmentRequest] = []
        self.edits: list[tuple[ChatMessageRef, str, tuple[ChatAction, ...]]] = []
        self.deleted_messages: list[ChatMessageRef] = []
        self.acked_interactions: list[tuple[ChatInteractionRef, Optional[str]]] = []

    @property
    def platform(self) -> str:
        return self._platform

    @property
    def capabilities(self) -> ChatCapabilities:
        return self._capabilities

    @property
    def renderer(self) -> TextRenderer:
        return self._renderer

    async def poll_events(
        self, *, timeout_seconds: float = 30.0
    ) -> Sequence[ChatEvent]:
        _ = timeout_seconds
        events = tuple(self._events)
        self._events.clear()
        return events

    async def send_text(self, request: SendTextRequest) -> ChatMessageRef:
        _validate_text_limit(request.text, self._capabilities.max_text_length)
        self.sent_text_requests.append(request)
        return self._new_message_ref(request.thread)

    async def edit_text(
        self,
        message: ChatMessageRef,
        text: str,
        *,
        actions: Sequence[ChatAction] = (),
    ) -> None:
        _validate_text_limit(text, self._capabilities.max_text_length)
        self.edits.append((message, text, tuple(actions)))

    async def delete_message(self, message: ChatMessageRef) -> None:
        self.deleted_messages.append(message)

    async def send_attachment(self, request: SendAttachmentRequest) -> ChatMessageRef:
        self.sent_attachment_requests.append(request)
        return self._new_message_ref(request.thread)

    async def ack_interaction(
        self,
        interaction: ChatInteractionRef,
        *,
        text: Optional[str] = None,
    ) -> None:
        self.acked_interactions.append((interaction, text))

    def emit_message_event(
        self,
        *,
        update_id: str,
        thread: ChatThreadRef,
        message_id: str,
        from_user_id: Optional[str],
        text: Optional[str],
        is_edited: bool = False,
        reply_to: Optional[ChatMessageRef] = None,
        attachments: Sequence[ChatAttachment] = (),
    ) -> ChatMessageEvent:
        event = ChatMessageEvent(
            update_id=update_id,
            thread=thread,
            message=ChatMessageRef(thread=thread, message_id=message_id),
            from_user_id=from_user_id,
            text=text,
            is_edited=is_edited,
            reply_to=reply_to,
            attachments=tuple(attachments),
        )
        self._events.append(event)
        return event

    def emit_interaction_event(
        self,
        *,
        update_id: str,
        thread: ChatThreadRef,
        interaction_id: str,
        from_user_id: Optional[str],
        payload: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> ChatInteractionEvent:
        _validate_payload_limit(payload, self._capabilities.max_callback_payload_bytes)
        message = None
        if message_id is not None:
            message = ChatMessageRef(thread=thread, message_id=message_id)
        event = ChatInteractionEvent(
            update_id=update_id,
            thread=thread,
            interaction=ChatInteractionRef(
                thread=thread, interaction_id=interaction_id
            ),
            from_user_id=from_user_id,
            payload=payload,
            message=message,
        )
        self._events.append(event)
        return event

    def round_trip_event(self, event: ChatEvent) -> ChatEvent:
        """Round-trip event via serialize/deserialize helpers."""

        return deserialize_chat_event(serialize_chat_event(event))

    def _new_message_ref(self, thread: ChatThreadRef) -> ChatMessageRef:
        message = ChatMessageRef(thread=thread, message_id=str(self._next_message_id))
        self._next_message_id += 1
        return message


def _validate_text_limit(text: str, limit: Optional[int]) -> None:
    if limit is not None and len(text) > limit:
        raise ValueError(f"text length {len(text)} exceeds max_text_length {limit}")


def _validate_payload_limit(payload: Optional[str], limit: Optional[int]) -> None:
    if payload is None or limit is None:
        return
    payload_bytes = len(payload.encode("utf-8"))
    if payload_bytes > limit:
        raise ValueError(
            f"payload size {payload_bytes} exceeds max_callback_payload_bytes {limit}"
        )


def _deserialize_thread(value: object) -> ChatThreadRef:
    if not isinstance(value, dict):
        raise TypeError("thread must be a dict")
    return ChatThreadRef(
        platform=str(value["platform"]),
        chat_id=str(value["chat_id"]),
        thread_id=_string_or_none(value.get("thread_id")),
    )


def _deserialize_message_ref(value: object) -> ChatMessageRef:
    if not isinstance(value, dict):
        raise TypeError("message must be a dict")
    return ChatMessageRef(
        thread=_deserialize_thread(value["thread"]),
        message_id=str(value["message_id"]),
    )


def _deserialize_optional_message_ref(value: object) -> Optional[ChatMessageRef]:
    if value is None:
        return None
    return _deserialize_message_ref(value)


def _deserialize_interaction_ref(value: object) -> ChatInteractionRef:
    if not isinstance(value, dict):
        raise TypeError("interaction must be a dict")
    return ChatInteractionRef(
        thread=_deserialize_thread(value["thread"]),
        interaction_id=str(value["interaction_id"]),
    )


def _deserialize_attachment(value: object) -> ChatAttachment:
    if not isinstance(value, dict):
        raise TypeError("attachment must be a dict")
    return ChatAttachment(
        kind=str(value["kind"]),
        file_id=str(value["file_id"]),
        file_name=_string_or_none(value.get("file_name")),
        mime_type=_string_or_none(value.get("mime_type")),
        size_bytes=_int_or_none(value.get("size_bytes")),
    )


def _coerce_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    raise TypeError("expected list or tuple")


def _string_or_none(value: object) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _int_or_none(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return None
