from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.adapters.telegram.client import (
    TelegramForwardOrigin,
    TelegramMessage,
)
from codex_autorunner.adapters.telegram.config import TelegramMediaCandidate
from codex_autorunner.adapters.telegram.handlers.commands.files import (
    FilesCommands,
    MediaBatchContext,
    MediaBatchResult,
    MediaBatchStats,
)
from codex_autorunner.adapters.telegram.state import TelegramTopicRecord


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
        self._logger = logging.getLogger(__name__)
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
    assert "Outbox:" in text
    assert "Use /bind" not in text


@pytest.mark.anyio
async def test_files_requires_binding_when_no_pma(tmp_path: Path) -> None:
    record = TelegramTopicRecord(pma_enabled=False)
    handler = _FilesHandlerStub(tmp_path, record)

    await handler._handle_files(_message(), "", _runtime=None)

    assert handler._sent
    assert "Use /bind" in handler._sent[-1]


def test_build_media_prompt_includes_forwarded_caption(tmp_path: Path) -> None:
    handler = FilesCommands.__new__(FilesCommands)
    handler._config = SimpleNamespace(
        media=SimpleNamespace(image_prompt="Describe the image", max_file_bytes=1024)
    )
    handler._hub_root = tmp_path
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=10,
        thread_id=20,
        from_user_id=2,
        text=None,
        caption="caption here",
        date=None,
        is_topic_message=True,
        forward_origin=TelegramForwardOrigin(source_label="Ops", message_id=9),
    )
    context = MediaBatchContext(
        first_message=message,
        sorted_messages=[message],
        record=TelegramTopicRecord(workspace_path=str(tmp_path), pma_enabled=False),
        runtime=None,
        topic_key="10:20",
        max_image_bytes=1024,
        max_file_bytes=1024,
    )
    result = MediaBatchResult(
        saved_image_paths=[],
        saved_image_inbox_info=[],
        saved_file_info=[("notes.txt", "/tmp/notes.txt", 12)],
        stats=MediaBatchStats(),
    )

    prompt, input_items = handler._build_media_prompt(context, result)

    assert "Forwarded message from Ops [message 9]:" in prompt
    assert "caption here" in prompt
    assert input_items is None


def test_repo_mode_inbox_file_saves_to_filebox(tmp_path: Path) -> None:
    handler = FilesCommands.__new__(FilesCommands)
    handler._hub_root = tmp_path / "hub"
    candidate = TelegramMediaCandidate(
        kind="document",
        file_id="file-1",
        file_name="notes.txt",
        mime_type="text/plain",
        file_size=5,
    )

    saved_path = handler._save_inbox_file(
        str(tmp_path),
        "10:20",
        b"hello",
        candidate=candidate,
        file_path=None,
        pma_enabled=False,
    )

    assert saved_path.parent == tmp_path / ".codex-autorunner" / "filebox" / "inbox"
    assert saved_path.read_bytes() == b"hello"


@pytest.mark.anyio
async def test_media_batch_passes_transcript_attachments(tmp_path: Path) -> None:
    handler = _FilesHandlerStub(
        tmp_path / "hub",
        TelegramTopicRecord(workspace_path=str(tmp_path), pma_enabled=False),
    )
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=10,
        thread_id=20,
        from_user_id=2,
        text=None,
        date=None,
        is_topic_message=True,
    )
    context = MediaBatchContext(
        first_message=message,
        sorted_messages=[message],
        record=TelegramTopicRecord(workspace_path=str(tmp_path), pma_enabled=False),
        runtime=None,
        topic_key="10:20",
        max_image_bytes=1024,
        max_file_bytes=1024,
    )
    saved_path = tmp_path / ".codex-autorunner" / "filebox" / "inbox" / "notes.txt"
    saved_path.parent.mkdir(parents=True)
    saved_path.write_bytes(b"hello")
    result = MediaBatchResult(
        saved_image_paths=[],
        saved_image_inbox_info=[],
        saved_file_info=[("notes.txt", str(saved_path), 5)],
        stats=MediaBatchStats(),
    )
    captured: dict[str, object] = {}

    async def _prepare_media_batch_context(
        _messages: list[TelegramMessage],
    ) -> MediaBatchContext:
        return context

    async def _process_media_messages(_context: MediaBatchContext) -> MediaBatchResult:
        return result

    def _build_media_prompt(
        _context: MediaBatchContext,
        _result: MediaBatchResult,
    ) -> tuple[str, None]:
        return "File received.", None

    async def _handle_normal_message(
        _message: TelegramMessage,
        _runtime: object,
        **kwargs: object,
    ) -> None:
        captured.update(kwargs)

    handler._prepare_media_batch_context = _prepare_media_batch_context
    handler._process_media_messages = _process_media_messages
    handler._build_media_prompt = _build_media_prompt
    handler._handle_normal_message = _handle_normal_message

    await handler._handle_media_batch([message])

    [attachment] = captured["transcript_attachments"]
    assert attachment["box"] == "inbox"
    assert attachment["name"] == "notes.txt"
    assert attachment["source_surface"] == "telegram"
    assert attachment["source_message_id"] == "2"
    assert attachment["source_thread_id"] == "20"
