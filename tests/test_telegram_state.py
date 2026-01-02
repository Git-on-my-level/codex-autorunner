import asyncio
from pathlib import Path

from codex_autorunner.telegram_state import (
    APPROVAL_MODE_SAFE,
    APPROVAL_MODE_YOLO,
    PendingApprovalRecord,
    TelegramStateStore,
    TopicQueue,
    parse_topic_key,
    topic_key,
)


def test_topic_key_roundtrip() -> None:
    key = topic_key(-100, None)
    assert key == "-100:root"
    chat_id, thread_id, scope = parse_topic_key(key)
    assert chat_id == -100
    assert thread_id is None
    assert scope is None


def test_topic_key_scope_roundtrip() -> None:
    key = topic_key(123, 55, scope="repo-1")
    chat_id, thread_id, scope = parse_topic_key(key)
    assert chat_id == 123
    assert thread_id == 55
    assert scope == "repo-1"


def test_state_store_roundtrip(tmp_path: Path) -> None:
    state_path = tmp_path / "telegram_state.json"
    store = TelegramStateStore(state_path)
    key = topic_key(123, 55)
    record = store.bind_topic(key, "/tmp/repo", repo_id="repo-1")
    assert record.workspace_path == "/tmp/repo"
    assert record.repo_id == "repo-1"
    record = store.set_active_thread(key, "thread-1")
    assert record.active_thread_id == "thread-1"
    record = store.set_approval_mode(key, APPROVAL_MODE_SAFE)
    assert record.approval_mode == APPROVAL_MODE_SAFE
    loaded = store.get_topic(key)
    assert loaded is not None
    assert loaded.approval_mode == APPROVAL_MODE_SAFE
    assert loaded.workspace_path == "/tmp/repo"


def test_state_store_defaults(tmp_path: Path) -> None:
    state_path = tmp_path / "telegram_state.json"
    store = TelegramStateStore(state_path, default_approval_mode=APPROVAL_MODE_SAFE)
    key = topic_key(321, 77)
    record = store.set_active_thread(key, "thread-2")
    assert record.approval_mode == APPROVAL_MODE_SAFE
    record = store.set_approval_mode(key, "invalid-mode")
    assert record.approval_mode == APPROVAL_MODE_SAFE
    record = store.set_approval_mode(key, APPROVAL_MODE_YOLO)
    assert record.approval_mode == APPROVAL_MODE_YOLO


def test_state_store_ensure_topic(tmp_path: Path) -> None:
    state_path = tmp_path / "telegram_state.json"
    store = TelegramStateStore(state_path, default_approval_mode=APPROVAL_MODE_SAFE)
    key = topic_key(444, None)
    record = store.ensure_topic(key)
    assert record.workspace_path is None
    assert record.approval_mode == APPROVAL_MODE_SAFE
    loaded = store.get_topic(key)
    assert loaded is not None


def test_topic_queue_serializes() -> None:
    async def runner() -> tuple[int, list[str]]:
        queue = TopicQueue()
        active = 0
        max_active = 0

        async def work(label: str) -> str:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return label

        tasks = [
            asyncio.create_task(queue.enqueue(lambda label=label: work(label)))
            for label in ("a", "b", "c")
        ]
        results = await asyncio.gather(*tasks)
        await queue.close()
        return max_active, results

    max_active, results = asyncio.run(runner())
    assert max_active == 1
    assert sorted(results) == ["a", "b", "c"]


def test_state_store_pending_approvals(tmp_path: Path) -> None:
    state_path = tmp_path / "telegram_state.json"
    store = TelegramStateStore(state_path)
    record = PendingApprovalRecord(
        request_id="req-1",
        turn_id="turn-1",
        chat_id=123,
        thread_id=45,
        message_id=67,
        prompt="Approve command?",
        created_at="2026-01-01T00:00:00Z",
    )
    store.upsert_pending_approval(record)
    pending = store.pending_approvals_for_topic(123, 45)
    assert len(pending) == 1
    assert pending[0].request_id == "req-1"
    store.clear_pending_approval("req-1")
    assert store.pending_approvals_for_topic(123, 45) == []
