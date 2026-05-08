from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from .....core.filebox import (
    inbox_dir as filebox_inbox_dir,
)
from .....core.filebox import (
    outbox_dir as filebox_outbox_dir,
)
from .....core.injected_context import wrap_injected_context
from .....core.logging_utils import log_event
from .....core.state import now_iso
from ....chat.constants import TOPIC_NOT_BOUND_MESSAGE
from ....chat.media import IMAGE_CONTENT_TYPES, IMAGE_EXTS
from ...adapter import TelegramAPIError, TelegramMessage
from ...config import TelegramMediaCandidate
from ...forwarding import format_forwarded_telegram_message_text
from ...state import PendingVoiceRecord, TelegramTopicRecord
from ..media_ingress import record_with_media_workspace as _record_with_media_workspace
from ..media_ingress import select_file_candidate, select_image_candidate
from .command_utils import (
    _format_download_failure_response,
    _format_telegram_download_error,
)
from .filebox import FileBoxCommandsMixin
from .shared import FILES_HINT_TEMPLATE, TelegramCommandSupportMixin

PMA_FILES_HINT_TEMPLATE = (
    "PMA inbox: {inbox}\n"
    "PMA outbox (pending): {outbox}\n"
    "Place files in outbox pending to send after this turn finishes.\n"
    "Check delivery with /files outbox.\n"
    "Max file size: {max_bytes} bytes."
)


@dataclass
class MediaBatchStats:
    """Track successes and failures while processing media batches."""

    failed_count: int = 0
    image_disabled: int = 0
    file_disabled: int = 0
    image_too_large: int = 0
    file_too_large: int = 0
    image_download_failed: int = 0
    file_download_failed: int = 0
    image_save_failed: int = 0
    file_save_failed: int = 0
    unsupported: int = 0
    image_download_detail: Optional[str] = None
    file_download_detail: Optional[str] = None


@dataclass
class MediaBatchContext:
    """Metadata required to process a batch of Telegram media messages."""

    first_message: TelegramMessage
    sorted_messages: list[TelegramMessage]
    record: "TelegramTopicRecord"
    runtime: Any
    topic_key: str
    max_image_bytes: int
    max_file_bytes: int


@dataclass
class MediaBatchResult:
    """Outcome of media batch processing."""

    saved_image_paths: list[Path]
    saved_image_inbox_info: list[tuple[str, str, int]]
    saved_file_info: list[tuple[str, str, int]]
    stats: MediaBatchStats


class FilesCommands(FileBoxCommandsMixin, TelegramCommandSupportMixin):
    def _format_media_batch_failure(
        self,
        *,
        image_disabled: int,
        file_disabled: int,
        image_too_large: int,
        file_too_large: int,
        image_download_failed: int,
        file_download_failed: int,
        image_download_detail: Optional[str] = None,
        file_download_detail: Optional[str] = None,
        image_save_failed: int,
        file_save_failed: int,
        unsupported: int,
        max_image_bytes: int,
        max_file_bytes: int,
    ) -> str:
        base = "Failed to process any media in the batch."
        details: list[str] = []
        if image_disabled:
            details.append(
                f"{image_disabled} image(s) skipped (image handling disabled)."
            )
        if file_disabled:
            details.append(f"{file_disabled} file(s) skipped (file handling disabled).")
        if image_too_large:
            details.append(
                f"{image_too_large} image(s) too large (max {max_image_bytes} bytes)."
            )
        if file_too_large:
            details.append(
                f"{file_too_large} file(s) too large (max {max_file_bytes} bytes)."
            )
        if image_download_failed:
            line = f"{image_download_failed} image(s) failed to download."
            if image_download_detail:
                label = "error" if image_download_failed == 1 else "last error"
                line = f"{line} ({label}: {image_download_detail})"
            details.append(line)
        if file_download_failed:
            line = f"{file_download_failed} file(s) failed to download."
            if file_download_detail:
                label = "error" if file_download_failed == 1 else "last error"
                line = f"{line} ({label}: {file_download_detail})"
            details.append(line)
        if image_save_failed:
            details.append(f"{image_save_failed} image(s) failed to save.")
        if file_save_failed:
            details.append(f"{file_save_failed} file(s) failed to save.")
        if unsupported:
            details.append(f"{unsupported} item(s) had unsupported media types.")
        if not details:
            return base
        return f"{base}\n" + "\n".join(f"- {line}" for line in details)

    async def _handle_image_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: Any,
        candidate: TelegramMediaCandidate,
        caption_text: str,
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.image.received",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
            file_size=candidate.file_size,
            has_caption=bool(caption_text),
        )
        max_bytes = self._config.media.max_image_bytes
        if candidate.file_size and candidate.file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Image too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            data, file_path, file_size = await self._download_telegram_file(
                candidate.file_id,
                max_bytes=max_bytes,
            )
        except asyncio.CancelledError:
            raise
        except (TelegramAPIError, RuntimeError) as exc:
            detail = _format_telegram_download_error(exc)
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.image.download_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                detail=detail,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _format_download_failure_response("image", detail),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if file_size and file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Image too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if len(data) > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Image too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            image_path = self._save_image_file(
                record.workspace_path,
                data,
                file_path,
                candidate,
                pma_enabled=bool(getattr(record, "pma_enabled", False)),
            )
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.image.save_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to save image.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        image_inbox_path: Optional[Path] = None
        pma_enabled = bool(getattr(record, "pma_enabled", False))
        try:
            image_inbox_path = self._save_inbox_file(
                record.workspace_path,
                key,
                data,
                candidate=candidate,
                file_path=file_path,
                pma_enabled=pma_enabled,
            )
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.image.inbox_save_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )
        prompt_parts = [caption_text.strip() or self._config.media.image_prompt]
        if image_inbox_path is not None:
            original_name = (
                candidate.file_name
                or (Path(file_path).name if file_path else None)
                or image_inbox_path.name
            )
            prompt_parts.append(
                "\n".join(
                    [
                        "Image details:",
                        f"- Name: {original_name}",
                        f"- Size: {file_size or len(data)} bytes",
                        f"- Saved to: {image_inbox_path}",
                    ]
                )
            )
        prompt_parts.append(
            self._build_files_hint(
                workspace_path=record.workspace_path,
                topic_key=key,
                pma_enabled=pma_enabled,
            )
        )
        prompt_text = "\n\n".join(prompt_parts)
        input_items = [
            {"type": "text", "text": prompt_text},
            {"type": "localImage", "path": str(image_path)},
        ]
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.image.ready",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            path=str(image_path),
            inbox_path=str(image_inbox_path) if image_inbox_path else None,
            prompt_len=len(prompt_text),
        )
        await self._handle_normal_message(
            message,
            runtime,
            text_override=prompt_text,
            input_items=input_items,
            record=record,
            placeholder_id=placeholder_id,
        )

    async def _handle_voice_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: Any,
        candidate: TelegramMediaCandidate,
        caption_text: str,
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.voice.received",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
            file_size=candidate.file_size,
            duration=candidate.duration,
            has_caption=bool(caption_text),
        )
        if (
            not self._voice_service
            or not self._voice_config
            or not self._voice_config.enabled
        ):
            await self._send_message(
                message.chat_id,
                "Voice transcription is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        max_bytes = self._config.media.max_voice_bytes
        if candidate.file_size and candidate.file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Voice note too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        pending = PendingVoiceRecord(
            record_id=secrets.token_hex(8),
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
            file_name=candidate.file_name,
            caption=caption_text,
            file_size=candidate.file_size,
            mime_type=candidate.mime_type,
            duration=candidate.duration,
            workspace_path=record.workspace_path,
            created_at=now_iso(),
        )
        await self._store.enqueue_pending_voice(pending)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.voice.queued",
            record_id=pending.record_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
        )
        self._spawn_task(self._voice_manager.attempt(pending.record_id))

    async def _handle_file_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: Any,
        candidate: TelegramMediaCandidate,
        caption_text: str,
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.file.received",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
            file_size=candidate.file_size,
            has_caption=bool(caption_text),
        )
        max_bytes = self._config.media.max_file_bytes
        if candidate.file_size and candidate.file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"File too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            data, file_path, file_size = await self._download_telegram_file(
                candidate.file_id,
                max_bytes=max_bytes,
            )
        except asyncio.CancelledError:
            raise
        except (TelegramAPIError, RuntimeError) as exc:
            detail = _format_telegram_download_error(exc)
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.file.download_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                detail=detail,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _format_download_failure_response("file", detail),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if file_size and file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"File too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if len(data) > max_bytes:
            await self._send_message(
                message.chat_id,
                f"File too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        try:
            file_path_local = self._save_inbox_file(
                record.workspace_path,
                key,
                data,
                candidate=candidate,
                file_path=file_path,
                pma_enabled=bool(getattr(record, "pma_enabled", False)),
            )
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.file.save_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to save file.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        prompt_text = self._format_file_prompt(
            caption_text,
            candidate=candidate,
            saved_path=file_path_local,
            source_path=file_path,
            file_size=file_size or len(data),
            topic_key=key,
            workspace_path=record.workspace_path,
            pma_enabled=bool(getattr(record, "pma_enabled", False)),
        )
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.file.ready",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            path=str(file_path_local),
        )
        await self._handle_normal_message(
            message,
            runtime,
            text_override=prompt_text,
            record=record,
            placeholder_id=placeholder_id,
        )

    async def _handle_media_batch(
        self,
        messages: Sequence[TelegramMessage],
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        context = await self._prepare_media_batch_context(messages)
        if context is None:
            return
        result = await self._process_media_messages(context)
        if not result.saved_image_paths and not result.saved_file_info:
            await self._handle_media_batch_failure(context, result)
            return
        combined_prompt, input_items = self._build_media_prompt(context, result)
        last_message = context.sorted_messages[-1]
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media_batch.ready",
            chat_id=context.first_message.chat_id,
            thread_id=context.first_message.thread_id,
            image_count=len(result.saved_image_paths),
            file_count=len(result.saved_file_info),
            failed_count=result.stats.failed_count,
            reply_to_message_id=last_message.message_id,
        )
        await self._handle_normal_message(
            last_message,
            context.runtime,
            text_override=combined_prompt,
            input_items=input_items,
            record=context.record,
            placeholder_id=placeholder_id,
        )

    async def _prepare_media_batch_context(
        self, messages: Sequence[TelegramMessage]
    ) -> Optional[MediaBatchContext]:
        """Validate the batch and resolve record/runtime context."""
        if not messages:
            return None
        if not self._config.media.enabled:
            first_msg = messages[0]
            await self._send_message(
                first_msg.chat_id,
                "Media handling is disabled.",
                thread_id=first_msg.thread_id,
                reply_to=first_msg.message_id,
            )
            return None
        first_msg = messages[0]
        topic_key = await self._resolve_topic_key(
            first_msg.chat_id, first_msg.thread_id
        )
        record = await self._router.get_topic(topic_key)
        record, pma_error = _record_with_media_workspace(self, record)
        if pma_error:
            await self._send_message(
                first_msg.chat_id,
                pma_error,
                thread_id=first_msg.thread_id,
                reply_to=first_msg.message_id,
            )
            return None
        if record is None or not record.workspace_path:
            await self._send_message(
                first_msg.chat_id,
                self._with_conversation_id(
                    TOPIC_NOT_BOUND_MESSAGE,
                    chat_id=first_msg.chat_id,
                    thread_id=first_msg.thread_id,
                ),
                thread_id=first_msg.thread_id,
                reply_to=first_msg.message_id,
            )
            return None
        runtime = self._router.runtime_for(topic_key)
        sorted_messages = sorted(messages, key=lambda m: m.message_id)
        return MediaBatchContext(
            first_message=first_msg,
            sorted_messages=list(sorted_messages),
            record=record,
            runtime=runtime,
            topic_key=topic_key,
            max_image_bytes=self._config.media.max_image_bytes,
            max_file_bytes=self._config.media.max_file_bytes,
        )

    async def _process_media_messages(
        self, context: MediaBatchContext
    ) -> MediaBatchResult:
        """Process all messages in the media batch and collect results."""
        stats = MediaBatchStats()
        saved_image_paths: list[Path] = []
        saved_image_inbox_info: list[tuple[str, str, int]] = []
        saved_file_info: list[tuple[str, str, int]] = []
        for msg in context.sorted_messages:
            image_candidate = select_image_candidate(msg)
            file_candidate = select_file_candidate(msg)
            if not image_candidate and not file_candidate:
                stats.unsupported += 1
                stats.failed_count += 1
                continue
            skip_remaining = False
            if image_candidate:
                skip_remaining = await self._process_image_candidate(
                    msg,
                    image_candidate,
                    context,
                    stats,
                    saved_image_paths,
                    saved_image_inbox_info,
                )
            if file_candidate and not skip_remaining:
                await self._process_file_candidate(
                    msg,
                    file_candidate,
                    context,
                    stats,
                    saved_file_info,
                )
        return MediaBatchResult(
            saved_image_paths=saved_image_paths,
            saved_image_inbox_info=saved_image_inbox_info,
            saved_file_info=saved_file_info,
            stats=stats,
        )

    async def _process_image_candidate(
        self,
        msg: TelegramMessage,
        candidate: TelegramMediaCandidate,
        context: MediaBatchContext,
        stats: MediaBatchStats,
        saved_image_paths: list[Path],
        saved_image_inbox_info: list[tuple[str, str, int]],
    ) -> bool:
        """Process a single image candidate; returns True to skip further work."""
        if not self._config.media.images:
            await self._send_message(
                msg.chat_id,
                "Image handling is disabled.",
                thread_id=msg.thread_id,
                reply_to=msg.message_id,
            )
            stats.image_disabled += 1
            stats.failed_count += 1
            return True
        try:
            data, file_path, file_size = await self._download_telegram_file(
                candidate.file_id,
                max_bytes=context.max_image_bytes,
            )
        except asyncio.CancelledError:
            raise
        except (TelegramAPIError, RuntimeError) as exc:
            detail = _format_telegram_download_error(exc)
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media_batch.image.download_failed",
                chat_id=msg.chat_id,
                thread_id=msg.thread_id,
                message_id=msg.message_id,
                detail=detail,
                exc=exc,
            )
            if detail and stats.image_download_detail is None:
                stats.image_download_detail = detail
            stats.image_download_failed += 1
            stats.failed_count += 1
            return True
        if file_size and file_size > context.max_image_bytes:
            await self._send_message(
                msg.chat_id,
                f"Image too large (max {context.max_image_bytes} bytes).",
                thread_id=msg.thread_id,
                reply_to=msg.message_id,
            )
            stats.image_too_large += 1
            stats.failed_count += 1
            return True
        if len(data) > context.max_image_bytes:
            await self._send_message(
                msg.chat_id,
                f"Image too large (max {context.max_image_bytes} bytes).",
                thread_id=msg.thread_id,
                reply_to=msg.message_id,
            )
            stats.image_too_large += 1
            stats.failed_count += 1
            return True
        try:
            image_path = self._save_image_file(
                context.record.workspace_path,
                data,
                file_path,
                candidate,
                pma_enabled=bool(getattr(context.record, "pma_enabled", False)),
            )
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media_batch.image.save_failed",
                chat_id=msg.chat_id,
                thread_id=msg.thread_id,
                message_id=msg.message_id,
                exc=exc,
            )
            stats.image_save_failed += 1
            stats.failed_count += 1
            return True
        saved_image_paths.append(image_path)
        try:
            image_inbox_path = self._save_inbox_file(
                context.record.workspace_path,
                context.topic_key,
                data,
                candidate=candidate,
                file_path=file_path,
                pma_enabled=bool(getattr(context.record, "pma_enabled", False)),
            )
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media_batch.image.inbox_save_failed",
                chat_id=msg.chat_id,
                thread_id=msg.thread_id,
                message_id=msg.message_id,
                exc=exc,
            )
        else:
            original_name = (
                candidate.file_name
                or (Path(file_path).name if file_path else None)
                or image_inbox_path.name
            )
            saved_image_inbox_info.append(
                (
                    original_name,
                    str(image_inbox_path),
                    file_size or len(data),
                )
            )
        return False

    async def _process_file_candidate(
        self,
        msg: TelegramMessage,
        candidate: TelegramMediaCandidate,
        context: MediaBatchContext,
        stats: MediaBatchStats,
        saved_file_info: list[tuple[str, str, int]],
    ) -> None:
        """Process a single file candidate within the media batch."""
        if not self._config.media.files:
            await self._send_message(
                msg.chat_id,
                "File handling is disabled.",
                thread_id=msg.thread_id,
                reply_to=msg.message_id,
            )
            stats.file_disabled += 1
            stats.failed_count += 1
            return
        try:
            data, file_path, file_size = await self._download_telegram_file(
                candidate.file_id,
                max_bytes=context.max_file_bytes,
            )
        except asyncio.CancelledError:
            raise
        except (TelegramAPIError, RuntimeError) as exc:
            detail = _format_telegram_download_error(exc)
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media_batch.file.download_failed",
                chat_id=msg.chat_id,
                thread_id=msg.thread_id,
                message_id=msg.message_id,
                detail=detail,
                exc=exc,
            )
            if detail and stats.file_download_detail is None:
                stats.file_download_detail = detail
            stats.file_download_failed += 1
            stats.failed_count += 1
            return
        if file_size is not None and file_size > context.max_file_bytes:
            await self._send_message(
                msg.chat_id,
                f"File too large (max {context.max_file_bytes} bytes).",
                thread_id=msg.thread_id,
                reply_to=msg.message_id,
            )
            stats.file_too_large += 1
            stats.failed_count += 1
            return
        if len(data) > context.max_file_bytes:
            await self._send_message(
                msg.chat_id,
                f"File too large (max {context.max_file_bytes} bytes).",
                thread_id=msg.thread_id,
                reply_to=msg.message_id,
            )
            stats.file_too_large += 1
            stats.failed_count += 1
            return
        try:
            file_path_local = self._save_inbox_file(
                context.record.workspace_path,
                context.topic_key,
                data,
                candidate=candidate,
                file_path=file_path,
                pma_enabled=bool(getattr(context.record, "pma_enabled", False)),
            )
            original_name = (
                candidate.file_name
                or (Path(file_path).name if file_path else None)
                or "unknown"
            )
            saved_file_info.append(
                (
                    original_name,
                    str(file_path_local),
                    file_size or len(data),
                )
            )
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media_batch.file.save_failed",
                chat_id=msg.chat_id,
                thread_id=msg.thread_id,
                message_id=msg.message_id,
                exc=exc,
            )
            stats.file_save_failed += 1
            stats.failed_count += 1

    def _build_media_prompt(
        self, context: MediaBatchContext, result: MediaBatchResult
    ) -> tuple[str, Optional[list[dict[str, Any]]]]:
        """Build the combined prompt text and image input payload."""
        captions = [
            format_forwarded_telegram_message_text(m, m.caption or m.text or "")
            for m in context.sorted_messages
            if (m.caption and m.caption.strip()) or m.forward_origin is not None
        ]
        prompt_parts: list[str] = []
        if captions:
            if len(captions) == 1:
                prompt_parts.append(captions[0].strip())
            else:
                prompt_parts.append("\n".join(f"- {c.strip()}" for c in captions))
        elif result.saved_image_paths:
            prompt_parts.append(self._config.media.image_prompt)
        else:
            prompt_parts.append("Media received.")
        if result.saved_image_inbox_info:
            image_summary = ["\nImages:"]
            for name, path, size in result.saved_image_inbox_info:
                image_summary.append(f"- {name} ({size} bytes) -> {path}")
            prompt_parts.append("\n".join(image_summary))
        if result.saved_file_info:
            file_summary = ["\nFiles:"]
            for name, path, size in result.saved_file_info:
                file_summary.append(f"- {name} ({size} bytes) -> {path}")
            prompt_parts.append("\n".join(file_summary))
        if result.stats.failed_count > 0:
            prompt_parts.append(
                f"\nFailed to process {result.stats.failed_count} item(s)."
            )
        hint = self._build_files_hint(
            workspace_path=context.record.workspace_path,
            topic_key=context.topic_key,
            pma_enabled=bool(getattr(context.record, "pma_enabled", False)),
        )
        prompt_parts.append(hint)
        combined_prompt = "\n\n".join(prompt_parts)
        input_items: Optional[list[dict[str, Any]]] = None
        if result.saved_image_paths:
            input_items = [{"type": "text", "text": combined_prompt}]
            for image_path in result.saved_image_paths:
                input_items.append({"type": "localImage", "path": str(image_path)})
        return combined_prompt, input_items

    async def _handle_media_batch_failure(
        self, context: MediaBatchContext, result: MediaBatchResult
    ) -> None:
        """Log and send a failure response for media batches with no usable items."""
        stats = result.stats
        log_event(
            self._logger,
            logging.WARNING,
            "telegram.media_batch.empty",
            chat_id=context.first_message.chat_id,
            thread_id=context.first_message.thread_id,
            media_group_id=context.first_message.media_group_id,
            message_ids=[m.message_id for m in context.sorted_messages],
            failed_count=stats.failed_count,
            image_disabled=stats.image_disabled,
            file_disabled=stats.file_disabled,
            image_too_large=stats.image_too_large,
            file_too_large=stats.file_too_large,
            image_download_failed=stats.image_download_failed,
            file_download_failed=stats.file_download_failed,
            image_save_failed=stats.image_save_failed,
            file_save_failed=stats.file_save_failed,
            unsupported_count=stats.unsupported,
            max_image_bytes=context.max_image_bytes,
            max_file_bytes=context.max_file_bytes,
        )
        await self._send_message(
            context.first_message.chat_id,
            self._format_media_batch_failure(
                image_disabled=stats.image_disabled,
                file_disabled=stats.file_disabled,
                image_too_large=stats.image_too_large,
                file_too_large=stats.file_too_large,
                image_download_failed=stats.image_download_failed,
                file_download_failed=stats.file_download_failed,
                image_download_detail=stats.image_download_detail,
                file_download_detail=stats.file_download_detail,
                image_save_failed=stats.image_save_failed,
                file_save_failed=stats.file_save_failed,
                unsupported=stats.unsupported,
                max_image_bytes=context.max_image_bytes,
                max_file_bytes=context.max_file_bytes,
            ),
            thread_id=context.first_message.thread_id,
            reply_to=context.first_message.message_id,
        )

    async def _download_telegram_file(
        self, file_id: str, *, max_bytes: Optional[int] = None
    ) -> tuple[bytes, Optional[str], Optional[int]]:
        payload = await self._bot.get_file(file_id)
        file_path = payload.get("file_path") if isinstance(payload, dict) else None
        file_size = payload.get("file_size") if isinstance(payload, dict) else None
        if file_size is not None and not isinstance(file_size, int):
            file_size = None
        if not isinstance(file_path, str) or not file_path:
            raise RuntimeError("Telegram getFile returned no file_path")
        if max_bytes is not None and max_bytes > 0:
            data = await self._bot.download_file(file_path, max_size_bytes=max_bytes)
        else:
            data = await self._bot.download_file(file_path)
        return data, file_path, file_size

    def _image_storage_dir(self, workspace_path: str, *, pma_enabled: bool) -> Path:
        if pma_enabled:
            pma_root = self._pma_root_dir()
            if pma_root is not None:
                return pma_root / "telegram-images"
        return (
            Path(workspace_path) / ".codex-autorunner" / "uploads" / "telegram-images"
        )

    def _choose_image_extension(
        self,
        *,
        file_path: Optional[str],
        file_name: Optional[str],
        mime_type: Optional[str],
    ) -> str:
        for candidate in (file_path, file_name):
            if candidate:
                suffix = Path(candidate).suffix.lower()
                if suffix in IMAGE_EXTS:
                    return suffix
        if mime_type:
            base = mime_type.lower().split(";", 1)[0].strip()
            mapped = IMAGE_CONTENT_TYPES.get(base)
            if mapped:
                return mapped
        return ".img"

    def _save_image_file(
        self,
        workspace_path: str,
        data: bytes,
        file_path: Optional[str],
        candidate: TelegramMediaCandidate,
        *,
        pma_enabled: bool,
    ) -> Path:
        images_dir = self._image_storage_dir(workspace_path, pma_enabled=pma_enabled)
        images_dir.mkdir(parents=True, exist_ok=True)
        ext = self._choose_image_extension(
            file_path=file_path,
            file_name=candidate.file_name,
            mime_type=candidate.mime_type,
        )
        token = secrets.token_hex(6)
        name = f"telegram-{int(time.time())}-{token}{ext}"
        path = images_dir / name
        path.write_bytes(data)
        return path

    def _files_root_dir(self, workspace_path: str) -> Path:
        return Path(workspace_path) / ".codex-autorunner" / "uploads" / "telegram-files"

    def _pma_root_dir(self) -> Optional[Path]:
        hub_root = getattr(self, "_hub_root", None)
        if hub_root is None:
            return None
        return Path(hub_root) / ".codex-autorunner" / "filebox"

    def _pma_inbox_dir(self) -> Optional[Path]:
        hub_root = getattr(self, "_hub_root", None)
        if hub_root is None:
            return None
        return filebox_inbox_dir(Path(hub_root))

    def _pma_outbox_dir(self) -> Optional[Path]:
        hub_root = getattr(self, "_hub_root", None)
        if hub_root is None:
            return None
        return filebox_outbox_dir(Path(hub_root))

    def _sanitize_topic_dir_name(self, key: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("._-")
        if not cleaned:
            cleaned = "topic"
        if len(cleaned) > 80:
            digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
            cleaned = f"{cleaned[:72]}-{digest}"
        return cleaned

    def _files_topic_dir(self, workspace_path: str, topic_key: str) -> Path:
        return self._files_root_dir(workspace_path) / self._sanitize_topic_dir_name(
            topic_key
        )

    def _files_inbox_dir(self, workspace_path: str, topic_key: str) -> Path:
        return self._files_topic_dir(workspace_path, topic_key) / "inbox"

    def _files_outbox_pending_dir(self, workspace_path: str, topic_key: str) -> Path:
        return self._files_topic_dir(workspace_path, topic_key) / "outbox" / "pending"

    def _files_outbox_sent_dir(self, workspace_path: str, topic_key: str) -> Path:
        return self._files_topic_dir(workspace_path, topic_key) / "outbox" / "sent"

    def _sanitize_filename_component(self, value: str, *, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
        return cleaned or fallback

    def _choose_file_extension(
        self,
        *,
        file_name: Optional[str],
        file_path: Optional[str],
        mime_type: Optional[str],
    ) -> str:
        for candidate in (file_name, file_path):
            if candidate:
                suffix = Path(candidate).suffix
                if suffix:
                    return suffix
        if mime_type and mime_type.startswith("text/"):
            return ".txt"
        return ".bin"

    def _choose_file_stem(
        self, file_name: Optional[str], file_path: Optional[str]
    ) -> str:
        for candidate in (file_name, file_path):
            if candidate:
                stem = Path(candidate).stem
                if stem:
                    return stem
        return "file"

    def _save_inbox_file(
        self,
        workspace_path: str,
        topic_key: str,
        data: bytes,
        *,
        candidate: TelegramMediaCandidate,
        file_path: Optional[str],
        pma_enabled: bool,
    ) -> Path:
        inbox_dir = self._files_inbox_dir(workspace_path, topic_key)
        if pma_enabled:
            pma_inbox = self._pma_inbox_dir()
            if pma_inbox is not None:
                inbox_dir = pma_inbox
        inbox_dir.mkdir(parents=True, exist_ok=True)
        stem = self._sanitize_filename_component(
            self._choose_file_stem(candidate.file_name, file_path),
            fallback="file",
        )
        ext = self._choose_file_extension(
            file_name=candidate.file_name,
            file_path=file_path,
            mime_type=candidate.mime_type,
        )
        token = secrets.token_hex(6)
        name = f"{stem}-{token}{ext}"
        path = inbox_dir / name
        path.write_bytes(data)
        return path

    def _format_file_prompt(
        self,
        caption_text: str,
        *,
        candidate: TelegramMediaCandidate,
        saved_path: Path,
        source_path: Optional[str],
        file_size: int,
        topic_key: str,
        workspace_path: str,
        pma_enabled: bool,
    ) -> str:
        header = caption_text.strip() or "File received."
        original_name = (
            candidate.file_name
            or (Path(source_path).name if source_path else None)
            or "unknown"
        )
        hint = self._build_files_hint(
            workspace_path=workspace_path,
            topic_key=topic_key,
            pma_enabled=pma_enabled,
        )
        parts = [
            header,
            "",
            "File details:",
            f"- Name: {original_name}",
            f"- Size: {file_size} bytes",
        ]
        if candidate.mime_type:
            parts.append(f"- Mime: {candidate.mime_type}")
        parts.append(f"- Saved to: {saved_path}")
        parts.append("")
        parts.append(hint)
        return "\n".join(parts)

    def _build_files_hint(
        self,
        *,
        workspace_path: str,
        topic_key: str,
        pma_enabled: bool,
    ) -> str:
        if pma_enabled:
            pma_inbox = self._pma_inbox_dir()
            pma_outbox = self._pma_outbox_dir()
            if pma_inbox is not None and pma_outbox is not None:
                return wrap_injected_context(
                    PMA_FILES_HINT_TEMPLATE.format(
                        inbox=str(pma_inbox),
                        outbox=str(pma_outbox),
                        max_bytes=self._config.media.max_file_bytes,
                    )
                )
        inbox_dir = self._files_inbox_dir(workspace_path, topic_key)
        outbox_dir = self._files_outbox_pending_dir(workspace_path, topic_key)
        topic_dir = self._files_topic_dir(workspace_path, topic_key)
        return wrap_injected_context(
            FILES_HINT_TEMPLATE.format(
                inbox=str(inbox_dir),
                outbox=str(outbox_dir),
                topic_key=topic_key,
                topic_dir=str(topic_dir),
                max_bytes=self._config.media.max_file_bytes,
            )
        )
