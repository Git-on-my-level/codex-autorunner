from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ...core.logging_utils import log_event
from ...core.state import now_iso
from ...voice import VoiceConfig, VoiceService, VoiceServiceError
from .config import TelegramBotConfig
from .constants import (
    VOICE_MAX_ATTEMPTS,
    VOICE_RETRY_AFTER_BUFFER_SECONDS,
    VOICE_RETRY_INITIAL_SECONDS,
    VOICE_RETRY_INTERVAL_SECONDS,
    VOICE_RETRY_JITTER_RATIO,
    VOICE_RETRY_MAX_SECONDS,
)
from .helpers import _format_future_time, _parse_iso_timestamp
from .retry import _extract_retry_after_seconds
from .state import PendingVoiceRecord, TelegramStateStore

SendMessageFn = Callable[..., Awaitable[None]]
EditMessageFn = Callable[..., Awaitable[bool]]
SendProgressMessageFn = Callable[[PendingVoiceRecord, str], Awaitable[Optional[int]]]
DeliverTranscriptFn = Callable[[PendingVoiceRecord, str], Awaitable[None]]
DownloadFileFn = Callable[[str], Awaitable[tuple[bytes, Optional[str], Optional[int]]]]


class TelegramVoiceManager:
    def __init__(
        self,
        config: TelegramBotConfig,
        store: TelegramStateStore,
        *,
        voice_config: Optional[VoiceConfig],
        voice_service: Optional[VoiceService],
        send_message: SendMessageFn,
        edit_message_text: EditMessageFn,
        send_progress_message: SendProgressMessageFn,
        deliver_transcript: DeliverTranscriptFn,
        download_file: DownloadFileFn,
        logger: logging.Logger,
    ) -> None:
        self._config = config
        self._store = store
        self._voice_config = voice_config
        self._voice_service = voice_service
        self._send_message = send_message
        self._edit_message_text = edit_message_text
        self._send_progress_message = send_progress_message
        self._deliver_transcript = deliver_transcript
        self._download_file = download_file
        self._logger = logger
        self._inflight: set[str] = set()
        self._lock: Optional[asyncio.Lock] = None

    def start(self) -> None:
        self._inflight = set()
        self._lock = asyncio.Lock()

    async def restore(self) -> None:
        records = self._store.list_pending_voice()
        if not records:
            return
        log_event(
            self._logger,
            logging.INFO,
            "telegram.voice.restore",
            count=len(records),
        )
        await self._flush(records)

    async def run_loop(self) -> None:
        while True:
            await asyncio.sleep(VOICE_RETRY_INTERVAL_SECONDS)
            try:
                records = self._store.list_pending_voice()
                if records:
                    await self._flush(records)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.voice.flush_failed",
                    exc=exc,
                )

    async def attempt(self, record_id: str) -> bool:
        record = self._store.get_pending_voice(record_id)
        if record is None:
            return False
        if not self._ready_for_attempt(record):
            return False
        if not await self._mark_inflight(record.record_id):
            return False
        inflight_id = record.record_id
        try:
            current_record = self._store.get_pending_voice(record.record_id)
            if current_record is None:
                return False
            if not self._ready_for_attempt(current_record):
                return False
            done = await self._process(current_record)
        except Exception as exc:
            retry_after = _extract_retry_after_seconds(exc)
            await self._record_failure(record, exc, retry_after=retry_after)
            return False
        finally:
            await self._clear_inflight(inflight_id)
        if done:
            self._store.delete_pending_voice(record.record_id)
        return done

    async def _flush(self, records: list[PendingVoiceRecord]) -> None:
        for record in records:
            if record.attempts >= VOICE_MAX_ATTEMPTS:
                await self._give_up(
                    record,
                    "Voice transcription failed after retries. Please resend.",
                )
                continue
            await self.attempt(record.record_id)

    async def _process(self, record: PendingVoiceRecord) -> bool:
        if (
            not self._voice_service
            or not self._voice_config
            or not self._voice_config.enabled
        ):
            await self._send_message(
                record.chat_id,
                "Voice transcription is disabled.",
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
            self._remove_voice_file(record)
            return True
        max_bytes = self._config.media.max_voice_bytes
        if record.file_size and record.file_size > max_bytes:
            await self._send_message(
                record.chat_id,
                f"Voice note too large (max {max_bytes} bytes).",
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
            self._remove_voice_file(record)
            return True
        if record.transcript_text:
            await self._deliver_transcript(record, record.transcript_text)
            self._remove_voice_file(record)
            return True
        path = self._resolve_voice_download_path(record)
        if path is None:
            data, file_path, file_size = await self._download_file(record.file_id)
            if file_size and file_size > max_bytes:
                await self._send_message(
                    record.chat_id,
                    f"Voice note too large (max {max_bytes} bytes).",
                    thread_id=record.thread_id,
                    reply_to=record.message_id,
                )
                return True
            if len(data) > max_bytes:
                await self._send_message(
                    record.chat_id,
                    f"Voice note too large (max {max_bytes} bytes).",
                    thread_id=record.thread_id,
                    reply_to=record.message_id,
                )
                return True
            path = self._persist_voice_payload(record, data, file_path=file_path)
            record.download_path = str(path)
            if file_size is not None:
                record.file_size = file_size
            else:
                record.file_size = len(data)
            self._store.update_pending_voice(record)
        data = path.read_bytes()
        try:
            result = await self._voice_service.transcribe_async(
                data,
                client="telegram",
                filename=record.file_name or path.name,
                content_type=record.mime_type,
            )
        except VoiceServiceError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.voice.transcribe_failed",
                chat_id=record.chat_id,
                thread_id=record.thread_id,
                message_id=record.message_id,
                reason=exc.reason,
            )
            await self._send_message(
                record.chat_id,
                exc.detail,
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
            self._remove_voice_file(record)
            return True
        transcript = ""
        if isinstance(result, dict):
            transcript = str(result.get("text") or "")
        transcript = transcript.strip()
        if not transcript:
            await self._send_message(
                record.chat_id,
                "Voice note transcribed to empty text.",
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
            self._remove_voice_file(record)
            return True
        combined = record.caption.strip()
        if combined:
            combined = f"{combined}\n\n{transcript}"
        else:
            combined = transcript
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.voice.transcribed",
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            message_id=record.message_id,
            text_len=len(transcript),
        )
        record.transcript_text = combined
        self._store.update_pending_voice(record)
        await self._deliver_transcript(record, combined)
        self._remove_voice_file(record)
        return True

    async def _record_failure(
        self,
        record: PendingVoiceRecord,
        exc: Exception,
        *,
        retry_after: Optional[int],
    ) -> None:
        record.attempts += 1
        record.last_error = str(exc)[:500]
        record.last_attempt_at = now_iso()
        delay = self._retry_delay(record.attempts, retry_after=retry_after)
        record.next_attempt_at = _format_future_time(delay)
        self._store.update_pending_voice(record)
        log_event(
            self._logger,
            logging.WARNING,
            "telegram.voice.retry",
            record_id=record.record_id,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            attempts=record.attempts,
            retry_after=retry_after,
            next_attempt_at=record.next_attempt_at,
            exc=exc,
        )
        if record.attempts == 1 and record.progress_message_id is None:
            progress_id = await self._send_progress_message(
                record,
                "Queued voice note, retrying download...",
            )
            if progress_id is not None:
                record.progress_message_id = progress_id
                self._store.update_pending_voice(record)
        if record.attempts >= VOICE_MAX_ATTEMPTS:
            await self._give_up(
                record,
                "Voice transcription failed after retries. Please resend.",
            )

    async def _give_up(self, record: PendingVoiceRecord, message: str) -> None:
        if record.progress_message_id is not None:
            await self._edit_message_text(
                record.chat_id,
                record.progress_message_id,
                message,
            )
        else:
            await self._send_message(
                record.chat_id,
                message,
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
        self._remove_voice_file(record)
        self._store.delete_pending_voice(record.record_id)
        log_event(
            self._logger,
            logging.WARNING,
            "telegram.voice.gave_up",
            record_id=record.record_id,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            attempts=record.attempts,
        )

    async def _mark_inflight(self, record_id: str) -> bool:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if record_id in self._inflight:
                return False
            self._inflight.add(record_id)
            return True

    async def _clear_inflight(self, record_id: str) -> None:
        if self._lock is None:
            return
        async with self._lock:
            self._inflight.discard(record_id)

    def _ready_for_attempt(self, record: PendingVoiceRecord) -> bool:
        next_attempt = _parse_iso_timestamp(record.next_attempt_at)
        if next_attempt is None:
            return True
        return datetime.now(timezone.utc) >= next_attempt

    def _retry_delay(self, attempts: int, *, retry_after: Optional[int]) -> float:
        if retry_after is not None and retry_after > 0:
            return float(retry_after) + VOICE_RETRY_AFTER_BUFFER_SECONDS
        delay: float = VOICE_RETRY_INITIAL_SECONDS * (2 ** max(attempts - 1, 0))
        delay = float(min(delay, VOICE_RETRY_MAX_SECONDS))
        jitter = delay * VOICE_RETRY_JITTER_RATIO
        if jitter:
            delay += random.uniform(0, jitter)
        return delay

    def _resolve_voice_download_path(
        self, record: PendingVoiceRecord
    ) -> Optional[Path]:
        if not record.download_path:
            return None
        path = Path(record.download_path)
        if path.exists():
            return path
        record.download_path = None
        self._store.update_pending_voice(record)
        return None

    def _persist_voice_payload(
        self,
        record: PendingVoiceRecord,
        data: bytes,
        *,
        file_path: Optional[str],
    ) -> Path:
        workspace_path = record.workspace_path or str(self._config.root)
        storage_dir = self._voice_storage_dir(workspace_path)
        storage_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(6)
        ext = self._choose_voice_extension(
            record.file_name,
            record.mime_type,
            file_path=file_path,
        )
        name = f"telegram-voice-{int(time.time())}-{token}{ext}"
        path = storage_dir / name
        path.write_bytes(data)
        return path

    def _voice_storage_dir(self, workspace_path: str) -> Path:
        return Path(workspace_path) / ".codex-autorunner" / "uploads" / "telegram-voice"

    def _choose_voice_extension(
        self,
        file_name: Optional[str],
        mime_type: Optional[str],
        *,
        file_path: Optional[str],
    ) -> str:
        for candidate in (file_name, file_path):
            if candidate:
                suffix = Path(candidate).suffix
                if suffix:
                    return suffix
        if mime_type == "audio/ogg":
            return ".ogg"
        if mime_type == "audio/opus":
            return ".opus"
        if mime_type == "audio/mpeg":
            return ".mp3"
        if mime_type == "audio/wav":
            return ".wav"
        return ".dat"

    def _remove_voice_file(self, record: PendingVoiceRecord) -> None:
        if not record.download_path:
            return
        path = Path(record.download_path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.voice.cleanup_failed",
                record_id=record.record_id,
                path=str(path),
            )
