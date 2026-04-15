from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.core.orchestration.chat_operation_ledger import (
    SQLiteChatOperationLedger,
)
from codex_autorunner.core.orchestration.chat_operation_state import (
    ChatOperationState,
)
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as execution_commands_module,
)
from codex_autorunner.integrations.telegram.state_types import TelegramTopicRecord
from tests.telegram_pma_routing_support import _ManagedThreadPMAHandler


@pytest.mark.anyio
async def test_pma_interrupt_reports_already_finished_when_turn_is_no_longer_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    handler = _ManagedThreadPMAHandler(record, tmp_path)
    handler._chat_operation_store = SQLiteChatOperationLedger(tmp_path)
    handler._current_chat_operation_id = lambda: "op-finished"  # type: ignore[method-assign]
    handler._chat_operation_store.register_operation(
        operation_id="op-finished",
        surface_kind="telegram",
        surface_operation_key="op-finished",
        state=ChatOperationState.RECEIVED,
    )

    class _FakeThreadService:
        def get_running_execution(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return None

        async def stop_thread(self, thread_target_id: str, **kwargs: Any) -> Any:
            assert thread_target_id == "thread-1"
            _ = kwargs
            return SimpleNamespace(
                interrupted_active=False,
                recovered_lost_backend=False,
                cancelled_queued=0,
                execution=None,
            )

    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="pma"),
            SimpleNamespace(thread_target_id="thread-1"),
        ),
    )

    await handler._process_interrupt(
        chat_id=-1001,
        thread_id=101,
        reply_to=10,
        runtime=SimpleNamespace(
            current_turn_id=None,
            interrupt_requested=False,
            interrupt_turn_id=None,
        ),
        message_id=100,
    )

    assert handler._sent == ["Active PMA turn already finished."]
