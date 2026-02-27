from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from codex_autorunner.core.pma_delivery_targets import PmaDeliveryTargetsStore
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
async def test_pma_delivery_telegram_chunking_has_no_part_prefix(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    sink_store = PmaActiveSinkStore(hub_root)
    sink_store.set_telegram(chat_id=123, thread_id=456)

    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text=("telegram chunk " * 500),
        turn_id="turn-1b",
        lifecycle_event={"event_type": "flow_completed"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
    )
    assert delivered is True

    store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
    try:
        outbox = await store.list_outbox()
    finally:
        await store.close()
    assert len(outbox) >= 2
    assert all(not record.text.startswith("Part ") for record in outbox)


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
async def test_pma_delivery_discord_chunking_has_no_part_prefix(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    sink_store = PmaActiveSinkStore(hub_root)
    sink_store.set_chat(
        "discord",
        chat_id="123456789012345678",
    )
    discord_state_path = hub_root / ".codex-autorunner" / "discord_state.sqlite3"

    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text=("discord chunk " * 500),
        turn_id="turn-2b",
        lifecycle_event={"event_type": "flow_paused"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=discord_state_path,
    )
    assert delivered is True

    store = DiscordStateStore(discord_state_path)
    try:
        await store.initialize()
        outbox = await store.list_outbox()
    finally:
        await store.close()
    assert len(outbox) >= 2
    for record in outbox:
        content = str(record.payload_json.get("content", ""))
        assert not content.startswith("Part ")


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


@pytest.mark.anyio
async def test_pma_delivery_fanout_telegram_and_discord(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hub_root = tmp_path / "hub"
    targets = PmaDeliveryTargetsStore(hub_root)
    targets.set_targets(
        [
            {
                "kind": "chat",
                "platform": "telegram",
                "chat_id": "123",
                "thread_id": "456",
            },
            {"kind": "chat", "platform": "discord", "chat_id": "987654321012345678"},
        ]
    )

    caplog.set_level(logging.INFO)
    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="fanout hello",
        turn_id="turn-fanout",
        lifecycle_event={"event_type": "flow_completed"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )
    assert delivered is True
    assert "pma.delivery.multi_target" in caplog.text

    telegram_store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
    discord_store = DiscordStateStore(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    try:
        telegram_outbox = await telegram_store.list_outbox()
        await discord_store.initialize()
        discord_outbox = await discord_store.list_outbox()
    finally:
        await telegram_store.close()
        await discord_store.close()

    assert len(telegram_outbox) == 1
    assert len(discord_outbox) == 1
    assert telegram_outbox[0].chat_id == 123
    assert telegram_outbox[0].thread_id == 456
    assert discord_outbox[0].channel_id == "987654321012345678"


@pytest.mark.anyio
async def test_pma_delivery_local_target_writes_jsonl(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    local_path = hub_root / ".codex-autorunner" / "pma" / "local_outbox.jsonl"
    PmaDeliveryTargetsStore(hub_root).set_targets(
        [
            {"kind": "local", "path": str(local_path)},
        ]
    )

    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="local sink output",
        turn_id="turn-local",
        lifecycle_event={"event_type": "flow_completed"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )
    assert delivered is True
    assert local_path.exists()

    raw_lines = [
        line.strip()
        for line in local_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(raw_lines) == 1
    payload = json.loads(raw_lines[0])
    assert isinstance(payload.get("ts"), str)
    assert payload["turn_id"] == "turn-local"
    assert payload["event_type"] == "flow_completed"
    assert payload["text_preview"] == "local sink output"
    assert payload["text_bytes"] == len("local sink output".encode("utf-8"))


@pytest.mark.anyio
async def test_pma_delivery_web_target_is_noop(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    PmaDeliveryTargetsStore(hub_root).set_targets([{"kind": "web"}])

    telegram_store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
    discord_store = DiscordStateStore(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    try:
        delivered = await deliver_pma_output_to_active_sink(
            hub_root=hub_root,
            assistant_text="web sink output",
            turn_id="turn-web-no-op",
            lifecycle_event={"event_type": "flow_completed"},
            telegram_state_path=hub_root / "telegram_state.sqlite3",
            discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        )
    finally:
        try:
            telegram_outbox = await telegram_store.list_outbox()
        finally:
            await telegram_store.close()
        try:
            await discord_store.initialize()
            discord_outbox = await discord_store.list_outbox()
        finally:
            await discord_store.close()

    assert delivered is True
    assert telegram_outbox == []
    assert discord_outbox == []
    state = PmaDeliveryTargetsStore(hub_root).load()
    assert state["last_delivery_by_target"] == {"web": "turn-web-no-op"}


@pytest.mark.anyio
async def test_pma_delivery_partial_failure_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    targets = PmaDeliveryTargetsStore(hub_root)
    targets.set_targets(
        [
            {"kind": "chat", "platform": "discord", "chat_id": "111"},
            {"kind": "chat", "platform": "telegram", "chat_id": "222"},
        ]
    )

    original_discord_enqueue = DiscordStateStore.enqueue_outbox

    async def _fail_discord_enqueue(self, record):
        raise RuntimeError("discord write failed")

    monkeypatch.setattr(DiscordStateStore, "enqueue_outbox", _fail_discord_enqueue)

    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="partial failure content",
        turn_id="turn-fanout-partial",
        lifecycle_event={"event_type": "flow_completed"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )
    assert delivered is False

    delivery_state = targets.load()
    assert delivery_state["last_delivery_by_target"] == {
        "chat:telegram:222": "turn-fanout-partial"
    }

    monkeypatch.setattr(DiscordStateStore, "enqueue_outbox", original_discord_enqueue)
    retry_delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="partial failure content",
        turn_id="turn-fanout-partial",
        lifecycle_event={"event_type": "flow_completed"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )
    assert retry_delivered is True

    telegram_store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
    discord_store = DiscordStateStore(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    try:
        await discord_store.initialize()
        telegram_outbox = await telegram_store.list_outbox()
        discord_outbox = await discord_store.list_outbox()
    finally:
        await telegram_store.close()
        await discord_store.close()

    assert len(telegram_outbox) == 1
    assert telegram_outbox[0].chat_id == 222
    assert len(discord_outbox) == 1
    assert discord_outbox[0].channel_id == "111"

    delivery_state = targets.load()
    assert delivery_state["last_delivery_by_target"] == {
        "chat:discord:111": "turn-fanout-partial",
        "chat:telegram:222": "turn-fanout-partial",
    }
