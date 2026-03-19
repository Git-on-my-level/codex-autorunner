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
        self.edited_messages: list[dict[str, object]] = []

    async def send_message_chunks(self, chat_id, text, **kwargs):  # type: ignore[no-untyped-def]
        self.sent_messages.append({"chat_id": chat_id, "text": text, **kwargs})
        return []

    async def send_document(self, chat_id, document, **kwargs):  # type: ignore[no-untyped-def]
        self.sent_docs.append({"chat_id": chat_id, "document": document, **kwargs})
        return {}

    async def edit_message_text(  # type: ignore[no-untyped-def]
        self, chat_id, message_id, text, **kwargs
    ):
        self.edited_messages.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text, **kwargs}
        )
        return {}


class _DummyTransport(TelegramMessageTransport):
    def __init__(self, parse_mode: Optional[str]) -> None:
        self._config = _DummyConfig(parse_mode=parse_mode)
        self._bot = _DummyBot()
        self._logger = logging.getLogger("test")

    def _build_debug_prefix(self, *, chat_id, thread_id, reply_to=None, **_kwargs):  # type: ignore[no-untyped-def]
        return ""

    def _render_message(  # type: ignore[no-untyped-def]
        self, text: str, *, parse_mode: Optional[str] = None
    ):
        parse_mode = self._config.parse_mode if parse_mode is None else parse_mode
        if not parse_mode:
            return text, None
        if parse_mode == "HTML":
            return _format_telegram_html(text), parse_mode
        if parse_mode in ("Markdown", "MarkdownV2"):
            return _format_telegram_markdown(text, parse_mode), parse_mode
        return text, parse_mode

    def _prepare_message(  # type: ignore[no-untyped-def]
        self, text: str, *, parse_mode: Optional[str] = None
    ):
        rendered, used_mode = self._render_message(text, parse_mode=parse_mode)
        if used_mode and len(rendered) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return rendered, used_mode
        return text, None


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


@pytest.mark.anyio
async def test_deliver_turn_response_sends_intermediate_separator_and_final() -> None:
    transport = _DummyTransport(parse_mode=None)
    calls: list[dict[str, object]] = []

    async def _fake_send_message_with_outbox(
        chat_id,
        text,
        *,
        thread_id,
        reply_to,
        placeholder_id=None,
        delete_placeholder_on_delivery=True,
    ):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "thread_id": thread_id,
                "reply_to": reply_to,
                "placeholder_id": placeholder_id,
                "delete_placeholder_on_delivery": delete_placeholder_on_delivery,
            }
        )
        return True

    transport._send_message_with_outbox = _fake_send_message_with_outbox  # type: ignore[assignment]

    delivered = await transport._deliver_turn_response(
        chat_id=123,
        thread_id=456,
        reply_to=789,
        placeholder_id=321,
        intermediate_response="intermediate output",
        response="final output",
    )

    assert delivered is True
    assert calls == [
        {
            "chat_id": 123,
            "text": "intermediate output",
            "thread_id": 456,
            "reply_to": 789,
            "placeholder_id": 321,
            "delete_placeholder_on_delivery": False,
        },
        {
            "chat_id": 123,
            "text": "---",
            "thread_id": 456,
            "reply_to": None,
            "placeholder_id": None,
            "delete_placeholder_on_delivery": True,
        },
        {
            "chat_id": 123,
            "text": "final output",
            "thread_id": 456,
            "reply_to": None,
            "placeholder_id": 321,
            "delete_placeholder_on_delivery": True,
        },
    ]


@pytest.mark.parametrize("parse_mode", ["Markdown", "MarkdownV2"])
def test_telegram_markdown_collapses_local_file_links(parse_mode: str) -> None:
    rendered = _format_telegram_markdown(
        "See [archive_helpers.py](/Users/dazheng/worktree/src/archive_helpers.py) "
        "and [docs](https://example.com/docs).",
        parse_mode,
    )

    assert "archive" in rendered
    assert "helpers" in rendered
    assert "/Users/dazheng/worktree/src/archive_helpers.py" not in rendered
    assert "docs" in rendered
    assert "example" in rendered
    assert "docs" in rendered


def test_telegram_html_collapses_local_file_links() -> None:
    rendered = _format_telegram_html(
        "See [archive_helpers.py](/Users/dazheng/worktree/src/archive_helpers.py) "
        "and [docs](https://example.com/docs)."
    )

    assert "archive_helpers.py" in rendered
    assert "/Users/dazheng/worktree/src/archive_helpers.py" not in rendered
    assert "docs" in rendered
    assert "https://example.com/docs" in rendered


@pytest.mark.anyio
async def test_append_metrics_to_placeholder_preserves_metrics_when_text_is_long() -> (
    None
):
    transport = _DummyTransport(parse_mode=None)
    metrics = "Metrics: elapsed 5s, tokens 123"
    base_text = "x" * (TELEGRAM_MAX_MESSAGE_LENGTH - len(metrics) + 200)

    edited = await transport._append_metrics_to_placeholder(
        123,
        456,
        metrics,
        base_text=base_text,
    )

    assert edited is True
    assert transport._bot.edited_messages
    payload_text = str(transport._bot.edited_messages[-1]["text"])
    assert len(payload_text) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert payload_text.endswith(metrics)
    assert metrics in payload_text
