from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .....core.logging_utils import log_event
from ...adapter import TelegramMessage
from ...constants import PLACEHOLDER_TEXT
from ...state import PendingVoiceRecord

if TYPE_CHECKING:
    pass

from .shared import SharedHelpers


class VoiceCommands(SharedHelpers):
    async def _send_voice_progress_message(
        self, record: PendingVoiceRecord, text: str
    ) -> Optional[int]:
        payload_text, parse_mode = self._prepare_outgoing_text(
            text,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            reply_to=record.message_id,
            workspace_path=record.workspace_path,
        )
        try:
            response = await self._bot.send_message(
                record.chat_id,
                payload_text,
                message_thread_id=record.thread_id,
                reply_to_message_id=record.message_id,
                parse_mode=parse_mode,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.voice.progress_failed",
                record_id=record.record_id,
                chat_id=record.chat_id,
                thread_id=record.thread_id,
                exc=exc,
            )
            return None
        message_id = response.get("message_id") if isinstance(response, dict) else None
        return message_id if isinstance(message_id, int) else None

    async def _update_voice_progress_message(
        self, record: PendingVoiceRecord, text: str
    ) -> None:
        if record.progress_message_id is None:
            return
        await self._edit_message_text(
            record.chat_id,
            record.progress_message_id,
            text,
        )

    async def _deliver_voice_transcript(
        self,
        record: PendingVoiceRecord,
        transcript_text: str,
    ) -> None:
        if record.transcript_message_id is None:
            transcript_message = self._format_voice_transcript_message(
                transcript_text,
                PLACEHOLDER_TEXT,
            )
            record.transcript_message_id = await self._send_voice_transcript_message(
                record.chat_id,
                transcript_message,
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
            await self._store.update_pending_voice(record)
        if record.transcript_message_id is None:
            raise RuntimeError("Failed to send voice transcript message")
        await self._update_voice_progress_message(record, "Voice note transcribed.")
        message = TelegramMessage(
            update_id=0,
            message_id=record.message_id,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            from_user_id=None,
            text=None,
            date=None,
            is_topic_message=record.thread_id is not None,
        )
        key = await self._resolve_topic_key(record.chat_id, record.thread_id)
        runtime = self._router.runtime_for(key)
        if self._config.concurrency.per_topic_queue:
            await runtime.queue.enqueue(
                lambda: self._handle_normal_message(
                    message,
                    runtime,
                    text_override=transcript_text,
                    send_placeholder=True,
                    transcript_message_id=record.transcript_message_id,
                    transcript_text=transcript_text,
                )
            )
        else:
            await self._handle_normal_message(
                message,
                runtime,
                text_override=transcript_text,
                send_placeholder=True,
                transcript_message_id=record.transcript_message_id,
                transcript_text=transcript_text,
            )
