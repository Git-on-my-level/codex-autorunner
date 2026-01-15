import logging
from dataclasses import dataclass
from typing import Optional

import pytest

from codex_autorunner.integrations.telegram.constants import TELEGRAM_MAX_MESSAGE_LENGTH
from codex_autorunner.integrations.telegram.rendering import (
    _format_telegram_html,
    _format_telegram_markdown,
)
from codex_autorunner.integrations.telegram.transport import TelegramMessageTransport


@dataclass
class _DummyConfig:
    parse_mode: Optional[str]


class _DummyBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, object]] = []
        self.sent_docs: list[dict[str, object]] = []

    async def send_message_chunks(self, chat_id, text, **kwargs):  # type: ignore[no-untyped-def]
        self.sent_messages.append({"chat_id": chat_id, "text": text, **kwargs})
        return []

    async def send_document(self, chat_id, document, **kwargs):  # type: ignore[no-untyped-def]
        self.sent_docs.append({"chat_id": chat_id, "document": document, **kwargs})
        return {}


class _DummyTransport(TelegramMessageTransport):
    def __init__(self, parse_mode: Optional[str]) -> None:
        self._config = _DummyConfig(parse_mode=parse_mode)
        self._bot = _DummyBot()
        self._logger = logging.getLogger("test")

    def _build_debug_prefix(self, *, chat_id, thread_id, reply_to=None, **_kwargs):  # type: ignore[no-untyped-def]
        return ""

    def _render_message(self, text: str):  # type: ignore[no-untyped-def]
        parse_mode = self._config.parse_mode
        if not parse_mode:
            return text, None
        if parse_mode == "HTML":
            return _format_telegram_html(text), parse_mode
        if parse_mode in ("Markdown", "MarkdownV2"):
            return _format_telegram_markdown(text, parse_mode), parse_mode
        return text, parse_mode


@pytest.mark.anyio
@pytest.mark.parametrize("parse_mode", ["Markdown", "MarkdownV2", "HTML"])
async def test_send_long_message_uses_markdown_document(parse_mode: str) -> None:
    transport = _DummyTransport(parse_mode=parse_mode)
    long_text = "x" * (TELEGRAM_MAX_MESSAGE_LENGTH + 5)

    await transport._send_message(123, long_text)

    assert not transport._bot.sent_messages
    assert len(transport._bot.sent_docs) == 1
    payload = transport._bot.sent_docs[0]
    assert payload["filename"] == "response.md"
    assert payload["caption"] == "Response too long; attached as response.md."
    assert payload["document"] == long_text.encode("utf-8")
