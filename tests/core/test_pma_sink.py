from __future__ import annotations

from pathlib import Path

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
