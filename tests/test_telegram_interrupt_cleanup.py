from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import pytest

from codex_autorunner.integrations.telegram.handlers.commands.shared import (
    SharedHelpers,
)


class _InterruptCleanupStub(SharedHelpers):
    def __init__(self, *, delete_result: bool) -> None:
        self.delete_result = delete_result
        self.deleted: list[tuple[int, int]] = []
        self.edited: list[tuple[int, int, str]] = []

    async def _delete_message(
        self, chat_id: int, message_id: Optional[int], thread_id: Optional[int] = None
    ) -> bool:
        _ = thread_id
        if message_id is None:
            return False
        self.deleted.append((chat_id, message_id))
        return self.delete_result

    async def _edit_message_text(
        self, chat_id: int, message_id: int, text: str
    ) -> bool:
        self.edited.append((chat_id, message_id, text))
        return True


@pytest.mark.anyio
async def test_clear_interrupt_status_message_prefers_delete() -> None:
    handler = _InterruptCleanupStub(delete_result=True)
    runtime = SimpleNamespace(interrupt_message_id=77, interrupt_turn_id="turn-1")

    await handler._clear_interrupt_status_message(
        chat_id=123,
        runtime=runtime,
        turn_id="turn-1",
        fallback_text="Interrupted.",
    )

    assert handler.deleted == [(123, 77)]
    assert handler.edited == []
    assert runtime.interrupt_message_id is None
    assert runtime.interrupt_turn_id is None


@pytest.mark.anyio
async def test_clear_interrupt_status_message_falls_back_to_edit() -> None:
    handler = _InterruptCleanupStub(delete_result=False)
    runtime = SimpleNamespace(interrupt_message_id=88, interrupt_turn_id="turn-2")

    await handler._clear_interrupt_status_message(
        chat_id=456,
        runtime=runtime,
        turn_id="turn-2",
        fallback_text="Interrupt requested; turn completed.",
    )

    assert handler.deleted == [(456, 88)]
    assert handler.edited == [(456, 88, "Interrupt requested; turn completed.")]
    assert runtime.interrupt_message_id is None
    assert runtime.interrupt_turn_id is None
