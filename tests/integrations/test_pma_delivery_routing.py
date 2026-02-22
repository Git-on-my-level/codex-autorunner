from __future__ import annotations

import logging
from pathlib import Path

import pytest

from codex_autorunner.core.pma_sink import PmaActiveSinkStore
from codex_autorunner.integrations.pma_delivery import deliver_pma_output_to_active_sink
from codex_autorunner.integrations.telegram.state import TelegramStateStore


@pytest.mark.anyio
async def test_pma_delivery_legacy_telegram_sink_still_enqueues_outbox(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    sink_store = PmaActiveSinkStore(hub_root)
    sink_store.set_telegram(chat_id=123, thread_id=456)

    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="hello from pma",
        turn_id="turn-1",
        lifecycle_event={"event_type": "flow_completed"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
    )
    assert delivered is True

    store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
    try:
        outbox = await store.list_outbox()
    finally:
        await store.close()
    assert len(outbox) == 1
    assert outbox[0].chat_id == 123
    assert outbox[0].thread_id == 456


@pytest.mark.anyio
async def test_pma_delivery_chat_discord_returns_false_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hub_root = tmp_path / "hub"
    sink_store = PmaActiveSinkStore(hub_root)
    sink_store.set_chat(
        "discord",
        chat_id="123456789012345678",
        thread_id="987654321",
        conversation_key="discord:123456789012345678:987654321",
    )

    caplog.set_level(logging.INFO)
    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="discord sink text",
        turn_id="turn-2",
        lifecycle_event={"event_type": "flow_paused"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
    )
    assert delivered is False
    assert "pma.delivery.discord_unavailable" in caplog.text

    store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
    try:
        outbox = await store.list_outbox()
    finally:
        await store.close()
    assert outbox == []
