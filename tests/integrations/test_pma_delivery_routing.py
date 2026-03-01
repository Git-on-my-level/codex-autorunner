from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.pma_delivery_targets import PmaDeliveryTargetsStore
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.pma_delivery import deliver_pma_output_to_active_sink
from codex_autorunner.integrations.telegram.state import TelegramStateStore


@pytest.mark.anyio
async def test_pma_delivery_returns_no_content_for_blank_text(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"

    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="   ",
        turn_id="turn-blank",
        lifecycle_event={"event_type": "flow_completed"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )

    assert delivered.ok is False
    assert delivered.status == "no_content"
    assert delivered.configured_targets == 0
    assert delivered.delivered_targets == 0
    assert delivered.failed_targets == 0


@pytest.mark.anyio
async def test_pma_delivery_returns_invalid_for_missing_turn_id(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"

    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="hello",
        turn_id="",
        lifecycle_event={"event_type": "flow_completed"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )

    assert delivered.ok is False
    assert delivered.status == "invalid"
    assert delivered.configured_targets == 0
    assert delivered.delivered_targets == 0
    assert delivered.failed_targets == 0


@pytest.mark.anyio
async def test_pma_delivery_returns_no_targets_even_with_legacy_target_state(
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
            {"kind": "chat", "platform": "discord", "chat_id": "789"},
            {"kind": "web"},
        ]
    )

    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="hello from pma",
        turn_id="turn-no-targets",
        lifecycle_event={"event_type": "flow_completed"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )

    assert delivered.ok is False
    assert delivered.status == "no_targets"
    assert delivered.configured_targets == 0
    assert delivered.delivered_targets == 0
    assert delivered.failed_targets == 0


@pytest.mark.anyio
async def test_pma_delivery_no_targets_has_no_outbox_side_effects(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    local_path = hub_root / ".codex-autorunner" / "pma" / "local_outbox.jsonl"
    PmaDeliveryTargetsStore(hub_root).set_targets(
        [{"kind": "local", "path": str(local_path)}]
    )

    delivered = await deliver_pma_output_to_active_sink(
        hub_root=hub_root,
        assistant_text="still no routing",
        turn_id="turn-no-effects",
        lifecycle_event={"event_type": "flow_completed"},
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )

    telegram_store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
    discord_store = DiscordStateStore(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    try:
        telegram_outbox = await telegram_store.list_outbox()
    finally:
        await telegram_store.close()
    try:
        await discord_store.initialize()
        discord_outbox = await discord_store.list_outbox()
    finally:
        await discord_store.close()

    assert delivered.status == "no_targets"
    assert telegram_outbox == []
    assert discord_outbox == []
    assert not local_path.exists()
