from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.orchestration.runtime_bindings import (
    clear_runtime_thread_binding,
)
from tests import telegram_pma_managed_thread_support as support

_SESSION_NOTICE = "Notice: I started a new live session for this conversation."


@pytest.mark.anyio
async def test_repo_turn_notifies_user_when_runtime_binding_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support.patch_sqlite_connection_cache(monkeypatch)

    record = support.TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="codex",
    )
    handler = support._ManagedThreadPMAHandler(record, tmp_path)

    harness = support._SessionRecoveryFakeHarness(thread_prefix="repo-backend-thread")
    support.patch_registered_agents(monkeypatch, harness)

    first_message = support.TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="first repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )
    second_message = support.TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="second repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(first_message, runtime=support._RuntimeStub())

    orchestration_service = (
        support.execution_commands_module._build_telegram_thread_orchestration_service(
            handler
        )
    )
    binding = orchestration_service.get_binding(
        surface_kind="telegram",
        surface_key="-1001:101",
    )
    assert binding is not None
    clear_runtime_thread_binding(tmp_path, binding.thread_target_id)

    await handler._handle_normal_message(second_message, runtime=support._RuntimeStub())

    assert harness.start_calls[0] == (
        "repo-backend-thread-1",
        "first repo orchestration prompt",
    )
    assert harness.start_calls[1][0] == "repo-backend-thread-2"
    assert harness.start_calls[1][1] == "second repo orchestration prompt"
    assert handler._sent[-1].startswith(_SESSION_NOTICE) or (
        any(s.startswith(_SESSION_NOTICE) for s in handler._sent)
    )
    assert "reply for repo-backend-thread-2:turn-2" in "".join(handler._sent)
    assert record.active_thread_id == "repo-backend-thread-2"
