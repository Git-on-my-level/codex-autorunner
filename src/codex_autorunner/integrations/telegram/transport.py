from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...core.state import now_iso
from .adapter import TelegramCallbackQuery
from .constants import PLACEHOLDER_TEXT, TELEGRAM_MAX_MESSAGE_LENGTH
from .helpers import _format_turn_metrics, _should_trace_message, _with_conversation_id
from .overflow import split_markdown_message, trim_markdown_message
from .rendering import _format_telegram_html, _format_telegram_markdown
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
        message_thread_id: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> bool:
        try:
            payload_text, parse_mode = self._prepare_message(text)
            if len(payload_text) > TELEGRAM_MAX_MESSAGE_LENGTH:
                trimmed = trim_markdown_message(
                    payload_text,
                    max_len=TELEGRAM_MAX_MESSAGE_LENGTH,
                    render=(
                        _format_telegram_html
                        if parse_mode == "HTML"
                        else (
                            lambda v: (
                                _format_telegram_markdown(v, parse_mode)
                                if parse_mode in ("Markdown", "MarkdownV2")
                                else v
                            )
                        )
                    ),
                )
                payload_text = trimmed
            await self._bot.edit_message_text(
                chat_id,
                message_id,
                payload_text,
                message_thread_id=message_thread_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        except Exception:
            return False
        return True

    async def _delete_message(
        self, chat_id: int, message_id: Optional[int], thread_id: Optional[int] = None
    ) -> bool:
        if message_id is None:
            return False
        try:
            return bool(
                await self._bot.delete_message(
                    chat_id, message_id, message_thread_id=thread_id
                )
            )
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
            message_thread_id=callback.thread_id,
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
        text: str = PLACEHOLDER_TEXT,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> Optional[int]:
        try:
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
                reply_markup=reply_markup,
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

    def _format_turn_metrics_text(
        self,
        token_usage: Optional[dict[str, Any]],
        elapsed_seconds: Optional[float],
    ) -> Optional[str]:
        return _format_turn_metrics(token_usage, elapsed_seconds)

    def _metrics_mode(self) -> str:
        return getattr(self._config, "metrics_mode", "separate")

    async def _send_turn_metrics(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        elapsed_seconds: Optional[float],
        token_usage: Optional[dict[str, Any]],
    ) -> bool:
        metrics = self._format_turn_metrics_text(token_usage, elapsed_seconds)
        if not metrics:
            return False
        return await self._send_message_with_outbox(
            chat_id,
            metrics,
            thread_id=thread_id,
            reply_to=reply_to,
        )

    async def _append_metrics_to_placeholder(
        self,
        chat_id: int,
        message_id: Optional[int],
        metrics: str,
    ) -> bool:
        if message_id is None:
            return False
        updated = f"{PLACEHOLDER_TEXT}\n\n{metrics}"
        edited = await self._edit_message_text(chat_id, message_id, updated)
        return edited

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
                overflow_mode = getattr(self._config, "message_overflow", "document")
                if overflow_mode == "split" and used_mode in (
                    "HTML",
                    "Markdown",
                    "MarkdownV2",
                ):
                    if used_mode == "HTML":
                        split_renderer = _format_telegram_html
                    else:

                        def _render_markdown(value: str, mode: str = used_mode) -> str:
                            return _format_telegram_markdown(value, mode)

                        split_renderer = _render_markdown
                    chunks = split_markdown_message(
                        text,
                        max_len=TELEGRAM_MAX_MESSAGE_LENGTH,
                        render=split_renderer,
                    )
                    for idx, chunk in enumerate(chunks):
                        await self._bot.send_message(
                            chat_id,
                            chunk,
                            message_thread_id=thread_id,
                            reply_to_message_id=reply_to if idx == 0 else None,
                            reply_markup=reply_markup if idx == 0 else None,
                            parse_mode=used_mode,
                        )
                    return
                if overflow_mode == "trim":
                    if used_mode == "HTML":
                        renderer = _format_telegram_html
                    elif used_mode in ("Markdown", "MarkdownV2"):

                        def _render_markdown(value: str, mode: str = used_mode) -> str:
                            return _format_telegram_markdown(value, mode)

                        renderer = _render_markdown
                    else:

                        def _render_text(value: str) -> str:
                            return value

                        renderer = _render_text
                    trimmed = trim_markdown_message(
                        text,
                        max_len=TELEGRAM_MAX_MESSAGE_LENGTH,
                        render=renderer,
                    )
                    await self._bot.send_message_chunks(
                        chat_id,
                        trimmed,
                        message_thread_id=thread_id,
                        reply_to_message_id=reply_to,
                        reply_markup=reply_markup,
                        parse_mode=used_mode,
                    )
                    return
                await self._send_document(
                    chat_id,
                    text.encode("utf-8"),
                    filename="response.md",
                    thread_id=thread_id,
                    reply_to=reply_to,
                    caption="Response too long; attached as response.md.",
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
    ) -> bool:
        try:
            await self._bot.send_document(
                chat_id,
                data,
                filename=filename,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
                caption=caption,
            )
            return True
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
            return False

    async def _answer_callback(
        self, callback: Optional[TelegramCallbackQuery], text: str
    ) -> None:
        if callback is None:
            return
        try:
            await self._bot.answer_callback_query(
                callback.callback_id,
                chat_id=callback.chat_id,
                thread_id=callback.thread_id,
                message_id=callback.message_id,
                text=text,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.answer_callback.failed",
                chat_id=callback.chat_id,
                thread_id=callback.thread_id,
                message_id=callback.message_id,
                callback_id=callback.callback_id,
                exc=exc,
            )
