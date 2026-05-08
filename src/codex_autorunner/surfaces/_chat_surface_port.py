from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

from ..adapters.chat.adapter import ChatAdapter, SendTextRequest
from ..adapters.chat.models import (
    ChatEvent,
    ChatInteractionEvent,
    ChatMessageEvent,
    ChatThreadRef,
)
from ..core.domain.refs import ParticipantRef, SurfaceRef
from ..core.ports.surface_port import (
    EngineCommand,
    InboundEvent,
    OutboundDelivery,
    SurfaceCapabilities,
    SurfaceHealth,
    SurfaceHealthStatus,
)
from ..core.time_utils import now_iso


@dataclass(frozen=True)
class ChatSurfacePortConfig:
    surface_kind: str
    supports_files: bool
    supports_reactions: bool
    supports_typing_indicator: bool
    max_message_length: Optional[int]
    features: tuple[str, ...]
    poll_timeout_seconds: float = 30.0


def surface_ref_for_thread(thread: ChatThreadRef) -> SurfaceRef:
    key = str(thread.chat_id)
    if thread.thread_id:
        key = f"{key}:{thread.thread_id}"
    return SurfaceRef(kind=thread.platform, key=key)


def engine_command_from_chat_event(event: ChatEvent) -> EngineCommand:
    if isinstance(event, ChatMessageEvent):
        target = surface_ref_for_thread(event.thread)
        payload: dict[str, Any] = {
            "surface_kind": target.kind,
            "surface_key": target.key,
            "prompt_text": event.text or "",
            "message_id": event.message.message_id,
            "update_id": event.update_id,
            "from_user_id": event.from_user_id,
            "is_edited": event.is_edited,
            "attachments": [attachment.__dict__ for attachment in event.attachments],
        }
        if event.reply_to is not None:
            payload["reply_to_message_id"] = event.reply_to.message_id
        if event.reply_context is not None:
            payload["reply_context"] = {
                "message_id": event.reply_context.message.message_id,
                "text": event.reply_context.text,
                "author_label": event.reply_context.author_label,
                "is_bot": event.reply_context.is_bot,
            }
        if event.forwarded_from is not None:
            payload["forwarded_from"] = event.forwarded_from.__dict__
        return EngineCommand(
            command_type="surface.message.received",
            target=target,
            payload=payload,
        )

    if isinstance(event, ChatInteractionEvent):
        target = surface_ref_for_thread(event.thread)
        payload = {
            "surface_kind": target.kind,
            "surface_key": target.key,
            "interaction_id": event.interaction.interaction_id,
            "update_id": event.update_id,
            "from_user_id": event.from_user_id,
            "payload": event.payload,
            "message_id": event.message.message_id if event.message else None,
        }
        return EngineCommand(
            command_type="surface.interaction.received",
            target=target,
            payload=payload,
        )

    raise TypeError(f"Unsupported chat event type: {type(event)!r}")


def _event_id(event: ChatEvent) -> str:
    if isinstance(event, ChatMessageEvent):
        return event.message.message_id
    if isinstance(event, ChatInteractionEvent):
        return event.interaction.interaction_id
    return str(getattr(event, "update_id", "unknown"))


def _participant(event: ChatEvent) -> Optional[ParticipantRef]:
    user_id = getattr(event, "from_user_id", None)
    if not user_id:
        return None
    return ParticipantRef(kind="user", id=str(user_id))


def inbound_event_from_chat_event(event: ChatEvent) -> InboundEvent:
    command = engine_command_from_chat_event(event)
    target = command.target or surface_ref_for_thread(event.thread)
    return InboundEvent(
        surface=target,
        event_id=_event_id(event),
        event_type=command.command_type,
        timestamp=now_iso(),
        participant=_participant(event),
        payload={
            "chat_event": event,
            "engine_command": command,
            "engine_command_type": command.command_type,
            "engine_command_payload": command.payload,
        },
    )


class ChatSurfacePort:
    def __init__(
        self,
        *,
        adapter: Optional[ChatAdapter],
        config: ChatSurfacePortConfig,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._logger = logger or logging.getLogger(__name__)

    async def capabilities(self, surface: SurfaceRef) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            surface=self._surface_ref(surface),
            supports_threads=True,
            supports_reactions=self._config.supports_reactions,
            supports_files=self._config.supports_files,
            supports_typing_indicator=self._config.supports_typing_indicator,
            max_message_length=self._config.max_message_length,
            features=list(self._config.features),
        )

    async def health(self, surface: SurfaceRef) -> SurfaceHealth:
        ref = self._surface_ref(surface)
        if self._adapter is None:
            return SurfaceHealth(
                surface=ref,
                status=SurfaceHealthStatus.DEGRADED,
                message="No chat adapter configured",
                checked_at=now_iso(),
            )
        return SurfaceHealth(
            surface=ref,
            status=SurfaceHealthStatus.HEALTHY,
            message="ok",
            checked_at=now_iso(),
        )

    async def send(self, command: EngineCommand) -> OutboundDelivery:
        target = command.target or SurfaceRef(
            kind=self._config.surface_kind, key="default"
        )
        if self._adapter is None:
            return OutboundDelivery(
                delivery_id=f"{self._config.surface_kind}-{now_iso()}",
                surface=target,
                status="unavailable",
                error="No chat adapter configured",
            )

        text = command.payload.get("text") or command.payload.get("message")
        if not isinstance(text, str) or not text:
            text = str(command.payload.get("assistant_text") or "")
        if not text:
            return OutboundDelivery(
                delivery_id=f"{self._config.surface_kind}-{now_iso()}",
                surface=target,
                status="rejected",
                error="No outbound text payload",
            )

        thread = self._thread_ref(target)
        message = await self._adapter.send_text(
            SendTextRequest(thread=thread, text=text)
        )
        return OutboundDelivery(
            delivery_id=message.message_id,
            surface=target,
            status="delivered",
        )

    async def receive(self, surface: SurfaceRef) -> AsyncGenerator[InboundEvent, None]:
        if self._adapter is None:
            return
        while True:
            events = await self._adapter.poll_events(
                timeout_seconds=self._config.poll_timeout_seconds
            )
            for event in events:
                yield inbound_event_from_chat_event(event)

    def command_from_event(self, event: ChatEvent) -> EngineCommand:
        return engine_command_from_chat_event(event)

    def _surface_ref(self, surface: SurfaceRef) -> SurfaceRef:
        if surface.kind == self._config.surface_kind:
            return surface
        return SurfaceRef(kind=self._config.surface_kind, key=surface.key)

    def _thread_ref(self, surface: SurfaceRef) -> ChatThreadRef:
        chat_id, _, thread_id = surface.key.partition(":")
        return ChatThreadRef(
            platform=self._config.surface_kind,
            chat_id=chat_id,
            thread_id=thread_id or None,
        )


__all__ = [
    "ChatSurfacePort",
    "ChatSurfacePortConfig",
    "engine_command_from_chat_event",
    "inbound_event_from_chat_event",
    "surface_ref_for_thread",
]
