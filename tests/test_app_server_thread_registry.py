import json
from pathlib import Path

import pytest

from codex_autorunner.core.app_server_threads import (
    FILE_CHAT_HERMES_PREFIX,
    FILE_CHAT_OPENCODE_PREFIX,
    FILE_CHAT_PREFIX,
    PMA_KEY,
    PMA_OPENCODE_KEY,
    PMA_OPENCODE_PREFIX,
    PMA_PREFIX,
    AppServerThreadRegistry,
    file_chat_discord_key,
    normalize_feature_key,
    pma_base_key,
    pma_legacy_alias_keys,
    pma_legacy_migration_fallback_keys,
    pma_prefix_for_agent,
    pma_prefixes_for_reset,
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
    assert normalize_feature_key("pma.hermes") == "pma.hermes"
    assert normalize_feature_key("PMA:OPENCODE") == "pma.opencode"


class TestFeatureKeyNormalization:
    def test_accepts_all_static_feature_keys(self) -> None:
        static_keys = [
            "file_chat",
            "file_chat.opencode",
            "file_chat.hermes",
            "pma",
            "pma.opencode",
            "pma.hermes",
            "autorunner",
            "autorunner.opencode",
            "autorunner.hermes",
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
            normalize_feature_key("file_chat.hermes.discord.456")
            == "file_chat.hermes.discord.456"
        )
        assert (
            normalize_feature_key("FILE_CHAT/workspace.spec")
            == "file_chat.workspace.spec"
        )

    def test_accepts_hermes_prefixed_keys(self) -> None:
        assert normalize_feature_key("file_chat.hermes") == "file_chat.hermes"
        assert normalize_feature_key("pma.hermes") == "pma.hermes"
        assert normalize_feature_key("autorunner.hermes") == "autorunner.hermes"
        assert normalize_feature_key("pma.hermes.topic-abc") == "pma.hermes.topic-abc"
        assert (
            normalize_feature_key("file_chat.hermes.ticket.1")
            == "file_chat.hermes.ticket.1"
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

    def test_accepts_pma_topic_scoped_keys(self) -> None:
        assert normalize_feature_key("pma.-1001234567890:42") == "pma.-1001234567890.42"
        assert normalize_feature_key("pma.123:root") == "pma.123.root"
        assert (
            normalize_feature_key("PMA.OPENCODE.-1001234567890:42")
            == "pma.opencode.-1001234567890.42"
        )
        assert (
            normalize_feature_key("pma.opencode.topic-abc") == "pma.opencode.topic-abc"
        )

    def test_rejects_bare_pma_prefix(self) -> None:
        with pytest.raises(ValueError, match="invalid feature key"):
            normalize_feature_key("pma.")


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


class TestScopedPmaKeys:
    def test_set_and_get_topic_scoped_pma_key(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)

        scoped_key = "pma.-1001234567890:42"
        registry.set_thread_id(scoped_key, "thread-scoped-1")
        assert registry.get_thread_id(scoped_key) == "thread-scoped-1"

    def test_set_and_get_topic_scoped_pma_opencode_key(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)

        scoped_key = "pma.opencode.123:root"
        registry.set_thread_id(scoped_key, "thread-scoped-2")
        assert registry.get_thread_id(scoped_key) == "thread-scoped-2"

    def test_persistence_round_trip_scoped_pma_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry1 = AppServerThreadRegistry(path)
        registry1.set_thread_id("pma", "thread-global")
        registry1.set_thread_id("pma.-1001:42", "thread-topic-42")
        registry1.set_thread_id("pma.opencode.-1002:99", "thread-topic-99")

        registry2 = AppServerThreadRegistry(path)
        assert registry2.get_thread_id("pma") == "thread-global"
        assert registry2.get_thread_id("pma.-1001:42") == "thread-topic-42"
        assert registry2.get_thread_id("pma.opencode.-1002:99") == "thread-topic-99"

    def test_reset_thread_removes_scoped_pma_key(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)
        scoped_key = "pma.-1001234567890:42"
        registry.set_thread_id(scoped_key, "thread-scoped")

        assert registry.reset_thread(scoped_key) is True
        assert registry.get_thread_id(scoped_key) is None
        assert registry.reset_thread(scoped_key) is False

    def test_scoped_pma_keys_coexist_with_global_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)

        registry.set_thread_id("pma", "global-thread")
        registry.set_thread_id("pma.-1001:42", "topic-42-thread")
        registry.set_thread_id("pma.-1001:99", "topic-99-thread")

        assert registry.get_thread_id("pma") == "global-thread"
        assert registry.get_thread_id("pma.-1001:42") == "topic-42-thread"
        assert registry.get_thread_id("pma.-1001:99") == "topic-99-thread"

    def test_reset_global_pma_preserves_scoped_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)

        registry.set_thread_id("pma", "global-thread")
        registry.set_thread_id("pma.-1001:42", "topic-42-thread")

        registry.reset_thread("pma")

        assert registry.get_thread_id("pma") is None
        assert registry.get_thread_id("pma.-1001:42") == "topic-42-thread"

    def test_pma_topic_scoped_key_matches_registry_key(self, tmp_path: Path) -> None:
        def mock_topic_key(chat_id: int, thread_id: "int | None") -> str:
            return f"{chat_id}:{thread_id or 'root'}"

        built_key = pma_topic_scoped_key(
            agent="opencode",
            chat_id=-1001234567890,
            thread_id=42,
            topic_key_fn=mock_topic_key,
        )

        registry = AppServerThreadRegistry(tmp_path / "threads.json")
        registry.set_thread_id(built_key, "test-thread")

        assert registry.get_thread_id(built_key) == "test-thread"


class TestPmaBaseKeyHelper:
    def test_returns_opencode_key_for_opencode_agent(self) -> None:
        assert pma_base_key("opencode") == PMA_OPENCODE_KEY
        assert pma_base_key("OpenCode") == PMA_OPENCODE_KEY
        assert pma_base_key("  OPENCODE  ") == PMA_OPENCODE_KEY

    def test_returns_codex_key_for_default_codex_family(self) -> None:
        assert pma_base_key("codex") == PMA_KEY
        assert pma_base_key("") == PMA_KEY
        assert pma_base_key(None) == PMA_KEY  # type: ignore

    def test_returns_agent_scoped_key_for_other_runtimes(self) -> None:
        assert pma_base_key("hermes") == "pma.hermes"
        assert pma_base_key("zeroclaw") == "pma.zeroclaw"
        assert pma_base_key("codex-alt") == "pma.codex-alt"


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

    def test_builds_hermes_discord_key(self) -> None:
        key = file_chat_discord_key(
            agent="hermes",
            channel_id="111222333",
            workspace_path="/workspace/hermes-repo",
        )
        assert key.startswith(FILE_CHAT_HERMES_PREFIX + "discord.111222333.")
        assert len(key.split(".")[-1]) == 12

    def test_builds_other_agent_discord_key(self) -> None:
        key = file_chat_discord_key(
            agent="custom-agent",
            channel_id="999888777",
            workspace_path="/workspace/custom-repo",
        )
        assert key.startswith("file_chat.custom-agent.discord.999888777.")
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


class TestResetThreadsByPrefix:
    def test_clears_keys_matching_prefix(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)

        registry.set_thread_id("pma", "global-thread")
        registry.set_thread_id("pma.-1001.42", "topic-42-thread")
        registry.set_thread_id("pma.-1001.99", "topic-99-thread")
        registry.set_thread_id("file_chat", "file-chat-thread")

        cleared = registry.reset_threads_by_prefix(PMA_PREFIX)

        assert "pma" not in cleared
        assert "pma.-1001.42" in cleared
        assert "pma.-1001.99" in cleared
        assert registry.get_thread_id("pma") == "global-thread"
        assert registry.get_thread_id("pma.-1001.42") is None
        assert registry.get_thread_id("pma.-1001.99") is None
        assert registry.get_thread_id("file_chat") == "file-chat-thread"

    def test_clears_opencode_scoped_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)

        registry.set_thread_id("pma.opencode", "global-opencode")
        registry.set_thread_id("pma.opencode.-1001.42", "topic-opencode-42")
        registry.set_thread_id("pma", "global-codex")

        cleared = registry.reset_threads_by_prefix(PMA_OPENCODE_PREFIX)

        assert "pma.opencode" not in cleared
        assert "pma.opencode.-1001.42" in cleared
        assert "pma" not in cleared
        assert registry.get_thread_id("pma.opencode") == "global-opencode"
        assert registry.get_thread_id("pma.opencode.-1001.42") is None
        assert registry.get_thread_id("pma") == "global-codex"

    def test_can_exclude_nested_prefix_families(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)

        registry.set_thread_id("pma.-1001.42", "topic-codex-42")
        registry.set_thread_id("pma.opencode.-1001.42", "topic-opencode-42")

        cleared = registry.reset_threads_by_prefix(
            PMA_PREFIX, exclude_prefixes=(PMA_OPENCODE_PREFIX,)
        )

        assert "pma.-1001.42" in cleared
        assert "pma.opencode.-1001.42" not in cleared
        assert registry.get_thread_id("pma.-1001.42") is None
        assert registry.get_thread_id("pma.opencode.-1001.42") == "topic-opencode-42"

    def test_returns_empty_list_when_no_matches(self, tmp_path: Path) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)

        registry.set_thread_id("pma", "global-thread")
        cleared = registry.reset_threads_by_prefix("file_chat.")

        assert cleared == []
        assert registry.get_thread_id("pma") == "global-thread"


class TestPmaPrefixForAgent:
    def test_returns_opencode_prefix_for_opencode(self) -> None:
        assert pma_prefix_for_agent("opencode") == PMA_OPENCODE_PREFIX
        assert pma_prefix_for_agent("OpenCode") == PMA_OPENCODE_PREFIX
        assert pma_prefix_for_agent("  OPENCODE  ") == PMA_OPENCODE_PREFIX

    def test_returns_codex_prefix_for_default_codex_family(self) -> None:
        assert pma_prefix_for_agent("codex") == PMA_PREFIX
        assert pma_prefix_for_agent("") == PMA_PREFIX
        assert pma_prefix_for_agent(None) == PMA_PREFIX

    def test_returns_agent_scoped_prefix_for_other_runtimes(self) -> None:
        assert pma_prefix_for_agent("hermes") == "pma.hermes."
        assert pma_prefix_for_agent("zeroclaw") == "pma.zeroclaw."
        assert pma_prefix_for_agent("codex-alt") == "pma.codex-alt."


class TestPmaPrefixesForReset:
    def test_returns_opencode_prefix_for_opencode_agent(self) -> None:
        result = pma_prefixes_for_reset("opencode")
        assert result == [PMA_OPENCODE_PREFIX]

    def test_returns_codex_prefix_for_codex_agent(self) -> None:
        result = pma_prefixes_for_reset("codex")
        assert result == [PMA_PREFIX]

    def test_returns_agent_scoped_prefix_for_other_runtimes(self) -> None:
        assert pma_prefixes_for_reset("hermes") == ["pma.hermes."]
        assert pma_prefixes_for_reset("zeroclaw") == ["pma.zeroclaw."]

    def test_returns_codex_family_prefix_for_all_or_none(self) -> None:
        for agent in (None, "all", ""):
            result = pma_prefixes_for_reset(agent)
            assert result == [PMA_PREFIX]


class TestPmaLegacyAliasMigration:
    def test_legacy_keys_include_hyphen_and_underscore_alias_shapes(self) -> None:
        keys = pma_legacy_alias_keys("hermes", "m4-pma")
        new_key = pma_base_key("hermes", "m4-pma")
        assert pma_base_key("hermes-m4-pma") in keys
        assert pma_base_key("hermes_m4_pma") in keys
        assert new_key not in keys
        assert len(keys) >= 2

    def test_migration_fallback_appends_topic_suffix_to_each_legacy_base(self) -> None:
        agent, profile = "hermes", "m4-pma"
        logical_base = pma_base_key(agent, profile)
        canonical = f"{logical_base}.123:root"
        fallbacks = pma_legacy_migration_fallback_keys(canonical, agent, profile)
        assert fallbacks
        for fb in fallbacks:
            assert fb.startswith("pma.")
            assert fb.endswith(".123:root")
        assert pma_base_key("hermes-m4-pma") + ".123:root" in fallbacks

    def test_get_thread_id_with_fallback_migrates_topic_scoped_legacy_key(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "app_server_threads.json"
        registry = AppServerThreadRegistry(path)
        agent, profile = "hermes", "m4-pma"
        logical_base = pma_base_key(agent, profile)
        topic = "99:7"
        canonical = f"{logical_base}.{topic}"
        legacy = pma_base_key("hermes-m4-pma") + f".{topic}"
        registry.set_thread_id(legacy, "thread-migrated")
        fallbacks = pma_legacy_migration_fallback_keys(canonical, agent, profile)
        assert legacy in fallbacks
        resolved = registry.get_thread_id_with_fallback(canonical, *fallbacks)
        assert resolved == "thread-migrated"
        assert registry.get_thread_id(canonical) == "thread-migrated"
        assert registry.get_thread_id(legacy) is None
