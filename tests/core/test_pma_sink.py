from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.pma_delivery_targets import PmaDeliveryTargetsStore
from codex_autorunner.core.pma_sink import PmaActiveSinkStore


def test_set_chat_writes_version2_chat_payload(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaActiveSinkStore(hub_root)
    payload = store.set_chat(
        "discord",
        chat_id="123456789012345678",
        thread_id="9876543210",
        conversation_key="discord:channel:thread",
    )
    loaded = store.load()
    assert loaded == payload
    assert payload["version"] == 2
    assert payload["kind"] == "chat"
    assert payload["platform"] == "discord"
    assert payload["chat_id"] == "123456789012345678"
    assert payload["thread_id"] == "9876543210"
    assert payload["conversation_key"] == "discord:channel:thread"
    assert payload.get("last_delivery_turn_id") is None
    assert isinstance(payload.get("updated_at"), str)


def test_set_chat_preserves_last_delivery_turn_id(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaActiveSinkStore(hub_root)
    store.set_telegram(chat_id=111, thread_id=222)
    assert store.mark_delivered("turn-42") is True

    payload = store.set_chat("telegram", chat_id="111", thread_id="222")
    assert payload["last_delivery_turn_id"] == "turn-42"


def test_set_chat_resets_last_delivery_when_target_changes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaActiveSinkStore(hub_root)
    store.set_chat("discord", chat_id="111")
    assert store.mark_delivered("turn-42") is True

    payload = store.set_chat("discord", chat_id="222")
    assert payload["last_delivery_turn_id"] is None


def test_set_telegram_preserves_last_delivery_for_same_target(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaActiveSinkStore(hub_root)
    store.set_telegram(chat_id=111, thread_id=222)
    assert store.mark_delivered("turn-42") is True

    payload = store.set_telegram(chat_id=111, thread_id=222)
    assert payload["last_delivery_turn_id"] == "turn-42"


def test_set_web_preserves_non_web_targets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    local_path = hub_root / ".codex-autorunner" / "pma" / "local_sink.jsonl"
    targets_store = PmaDeliveryTargetsStore(hub_root)
    targets_store.set_targets(
        [
            {"kind": "chat", "platform": "telegram", "chat_id": "111"},
            {"kind": "local", "path": str(local_path)},
        ]
    )
    assert targets_store.mark_delivered("chat:telegram:111", "turn-42") is True

    payload = PmaActiveSinkStore(hub_root).set_web()
    assert payload["kind"] == "web"

    state = targets_store.load()
    assert state["targets"] == [
        {"kind": "web"},
        {"kind": "chat", "platform": "telegram", "chat_id": "111"},
        {"kind": "local", "path": str(local_path)},
    ]
    assert state["last_delivery_by_target"]["chat:telegram:111"] == "turn-42"


def test_set_chat_preserves_non_chat_targets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    local_path = hub_root / ".codex-autorunner" / "pma" / "local_sink.jsonl"
    targets_store = PmaDeliveryTargetsStore(hub_root)
    targets_store.set_targets(
        [
            {"kind": "chat", "platform": "discord", "chat_id": "old-channel"},
            {"kind": "local", "path": str(local_path)},
            {"kind": "web"},
        ]
    )

    payload = PmaActiveSinkStore(hub_root).set_chat("discord", chat_id="new-channel")
    assert payload["kind"] == "chat"
    assert payload["platform"] == "discord"
    assert payload["chat_id"] == "new-channel"

    state = targets_store.load()
    assert state["targets"] == [
        {"kind": "chat", "platform": "discord", "chat_id": "new-channel"},
        {"kind": "local", "path": str(local_path)},
        {"kind": "web"},
    ]


def test_set_telegram_preserves_non_chat_targets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    local_path = hub_root / ".codex-autorunner" / "pma" / "local_sink.jsonl"
    targets_store = PmaDeliveryTargetsStore(hub_root)
    targets_store.set_targets(
        [
            {"kind": "chat", "platform": "discord", "chat_id": "old-channel"},
            {"kind": "local", "path": str(local_path)},
            {"kind": "web"},
        ]
    )

    payload = PmaActiveSinkStore(hub_root).set_telegram(chat_id=111, thread_id=222)
    assert payload["kind"] == "telegram"
    assert payload["chat_id"] == 111
    assert payload["thread_id"] == 222

    state = targets_store.load()
    assert state["targets"] == [
        {
            "kind": "chat",
            "platform": "telegram",
            "chat_id": "111",
            "thread_id": "222",
        },
        {"kind": "local", "path": str(local_path)},
        {"kind": "web"},
    ]
