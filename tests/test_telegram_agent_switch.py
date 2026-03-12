from __future__ import annotations

import logging

import pytest

from codex_autorunner.integrations.telegram.handlers.commands.workspace import (
    WorkspaceCommands,
)
from codex_autorunner.integrations.telegram.state import (
    TelegramTopicRecord,
    ThreadSummary,
)


class _RouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self.record = record

    async def update_topic(
        self, _chat_id: int, _thread_id: int | None, apply
    ) -> TelegramTopicRecord:
        apply(self.record)
        return self.record


class _AgentSwitchHandler(WorkspaceCommands):
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._logger = logging.getLogger("test")
        self._router = _RouterStub(record)


@pytest.mark.anyio
async def test_apply_agent_change_resets_runtime_state_and_applies_default_model() -> (
    None
):
    record = TelegramTopicRecord(
        agent="codex",
        active_thread_id="thread-1",
        thread_ids=["thread-1", "thread-2"],
        thread_summaries={
            "thread-1": ThreadSummary(user_preview="hi", assistant_preview="hello")
        },
        pending_compact_seed="seed",
        pending_compact_seed_thread_id="thread-1",
        model="gpt-5.4",
        effort="high",
    )
    handler = _AgentSwitchHandler(record)

    note = await handler._apply_agent_change(123, None, "opencode")

    assert note == ""
    assert record.agent == "opencode"
    assert record.active_thread_id is None
    assert record.thread_ids == []
    assert record.thread_summaries == {}
    assert record.pending_compact_seed is None
    assert record.pending_compact_seed_thread_id is None
    assert record.model == "zai-coding-plan/glm-5"
    assert record.effort is None
