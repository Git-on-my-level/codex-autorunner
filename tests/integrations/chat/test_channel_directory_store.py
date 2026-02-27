from __future__ import annotations

import json
from pathlib import Path

import pytest

import codex_autorunner.integrations.chat.channel_directory as channel_directory_module
from codex_autorunner.integrations.chat.channel_directory import (
    ChannelDirectoryStore,
    channel_entry_key,
)


def test_load_defaults_when_missing(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = ChannelDirectoryStore(hub_root)

    state = store.load()

    assert state["version"] == 1
    assert state["entries"] == []
    assert not store.path.exists()


def test_record_seen_is_idempotent_for_same_channel_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        channel_directory_module,
        "now_iso",
        lambda: "2026-02-27T10:00:00Z",
    )
    store = ChannelDirectoryStore(tmp_path / "hub")

    store.record_seen(
        "telegram",
        123,
        None,
        "Main Room",
        {"chat_type": "group"},
    )
    store.record_seen(
        "telegram",
        123,
        None,
        "Main Room",
        {"chat_type": "group"},
    )

    state = store.load()
    assert len(state["entries"]) == 1
    entry = state["entries"][0]
    assert channel_entry_key(entry) == "telegram:123"
    assert entry["display"] == "Main Room"
    assert entry["meta"] == {"chat_type": "group"}


def test_record_seen_keys_by_thread_id(tmp_path: Path) -> None:
    store = ChannelDirectoryStore(tmp_path / "hub")

    store.record_seen("telegram", "123", None, "Group", {})
    store.record_seen("telegram", "123", "777", "Group / Topic", {})

    keys = {
        key
        for key in (channel_entry_key(entry) for entry in store.load()["entries"])
        if isinstance(key, str)
    }
    assert keys == {"telegram:123", "telegram:123:777"}


def test_eviction_uses_least_recently_seen_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = {"value": 0}

    def _next_seen() -> str:
        current = index["value"]
        index["value"] = current + 1
        return f"2026-02-27T10:00:{current:02d}Z"

    monkeypatch.setattr(channel_directory_module, "now_iso", _next_seen)
    store = ChannelDirectoryStore(tmp_path / "hub", max_entries=2)

    store.record_seen("discord", "c1", None, "Channel 1", {})
    store.record_seen("discord", "c2", None, "Channel 2", {})
    store.record_seen("discord", "c3", None, "Channel 3", {})

    keys = [channel_entry_key(entry) for entry in store.list_entries(limit=None)]
    assert keys == ["discord:c3", "discord:c2"]


def test_serialization_stability_and_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        channel_directory_module,
        "now_iso",
        lambda: "2026-02-27T10:00:00Z",
    )
    store = ChannelDirectoryStore(tmp_path / "hub")

    store.record_seen(
        "telegram",
        -1001,
        77,
        "My Group / Topic",
        {"chat_type": "group", "admins": ["a", "b"]},
    )
    first = store.path.read_text(encoding="utf-8")

    store.record_seen(
        "telegram",
        -1001,
        77,
        "My Group / Topic",
        {"chat_type": "group", "admins": ["a", "b"]},
    )
    second = store.path.read_text(encoding="utf-8")

    assert second == first
    payload = json.loads(second)
    assert isinstance(payload, dict)
    assert payload["entries"][0]["platform"] == "telegram"
