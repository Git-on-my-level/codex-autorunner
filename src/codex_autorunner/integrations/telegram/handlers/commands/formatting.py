import logging
from typing import Any, Optional

from .....core.logging_utils import log_event
from ...adapter import TelegramMessage
from ...constants import TELEGRAM_MAX_MESSAGE_LENGTH
from ...helpers import _compact_preview


class FormattingHelpers:
    def _prepare_compact_summary_delivery(
        self, summary_text: str
    ) -> tuple[str, Optional[bytes]]:
        summary_text = summary_text.strip() or "(no summary)"
        if len(summary_text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return summary_text, None
        header = "Summary preview:\n"
        footer = "\n\nFull summary attached as compact-summary.txt"
        preview_limit = TELEGRAM_MAX_MESSAGE_LENGTH - len(header) - len(footer)
        if preview_limit < 20:
            preview_limit = 20
        preview = _compact_preview(summary_text, limit=preview_limit)
        display_text = f"{header}{preview}{footer}"
        if len(display_text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            display_text = display_text[: TELEGRAM_MAX_MESSAGE_LENGTH - 3] + "..."
        return display_text, summary_text.encode("utf-8")

    async def _send_compact_summary_message(
        self,
        message: "TelegramMessage",
        summary_text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[int], str]:
        display_text, attachment = self._prepare_compact_summary_delivery(summary_text)
        payload_text, parse_mode = self._prepare_outgoing_text(
            display_text,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        message_id = None
        try:
            response = await self._bot.send_message(
                message.chat_id,
                payload_text,
                message_thread_id=message.thread_id,
                reply_to_message_id=message.message_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            message_id = (
                response.get("message_id") if isinstance(response, dict) else None
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.compact.send_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
        if attachment is not None:
            await self._send_document(
                message.chat_id,
                attachment,
                filename="compact-summary.txt",
                thread_id=message.thread_id,
                reply_to=message.message_id,
                caption="Full summary attached.",
            )
        return message_id if isinstance(message_id, int) else None, display_text

    def _build_compact_seed_prompt(self, summary_text: str) -> str:
        summary_text = summary_text.strip() or "(no summary)"
        return (
            "Context handoff from previous thread:\n\n"
            f"{summary_text}\n\n"
            "Continue from this context. Ask for missing info if needed."
        )
