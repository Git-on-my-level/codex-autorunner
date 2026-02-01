from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands.files import (
    FilesCommands,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord


class _RouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def ensure_topic(
        self, _chat_id: int, _thread_id: int | None
    ) -> TelegramTopicRecord:
        return self._record

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record


class _FilesHandlerStub(FilesCommands):
    def __init__(self, hub_root: Path, record: TelegramTopicRecord) -> None:
        media_cfg = SimpleNamespace(
            enabled=True,
            files=True,
            max_image_bytes=1024 * 1024,
            max_file_bytes=1024 * 1024,
            batch_uploads=False,
        )
        self._config = SimpleNamespace(media=media_cfg)
        self._hub_root = hub_root
        self._router = _RouterStub(record)
        self._sent: list[str] = []

    def _with_conversation_id(
        self, text: str, *, chat_id: int, thread_id: int | None
    ) -> str:
        _ = (chat_id, thread_id)
        return text

    async def _resolve_topic_key(self, chat_id: int, thread_id: int | None) -> str:
        return f"{chat_id}:{thread_id}"

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> None:
        _ = (thread_id, reply_to, reply_markup)
        self._sent.append(text)


def _message(text: str = "/files") -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=1,
        chat_id=10,
        thread_id=20,
        from_user_id=2,
        text=text,
        date=None,
        is_topic_message=True,
    )


@pytest.mark.anyio
async def test_files_lists_for_pma_topic(tmp_path: Path) -> None:
    record = TelegramTopicRecord(pma_enabled=True)
    handler = _FilesHandlerStub(tmp_path, record)

    await handler._handle_files(_message(), "", _runtime=None)

    assert handler._sent, "should respond in PMA mode"
    text = handler._sent[-1]
    assert "Inbox:" in text
    assert "Outbox pending:" in text
    assert "Use /bind" not in text


@pytest.mark.anyio
async def test_files_requires_binding_when_no_pma(tmp_path: Path) -> None:
    record = TelegramTopicRecord(pma_enabled=False)
    handler = _FilesHandlerStub(tmp_path, record)

    await handler._handle_files(_message(), "", _runtime=None)

    assert handler._sent
    assert "Use /bind" in handler._sent[-1]
