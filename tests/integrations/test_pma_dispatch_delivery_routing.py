from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.pma_delivery_targets import PmaDeliveryTargetsStore
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.pma_delivery import (
    deliver_pma_dispatches_to_delivery_targets,
)
from codex_autorunner.integrations.telegram.state import TelegramStateStore


def _dispatch_record(dispatch_id: str) -> dict[str, object]:
    return {
        "dispatch_id": dispatch_id,
        "title": "Needs review",
        "body": "Please check this run.",
        "priority": "action",
        "links": [{"label": "Run", "href": "https://example.test/run"}],
    }


@pytest.mark.anyio
async def test_pma_dispatch_delivery_returns_invalid_for_missing_turn_id(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"

    delivered = await deliver_pma_dispatches_to_delivery_targets(
        hub_root=hub_root,
        turn_id="",
        dispatches=[_dispatch_record("dispatch-1")],
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )

    assert delivered.ok is False
    assert delivered.status == "invalid"
    assert delivered.dispatch_count == 0


@pytest.mark.anyio
async def test_pma_dispatch_delivery_returns_invalid_for_bad_dispatch_payload(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"

    delivered = await deliver_pma_dispatches_to_delivery_targets(
        hub_root=hub_root,
        turn_id="turn-dispatch-invalid",
        dispatches=[{"title": "missing id"}],
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )

    assert delivered.ok is False
    assert delivered.status == "invalid"
    assert delivered.dispatch_count == 0


@pytest.mark.anyio
async def test_pma_dispatch_delivery_returns_no_targets_with_valid_dispatch(
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
            {"kind": "web"},
        ]
    )

    delivered = await deliver_pma_dispatches_to_delivery_targets(
        hub_root=hub_root,
        turn_id="turn-dispatch-no-targets",
        dispatches=[_dispatch_record("dispatch-1")],
        telegram_state_path=hub_root / "telegram_state.sqlite3",
        discord_state_path=hub_root / ".codex-autorunner" / "discord_state.sqlite3",
    )

    assert delivered.ok is False
    assert delivered.status == "no_targets"
    assert delivered.dispatch_count == 1
    assert delivered.configured_targets == 0
    assert delivered.delivered_targets == 0
    assert delivered.failed_targets == 0


@pytest.mark.anyio
async def test_pma_dispatch_delivery_no_targets_has_no_outbox_side_effects(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    PmaDeliveryTargetsStore(hub_root).set_targets(
        [{"kind": "chat", "platform": "telegram", "chat_id": "123"}]
    )

    delivered = await deliver_pma_dispatches_to_delivery_targets(
        hub_root=hub_root,
        turn_id="turn-dispatch-no-effects",
        dispatches=[_dispatch_record("dispatch-2")],
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
