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

from .....core.injected_context import wrap_injected_context
from .....core.logging_utils import log_event
from .....core.state import now_iso
from ...adapter import TelegramMessage
from ...config import TelegramMediaCandidate
from ...helpers import _path_within, format_public_error
from ...state import PendingVoiceRecord, TelegramTopicRecord
from .. import messages as message_handlers
from .shared import SharedHelpers

FILES_HINT_TEMPLATE = (
    "Inbox: {inbox}\n"
    "Outbox (pending): {outbox}\n"
    "Topic key: {topic_key}\n"
    "Topic dir: {topic_dir}\n"
    "Place files in outbox pending to send after this turn finishes.\n"
    "Check delivery with /files outbox.\n"
    "Max file size: {max_bytes} bytes."
)
PMA_FILES_HINT_TEMPLATE = (
    "PMA inbox: {inbox}\n"
    "PMA outbox (pending): {outbox}\n"
    "Place files in outbox pending to send after this turn finishes.\n"
    "Check delivery with /files outbox.\n"
    "Max file size: {max_bytes} bytes."
)


_GENERIC_TELEGRAM_ERRORS = {
    "Telegram request failed",
    "Telegram file download failed",
    "Telegram API returned error",
}


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def _sanitize_error_detail(detail: str, *, limit: int = 200) -> str:
    return format_public_error(detail, limit=limit)


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
    saved_file_info: list[tuple[str, str, int]]
    stats: MediaBatchStats


class FilesCommands(SharedHelpers):
    def _files_usage(self, *, pma: bool) -> str:
        header = "Usage:"
        lines = [
            header,
            "/files",
            "/files inbox",
            "/files outbox",
            "/files all",
            "/files send <filename>",
            "/files clear inbox|outbox|all",
        ]
        if pma:
            lines.append("Note: PMA files live in .codex-autorunner/pma/inbox|outbox.")
        return "\n".join(lines)

    async def _send_pma_outbox_file(
        self,
        path: Path,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> bool:
        try:
            data = path.read_bytes()
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.pma_outbox.read_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        try:
            await self._bot.send_document(
                chat_id,
                data,
                filename=path.name,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.pma_outbox.send_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        return True

    def _format_telegram_download_error(self, exc: Exception) -> Optional[str]:
        for current in _iter_exception_chain(exc):
            if isinstance(current, Exception):
                detail = self._format_httpx_exception(current)
                if detail:
                    return format_public_error(detail)
                message = str(current).strip()
                if message and message not in _GENERIC_TELEGRAM_ERRORS:
                    return format_public_error(message)
        return None

    def _format_download_failure_response(
        self, kind: str, detail: Optional[str]
    ) -> str:
        base = f"Failed to download {kind}."
        if detail:
            return f"{base} Reason: {format_public_error(detail)}"
        return base

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
        except Exception as exc:
            detail = self._format_telegram_download_error(exc)
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
                self._format_download_failure_response("image", detail),
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
        except Exception as exc:
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
        prompt_text = caption_text.strip()
        if not prompt_text:
            prompt_text = self._config.media.image_prompt
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
        except Exception as exc:
            detail = self._format_telegram_download_error(exc)
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
                self._format_download_failure_response("file", detail),
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
        except Exception as exc:
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
        record, pma_error = message_handlers._record_with_media_workspace(self, record)
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
                    "Topic not bound. Use /bind <repo_id> or /bind <path>.",
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
        saved_file_info: list[tuple[str, str, int]] = []
        for msg in context.sorted_messages:
            image_candidate = message_handlers.select_image_candidate(msg)
            file_candidate = message_handlers.select_file_candidate(msg)
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
        except Exception as exc:
            detail = self._format_telegram_download_error(exc)
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
            saved_image_paths.append(image_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
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
        except Exception as exc:
            detail = self._format_telegram_download_error(exc)
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
        except Exception as exc:
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
            m.caption or ""
            for m in context.sorted_messages
            if m.caption and m.caption.strip()
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
            pma_inbox = self._pma_inbox_dir()
            if pma_inbox is not None:
                return pma_inbox
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
                if suffix in message_handlers.IMAGE_EXTS:
                    return suffix
        if mime_type:
            base = mime_type.lower().split(";", 1)[0].strip()
            mapped = message_handlers.IMAGE_CONTENT_TYPES.get(base)
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
        return Path(hub_root) / ".codex-autorunner" / "pma"

    def _pma_inbox_dir(self) -> Optional[Path]:
        pma_root = self._pma_root_dir()
        if pma_root is None:
            return None
        return pma_root / "inbox"

    def _pma_outbox_dir(self) -> Optional[Path]:
        pma_root = self._pma_root_dir()
        if pma_root is None:
            return None
        return pma_root / "outbox"

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

    def _format_bytes(self, size: int) -> str:
        if size < 1024:
            return f"{size} B"
        value = size / 1024
        for unit in ("KB", "MB", "GB", "TB"):
            if value < 1024:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} PB"

    def _list_files(self, folder: Path) -> list[Path]:
        if not folder.exists():
            return []
        files: list[Path] = []
        for path in folder.iterdir():
            try:
                if path.is_file():
                    files.append(path)
            except OSError:
                continue

        def _mtime(entry: Path) -> float:
            try:
                return entry.stat().st_mtime
            except OSError:
                return 0.0

        return sorted(files, key=_mtime, reverse=True)

    async def _send_outbox_file(
        self,
        path: Path,
        *,
        sent_dir: Path,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> bool:
        try:
            data = path.read_bytes()
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.outbox.read_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        try:
            await self._bot.send_document(
                chat_id,
                data,
                filename=path.name,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.outbox.send_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        try:
            sent_dir.mkdir(parents=True, exist_ok=True)
            destination = sent_dir / path.name
            if destination.exists():
                token = secrets.token_hex(3)
                destination = sent_dir / f"{path.stem}-{token}{path.suffix}"
            path.replace(destination)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.outbox.move_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        log_event(
            self._logger,
            logging.INFO,
            "telegram.files.outbox.sent",
            chat_id=chat_id,
            thread_id=thread_id,
            path=str(path),
        )
        return True

    async def _flush_outbox_files(
        self,
        record: Optional["TelegramTopicRecord"],
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        topic_key: Optional[str] = None,
    ) -> None:
        if (
            record is None
            or not record.workspace_path
            or not self._config.media.enabled
            or not self._config.media.files
        ):
            return
        if topic_key:
            key = topic_key
        else:
            key = await self._resolve_topic_key(chat_id, thread_id)
        pma_enabled = bool(getattr(record, "pma_enabled", False))
        pending_dir = self._files_outbox_pending_dir(record.workspace_path, key)
        if pma_enabled:
            pma_outbox = self._pma_outbox_dir()
            if pma_outbox is not None:
                pending_dir = pma_outbox
        if not pending_dir.exists():
            return
        files = self._list_files(pending_dir)
        if not files:
            return
        sent_dir = self._files_outbox_sent_dir(record.workspace_path, key)
        max_bytes = self._config.media.max_file_bytes
        for path in files:
            if not _path_within(pending_dir, path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > max_bytes:
                await self._send_message(
                    chat_id,
                    f"Outbox file too large: {path.name} (max {max_bytes} bytes).",
                    thread_id=thread_id,
                    reply_to=reply_to,
                )
                continue
            if pma_enabled:
                success = await self._send_pma_outbox_file(
                    path,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to=reply_to,
                )
                if success:
                    try:
                        path.unlink()
                    except OSError as exc:
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "telegram.files.pma_outbox.delete_failed",
                            chat_id=chat_id,
                            thread_id=thread_id,
                            path=str(path),
                            exc=exc,
                        )
            else:
                await self._send_outbox_file(
                    path,
                    sent_dir=sent_dir,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to=reply_to,
                )

    def _format_file_listing(self, title: str, files: list[Path]) -> str:
        if not files:
            return f"{title}: (empty)"
        lines = [f"{title} ({len(files)}):"]
        for path in files[:50]:
            try:
                stats = path.stat()
            except OSError:
                continue
            from datetime import datetime

            mtime = datetime.fromtimestamp(stats.st_mtime).isoformat(timespec="seconds")
            lines.append(
                f"- {path.name} ({self._format_bytes(stats.st_size)}, {mtime})"
            )
        if len(files) > 50:
            lines.append(f"... and {len(files) - 50} more")
        return "\n".join(lines)

    def _delete_files_in_dir(self, folder: Path) -> int:
        if not folder.exists():
            return 0
        deleted = 0
        for path in folder.iterdir():
            try:
                if path.is_file():
                    path.unlink()
                    deleted += 1
            except OSError:
                continue
        return deleted

    async def _handle_files(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        if not self._config.media.enabled or not self._config.media.files:
            await self._send_message(
                message.chat_id,
                "File handling is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.ensure_topic(message.chat_id, message.thread_id)
        record, pma_error = message_handlers._record_with_media_workspace(self, record)
        if pma_error:
            await self._send_message(
                message.chat_id,
                pma_error,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                self._with_conversation_id(
                    "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        pma_enabled = bool(getattr(record, "pma_enabled", False))
        if pma_enabled:
            hub_root = getattr(self, "_hub_root", None)
            if hub_root is None:
                await self._send_message(
                    message.chat_id,
                    "PMA unavailable; hub root not configured.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            pma_root = Path(hub_root) / ".codex-autorunner" / "pma"
            inbox_dir = pma_root / "inbox"
            outbox_dir = pma_root / "outbox"
            pending_dir = outbox_dir
            sent_dir = outbox_dir
        else:
            inbox_dir = self._files_inbox_dir(record.workspace_path, key)
            pending_dir = self._files_outbox_pending_dir(record.workspace_path, key)
            sent_dir = self._files_outbox_sent_dir(record.workspace_path, key)
        argv = self._parse_command_args(args)
        if not argv:
            inbox_items = self._list_files(inbox_dir)
            pending_items = self._list_files(pending_dir)
            sent_items = [] if pma_enabled else self._list_files(sent_dir)
            usage = self._files_usage(pma=pma_enabled)
            if pma_enabled:
                text = "\n".join(
                    [
                        f"Inbox: {len(inbox_items)} file(s)",
                        f"Outbox: {len(pending_items)} file(s)",
                        usage,
                    ]
                )
            else:
                text = "\n".join(
                    [
                        f"Inbox: {len(inbox_items)} item(s)",
                        f"Outbox pending: {len(pending_items)} item(s)",
                        f"Outbox sent: {len(sent_items)} item(s)",
                        usage,
                    ]
                )
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        subcommand = argv[0].lower()
        if subcommand == "inbox":
            files = self._list_files(inbox_dir)
            text = self._format_file_listing("Inbox", files)
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if subcommand == "outbox":
            pending_items = self._list_files(pending_dir)
            if pma_enabled:
                text = self._format_file_listing("Outbox", pending_items)
            else:
                sent_items = self._list_files(sent_dir)
                text = "\n".join(
                    [
                        self._format_file_listing("Outbox pending", pending_items),
                        "",
                        self._format_file_listing("Outbox sent", sent_items),
                    ]
                )
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if subcommand == "all":
            inbox_items = self._list_files(inbox_dir)
            pending_items = self._list_files(pending_dir)
            if pma_enabled:
                text = "\n".join(
                    [
                        self._format_file_listing("Inbox", inbox_items),
                        "",
                        self._format_file_listing("Outbox", pending_items),
                    ]
                )
            else:
                sent_items = self._list_files(sent_dir)
                text = "\n".join(
                    [
                        self._format_file_listing("Inbox", inbox_items),
                        "",
                        self._format_file_listing("Outbox pending", pending_items),
                        "",
                        self._format_file_listing("Outbox sent", sent_items),
                    ]
                )
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if subcommand == "clear":
            if len(argv) < 2:
                await self._send_message(
                    message.chat_id,
                    self._files_usage(pma=pma_enabled),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            target = argv[1].lower()
            deleted = 0
            if target == "inbox":
                deleted = self._delete_files_in_dir(inbox_dir)
            elif target == "outbox":
                deleted = self._delete_files_in_dir(pending_dir)
                if not pma_enabled:
                    deleted += self._delete_files_in_dir(sent_dir)
            elif target == "all":
                deleted = self._delete_files_in_dir(inbox_dir)
                deleted += self._delete_files_in_dir(pending_dir)
                if not pma_enabled:
                    deleted += self._delete_files_in_dir(sent_dir)
            else:
                await self._send_message(
                    message.chat_id,
                    self._files_usage(pma=pma_enabled),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                f"Deleted {deleted} file(s).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if subcommand == "send":
            if len(argv) < 2:
                await self._send_message(
                    message.chat_id,
                    self._files_usage(pma=pma_enabled),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            name = Path(argv[1]).name
            candidate = pending_dir / name
            if not _path_within(pending_dir, candidate) or not candidate.is_file():
                await self._send_message(
                    message.chat_id,
                    f"Outbox file not found: {name}",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            size = candidate.stat().st_size
            max_bytes = self._config.media.max_file_bytes
            if size > max_bytes:
                await self._send_message(
                    message.chat_id,
                    f"Outbox file too large: {name} (max {max_bytes} bytes).",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if pma_enabled:
                success = await self._send_pma_outbox_file(
                    candidate,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
            else:
                success = await self._send_outbox_file(
                    candidate,
                    sent_dir=sent_dir,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
            result = "Sent." if success else "Failed to send."
            await self._send_message(
                message.chat_id,
                result,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            self._files_usage(pma=pma_enabled),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
