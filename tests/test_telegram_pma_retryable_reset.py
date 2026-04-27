from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.core.hub_control_plane import HubControlPlaneError
from codex_autorunner.core.managed_thread_identity import AppServerThreadRegistry
from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as execution_commands_module,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord
from tests.telegram_pma_workspace_support import _PMAWorkspaceHandler


@pytest.mark.anyio
async def test_pma_new_reports_retryable_hub_reset_failure(tmp_path: Path) -> None:
    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None, agent="codex")
    handler = _PMAWorkspaceHandler(record, registry, hub_root=tmp_path)
    handler._config = SimpleNamespace(require_topics=False, root=tmp_path)
    handler._spawn_task = lambda coro: None

    async def _failing_reset_telegram_thread_binding(
        _handlers: Any, **kwargs: Any
    ) -> tuple[bool, str]:
        _ = kwargs
        raise HubControlPlaneError(
            "hub_unavailable",
            "Hub control-plane unavailable during create_thread_target: request timed out after 10s",
            retryable=True,
        )

    original_reset = execution_commands_module._reset_telegram_thread_binding
    execution_commands_module._reset_telegram_thread_binding = (
        _failing_reset_telegram_thread_binding
    )
    try:
        message = TelegramMessage(
            update_id=1,
            message_id=2,
            chat_id=-2002,
            thread_id=333,
            from_user_id=99,
            text="/new",
            date=None,
            is_topic_message=True,
        )

        await handler._handle_new(message)
    finally:
        execution_commands_module._reset_telegram_thread_binding = original_reset

    assert (
        handler._sent
        and handler._sent[-1]
        == "PMA session reset is temporarily unavailable while the hub control plane recovers. Retry `/new` in a few seconds."
    )
