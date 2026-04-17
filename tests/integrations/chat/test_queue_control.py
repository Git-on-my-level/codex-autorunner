from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.integrations.chat.queue_control import ChatQueueControlStore


@pytest.mark.anyio
async def test_take_reset_requests_no_write_when_empty(tmp_path: Path) -> None:
    store = ChatQueueControlStore(tmp_path)
    commands_path = store._commands_path

    taken = store.take_reset_requests(platform="discord")
    assert taken == []
    assert (
        not commands_path.exists()
    ), "take_reset_requests should not create the file when there are no requests"


@pytest.mark.anyio
async def test_has_reset_requests_returns_false_when_no_file(tmp_path: Path) -> None:
    store = ChatQueueControlStore(tmp_path)
    assert store.has_reset_requests(platform="discord") is False


@pytest.mark.anyio
async def test_has_reset_requests_returns_true_when_pending(
    tmp_path: Path,
) -> None:
    store = ChatQueueControlStore(tmp_path)
    store.request_reset(
        conversation_id="discord:123:-",
        platform="discord",
        chat_id="123",
        thread_id=None,
        reason="stuck",
    )
    assert store.has_reset_requests(platform="discord") is True


@pytest.mark.anyio
async def test_has_reset_requests_filters_by_platform(tmp_path: Path) -> None:
    store = ChatQueueControlStore(tmp_path)
    store.request_reset(
        conversation_id="telegram:456:-",
        platform="telegram",
        chat_id="456",
        thread_id=None,
    )
    assert store.has_reset_requests(platform="discord") is False
    assert store.has_reset_requests(platform="telegram") is True


@pytest.mark.anyio
async def test_take_reset_requests_observes_promptly(tmp_path: Path) -> None:
    store = ChatQueueControlStore(tmp_path)
    store.request_reset(
        conversation_id="discord:789:-",
        platform="discord",
        chat_id="789",
        thread_id=None,
        requested_by="test",
    )

    taken = store.take_reset_requests(platform="discord")
    assert len(taken) == 1
    assert taken[0]["conversation_id"] == "discord:789:-"
    assert taken[0]["requested_by"] == "test"

    second = store.take_reset_requests(platform="discord")
    assert second == []


@pytest.mark.anyio
async def test_take_reset_requests_preserves_other_platform(
    tmp_path: Path,
) -> None:
    store = ChatQueueControlStore(tmp_path)
    store.request_reset(
        conversation_id="discord:100:-",
        platform="discord",
        chat_id="100",
        thread_id=None,
    )
    store.request_reset(
        conversation_id="telegram:200:-",
        platform="telegram",
        chat_id="200",
        thread_id=None,
    )

    taken = store.take_reset_requests(platform="discord")
    assert len(taken) == 1
    assert taken[0]["conversation_id"] == "discord:100:-"

    assert store.has_reset_requests(platform="telegram") is True
    remaining = store.take_reset_requests(platform="telegram")
    assert len(remaining) == 1
    assert remaining[0]["conversation_id"] == "telegram:200:-"

    final = store.take_reset_requests()
    assert final == []
