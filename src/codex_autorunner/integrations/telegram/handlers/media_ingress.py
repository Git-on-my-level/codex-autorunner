from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ....core.logging_utils import log_event
from ....core.orchestration import (
    PausedFlowTarget,
    SurfaceThreadMessageRequest,
    build_surface_orchestration_ingress,
)
from ....core.pma_notification_store import (
    build_notification_context_block,
    notification_surface_key,
)
from ....core.utils import canonicalize_path
from ....integrations.chat.constants import TOPIC_NOT_BOUND_MESSAGE
from ...chat.forwarding import compose_forwarded_message_text
from ...chat.media import audio_content_type_for_input, is_image_mime_or_path
from ..adapter import (
    TelegramDocument,
    TelegramMessage,
    TelegramPhotoSize,
)
from ..config import TelegramMediaCandidate
from ..forwarding import (
    format_forwarded_telegram_message_text,
    message_forward_info,
)
from ..state import TelegramTopicRecord
from .message_policy import event_logger
from .paused_flow_reply import resolve_paused_flow_core, submit_flow_reply_core

_logger = logging.getLogger(__name__)


async def download_message_media(
    handlers: Any,
    message: TelegramMessage,
) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    if message.photos:
        photos = sorted(
            message.photos,
            key=lambda p: (p.file_size or 0, p.width * p.height),
            reverse=True,
        )
        if photos:
            best = photos[0]
            try:
                file_info = await handlers._bot.get_file(best.file_id)
                data = await handlers._bot.download_file(
                    file_info.file_path,
                    max_size_bytes=handlers._config.media.max_image_bytes,
                )
                files.append((f"photo_{best.file_id}.jpg", data))
            except Exception as exc:
                handlers._logger.debug("Failed to download photo: %s", exc)
    elif message.document:
        try:
            file_info = await handlers._bot.get_file(message.document.file_id)
            data = await handlers._bot.download_file(
                file_info.file_path,
                max_size_bytes=handlers._config.media.max_file_bytes,
            )
            filename = (
                message.document.file_name or f"document_{message.document.file_id}"
            )
            files.append((filename, data))
        except Exception as exc:
            handlers._logger.debug("Failed to download document: %s", exc)
    elif message.audio:
        try:
            file_info = await handlers._bot.get_file(message.audio.file_id)
            data = await handlers._bot.download_file(
                file_info.file_path,
                max_size_bytes=handlers._config.media.max_file_bytes,
            )
            filename = message.audio.file_name or f"audio_{message.audio.file_id}"
            files.append((filename, data))
        except Exception as exc:
            handlers._logger.debug("Failed to download audio: %s", exc)
    elif message.voice:
        try:
            file_info = await handlers._bot.get_file(message.voice.file_id)
            data = await handlers._bot.download_file(
                file_info.file_path,
                max_size_bytes=handlers._config.media.max_voice_bytes,
            )
            files.append((f"voice_{message.voice.file_id}.ogg", data))
        except Exception as exc:
            handlers._logger.debug("Failed to download voice: %s", exc)
    return files


def message_has_media(message: TelegramMessage) -> bool:
    return bool(message.photos or message.document or message.voice or message.audio)


def select_photo(
    photos: Sequence[TelegramPhotoSize],
) -> Optional[TelegramPhotoSize]:
    if not photos:
        return None
    return max(
        photos,
        key=lambda item: ((item.file_size or 0), item.width * item.height),
    )


def document_is_image(document: TelegramDocument) -> bool:
    return is_image_mime_or_path(document.mime_type, document.file_name)


def select_image_candidate(
    message: TelegramMessage,
) -> Optional[TelegramMediaCandidate]:
    photo = select_photo(message.photos)
    if photo:
        return TelegramMediaCandidate(
            kind="photo",
            file_id=photo.file_id,
            file_name=None,
            mime_type=None,
            file_size=photo.file_size,
        )
    if message.document and document_is_image(message.document):
        document = message.document
        return TelegramMediaCandidate(
            kind="document",
            file_id=document.file_id,
            file_name=document.file_name,
            mime_type=document.mime_type,
            file_size=document.file_size,
        )
    return None


def select_voice_candidate(
    message: TelegramMessage,
) -> Optional[TelegramMediaCandidate]:
    if message.voice:
        voice = message.voice
        mime_type = audio_content_type_for_input(
            mime_type=voice.mime_type,
            file_name=None,
            source_url=None,
        )
        return TelegramMediaCandidate(
            kind="voice",
            file_id=voice.file_id,
            file_name=None,
            mime_type=mime_type,
            file_size=voice.file_size,
            duration=voice.duration,
        )
    if message.audio:
        audio = message.audio
        mime_type = audio_content_type_for_input(
            mime_type=audio.mime_type,
            file_name=audio.file_name,
            source_url=None,
        )
        return TelegramMediaCandidate(
            kind="audio",
            file_id=audio.file_id,
            file_name=audio.file_name,
            mime_type=mime_type,
            file_size=audio.file_size,
            duration=audio.duration,
        )
    return None


def select_file_candidate(
    message: TelegramMessage,
) -> Optional[TelegramMediaCandidate]:
    if message.document and not document_is_image(message.document):
        document = message.document
        return TelegramMediaCandidate(
            kind="file",
            file_id=document.file_id,
            file_name=document.file_name,
            mime_type=document.mime_type,
            file_size=document.file_size,
        )
    return None


def has_batchable_media(message: TelegramMessage) -> bool:
    return bool(message.photos or message.document)


async def media_batch_key(handlers: Any, message: TelegramMessage) -> str:
    topic_key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    user_id = message.from_user_id
    if message.media_group_id:
        return f"{topic_key}:user:{user_id}:mg:{message.media_group_id}"
    return f"{topic_key}:user:{user_id}:burst"


def record_with_media_workspace(
    handlers: Any, record: Any
) -> tuple[Any, Optional[str]]:
    if record is None:
        return None, None
    pma_enabled = bool(getattr(record, "pma_enabled", False))
    if not pma_enabled:
        return record, None
    hub_root = getattr(handlers, "_hub_root", None)
    if hub_root is None:
        return None, "PMA unavailable; hub root not configured."
    return dataclasses.replace(record, workspace_path=str(hub_root)), None


@dataclass
class MediaBatchBuffer:
    topic_key: str
    messages: list[TelegramMessage] = field(default_factory=list)
    placeholder_id: Optional[int] = None
    task: Optional[asyncio.Task[None]] = None
    media_group_id: Optional[str] = None
    created_at: float = 0.0


def _build_normal_message_kwargs(
    *,
    text_override: str,
    placeholder_id: Optional[int],
    record: Any,
    notification_reply: Any,
    handlers: Any,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "text_override": text_override,
        "placeholder_id": placeholder_id,
    }
    if notification_reply is not None:
        kwargs["record"] = dataclasses.replace(
            record or TelegramTopicRecord(),
            pma_enabled=True,
            repo_id=notification_reply.repo_id or getattr(record, "repo_id", None),
            workspace_path=(
                notification_reply.workspace_root
                or str(getattr(handlers, "_hub_root", None) or "")
                or getattr(record, "workspace_path", None)
            ),
        )
        kwargs["surface_key_override"] = notification_surface_key(
            notification_reply.notification_id
        )
        kwargs["pma_context_prefix"] = build_notification_context_block(
            notification_reply
        )
    return kwargs


async def submit_thread_message_core(
    handlers: Any,
    message: TelegramMessage,
    runtime: Any,
    record: Any,
    text_override: str,
    placeholder_id: Optional[int],
    notification_reply: Any,
) -> None:
    image_candidate = select_image_candidate(message)
    if image_candidate:
        if not handlers._config.media.images:
            await handlers._send_message(
                message.chat_id,
                "Image handling is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await handlers._handle_image_message(
            message,
            runtime,
            record,
            image_candidate,
            text_override,
            placeholder_id=placeholder_id,
        )
        return

    voice_candidate = select_voice_candidate(message)
    if voice_candidate:
        if not handlers._config.media.voice:
            await handlers._send_message(
                message.chat_id,
                "Voice transcription is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await handlers._handle_voice_message(
            message,
            runtime,
            record,
            voice_candidate,
            text_override,
            placeholder_id=placeholder_id,
        )
        return

    file_candidate = select_file_candidate(message)
    if file_candidate:
        if not handlers._config.media.files:
            await handlers._send_message(
                message.chat_id,
                "File handling is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await handlers._handle_file_message(
            message,
            runtime,
            record,
            file_candidate,
            text_override,
            placeholder_id=placeholder_id,
        )
        return

    if not text_override:
        await handlers._send_message(
            message.chat_id,
            "Unsupported media type.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return

    normal_kwargs = _build_normal_message_kwargs(
        text_override=text_override,
        placeholder_id=placeholder_id,
        record=record,
        notification_reply=notification_reply,
        handlers=handlers,
    )
    await handlers._handle_normal_message(message, runtime, **normal_kwargs)


async def handle_media_message(
    handlers: Any,
    message: TelegramMessage,
    runtime: Any,
    caption_text: str,
    *,
    placeholder_id: Optional[int] = None,
) -> None:
    if not handlers._config.media.enabled:
        await handlers._send_message(
            message.chat_id,
            "Media handling is disabled.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    record = await handlers._router.get_topic(key)
    record, pma_error = record_with_media_workspace(handlers, record)
    if pma_error:
        await handlers._send_message(
            message.chat_id,
            pma_error,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    if record is None or not record.workspace_path:
        await handlers._send_message(
            message.chat_id,
            handlers._with_conversation_id(
                TOPIC_NOT_BOUND_MESSAGE,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return

    pma_enabled = bool(getattr(record, "pma_enabled", False))
    workspace_root = canonicalize_path(Path(record.workspace_path))
    notification_reply = None
    if message.reply_to_message_id is not None:
        hub_client = getattr(handlers, "_hub_client", None)
        if hub_client is not None:
            from ....core.hub_control_plane import (
                NotificationReplyTargetLookupRequest as _CPReplyTargetRequest,
            )

            try:
                cp_response = await hub_client.get_notification_reply_target(
                    _CPReplyTargetRequest(
                        surface_kind="telegram",
                        surface_key=key,
                        delivered_message_id=str(message.reply_to_message_id),
                    )
                )
                if cp_response.record is not None:
                    notification_reply = cp_response.record
            except Exception as exc:
                log_event(
                    event_logger(handlers),
                    logging.WARNING,
                    "telegram.media.reply_target.control_plane_failed",
                    topic_key=key,
                    message_id=message.message_id,
                    exc=exc,
                )
    turn_caption_text = format_forwarded_telegram_message_text(message, caption_text)
    paused = None
    if not pma_enabled and notification_reply is None:
        preferred_run_id = handlers._ticket_flow_pause_targets.get(
            str(workspace_root), None
        )
        paused = handlers._get_paused_ticket_flow(
            workspace_root, preferred_run_id=preferred_run_id
        )
    evt_logger = event_logger(handlers)
    ingress = build_surface_orchestration_ingress(
        event_sink=lambda orchestration_event: log_event(
            evt_logger,
            logging.INFO,
            f"telegram.{orchestration_event.event_type}",
            topic_key=key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            surface_kind=orchestration_event.surface_kind,
            target_kind=orchestration_event.target_kind,
            target_id=orchestration_event.target_id,
            status=orchestration_event.status,
            **orchestration_event.metadata,
        )
    )

    async def _resolve_paused_flow(
        _request: SurfaceThreadMessageRequest,
    ) -> Optional[PausedFlowTarget]:
        return await resolve_paused_flow_core(paused, workspace_root)

    async def _submit_flow_reply(
        _request: SurfaceThreadMessageRequest, flow_target: PausedFlowTarget
    ) -> None:
        reply_text = caption_text.strip() if isinstance(caption_text, str) else ""
        if not reply_text:
            reply_text = "Media reply attached."
        files = await download_message_media(handlers, message)
        await submit_flow_reply_core(
            handlers,
            message,
            paused,
            workspace_root,
            compose_forwarded_message_text(
                reply_text,
                message_forward_info(message, reply_text),
            ),
            files=files,
        )

    async def _submit_thread_message(
        _request: SurfaceThreadMessageRequest,
    ) -> None:
        await submit_thread_message_core(
            handlers,
            message,
            runtime,
            record,
            text_override=turn_caption_text,
            placeholder_id=placeholder_id,
            notification_reply=notification_reply,
        )

    if notification_reply is not None:
        await _submit_thread_message(
            SurfaceThreadMessageRequest(
                surface_kind="telegram",
                workspace_root=workspace_root,
                prompt_text=turn_caption_text,
                agent_id=getattr(record, "agent", None),
                pma_enabled=True,
            )
        )
        from .commands.execution import _build_telegram_thread_orchestration_service

        orch_service = _build_telegram_thread_orchestration_service(handlers)
        orch_binding = (
            orch_service.get_binding(
                surface_kind="telegram",
                surface_key=notification_surface_key(
                    notification_reply.notification_id
                ),
            )
            if orch_service is not None
            else None
        )
        if orch_binding is not None:
            hub_client = getattr(handlers, "_hub_client", None)
            if hub_client is not None:
                from ....core.hub_control_plane import (
                    NotificationContinuationBindRequest as _CPContinuationRequest,
                )

                try:
                    await hub_client.bind_notification_continuation(
                        _CPContinuationRequest(
                            notification_id=notification_reply.notification_id,
                            thread_target_id=orch_binding.thread_target_id,
                        )
                    )
                except Exception as exc:
                    log_event(
                        event_logger(handlers),
                        logging.WARNING,
                        "telegram.media.continuation_bind.control_plane_failed",
                        notification_id=notification_reply.notification_id,
                        exc=exc,
                    )
            else:
                log_event(
                    event_logger(handlers),
                    logging.WARNING,
                    "telegram.media.continuation_bind.hub_client_unavailable",
                    notification_id=notification_reply.notification_id,
                )
        return
    await ingress.submit_message(
        SurfaceThreadMessageRequest(
            surface_kind="telegram",
            workspace_root=workspace_root,
            prompt_text=turn_caption_text,
            agent_id=getattr(record, "agent", None),
            pma_enabled=pma_enabled,
        ),
        resolve_paused_flow_target=_resolve_paused_flow,
        submit_flow_reply=_submit_flow_reply,
        submit_thread_message=_submit_thread_message,
    )
