import logging
from pathlib import Path

import pytest

from codex_autorunner.integrations.telegram.config import PauseDispatchNotifications
from codex_autorunner.integrations.telegram.ticket_flow_bridge import (
    TelegramTicketFlowBridge,
)


class _DummyRecord:
    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = str(workspace_path)
        self.last_ticket_dispatch_seq = None


class _DummyStore:
    def __init__(self, topics: dict[str, _DummyRecord]) -> None:
        self._topics = topics

    async def list_topics(self) -> dict[str, _DummyRecord]:
        return self._topics

    async def update_topic(self, key: str, fn) -> None:
        fn(self._topics[key])


@pytest.mark.asyncio
async def test_pause_dispatch_sends_text_and_attachments(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    small = workspace / "dispatch_history" / "0001"
    small.mkdir(parents=True)
    (small / "DISPATCH.md").write_text("hello", encoding="utf-8")
    (small / "note.txt").write_text("attachment", encoding="utf-8")
    big_path = small / "big.bin"
    big_path.write_bytes(b"x" * (60 * 1024 * 1024))

    calls: list[tuple[int, str, int | None]] = []
    docs: list[str] = []

    async def send_message_with_outbox(
        chat_id: int, text: str, thread_id=None, reply_to=None
    ):
        calls.append((chat_id, text, thread_id))
        return True

    async def send_document(
        chat_id: int,
        data: bytes,
        *,
        filename: str,
        thread_id=None,
        reply_to=None,
        caption=None,
    ):
        docs.append(filename)
        return True

    pause_config = PauseDispatchNotifications(
        enabled=True,
        send_attachments=True,
        max_file_size_bytes=50 * 1024 * 1024,
        chunk_long_messages=True,
    )
    record = _DummyRecord(workspace)
    store = _DummyStore({"123:root": record})
    bridge = TelegramTicketFlowBridge(
        logger=logging.getLogger("test"),
        store=store,
        pause_targets={},
        send_message_with_outbox=send_message_with_outbox,
        send_document=send_document,
        pause_config=pause_config,
        default_notification_chat_id=None,
        hub_root=None,
        manifest_path=None,
        config_root=workspace,
    )

    bridge._load_ticket_flow_pause = lambda path: ("run1", "0001", "x" * 5000, small)  # type: ignore

    await bridge._notify_ticket_flow_pause(workspace, [("123:root", record)])

    # Chunked text should produce more than one message
    assert len(calls) >= 2
    assert docs == ["note.txt"]
    assert record.last_ticket_dispatch_seq == "run1:0001"


@pytest.mark.asyncio
async def test_pause_dispatch_reports_attachment_send_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "ws_fail"
    workspace.mkdir()
    history = workspace / "dispatch_history" / "0001"
    history.mkdir(parents=True)
    (history / "DISPATCH.md").write_text("body", encoding="utf-8")
    (history / "note.txt").write_text("attachment", encoding="utf-8")

    calls: list[str] = []

    async def send_message_with_outbox(
        chat_id: int, text: str, thread_id=None, reply_to=None
    ):
        calls.append(text)
        return True

    async def send_document(
        chat_id: int,
        data: bytes,
        *,
        filename: str,
        thread_id=None,
        reply_to=None,
        caption=None,
    ):
        return False

    pause_config = PauseDispatchNotifications(
        enabled=True,
        send_attachments=True,
        max_file_size_bytes=50 * 1024 * 1024,
        chunk_long_messages=False,
    )
    record = _DummyRecord(workspace)
    store = _DummyStore({"123:root": record})
    bridge = TelegramTicketFlowBridge(
        logger=logging.getLogger("test"),
        store=store,
        pause_targets={},
        send_message_with_outbox=send_message_with_outbox,
        send_document=send_document,
        pause_config=pause_config,
        default_notification_chat_id=None,
        hub_root=None,
        manifest_path=None,
        config_root=workspace,
    )

    bridge._load_ticket_flow_pause = lambda path: ("run1", "0001", "body", history)  # type: ignore

    await bridge._notify_ticket_flow_pause(workspace, [("123:root", record)])

    assert any("Failed to send attachment note.txt." in call for call in calls)


@pytest.mark.asyncio
async def test_default_chat_dedupes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws2"
    workspace.mkdir()
    seq_dir = workspace / ".codex-autorunner" / "runs"
    seq_dir.mkdir(parents=True, exist_ok=True)

    calls: list[str] = []

    async def send_message_with_outbox(
        chat_id: int, text: str, thread_id=None, reply_to=None
    ):
        calls.append(text)
        return True

    async def send_document(**kwargs):
        return True

    pause_config = PauseDispatchNotifications(
        enabled=True,
        send_attachments=False,
        max_file_size_bytes=10,
        chunk_long_messages=False,
    )
    bridge = TelegramTicketFlowBridge(
        logger=logging.getLogger("test"),
        store=_DummyStore({}),
        pause_targets={},
        send_message_with_outbox=send_message_with_outbox,
        send_document=send_document,
        pause_config=pause_config,
        default_notification_chat_id=999,
        hub_root=None,
        manifest_path=None,
        config_root=workspace,
    )

    bridge._load_ticket_flow_pause = lambda path: ("run2", "0002", "body", None)  # type: ignore

    await bridge._notify_via_default_chat(workspace)
    await bridge._notify_via_default_chat(workspace)

    assert len(calls) == 1
