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
            rendered.text, max_len=limit, with_numbering=True
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
        self._event_queue: asyncio.Queue[ChatEvent] = asyncio.Queue()
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
        try:
            event = await asyncio.wait_for(
                self._event_queue.get(), timeout=timeout_seconds
            )
            return (event,)
        except asyncio.TimeoutError:
            return ()

    def enqueue_interaction_event(
        self, interaction_payload: dict[str, Any]
    ) -> Optional[ChatEvent]:
        event = self._parse_interaction_to_event(interaction_payload)
        if event is not None:
            self._event_queue.put_nowait(event)
        return event

    def enqueue_message_event(
        self, message_payload: dict[str, Any]
    ) -> Optional[ChatEvent]:
        event = self._parse_message_to_event(message_payload)
        if event is not None:
            self._event_queue.put_nowait(event)
        return event

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

            kind = "document"
            if content_type:
                if content_type.startswith("image/"):
                    kind = "image"
                elif content_type.startswith("video/"):
                    kind = "video"
                elif content_type.startswith("audio/"):
                    kind = "audio"

            attachments.append(
                ChatAttachment(
                    kind=kind,
                    file_id=str(attachment_id),
                    file_name=filename,
                    mime_type=content_type,
                    size_bytes=size if isinstance(size, int) else None,
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

        self._interaction_tokens[interaction_id] = interaction_token

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
            self._message_interaction_tokens[message_id] = token
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
