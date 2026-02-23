from __future__ import annotations

import logging
from pathlib import Path

import pytest

from codex_autorunner.core.pma_sink import PmaActiveSinkStore
from codex_autorunner.integrations.discord.state import DiscordStateStore
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
async def test_pma_delivery_chat_discord_enqueues_to_discord_outbox(
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

    discord_state_path = hub_root / ".codex-autorunner" / "discord_state.sqlite3"

    caplog.set_level(logging.INFO)
    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="discord sink text",
        turn_id="turn-2",
        lifecycle_event={"event_type": "flow_paused"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=discord_state_path,
    )
    assert delivered is True
    assert "pma.delivery.discord" in caplog.text

    store = DiscordStateStore(discord_state_path)
    try:
        await store.initialize()
        outbox = await store.list_outbox()
    finally:
        await store.close()
    assert len(outbox) == 1
    assert outbox[0].channel_id == "123456789012345678"
    assert "discord sink text" in outbox[0].payload_json.get("content", "")


@pytest.mark.anyio
async def test_pma_delivery_telegram_outbox_empty_when_discord_target(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    sink_store = PmaActiveSinkStore(hub_root)
    sink_store.set_chat(
        "discord",
        chat_id="123456789012345678",
    )

    await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="discord only text",
        turn_id="turn-3",
        lifecycle_event={"event_type": "flow_paused"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )

    store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
    try:
        outbox = await store.list_outbox()
    finally:
        await store.close()
    assert outbox == []


@pytest.mark.anyio
async def test_pma_delivery_discord_allows_non_lifecycle_turns(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    sink_store = PmaActiveSinkStore(hub_root)
    sink_store.set_chat("discord", chat_id="123456789012345678")

    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="discord turn",
        turn_id="turn-user-1",
        lifecycle_event=None,
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )
    assert delivered is True

    store = DiscordStateStore(hub_root / ".codex-autorunner" / "discord_state.sqlite3")
    try:
        await store.initialize()
        outbox = await store.list_outbox()
    finally:
        await store.close()
    assert len(outbox) == 1
    assert outbox[0].channel_id == "123456789012345678"


@pytest.mark.anyio
async def test_pma_delivery_same_turn_allowed_after_sink_target_changes(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    sink_store = PmaActiveSinkStore(hub_root)
    state_path = hub_root / ".codex-autorunner" / "discord_state.sqlite3"

    sink_store.set_chat("discord", chat_id="111111111111111111")
    first = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="first channel",
        turn_id="turn-shared",
        lifecycle_event=None,
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=state_path,
    )
    assert first is True

    sink_store.set_chat("discord", chat_id="222222222222222222")
    second = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="second channel",
        turn_id="turn-shared",
        lifecycle_event=None,
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=state_path,
    )
    assert second is True

    store = DiscordStateStore(state_path)
    try:
        await store.initialize()
        outbox = await store.list_outbox()
    finally:
        await store.close()
    assert len(outbox) == 1
    assert outbox[0].channel_id == "222222222222222222"
    assert "second channel" in outbox[0].payload_json.get("content", "")
