from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.context_awareness import (
    CAR_AWARENESS_BLOCK,
    PROMPT_WRITING_HINT,
)
from codex_autorunner.integrations.telegram.adapter import (
    TelegramDocument,
    TelegramMessage,
)
from codex_autorunner.integrations.telegram.handlers.commands.execution import (
    ExecutionCommands,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord


class _ContextExecutionStub(ExecutionCommands):
    def __init__(self, workspace_path: Path) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace(media=SimpleNamespace(max_file_bytes=1024))
        self._workspace_path = workspace_path
        self.sent_messages: list[dict[str, object]] = []

    async def _resolve_topic_key(self, chat_id: int, thread_id: int | None) -> str:
        return f"{chat_id}:{thread_id}"

    async def _maybe_inject_github_context(
        self,
        prompt_text: str,
        _record: object,
        *,
        link_source_text: str | None = None,
        allow_cross_repo: bool = False,
    ) -> tuple[str, bool]:
        _ = (link_source_text, allow_cross_repo)
        return prompt_text, False

    def _files_topic_dir(self, workspace_path: str, topic_key: str) -> Path:
        return Path(workspace_path) / ".codex-autorunner" / "filebox" / topic_key

    def _files_inbox_dir(self, workspace_path: str, topic_key: str) -> Path:
        return self._files_topic_dir(workspace_path, topic_key) / "inbox"

    def _files_outbox_pending_dir(self, workspace_path: str, topic_key: str) -> Path:
        return self._files_topic_dir(workspace_path, topic_key) / "outbox" / "pending"

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
    ) -> None:
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "thread_id": thread_id,
                "reply_to": reply_to,
            }
        )


def test_telegram_car_context_not_injected_for_plain_repo_turn() -> None:
    handler = ExecutionCommands()
    prompt, injected = handler._maybe_inject_car_context(
        "fix failing tests in src/foo.py"
    )

    assert injected is False
    assert prompt == "fix failing tests in src/foo.py"


def test_telegram_car_context_injected_for_car_trigger() -> None:
    handler = ExecutionCommands()
    prompt, injected = handler._maybe_inject_car_context(
        "please update .codex-autorunner/tickets/TICKET-001.md"
    )

    assert injected is True
    assert "<injected context>" in prompt
    assert "</injected context>" in prompt
    assert ".codex-autorunner/ABOUT_CAR.md" in prompt
    assert ".codex-autorunner/tickets/TICKET-001.md" in prompt


@pytest.mark.anyio
async def test_telegram_file_hints_injected_for_plain_text_outbox_keyword(
    tmp_path: Path,
) -> None:
    handler = _ContextExecutionStub(tmp_path)
    record = TelegramTopicRecord(workspace_path=str(tmp_path))
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="check outbox for report.txt",
        date=None,
        is_topic_message=True,
    )

    prompt, _topic_key = await handler._prepare_turn_context(
        message,
        "check outbox for report.txt",
        record,
        input_items=None,
    )

    assert "Outbox (pending):" in prompt
    assert "Inbox:" in prompt


@pytest.mark.anyio
async def test_telegram_file_hints_injected_for_plain_text_inbox_keyword(
    tmp_path: Path,
) -> None:
    handler = _ContextExecutionStub(tmp_path)
    record = TelegramTopicRecord(workspace_path=str(tmp_path))
    message = TelegramMessage(
        update_id=6,
        message_id=7,
        chat_id=8,
        thread_id=9,
        from_user_id=10,
        text="check inbox",
        date=None,
        is_topic_message=True,
    )

    prompt, _topic_key = await handler._prepare_turn_context(
        message,
        "check inbox",
        record,
        input_items=None,
    )

    assert "Outbox (pending):" in prompt
    assert "Inbox:" in prompt


@pytest.mark.anyio
async def test_telegram_file_hints_do_not_trigger_from_injected_car_context(
    tmp_path: Path,
) -> None:
    handler = _ContextExecutionStub(tmp_path)
    record = TelegramTopicRecord(workspace_path=str(tmp_path))
    message = TelegramMessage(
        update_id=11,
        message_id=12,
        chat_id=13,
        thread_id=14,
        from_user_id=15,
        text="summarize the notes",
        date=None,
        is_topic_message=True,
    )

    prompt, _topic_key = await handler._prepare_turn_context(
        message,
        CAR_AWARENESS_BLOCK,
        record,
        input_items=None,
        user_input_text="summarize the notes",
    )

    assert "Outbox (pending):" not in prompt
    assert "Inbox:" not in prompt


@pytest.mark.anyio
async def test_telegram_file_hints_injected_when_file_context_exists(
    tmp_path: Path,
) -> None:
    handler = _ContextExecutionStub(tmp_path)
    record = TelegramTopicRecord(workspace_path=str(tmp_path))
    message = TelegramMessage(
        update_id=10,
        message_id=20,
        chat_id=30,
        thread_id=40,
        from_user_id=50,
        text="please summarize this",
        date=None,
        is_topic_message=True,
        document=TelegramDocument(
            file_id="doc-1",
            file_unique_id=None,
            file_name="notes.txt",
            mime_type="text/plain",
            file_size=123,
        ),
    )

    prompt, topic_key = await handler._prepare_turn_context(
        message,
        "please summarize this",
        record,
        input_items=None,
    )

    assert "Inbox:" in prompt
    assert "Outbox (pending):" in prompt
    assert topic_key in prompt


@pytest.mark.anyio
async def test_telegram_prompt_hint_uses_raw_user_text_not_github_injection(
    tmp_path: Path,
) -> None:
    handler = _ContextExecutionStub(tmp_path)
    record = TelegramTopicRecord(workspace_path=str(tmp_path))
    message = TelegramMessage(
        update_id=31,
        message_id=32,
        chat_id=33,
        thread_id=34,
        from_user_id=35,
        text="review this issue",
        date=None,
        is_topic_message=True,
    )

    async def _fake_github_context(
        prompt_text: str,
        _record: object,
        *,
        link_source_text: str | None = None,
        allow_cross_repo: bool = False,
    ) -> tuple[str, bool]:
        _ = (link_source_text, allow_cross_repo)
        return f"{prompt_text}\n\n<injected context>\nprompt\n</injected context>", True

    handler._maybe_inject_github_context = _fake_github_context  # type: ignore[method-assign]

    prompt, _topic_key = await handler._prepare_turn_context(
        message,
        "review this issue",
        record,
        input_items=None,
    )

    assert "<injected context>\nprompt\n</injected context>" in prompt
    assert PROMPT_WRITING_HINT not in prompt


@pytest.mark.anyio
async def test_telegram_github_context_uses_raw_user_text_as_link_source(
    tmp_path: Path,
) -> None:
    handler = _ContextExecutionStub(tmp_path)
    record = TelegramTopicRecord(workspace_path=str(tmp_path))
    message = TelegramMessage(
        update_id=41,
        message_id=42,
        chat_id=43,
        thread_id=44,
        from_user_id=45,
        text="plain user text",
        date=None,
        is_topic_message=True,
    )
    captured_link_sources: list[str | None] = []

    async def _fake_github_context(
        prompt_text: str,
        _record: object,
        *,
        link_source_text: str | None = None,
        allow_cross_repo: bool = False,
    ) -> tuple[str, bool]:
        _ = (prompt_text, allow_cross_repo)
        captured_link_sources.append(link_source_text)
        return prompt_text, False

    handler._maybe_inject_github_context = _fake_github_context  # type: ignore[method-assign]

    await handler._prepare_turn_context(
        message,
        "mutated prompt text",
        record,
        input_items=None,
        user_input_text="https://github.com/example/repo/issues/123",
    )

    assert captured_link_sources == ["https://github.com/example/repo/issues/123"]
