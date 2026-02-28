from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.pma_delivery_targets import (
    PmaDeliveryTargetsStore,
    target_key,
)


def _pma_dir(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma"


def _legacy_active_sink_path(hub_root: Path) -> Path:
    return _pma_dir(hub_root) / "active_sink.json"


def test_load_defaults_when_missing(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaDeliveryTargetsStore(hub_root)

    state = store.load()

    assert state["version"] == 1
    assert state["targets"] == []
    assert state["last_delivery_by_target"] == {}
    assert state["active_target_key"] is None
    assert not store.path.exists()


def test_load_reads_legacy_active_sink_v1_telegram_without_writing_new(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    legacy_path = _legacy_active_sink_path(hub_root)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "telegram",
                "chat_id": 123,
                "thread_id": 456,
                "updated_at": "2026-02-26T00:00:00Z",
                "last_delivery_turn_id": "turn-legacy",
            }
        ),
        encoding="utf-8",
    )

    store = PmaDeliveryTargetsStore(hub_root)
    state = store.load()

    assert state["targets"] == [
        {
            "kind": "chat",
            "platform": "telegram",
            "chat_id": "123",
            "thread_id": "456",
        }
    ]
    assert state["last_delivery_by_target"] == {"chat:telegram:123:456": "turn-legacy"}
    assert state["active_target_key"] == "chat:telegram:123:456"
    assert not store.path.exists()


def test_load_reads_legacy_active_sink_v2_chat_without_writing_new(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    legacy_path = _legacy_active_sink_path(hub_root)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "version": 2,
                "kind": "chat",
                "platform": "discord",
                "chat_id": "123456789012345678",
                "thread_id": "987654321",
                "conversation_key": "discord:channel:thread",
                "updated_at": "2026-02-26T00:00:00Z",
                "last_delivery_turn_id": "turn-legacy-v2",
            }
        ),
        encoding="utf-8",
    )

    store = PmaDeliveryTargetsStore(hub_root)
    state = store.load()

    assert state["targets"] == [
        {
            "kind": "chat",
            "platform": "discord",
            "chat_id": "123456789012345678",
            "thread_id": "987654321",
            "conversation_key": "discord:channel:thread",
        }
    ]
    assert state["last_delivery_by_target"] == {
        "chat:discord:123456789012345678:987654321": "turn-legacy-v2"
    }
    assert state["active_target_key"] == "chat:discord:123456789012345678:987654321"
    assert not store.path.exists()


def test_first_write_migrates_legacy_state_and_preserves_last_delivery(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    legacy_path = _legacy_active_sink_path(hub_root)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "version": 2,
                "kind": "chat",
                "platform": "discord",
                "chat_id": "111",
                "updated_at": "2026-02-26T00:00:00Z",
                "last_delivery_turn_id": "turn-keep",
            }
        ),
        encoding="utf-8",
    )

    store = PmaDeliveryTargetsStore(hub_root)
    store.set_targets([{"kind": "chat", "platform": "discord", "chat_id": "111"}])
    state = store.load()

    assert store.path.exists()
    assert state["targets"] == [
        {"kind": "chat", "platform": "discord", "chat_id": "111"}
    ]
    assert state["last_delivery_by_target"] == {"chat:discord:111": "turn-keep"}
    assert state["active_target_key"] == "chat:discord:111"
    assert legacy_path.exists()


def test_add_and_remove_targets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaDeliveryTargetsStore(hub_root)

    store.add_target({"kind": "web"})
    store.add_target({"kind": "chat", "platform": "discord", "chat_id": "123"})
    state = store.load()

    assert [target_key(target) for target in state["targets"]] == [
        "web",
        "chat:discord:123",
    ]

    assert store.remove_target("chat:discord:123") is True
    assert store.remove_target("chat:discord:does-not-exist") is False
    state = store.load()
    assert state["targets"] == [{"kind": "web"}]
    assert state["active_target_key"] == "web"


def test_mark_delivered_is_per_target_and_deduped(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaDeliveryTargetsStore(hub_root)
    store.set_targets(
        [
            {"kind": "chat", "platform": "discord", "chat_id": "123"},
            {
                "kind": "chat",
                "platform": "telegram",
                "chat_id": "456",
                "thread_id": "7",
            },
        ]
    )

    assert store.mark_delivered("chat:discord:123", "turn-1") is True
    assert store.mark_delivered("chat:discord:123", "turn-1") is False
    assert store.mark_delivered("chat:telegram:456:7", "turn-1") is True
    assert store.mark_delivered("chat:unknown:999", "turn-1") is False

    state = store.load()
    assert state["last_delivery_by_target"] == {
        "chat:discord:123": "turn-1",
        "chat:telegram:456:7": "turn-1",
    }


def test_set_active_target_without_reordering(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaDeliveryTargetsStore(hub_root)
    store.set_targets(
        [
            {"kind": "web"},
            {"kind": "chat", "platform": "discord", "chat_id": "123"},
            {"kind": "chat", "platform": "telegram", "chat_id": "456"},
        ]
    )
    before = list(store.load()["targets"])
    assert store.set_active_target("chat:telegram:456") is True
    state = store.load()
    assert state["targets"] == before
    assert state["active_target_key"] == "chat:telegram:456"
    assert store.get_active_target_key() == "chat:telegram:456"
    assert store.get_active_target() == {
        "kind": "chat",
        "platform": "telegram",
        "chat_id": "456",
    }
