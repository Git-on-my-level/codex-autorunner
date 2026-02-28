from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.core.pma_delivery_targets import PmaDeliveryTargetsStore
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.pma_delivery import (
    deliver_pma_dispatches_to_delivery_targets,
)
from codex_autorunner.integrations.telegram.state import TelegramStateStore


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _dispatch_record(dispatch_id: str) -> dict[str, object]:
    return {
        "dispatch_id": dispatch_id,
        "title": "Needs review",
        "body": "Please check this run.",
        "priority": "action",
        "links": [{"label": "Run", "href": "https://example.test/run"}],
    }


@pytest.mark.anyio
async def test_pma_dispatch_delivery_fanout_telegram_and_discord(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    PmaDeliveryTargetsStore(hub_root).set_targets(
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
    state_path = hub_root / ".codex-autorunner" / "discord_state.sqlite3"

    delivered = await deliver_pma_dispatches_to_delivery_targets(
        hub_root=hub_root,
        turn_id="turn-dispatch-1",
        dispatches=[_dispatch_record("dispatch-1")],
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=state_path,
    )
    assert delivered is True

    telegram_store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
    discord_store = DiscordStateStore(state_path)
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
    assert (
        telegram_outbox[0].record_id
        == "pma-dispatch:dispatch-1:chat:telegram:123:456:1"
    )
    assert telegram_outbox[0].outbox_key == telegram_outbox[0].record_id
    assert discord_outbox[0].channel_id == "987654321012345678"
    assert (
        discord_outbox[0].record_id
        == "pma-dispatch:dispatch-1:chat:discord:987654321012345678:1"
    )


@pytest.mark.anyio
async def test_pma_dispatch_delivery_fanout_two_telegram_targets(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    PmaDeliveryTargetsStore(hub_root).set_targets(
        [
            {
                "kind": "chat",
                "platform": "telegram",
                "chat_id": "123",
                "thread_id": "456",
            },
            {
                "kind": "chat",
                "platform": "telegram",
                "chat_id": "123",
                "thread_id": "789",
            },
        ]
    )

    delivered = await deliver_pma_dispatches_to_delivery_targets(
        hub_root=hub_root,
        turn_id="turn-dispatch-2",
        dispatches=[_dispatch_record("dispatch-2")],
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )
    assert delivered is True

    telegram_store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
    try:
        outbox = await telegram_store.list_outbox()
    finally:
        await telegram_store.close()

    assert len(outbox) == 2
    assert {(record.chat_id, record.thread_id) for record in outbox} == {
        (123, 456),
        (123, 789),
    }
    assert len({record.record_id for record in outbox}) == 2
    assert {record.record_id for record in outbox} == {
        "pma-dispatch:dispatch-2:chat:telegram:123:456:1",
        "pma-dispatch:dispatch-2:chat:telegram:123:789:1",
    }


@pytest.mark.anyio
async def test_pma_dispatch_delivery_local_target_writes_jsonl(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    local_path = hub_root / ".codex-autorunner" / "pma" / "dispatch_local.jsonl"
    PmaDeliveryTargetsStore(hub_root).set_targets(
        [
            {"kind": "local", "path": str(local_path)},
        ]
    )

    delivered = await deliver_pma_dispatches_to_delivery_targets(
        hub_root=hub_root,
        turn_id="turn-dispatch-local",
        dispatches=[_dispatch_record("dispatch-local-1")],
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )
    assert delivered is True

    rows = _read_jsonl(local_path)
    assert len(rows) == 1
    payload = rows[0]
    assert isinstance(payload.get("ts"), str)
    assert payload["kind"] == "dispatch"
    assert payload["dispatch_id"] == "dispatch-local-1"
    assert payload["turn_id"] == "turn-dispatch-local"
    assert payload["target"] == f"local:{local_path}"
    assert payload["chunk_count"] == 1


@pytest.mark.anyio
async def test_pma_dispatch_delivery_invalid_telegram_thread_id_fails(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    PmaDeliveryTargetsStore(hub_root).set_targets(
        [
            {
                "kind": "chat",
                "platform": "telegram",
                "chat_id": "123",
                "thread_id": "invalid-thread",
            }
        ]
    )

    delivered = await deliver_pma_dispatches_to_delivery_targets(
        hub_root=hub_root,
        turn_id="turn-dispatch-invalid-thread",
        dispatches=[_dispatch_record("dispatch-invalid-thread-1")],
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )
    assert delivered is False

    telegram_store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
    try:
        outbox = await telegram_store.list_outbox()
    finally:
        await telegram_store.close()
    assert outbox == []


@pytest.mark.anyio
async def test_pma_dispatch_delivery_invalid_local_target_fails(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    outside_path = tmp_path / "outside-dispatch-target.jsonl"
    fallback_path = hub_root / ".codex-autorunner" / "pma" / "dispatch_deliveries.jsonl"
    PmaDeliveryTargetsStore(hub_root).set_targets(
        [
            {"kind": "local", "path": str(outside_path)},
        ]
    )

    delivered = await deliver_pma_dispatches_to_delivery_targets(
        hub_root=hub_root,
        turn_id="turn-dispatch-local-invalid",
        dispatches=[_dispatch_record("dispatch-local-invalid-1")],
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )
    assert delivered is False
    assert not outside_path.exists()
    assert not fallback_path.exists()


@pytest.mark.anyio
async def test_pma_dispatch_delivery_web_target_is_noop(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    PmaDeliveryTargetsStore(hub_root).set_targets([{"kind": "web"}])

    delivered = await deliver_pma_dispatches_to_delivery_targets(
        hub_root=hub_root,
        turn_id="turn-dispatch-web",
        dispatches=[_dispatch_record("dispatch-web-1")],
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )
    assert delivered is True

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
    assert telegram_outbox == []
    assert discord_outbox == []
