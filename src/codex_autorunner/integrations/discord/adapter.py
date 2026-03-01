"""Discord adapter implementing the ChatAdapter protocol.

This module provides a normalized interface for Discord interactions,
converting Discord-specific events and payloads to platform-agnostic models.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional, Sequence

from ..chat.adapter import ChatAdapter, SendAttachmentRequest, SendTextRequest
from ..chat.capabilities import ChatCapabilities
from ..chat.media import is_audio_mime_or_path, normalize_mime_type
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
from .constants import DISCORD_MAX_MESSAGE_LENGTH
from .errors import DiscordAPIError
from .interactions import (
    extract_channel_id,
    extract_command_path_and_options,
    extract_component_custom_id,
    extract_guild_id,
    extract_interaction_id,
    extract_interaction_token,
    extract_user_id,
    is_component_interaction,
)
from .rendering import (
    chunk_discord_message,
    format_discord_message,
    truncate_for_discord,
)
from .rest import DiscordRestClient

DISCORD_EPHEMERAL_FLAG = 64
_MAX_INTERACTION_TOKEN_CACHE = 4096
_MAX_MESSAGE_INTERACTION_TOKEN_CACHE = 4096


class DiscordTextRenderer(TextRenderer):
    """Discord-specific text renderer implementing TextRenderer protocol."""

    def render_text(
        self, text: str, *, parse_mode: Optional[str] = None
    ) -> RenderedText:
        formatted = format_discord_message(text)
        return RenderedText(text=formatted, parse_mode=None)

    def split_text(
        self,
        rendered: RenderedText,
        *,
        max_length: Optional[int] = None,
    ) -> tuple[RenderedText, ...]:
        limit = max_length or DISCORD_MAX_MESSAGE_LENGTH
        chunks = chunk_discord_message(
            rendered.text, max_len=limit, with_numbering=False
        )
        return tuple(RenderedText(text=chunk, parse_mode=None) for chunk in chunks)


class DiscordChatAdapter(ChatAdapter):
    """ChatAdapter implementation for Discord platform."""

    def __init__(
        self,
        *,
        rest_client: DiscordRestClient,
        application_id: str,
        logger: Optional[logging.Logger] = None,
        message_overflow: str = "split",
    ) -> None:
        self._rest = rest_client
        self._application_id = application_id
        self._logger = logger or logging.getLogger(__name__)
        self._renderer = DiscordTextRenderer()
        self._event_queue: Optional[asyncio.Queue[ChatEvent]] = None
        self._event_queue_loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending_events: list[ChatEvent] = []
        self._message_overflow = message_overflow
        self._interaction_tokens: dict[str, str] = {}
        self._message_interaction_tokens: dict[str, str] = {}
        self._capabilities = ChatCapabilities(
            max_text_length=DISCORD_MAX_MESSAGE_LENGTH,
            max_caption_length=DISCORD_MAX_MESSAGE_LENGTH,
            max_callback_payload_bytes=100,
            supports_threads=True,
            supports_message_edits=True,
            supports_message_delete=True,
            supports_attachments=True,
            supports_interactions=True,
            supported_parse_modes=(),
        )

    @property
    def platform(self) -> str:
        return "discord"

    @property
    def capabilities(self) -> ChatCapabilities:
        return self._capabilities

    @property
    def renderer(self) -> TextRenderer:
        return self._renderer

    async def poll_events(
        self, *, timeout_seconds: float = 30.0
    ) -> Sequence[ChatEvent]:
        event_queue = self._ensure_event_queue()
        try:
            event = await asyncio.wait_for(event_queue.get(), timeout=timeout_seconds)
            return (event,)
        except asyncio.TimeoutError:
            return ()

    def enqueue_interaction_event(
        self, interaction_payload: dict[str, Any]
    ) -> Optional[ChatEvent]:
        event = self._parse_interaction_to_event(interaction_payload)
        if event is not None:
            self._enqueue_event(event)
        return event

    def parse_interaction_event(
        self, interaction_payload: dict[str, Any]
    ) -> Optional[ChatInteractionEvent]:
        event = self._parse_interaction_to_event(interaction_payload)
        if isinstance(event, ChatInteractionEvent):
            return event
        return None

    def enqueue_message_event(
        self, message_payload: dict[str, Any]
    ) -> Optional[ChatEvent]:
        event = self._parse_message_to_event(message_payload)
        if event is not None:
            self._enqueue_event(event)
        return event

    def _ensure_event_queue(self) -> asyncio.Queue[ChatEvent]:
        loop = asyncio.get_running_loop()
        if self._event_queue is not None and self._event_queue_loop is not loop:
            while True:
                try:
                    self._pending_events.append(self._event_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._event_queue = None
            self._event_queue_loop = None

        if self._event_queue is None:
            self._event_queue = asyncio.Queue()
            self._event_queue_loop = loop

        if self._pending_events:
            for pending_event in self._pending_events:
                self._event_queue.put_nowait(pending_event)
            self._pending_events.clear()

        return self._event_queue

    def _enqueue_event(self, event: ChatEvent) -> None:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if (
            self._event_queue is not None
            and self._event_queue_loop is running_loop
            and running_loop is not None
            and not running_loop.is_closed()
        ):
            self._event_queue.put_nowait(event)
            return

        self._pending_events.append(event)

    def parse_message_event(
        self, message_payload: dict[str, Any]
    ) -> Optional[ChatMessageEvent]:
        event = self._parse_message_to_event(message_payload)
        if isinstance(event, ChatMessageEvent):
            return event
        return None

    def _parse_message_to_event(self, payload: dict[str, Any]) -> Optional[ChatEvent]:
        channel_id = payload.get("channel_id")
        guild_id = payload.get("guild_id")
        message_id = payload.get("id")
        author = payload.get("author", {})
        user_id = author.get("id") if isinstance(author, dict) else None

        if not channel_id or not message_id or not user_id:
            return None

        if author.get("bot", False):
            return None

        thread = ChatThreadRef(
            platform="discord",
            chat_id=str(channel_id),
            thread_id=str(guild_id) if isinstance(guild_id, str) and guild_id else None,
        )
        message_ref = ChatMessageRef(thread=thread, message_id=str(message_id))

        content = payload.get("content", "")
        attachments = self._parse_discord_attachments(payload)

        reply_to: Optional[ChatMessageRef] = None
        message_reference = payload.get("message_reference")
        if isinstance(message_reference, dict):
            ref_message_id = message_reference.get("message_id")
            ref_channel_id = message_reference.get("channel_id")
            if ref_message_id and ref_channel_id:
                reply_to = ChatMessageRef(
                    thread=ChatThreadRef(
                        platform="discord", chat_id=str(ref_channel_id), thread_id=None
                    ),
                    message_id=str(ref_message_id),
                )

        return ChatMessageEvent(
            update_id=f"discord:message:{message_id}",
            thread=thread,
            message=message_ref,
            from_user_id=str(user_id),
            text=content if content else None,
            is_edited=False,
            reply_to=reply_to,
            attachments=attachments,
        )

    def _parse_discord_attachments(
        self, payload: dict[str, Any]
    ) -> tuple[ChatAttachment, ...]:
        attachments: list[ChatAttachment] = []

        for attachment in payload.get("attachments", []):
            if not isinstance(attachment, dict):
                continue
            attachment_id = attachment.get("id")
            if not attachment_id:
                continue

            filename = attachment.get("filename")
            content_type = attachment.get("content_type")
            size = attachment.get("size")
            source_url = attachment.get("url")
            if not isinstance(source_url, str) or not source_url.strip():
                source_url = attachment.get("proxy_url")
            if not isinstance(source_url, str) or not source_url.strip():
                source_url = None

            kind = "document"
            mime_base = normalize_mime_type(content_type)
            if mime_base:
                if mime_base.startswith("image/"):
                    kind = "image"
                elif mime_base.startswith("video/"):
                    kind = "video"
                elif mime_base.startswith("audio/"):
                    kind = "audio"
            if kind == "document" and is_audio_mime_or_path(
                mime_type=content_type if isinstance(content_type, str) else None,
                file_name=filename if isinstance(filename, str) else None,
                source_url=source_url if isinstance(source_url, str) else None,
                duration_seconds=attachment.get("duration_secs"),
            ):
                kind = "audio"

            attachments.append(
                ChatAttachment(
                    kind=kind,
                    file_id=str(attachment_id),
                    file_name=filename,
                    mime_type=content_type,
                    size_bytes=size if isinstance(size, int) else None,
                    source_url=source_url,
                )
            )

        return tuple(attachments)

    def _parse_interaction_to_event(
        self, payload: dict[str, Any]
    ) -> Optional[ChatEvent]:
        import json

        channel_id = extract_channel_id(payload)
        guild_id = extract_guild_id(payload)
        interaction_id = extract_interaction_id(payload)
        interaction_token = extract_interaction_token(payload)
        user_id = extract_user_id(payload)

        if not channel_id or not interaction_id or not interaction_token:
            return None

        _remember_token(
            self._interaction_tokens,
            key=interaction_id,
            token=interaction_token,
            max_size=_MAX_INTERACTION_TOKEN_CACHE,
        )

        thread = ChatThreadRef(
            platform="discord",
            chat_id=channel_id,
            thread_id=guild_id,
        )
        interaction_ref = ChatInteractionRef(
            thread=thread, interaction_id=interaction_id
        )

        message_ref: Optional[ChatMessageRef] = None
        message_data = payload.get("message")
        if isinstance(message_data, dict):
            message_id_raw = message_data.get("id")
            if isinstance(message_id_raw, str):
                message_ref = ChatMessageRef(thread=thread, message_id=message_id_raw)

        payload_data: dict[str, Any] = {
            "_discord_token": interaction_token,
            "_discord_interaction_id": interaction_id,
            "guild_id": guild_id,
        }

        if is_component_interaction(payload):
            custom_id = extract_component_custom_id(payload)
            payload_data["component_id"] = custom_id
            data = payload.get("data")
            if isinstance(data, dict):
                values = data.get("values")
                if isinstance(values, list):
                    payload_data["values"] = values
            payload_data["type"] = "component"
        else:
            command_path, options = extract_command_path_and_options(payload)
            command_str = ":".join(command_path) if command_path else ""
            payload_data["command"] = command_str
            payload_data["options"] = options
            payload_data["type"] = "command"

        return ChatInteractionEvent(
            update_id=f"discord:{interaction_id}",
            thread=thread,
            interaction=interaction_ref,
            from_user_id=user_id,
            payload=json.dumps(payload_data, separators=(",", ":")),
            message=message_ref,
        )

    async def send_text(self, request: SendTextRequest) -> ChatMessageRef:
        channel_id = request.thread.chat_id
        rendered = self._renderer.render_text(
            request.text, parse_mode=request.parse_mode
        )
        chunks = self._renderer.split_text(
            rendered, max_length=self._capabilities.max_text_length
        )

        try:
            if self._message_overflow == "document" and len(chunks) > 3:
                response = await self._rest.create_channel_message_with_attachment(
                    channel_id=channel_id,
                    data=request.text.encode("utf-8"),
                    filename="response.md",
                    caption="Response too long; attached as response.md.",
                )
                message_id = response.get("id", "unknown")
                return ChatMessageRef(thread=request.thread, message_id=message_id)

            first_message_id: Optional[str] = None
            for idx, chunk in enumerate(chunks):
                payload: dict[str, Any] = {"content": chunk.text}
                if idx == 0 and request.actions:
                    payload["components"] = self._build_action_components(
                        request.actions
                    )
                if request.reply_to and idx == 0:
                    payload["message_reference"] = {
                        "message_id": request.reply_to.message_id,
                        "channel_id": channel_id,
                    }

                response = await self._rest.create_channel_message(
                    channel_id=channel_id, payload=payload
                )
                if idx == 0:
                    first_message_id = response.get("id")

            return ChatMessageRef(
                thread=request.thread,
                message_id=first_message_id or "unknown",
            )
        except DiscordAPIError as exc:
            self._logger.error(
                "Failed to send text message to channel %s: %s",
                channel_id,
                exc,
            )
            raise
        except Exception as exc:
            self._logger.error(
                "Unexpected error sending text message to channel %s: %s",
                channel_id,
                exc,
            )
            raise

    async def edit_text(
        self,
        message: ChatMessageRef,
        text: str,
        *,
        actions: Sequence[ChatAction] = (),
    ) -> None:
        rendered = self._renderer.render_text(text)
        truncated = truncate_for_discord(
            rendered.text,
            max_len=self._capabilities.max_text_length or DISCORD_MAX_MESSAGE_LENGTH,
        )
        payload: dict[str, Any] = {"content": truncated}
        if actions:
            payload["components"] = self._build_action_components(actions)

        try:
            token = self._message_interaction_tokens.get(message.message_id)
            if token:
                await self._rest.edit_original_interaction_response(
                    application_id=self._application_id,
                    interaction_token=token,
                    payload=payload,
                )
            else:
                await self._rest.edit_channel_message(
                    channel_id=message.thread.chat_id,
                    message_id=message.message_id,
                    payload=payload,
                )
        except DiscordAPIError as exc:
            self._logger.error(
                "Failed to edit message %s: %s",
                message.message_id,
                exc,
            )
            raise
        except Exception as exc:
            self._logger.error(
                "Unexpected error editing message %s: %s",
                message.message_id,
                exc,
            )
            raise

    async def delete_message(self, message: ChatMessageRef) -> None:
        await self._rest.delete_channel_message(
            channel_id=message.thread.chat_id,
            message_id=message.message_id,
        )

    async def send_interaction_followup(
        self,
        interaction_id: str,
        text: str,
        *,
        actions: Sequence[ChatAction] = (),
        ephemeral: bool = False,
    ) -> Optional[ChatMessageRef]:
        token = self._interaction_tokens.get(interaction_id)
        if not token:
            self._logger.warning(
                "No token found for interaction %s, cannot send followup",
                interaction_id,
            )
            return None

        rendered = self._renderer.render_text(text)
        truncated = truncate_for_discord(
            rendered.text,
            max_len=self._capabilities.max_text_length or DISCORD_MAX_MESSAGE_LENGTH,
        )

        payload: dict[str, Any] = {"content": truncated}
        if actions:
            payload["components"] = self._build_action_components(actions)
        if ephemeral:
            payload["flags"] = DISCORD_EPHEMERAL_FLAG

        response = await self._rest.create_followup_message(
            application_id=self._application_id,
            interaction_token=token,
            payload=payload,
        )

        message_id = response.get("id")
        if message_id:
            _remember_token(
                self._message_interaction_tokens,
                key=message_id,
                token=token,
                max_size=_MAX_MESSAGE_INTERACTION_TOKEN_CACHE,
            )
            thread = ChatThreadRef(
                platform="discord",
                chat_id=response.get("channel_id", ""),
                thread_id=None,
            )
            return ChatMessageRef(thread=thread, message_id=message_id)
        return None

    async def send_attachment(self, request: SendAttachmentRequest) -> ChatMessageRef:
        path = Path(request.file_path)
        try:
            data = path.read_bytes()
            response = await self._rest.create_channel_message_with_attachment(
                channel_id=request.thread.chat_id,
                data=data,
                filename=path.name,
                caption=request.caption,
            )
            message_id = response.get("id", "unknown")
            return ChatMessageRef(thread=request.thread, message_id=message_id)
        except DiscordAPIError as exc:
            self._logger.error(
                "Failed to send attachment %s to channel %s: %s",
                path.name,
                request.thread.chat_id,
                exc,
            )
            raise
        except Exception as exc:
            self._logger.error(
                "Unexpected error sending attachment %s to channel %s: %s",
                path.name,
                request.thread.chat_id,
                exc,
            )
            raise

    async def ack_interaction(
        self,
        interaction: ChatInteractionRef,
        *,
        text: Optional[str] = None,
    ) -> None:
        content = text
        if content:
            content = truncate_for_discord(
                content,
                max_len=self._capabilities.max_text_length
                or DISCORD_MAX_MESSAGE_LENGTH,
            )
        payload: dict[str, Any] = {
            "type": 4,
            "data": {"flags": DISCORD_EPHEMERAL_FLAG},
        }
        if content:
            payload["data"]["content"] = content

        token = self._interaction_tokens.get(interaction.interaction_id)
        if not token:
            self._logger.warning(
                "No token found for interaction %s, cannot ack",
                interaction.interaction_id,
            )
            return

        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction.interaction_id,
                interaction_token=token,
                payload=payload,
            )
        except DiscordAPIError as exc:
            self._logger.error(
                "Failed to ack interaction %s: %s",
                interaction.interaction_id,
                exc,
            )
            raise
        except Exception as exc:
            self._logger.error(
                "Unexpected error acking interaction %s: %s",
                interaction.interaction_id,
                exc,
            )
            raise

    def _build_action_components(
        self, actions: Sequence[ChatAction]
    ) -> list[dict[str, Any]]:
        rows: list[list[dict[str, Any]]] = []
        current_row: list[dict[str, Any]] = []
        for action in actions:
            button = {
                "type": 2,
                "style": 1,
                "label": action.label,
                "custom_id": action.action_id,
            }
            current_row.append(button)
            if len(current_row) >= 5:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)
        return [{"type": 1, "components": row} for row in rows]


def _remember_token(
    cache: dict[str, str],
    *,
    key: str,
    token: str,
    max_size: int,
) -> None:
    if key in cache:
        cache.pop(key, None)
    cache[key] = token
    while len(cache) > max_size:
        cache.pop(next(iter(cache)), None)
