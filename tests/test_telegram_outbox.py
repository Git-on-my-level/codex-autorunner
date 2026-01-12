import logging
from pathlib import Path
from typing import Optional

import pytest

from codex_autorunner.core.state import now_iso
from codex_autorunner.integrations.telegram import outbox as outbox_module
from codex_autorunner.integrations.telegram.outbox import TelegramOutboxManager
from codex_autorunner.integrations.telegram.state import (
    OutboxRecord,
    TelegramStateStore,
)


@pytest.mark.anyio
async def test_outbox_immediate_retry_respects_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "OUTBOX_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(outbox_module, "OUTBOX_IMMEDIATE_RETRY_DELAYS", [0, 0, 0])
    store = TelegramStateStore(tmp_path / "state.json")
    calls = {"count": 0}

    async def send_message(
        _chat_id: int,
        _text: str,
        *,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
    ) -> None:
        calls["count"] += 1
        raise RuntimeError("fail")

    async def edit_message_text(*_args, **_kwargs) -> bool:
        return False

    async def delete_message(*_args, **_kwargs) -> bool:
        return False

    manager = TelegramOutboxManager(
        store,
        send_message=send_message,
        edit_message_text=edit_message_text,
        delete_message=delete_message,
        logger=logging.getLogger("test"),
    )
    manager.start()
    record = OutboxRecord(
        record_id="r1",
        chat_id=123,
        thread_id=None,
        reply_to_message_id=None,
        placeholder_message_id=None,
        text="hello",
        created_at=now_iso(),
    )
    delivered = await manager.send_message_with_outbox(record)

    assert delivered is False
    assert calls["count"] == 2
    stored = store.get_outbox("r1")
    assert stored is not None
    assert stored.attempts == 2
