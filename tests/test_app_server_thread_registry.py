import json
from pathlib import Path

import pytest

from codex_autorunner.core.app_server_threads import (
    FILE_CHAT_OPENCODE_PREFIX,
    FILE_CHAT_PREFIX,
    PMA_KEY,
    PMA_OPENCODE_KEY,
    AppServerThreadRegistry,
    file_chat_discord_key,
    normalize_feature_key,
    pma_base_key,
    pma_topic_scoped_key,
)


def test_thread_registry_corruption_creates_backup(tmp_path: Path) -> None:
    path = tmp_path / "app_server_threads.json"
    path.write_text("{not-json", encoding="utf-8")

    registry = AppServerThreadRegistry(path)
    threads = registry.load()

    assert threads == {}
    notice = registry.corruption_notice()
    assert notice is not None
    assert notice.get("status") == "corrupt"
    backup = notice.get("backup_path")
    assert backup
    assert Path(backup).exists()

    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired.get("threads") == {}


def test_thread_registry_reset_all_clears_notice(tmp_path: Path) -> None:
    path = tmp_path / "app_server_threads.json"
    path.write_text("{not-json", encoding="utf-8")
    registry = AppServerThreadRegistry(path)
    registry.load()
    assert registry.corruption_notice()

    registry.reset_all()

    assert registry.corruption_notice() is None


def test_normalize_feature_key_accepts_pma() -> None:
    assert normalize_feature_key("pma") == "pma"
    assert normalize_feature_key("pma.opencode") == "pma.opencode"
    assert normalize_feature_key("PMA:OPENCODE") == "pma.opencode"


class TestFeatureKeyNormalization:
    def test_accepts_all_static_feature_keys(self) -> None:
        static_keys = [
            "file_chat",
            "file_chat.opencode",
            "pma",
            "pma.opencode",
            "autorunner",
            "autorunner.opencode",
        ]
        for key in static_keys:
            assert normalize_feature_key(key) == key
            assert normalize_feature_key(key.upper()) == key

    def test_accepts_file_chat_prefixed_keys(self) -> None:
        assert normalize_feature_key("file_chat.ticket.1") == "file_chat.ticket.1"
        assert (
            normalize_feature_key("file_chat.opencode.discord.123")
            == "file_chat.opencode.discord.123"
        )
        assert (
            normalize_feature_key("FILE_CHAT/workspace.spec")
            == "file_chat.workspace.spec"
        )

    def test_normalizes_separators(self) -> None:
        assert normalize_feature_key("PMA:OPENCODE") == "pma.opencode"
        assert normalize_feature_key("FILE_CHAT/OPENCODE") == "file_chat.opencode"

    def test_rejects_empty_key(self) -> None:
        with pytest.raises(ValueError, match="feature key is required"):
            normalize_feature_key("")
        with pytest.raises(ValueError, match="feature key is required"):
            normalize_feature_key("   ")

    def test_rejects_non_string_key(self) -> None:
        with pytest.raises(ValueError, match="feature key must be a string"):
            normalize_feature_key(123)  # type: ignore

    def test_rejects_unknown_prefix(self) -> None:
        with pytest.raises(ValueError, match="invalid feature key"):
            normalize_feature_key("unknown.key")


class TestRegistryPersistence:
    def test_set_and_get_thread_id(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)

        registry.set_thread_id("pma", "thread-123")
        assert registry.get_thread_id("pma") == "thread-123"

    def test_persistence_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry1 = AppServerThreadRegistry(path)
        registry1.set_thread_id("pma", "thread-a")
        registry1.set_thread_id("pma.opencode", "thread-b")
        registry1.set_thread_id("file_chat.discord.abc", "thread-c")

        registry2 = AppServerThreadRegistry(path)
        assert registry2.get_thread_id("pma") == "thread-a"
        assert registry2.get_thread_id("pma.opencode") == "thread-b"
        assert registry2.get_thread_id("file_chat.discord.abc") == "thread-c"

    def test_reset_thread_removes_key(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)
        registry.set_thread_id("pma", "thread-123")

        assert registry.reset_thread("pma") is True
        assert registry.get_thread_id("pma") is None
        assert registry.reset_thread("pma") is False

    def test_reset_all_clears_everything(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)
        registry.set_thread_id("pma", "thread-a")
        registry.set_thread_id("file_chat", "thread-b")

        registry.reset_all()

        assert registry.get_thread_id("pma") is None
        assert registry.get_thread_id("file_chat") is None

    def test_get_missing_key_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)
        assert registry.get_thread_id("pma") is None


class TestRegistryThreadValidation:
    def test_rejects_empty_thread_id(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)
        with pytest.raises(ValueError, match="thread id is required"):
            registry.set_thread_id("pma", "")


class TestPmaBaseKeyHelper:
    def test_returns_opencode_key_for_opencode_agent(self) -> None:
        assert pma_base_key("opencode") == PMA_OPENCODE_KEY
        assert pma_base_key("OpenCode") == PMA_OPENCODE_KEY
        assert pma_base_key("  OPENCODE  ") == PMA_OPENCODE_KEY

    def test_returns_codex_key_for_other_agents(self) -> None:
        assert pma_base_key("codex") == PMA_KEY
        assert pma_base_key("codex-alt") == PMA_KEY
        assert pma_base_key("") == PMA_KEY
        assert pma_base_key(None) == PMA_KEY  # type: ignore


class TestPmaTopicScopedKeyHelper:
    def test_builds_topic_scoped_key(self) -> None:
        def mock_topic_key(chat_id: int, thread_id: "int | None") -> str:
            return f"{chat_id}:{thread_id or 'root'}"

        result = pma_topic_scoped_key(
            agent="opencode",
            chat_id=-1001234567890,
            thread_id=42,
            topic_key_fn=mock_topic_key,
        )
        assert result == f"{PMA_OPENCODE_KEY}.-1001234567890:42"

    def test_builds_topic_scoped_key_for_root_thread(self) -> None:
        def mock_topic_key(chat_id: int, thread_id: "int | None") -> str:
            return f"{chat_id}:root"

        result = pma_topic_scoped_key(
            agent="codex",
            chat_id=-1001234567890,
            thread_id=None,
            topic_key_fn=mock_topic_key,
        )
        assert result == f"{PMA_KEY}.-1001234567890:root"


class TestFileChatDiscordKeyHelper:
    def test_builds_codex_discord_key(self) -> None:
        key = file_chat_discord_key(
            agent="codex",
            channel_id="123456789",
            workspace_path="/workspace/repo",
        )
        assert key.startswith(FILE_CHAT_PREFIX + "discord.123456789.")
        assert len(key.split(".")[-1]) == 12

    def test_builds_opencode_discord_key(self) -> None:
        key = file_chat_discord_key(
            agent="opencode",
            channel_id="987654321",
            workspace_path="/workspace/other-repo",
        )
        assert key.startswith(FILE_CHAT_OPENCODE_PREFIX + "discord.987654321.")
        assert len(key.split(".")[-1]) == 12

    def test_stable_hash_for_same_path(self) -> None:
        key1 = file_chat_discord_key("codex", "chan1", "/workspace/repo")
        key2 = file_chat_discord_key("codex", "chan1", "/workspace/repo")
        assert key1 == key2

    def test_different_hash_for_different_path(self) -> None:
        key1 = file_chat_discord_key("codex", "chan1", "/workspace/repo1")
        key2 = file_chat_discord_key("codex", "chan1", "/workspace/repo2")
        assert key1 != key2
        assert key1.split(".")[3] != key2.split(".")[3]

    def test_strips_channel_id_whitespace(self) -> None:
        key = file_chat_discord_key("codex", "  123456  ", "/workspace/repo")
        assert "123456" in key
