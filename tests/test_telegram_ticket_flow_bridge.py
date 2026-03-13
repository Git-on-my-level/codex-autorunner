import json
import logging
import sqlite3
from pathlib import Path

import pytest

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
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


def _init_repo(workspace: Path) -> None:
    seed_hub_files(workspace, force=True)
    (workspace / ".git").mkdir()
    seed_repo_files(workspace, git_required=False)


def _create_paused_run_with_dispatch(
    workspace: Path,
    run_id: str,
    seq: str,
    *,
    dispatch_text: str,
) -> None:
    db_path = workspace / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        if store.get_flow_run(run_id) is None:
            store.create_flow_run(run_id, "ticket_flow", input_data={}, state={})
        store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)

    history_dir = (
        workspace / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / seq
    )
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / "DISPATCH.md").write_text(dispatch_text, encoding="utf-8")


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
    assert all(not text.startswith("Part ") for _, text, _ in calls)
    assert docs == ["note.txt"]
    assert record.last_ticket_dispatch_seq == "run1:0001"
    mirror_path = (
        workspace / ".codex-autorunner" / "flows" / "run1" / "chat" / "outbound.jsonl"
    )
    assert mirror_path.exists()
    mirror_records = [
        json.loads(line)
        for line in mirror_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert mirror_records
    assert mirror_records[-1]["event_type"] == "flow_pause_dispatch_notice"
    assert mirror_records[-1]["kind"] == "dispatch"


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
async def test_pause_dispatch_prefers_pause_dispatch_over_turn_summary(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws_real"
    workspace.mkdir()
    _init_repo(workspace)
    _create_paused_run_with_dispatch(
        workspace,
        "run-real",
        "0001",
        dispatch_text=(
            "---\nmode: pause\ntitle: Need input\n---\n\n"
            "Please answer the blocker before I continue.\n"
        ),
    )
    _create_paused_run_with_dispatch(
        workspace,
        "run-real",
        "0002",
        dispatch_text=(
            "---\nmode: turn_summary\n---\n\n"
            "This summary should not be sent to Telegram.\n"
        ),
    )

    calls: list[tuple[int, str, int | None]] = []

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
        return True

    pause_config = PauseDispatchNotifications(
        enabled=True,
        send_attachments=False,
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

    await bridge._notify_ticket_flow_pause(workspace, [("123:root", record)])

    assert len(calls) == 1
    text = calls[0][1]
    assert "Ticket flow paused (run run-real). Latest dispatch #0001:" in text
    assert f"Source: {workspace}" in text
    assert "Need input" in text
    assert "Please answer the blocker before I continue." in text
    assert "Use `/flow resume` to continue." in text
    assert "turn_summary" not in text
    assert "This summary should not be sent to Telegram." not in text


@pytest.mark.asyncio
async def test_pause_dispatch_surfaces_latest_invalid_dispatch_notice(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws_invalid"
    workspace.mkdir()
    _init_repo(workspace)
    _create_paused_run_with_dispatch(
        workspace,
        "run-invalid",
        "0001",
        dispatch_text="---\nmode: broken\n---\n\nMalformed latest dispatch.\n",
    )

    calls: list[tuple[int, str, int | None]] = []

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
        return True

    pause_config = PauseDispatchNotifications(
        enabled=True,
        send_attachments=False,
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

    await bridge._notify_ticket_flow_pause(workspace, [("123:root", record)])

    assert len(calls) == 1
    text = calls[0][1]
    assert "Ticket flow paused (run run-invalid). Latest dispatch #0001:" in text
    assert "Latest paused dispatch #0001 is unreadable or invalid." in text
    assert "frontmatter.mode must be 'notify', 'pause', or 'turn_summary'." in text
    assert "Use `/flow resume` to continue." in text


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


@pytest.mark.asyncio
async def test_default_chat_skips_when_discord_binding_is_preferred(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    workspace = hub_root / "worktrees" / "repo-a"
    workspace.mkdir(parents=True)
    state_dir = hub_root / ".codex-autorunner"
    state_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(state_dir / "discord_state.sqlite3")
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE channel_bindings (
                    channel_id TEXT PRIMARY KEY,
                    guild_id TEXT,
                    workspace_path TEXT NOT NULL,
                    repo_id TEXT,
                    last_pause_run_id TEXT,
                    last_pause_dispatch_seq TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO channel_bindings (
                    channel_id,
                    guild_id,
                    workspace_path,
                    repo_id,
                    last_pause_run_id,
                    last_pause_dispatch_seq,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "discord-123",
                    "guild-1",
                    str(workspace),
                    "repo-a",
                    None,
                    None,
                    "2026-03-12T02:00:00Z",
                ),
            )
    finally:
        conn.close()

    calls: list[str] = []

    async def send_message_with_outbox(
        chat_id: int, text: str, thread_id=None, reply_to=None
    ):
        calls.append(text)
        return True

    async def send_document(**kwargs):
        return True

    bridge = TelegramTicketFlowBridge(
        logger=logging.getLogger("test"),
        store=_DummyStore({}),
        pause_targets={},
        send_message_with_outbox=send_message_with_outbox,
        send_document=send_document,
        pause_config=PauseDispatchNotifications(
            enabled=True,
            send_attachments=False,
            max_file_size_bytes=10,
            chunk_long_messages=False,
        ),
        default_notification_chat_id=999,
        hub_root=hub_root,
        manifest_path=None,
        config_root=workspace,
        hub_raw_config={"discord_bot": {"enabled": True}},
    )

    bridge._load_ticket_flow_pause = lambda path: ("run2", "0002", "body", None)  # type: ignore

    await bridge._notify_via_default_chat(workspace)

    assert calls == []
