from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from codex_autorunner.agents.opencode.client import OpenCodeProtocolError
from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisorError
from codex_autorunner.integrations.telegram.constants import TELEGRAM_MAX_MESSAGE_LENGTH
from codex_autorunner.integrations.telegram.handlers.commands.command_utils import (
    _format_httpx_exception,
    _format_opencode_exception,
    _issue_only_link,
    _opencode_review_arguments,
)
from codex_autorunner.integrations.telegram.transport import TelegramMessageTransport


def test_issue_only_link_matches_single_link_wrappers() -> None:
    link = "https://example.com/issue/1"
    assert _issue_only_link(link, [link]) == link
    assert _issue_only_link(f"<{link}>", [link]) == link
    assert _issue_only_link(f"({link})", [link]) == link


def test_issue_only_link_ignores_non_wrapper_text() -> None:
    assert _issue_only_link("", ["https://example.com"]) is None
    assert _issue_only_link("check this", ["https://example.com"]) is None
    assert _issue_only_link("https://example.com", ["one", "two"]) is None


def test_opencode_review_arguments_reduces_known_target_types() -> None:
    assert _opencode_review_arguments({"type": "uncommittedChanges"}) == ""
    assert (
        _opencode_review_arguments({"type": "baseBranch", "branch": "feature/ci"})
        == "feature/ci"
    )
    assert _opencode_review_arguments({"type": "commit", "sha": "abc123"}) == "abc123"
    assert (
        _opencode_review_arguments({"type": "custom", "instructions": "add tests"})
        == "uncommitted\n\nadd tests"
    )
    assert (
        _opencode_review_arguments({"type": "custom", "instructions": "   "})
        == "uncommitted"
    )


def test_opencode_review_arguments_falls_back_to_json_payload() -> None:
    target = {"type": "other", "foo": "bar"}
    assert _opencode_review_arguments(target) == json.dumps(target, sort_keys=True)


def test_format_httpx_exception_uses_http_payload_detail() -> None:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(
        502,
        request=request,
        json={"message": "temporary outage"},
    )
    exc = httpx.HTTPStatusError("server error", request=request, response=response)
    assert _format_httpx_exception(exc) == "temporary outage"


def test_format_opencode_exception_formats_backend_unavailable() -> None:
    result = _format_opencode_exception(OpenCodeSupervisorError("service offline"))
    assert result == "OpenCode backend unavailable (service offline)."


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            OpenCodeProtocolError("invalid protocol"),
            "OpenCode protocol error: invalid protocol",
        ),
        (OpenCodeProtocolError(""), "OpenCode protocol error."),
    ],
)
def test_format_opencode_exception_formats_protocol_error(
    exc: Exception, expected: str
) -> None:
    assert _format_opencode_exception(exc) == expected


class _OverflowBotStub:
    def __init__(self) -> None:
        self.send_message_calls: list[dict[str, object]] = []
        self.send_message_chunks_calls: list[dict[str, object]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, object]:
        self.send_message_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "thread_id": message_thread_id,
                "reply_to": reply_to_message_id,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            }
        )
        return {"message_id": len(self.send_message_calls)}

    async def send_message_chunks(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self.send_message_chunks_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "thread_id": message_thread_id,
                "reply_to": reply_to_message_id,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            }
        )


class _OverflowTransportHarness(TelegramMessageTransport):
    def __init__(self, overflow_mode: str) -> None:
        self._bot = _OverflowBotStub()
        self._config = SimpleNamespace(
            parse_mode="HTML", message_overflow=overflow_mode
        )
        self.sent_documents: list[dict[str, object]] = []

    def _build_debug_prefix(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        reply_to: int | None,
    ) -> str | None:
        _ = (chat_id, thread_id, reply_to)
        return None

    def _render_message(
        self, text: str, *, parse_mode: str = "HTML"
    ) -> tuple[str, str]:
        _ = parse_mode
        return text, "HTML"

    def _prepare_outgoing_text(
        self,
        text: str,
        *,
        chat_id: int,
        thread_id: int | None = None,
        reply_to: int | None = None,
        topic_key: str | None = None,
        codex_thread_id: str | None = None,
    ) -> tuple[str, str | None]:
        _ = (chat_id, thread_id, reply_to, topic_key, codex_thread_id)
        return text, "HTML"

    async def _send_document(
        self,
        chat_id: int,
        content: bytes,
        *,
        filename: str,
        thread_id: int | None = None,
        reply_to: int | None = None,
        caption: str | None = None,
    ) -> bool:
        self.sent_documents.append(
            {
                "chat_id": chat_id,
                "content_len": len(content),
                "filename": filename,
                "thread_id": thread_id,
                "reply_to": reply_to,
                "caption": caption,
            }
        )
        return True


@pytest.mark.anyio
@pytest.mark.parametrize("overflow_mode", ["document", "split", "trim"])
async def test_long_message_overflow_modes_are_stable(overflow_mode: str) -> None:
    harness = _OverflowTransportHarness(overflow_mode)
    long_text = "x" * (TELEGRAM_MAX_MESSAGE_LENGTH + 500)

    await harness._send_message(1234, long_text, thread_id=42, reply_to=99)

    if overflow_mode == "document":
        assert len(harness.sent_documents) == 1
        assert harness.sent_documents[0]["filename"] == "response.md"
        assert harness._bot.send_message_calls == []
        assert harness._bot.send_message_chunks_calls == []
        return
    if overflow_mode == "split":
        assert len(harness._bot.send_message_calls) >= 2
        assert harness._bot.send_message_calls[0]["reply_to"] == 99
        assert all(
            call["parse_mode"] == "HTML" for call in harness._bot.send_message_calls
        )
        assert harness.sent_documents == []
        assert harness._bot.send_message_chunks_calls == []
        return
    assert len(harness._bot.send_message_chunks_calls) == 1
    payload = harness._bot.send_message_chunks_calls[0]["text"]
    assert isinstance(payload, str)
    assert len(payload) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert harness.sent_documents == []
    assert harness._bot.send_message_calls == []
