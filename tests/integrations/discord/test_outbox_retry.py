from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.state import now_iso
from codex_autorunner.integrations.discord.outbox import DiscordOutboxManager
from codex_autorunner.integrations.discord.rendering import (
    DISCORD_MAX_MESSAGE_LENGTH,
    chunk_discord_message,
)
from codex_autorunner.integrations.discord.state import DiscordStateStore, OutboxRecord


class _RetryAfterError(Exception):
    def __init__(self, seconds: float) -> None:
        super().__init__("rate limited")
        self.retry_after_seconds = seconds


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current = self.current + timedelta(seconds=seconds)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()
    seed_repo_files(workspace, git_required=False)
    return workspace


def _create_terminal_run(workspace: Path, run_id: str) -> None:
    with FlowStore(workspace / ".codex-autorunner" / "flows.db") as store:
        store.create_flow_run(run_id, "ticket_flow", input_data={}, state={})
        store.update_flow_run_status(run_id, FlowRunStatus.COMPLETED)


@pytest.mark.anyio
async def test_outbox_retry_uses_retry_after_and_eventually_delivers(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    calls = {"count": 0}

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        calls["count"] += 1
        if calls["count"] == 1:
            raise _RetryAfterError(2.0)
        return {"id": "msg-1"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        immediate_retry_delays=(0.0,),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        delivered = await manager.send_with_outbox(
            OutboxRecord(
                record_id="r1",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "hello"},
                created_at=now_iso(),
            )
        )
        assert delivered is True
        assert calls["count"] == 2
        assert any(delay >= 1.9 for delay in clock.sleeps)
        assert await store.get_outbox("r1") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flush_skips_future_next_attempt_records(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    calls = {"count": 0}

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        calls["count"] += 1
        return {"id": "msg-1"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        record = OutboxRecord(
            record_id="r1",
            channel_id="chan-1",
            message_id=None,
            operation="send",
            payload_json={"content": "hello"},
            created_at=now_iso(),
        )
        await store.enqueue_outbox(record)
        await store.record_outbox_failure("r1", error="later", retry_after_seconds=10.0)

        records = await store.list_outbox()
        await manager._flush(records)
        assert calls["count"] == 0
        assert await store.get_outbox("r1") is not None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_delete_operation_is_supported(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    calls: list[tuple[str, str]] = []

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        return {"id": "msg-1"}

    async def delete_message(channel_id: str, message_id: str) -> None:
        calls.append((channel_id, message_id))

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        delete_message=delete_message,
        logger=logging.getLogger("test"),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        delivered = await manager.send_with_outbox(
            OutboxRecord(
                record_id="del-1",
                channel_id="chan-1",
                message_id="msg-123",
                operation="delete",
                payload_json={},
                created_at=now_iso(),
            )
        )
        assert delivered is True
        assert calls == [("chan-1", "msg-123")]
        assert await store.get_outbox("del-1") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_edit_operation_is_supported(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    calls: list[tuple[str, str, dict]] = []

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        return {"id": "msg-1"}

    async def edit_message(channel_id: str, message_id: str, payload: dict) -> None:
        calls.append((channel_id, message_id, payload))

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        edit_message=edit_message,
        logger=logging.getLogger("test"),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        delivered = await manager.send_with_outbox(
            OutboxRecord(
                record_id="edit-1",
                channel_id="chan-1",
                message_id="msg-123",
                operation="edit",
                payload_json={"content": "working"},
                created_at=now_iso(),
            )
        )
        assert delivered is True
        assert calls == [("chan-1", "msg-123", {"content": "working"})]
        assert await store.get_outbox("edit-1") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_drops_record_after_exhausting_attempts(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    calls = {"count": 0}

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        calls["count"] += 1
        raise RuntimeError("boom")

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        max_attempts=2,
        immediate_retry_delays=(0.0,),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        delivered = await manager.send_with_outbox(
            OutboxRecord(
                record_id="drop-1",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "hello"},
                created_at=now_iso(),
            )
        )
        assert delivered is False
        assert calls["count"] == 2
        assert await store.get_outbox("drop-1") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_retry_resumes_from_first_unsent_chunk(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    sent_chunks: list[str] = []

    async def send_message(_channel_id: str, payload: dict) -> dict:
        content = payload.get("content")
        assert isinstance(content, str)
        sent_chunks.append(content)
        if len(sent_chunks) == 2:
            raise _RetryAfterError(1.0)
        return {"id": f"msg-{len(sent_chunks)}"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        immediate_retry_delays=(0.0,),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        content = ("a" * 1500) + "\n" + ("b" * 1500)
        chunks = chunk_discord_message(
            content,
            max_len=DISCORD_MAX_MESSAGE_LENGTH,
            with_numbering=False,
        )
        assert len(chunks) == 2

        delivered = await manager.send_with_outbox(
            OutboxRecord(
                record_id="chunked-1",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": content},
                created_at=now_iso(),
            )
        )

        assert delivered is True
        assert sent_chunks == [chunks[0], chunks[1], chunks[1]]
        assert sent_chunks.count(chunks[0]) == 1
        assert any(delay >= 0.9 for delay in clock.sleeps)
        assert await store.get_outbox("chunked-1") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flush_drops_previously_exhausted_record(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    calls = {"count": 0}

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        calls["count"] += 1
        return {"id": "msg-1"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        max_attempts=3,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="drop-2",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "hello"},
                attempts=3,
                created_at=now_iso(),
                last_error="permanent failure",
            )
        )
        records = await store.list_outbox()
        await manager._flush(records)
        assert calls["count"] == 0
        assert await store.get_outbox("drop-2") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flush_drops_terminal_notice_for_deleted_run(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    run_id = "run-deleted"
    _create_terminal_run(workspace, run_id)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    calls = {"count": 0}

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        calls["count"] += 1
        return {"id": "msg-1"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        await store.upsert_binding(
            channel_id="chan-1",
            guild_id=None,
            workspace_path=str(workspace),
            repo_id=None,
        )
        manager.start()
        await store.enqueue_outbox(
            OutboxRecord(
                record_id=f"terminal:chan-1:{run_id}",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "done"},
                created_at=now_iso(),
            )
        )
        with FlowStore(workspace / ".codex-autorunner" / "flows.db") as flow_store:
            assert flow_store.delete_flow_run(run_id) is True

        await manager._flush(await store.list_outbox())
        assert calls["count"] == 0
        assert await store.get_outbox(f"terminal:chan-1:{run_id}") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flush_drops_terminal_notice_for_archived_run(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    run_id = "run-archived"
    _create_terminal_run(workspace, run_id)
    (
        workspace / ".codex-autorunner" / "archive" / "runs" / run_id / "archived_runs"
    ).mkdir(parents=True, exist_ok=True)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    calls = {"count": 0}

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        calls["count"] += 1
        return {"id": "msg-1"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        await store.upsert_binding(
            channel_id="chan-1",
            guild_id=None,
            workspace_path=str(workspace),
            repo_id=None,
        )
        manager.start()
        await store.enqueue_outbox(
            OutboxRecord(
                record_id=f"terminal:chan-1:{run_id}",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "done"},
                created_at=now_iso(),
            )
        )

        await manager._flush(await store.list_outbox())
        assert calls["count"] == 0
        assert await store.get_outbox(f"terminal:chan-1:{run_id}") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flush_coalesces_by_operation_id(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    sent: list[dict] = []

    async def send_message(_channel_id: str, payload: dict) -> dict:
        sent.append(payload)
        return {"id": f"msg-{len(sent)}"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        ts = now_iso()
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="r1",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "stale"},
                created_at=ts,
                operation_id="op-100",
            )
        )
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="r2",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "fresh"},
                created_at=ts,
                operation_id="op-100",
            )
        )
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="r3",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "other"},
                created_at=ts,
                operation_id="op-200",
            )
        )

        await manager._flush(await store.list_outbox())
        assert len(sent) == 2
        contents = {s.get("content") for s in sent}
        assert "fresh" in contents
        assert "other" in contents
        assert "stale" not in contents
    finally:
        await store.close()


@pytest.mark.anyio
async def test_inflight_dedup_by_operation_id(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    send_count = {"n": 0}

    async def send_message(_channel_id: str, payload: dict) -> dict:
        send_count["n"] += 1
        if send_count["n"] == 1:
            raise _RetryAfterError(1.0)
        return {"id": "msg-ok"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        immediate_retry_delays=(0.0,),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        delivered = await manager.send_with_outbox(
            OutboxRecord(
                record_id="r1",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "hello"},
                created_at=now_iso(),
                operation_id="op-dedup",
            )
        )
        assert delivered is True
        assert send_count["n"] == 2
    finally:
        await store.close()


@pytest.mark.anyio
async def test_replay_after_restart_preserves_operation_id(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    attempts = {"n": 0}

    async def send_message(_channel_id: str, payload: dict) -> dict:
        attempts["n"] += 1
        if attempts["n"] <= 1:
            raise RuntimeError("transient")
        return {"id": "msg-ok"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        max_attempts=5,
        immediate_retry_delays=(0.0,),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()

        ts = now_iso()
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="r-replay",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "replay-me"},
                created_at=ts,
                operation_id="op-replay-1",
            )
        )

        records = await store.list_outbox()
        assert len(records) == 1
        assert records[0].operation_id == "op-replay-1"

        await manager._flush(records)
        assert attempts["n"] == 1

        failed_record = await store.get_outbox("r-replay")
        assert failed_record is not None
        assert failed_record.operation_id == "op-replay-1"
        assert failed_record.attempts == 1

        records2 = await store.list_outbox()
        await manager._flush(records2)
        assert attempts["n"] == 2

        remaining = await store.list_outbox()
        assert len(remaining) == 0
    finally:
        await store.close()


@pytest.mark.anyio
async def test_coalesce_prefers_latest_created_at(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    sent: list[str] = []

    async def send_message(_channel_id: str, payload: dict) -> dict:
        sent.append(payload.get("content", ""))
        return {"id": f"msg-{len(sent)}"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        ts_early = "2026-01-01T00:00:00Z"
        ts_late = "2026-01-01T00:01:00Z"
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="early",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "first"},
                created_at=ts_early,
                operation_id="op-same",
            )
        )
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="late",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "second"},
                created_at=ts_late,
                operation_id="op-same",
            )
        )

        await manager._flush(await store.list_outbox())
        assert sent == ["second"]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_give_up_invokes_delivery_callback_with_none(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    callbacks: list[tuple[str, str | None]] = []

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        raise RuntimeError("boom")

    async def on_delivered(
        record: OutboxRecord, delivered_message_id: str | None
    ) -> None:
        callbacks.append((record.record_id, delivered_message_id))

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        on_delivered=on_delivered,
        logger=logging.getLogger("test"),
        max_attempts=1,
        immediate_retry_delays=(0.0,),
    )

    try:
        await store.initialize()
        manager.start()

        delivered = await manager.send_with_outbox(
            OutboxRecord(
                record_id="give-up",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "hello"},
                created_at=now_iso(),
            )
        )

        assert delivered is False
        assert callbacks == [("give-up", None)]
        assert await store.list_outbox() == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_outbox_delivery_callback_io_error_does_not_retry_send(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    send_count = {"n": 0}

    async def send_message(_channel_id: str, _payload: dict) -> dict:
        send_count["n"] += 1
        return {"id": "msg-ok"}

    async def on_delivered(
        _record: OutboxRecord, _delivered_message_id: str | None
    ) -> None:
        raise OSError("sqlite unavailable")

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        on_delivered=on_delivered,
        logger=logging.getLogger("test"),
        immediate_retry_delays=(0.0,),
    )

    try:
        await store.initialize()
        manager.start()

        delivered = await manager.send_with_outbox(
            OutboxRecord(
                record_id="callback-io-error",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "hello"},
                created_at=now_iso(),
            )
        )

        assert delivered is True
        assert send_count["n"] == 1
        assert await store.list_outbox() == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_edit_failure_does_not_duplicate_on_retry(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    send_count = {"n": 0}

    async def send_message(_channel_id: str, payload: dict) -> dict:
        send_count["n"] += 1
        if send_count["n"] == 1:
            raise _RetryAfterError(1.0)
        return {"id": "msg-edit-ok"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        immediate_retry_delays=(0.0,),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()

        delivered = await manager.send_with_outbox(
            OutboxRecord(
                record_id="edit-1",
                channel_id="chan-1",
                message_id="msg-old",
                operation="send",
                payload_json={"content": "edited text"},
                created_at=now_iso(),
                operation_id="op-edit-1",
            )
        )
        assert delivered is True
        assert send_count["n"] == 2

        remaining = await store.list_outbox()
        assert len(remaining) == 0
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flush_skips_ready_older_record_when_newer_same_op_is_backed_off(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    clock = _Clock()
    sent: list[str] = []

    async def send_message(_channel_id: str, payload: dict) -> dict:
        sent.append(str(payload.get("content")))
        return {"id": f"msg-{len(sent)}"}

    manager = DiscordOutboxManager(
        store,
        send_message=send_message,
        logger=logging.getLogger("test"),
        immediate_retry_delays=(0.0,),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        await store.initialize()
        manager.start()
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="ready-old",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "old"},
                created_at="2026-01-01T00:00:00Z",
                operation_id="op-backoff",
            )
        )
        await store.enqueue_outbox(
            OutboxRecord(
                record_id="backoff-new",
                channel_id="chan-1",
                message_id=None,
                operation="send",
                payload_json={"content": "new"},
                created_at="2026-01-01T00:01:00Z",
                next_attempt_at="2026-01-01T00:02:00Z",
                operation_id="op-backoff",
            )
        )

        await manager._flush(await store.list_outbox())
        assert sent == []
    finally:
        await store.close()
