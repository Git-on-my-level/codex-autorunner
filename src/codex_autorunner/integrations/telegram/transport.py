from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...core.state import now_iso
from .adapter import TelegramCallbackQuery
from .constants import PLACEHOLDER_TEXT, TELEGRAM_MAX_MESSAGE_LENGTH
from .helpers import _format_turn_metrics, _should_trace_message, _with_conversation_id
from .state import OutboxRecord


class TelegramMessageTransport:
    async def _send_message_with_outbox(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        placeholder_id: Optional[int] = None,
    ) -> bool:
        record = OutboxRecord(
            record_id=secrets.token_hex(8),
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_message_id=reply_to,
            placeholder_message_id=placeholder_id,
            text=text,
            created_at=now_iso(),
        )
        return await self._outbox_manager.send_message_with_outbox(record)

    async def _edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> bool:
        try:
            payload_text, parse_mode = self._prepare_message(text)
            await self._bot.edit_message_text(
                chat_id,
                message_id,
                payload_text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        except Exception:
            return False
        return True

    async def _delete_message(self, chat_id: int, message_id: Optional[int]) -> bool:
        if message_id is None:
            return False
        try:
            return bool(await self._bot.delete_message(chat_id, message_id))
        except Exception:
            return False

    async def _edit_callback_message(
        self,
        callback: TelegramCallbackQuery,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> bool:
        if callback.chat_id is None or callback.message_id is None:
            return False
        return await self._edit_message_text(
            callback.chat_id,
            callback.message_id,
            text,
            reply_markup=reply_markup,
        )

    def _format_voice_transcript_message(self, text: str, agent_status: str) -> str:
        header = "User:\n"
        footer = f"\n\nAgent:\n{agent_status}"
        max_len = TELEGRAM_MAX_MESSAGE_LENGTH
        available = max_len - len(header) - len(footer)
        if available <= 0:
            return f"{header}{footer.lstrip()}"
        transcript = text
        truncation_note = "\n\n...(truncated)"
        if len(transcript) > available:
            remaining = available - len(truncation_note)
            if remaining < 0:
                remaining = 0
            transcript = transcript[:remaining].rstrip()
            transcript = f"{transcript}{truncation_note}"
        return f"{header}{transcript}{footer}"

    async def _send_voice_transcript_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> Optional[int]:
        payload_text, parse_mode = self._prepare_outgoing_text(
            text,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
        )
        response = await self._bot.send_message(
            chat_id,
            payload_text,
            message_thread_id=thread_id,
            reply_to_message_id=reply_to,
            parse_mode=parse_mode,
        )
        message_id = response.get("message_id") if isinstance(response, dict) else None
        return message_id if isinstance(message_id, int) else None

    async def _finalize_voice_transcript(
        self,
        chat_id: int,
        message_id: Optional[int],
        transcript_text: Optional[str],
    ) -> None:
        if message_id is None or transcript_text is None:
            return
        final_message = self._format_voice_transcript_message(
            transcript_text,
            "Reply below.",
        )
        await self._edit_message_text(chat_id, message_id, final_message)

    async def _send_placeholder(
        self,
        chat_id: int,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> Optional[int]:
        try:
            payload_text, parse_mode = self._prepare_outgoing_text(
                PLACEHOLDER_TEXT,
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to=reply_to,
            )
            response = await self._bot.send_message(
                chat_id,
                payload_text,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
                parse_mode=parse_mode,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.placeholder.failed",
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to,
                exc=exc,
            )
            return None
        message_id = response.get("message_id") if isinstance(response, dict) else None
        return message_id if isinstance(message_id, int) else None

    async def _deliver_turn_response(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        placeholder_id: Optional[int],
        response: str,
    ) -> bool:
        return await self._send_message_with_outbox(
            chat_id,
            response,
            thread_id=thread_id,
            reply_to=reply_to,
            placeholder_id=placeholder_id,
        )

    async def _send_turn_metrics(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        elapsed_seconds: Optional[float],
        token_usage: Optional[dict[str, Any]],
    ) -> bool:
        metrics = _format_turn_metrics(token_usage, elapsed_seconds)
        if not metrics:
            return False
        return await self._send_message_with_outbox(
            chat_id,
            metrics,
            thread_id=thread_id,
            reply_to=reply_to,
        )

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> None:
        if _should_trace_message(text):
            text = _with_conversation_id(
                text,
                chat_id=chat_id,
                thread_id=thread_id,
            )
        prefix = self._build_debug_prefix(
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
        )
        if prefix:
            text = f"{prefix}{text}"
        parse_mode = self._config.parse_mode
        if parse_mode:
            rendered, used_mode = self._render_message(text)
            if used_mode and len(rendered) > TELEGRAM_MAX_MESSAGE_LENGTH:
                extension = "txt"
                if used_mode in ("Markdown", "MarkdownV2"):
                    extension = "md"
                elif used_mode == "HTML":
                    extension = "html"
                await self._send_document(
                    chat_id,
                    text.encode("utf-8"),
                    filename=f"response.{extension}",
                    thread_id=thread_id,
                    reply_to=reply_to,
                    caption="Response too long; see attached.",
                )
                return
            payload_text = rendered if used_mode else text
            await self._bot.send_message_chunks(
                chat_id,
                payload_text,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
                reply_markup=reply_markup,
                parse_mode=used_mode,
            )
            return
        payload_text, parse_mode = self._prepare_outgoing_text(
            text,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
        )
        await self._bot.send_message_chunks(
            chat_id,
            payload_text,
            message_thread_id=thread_id,
            reply_to_message_id=reply_to,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    async def _send_document(
        self,
        chat_id: int,
        data: bytes,
        *,
        filename: str,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        caption: Optional[str] = None,
    ) -> None:
        try:
            await self._bot.send_document(
                chat_id,
                data,
                filename=filename,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
                caption=caption,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.send_document.failed",
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to,
                exc=exc,
            )

    async def _answer_callback(
        self, callback: Optional[TelegramCallbackQuery], text: str
    ) -> None:
        if callback is None:
            return
        try:
            await self._bot.answer_callback_query(callback.callback_id, text=text)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.answer_callback.failed",
                chat_id=callback.chat_id,
                thread_id=callback.thread_id,
                callback_id=callback.callback_id,
                exc=exc,
            )
