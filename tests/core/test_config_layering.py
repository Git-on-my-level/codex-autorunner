import pytest

from codex_autorunner.core.config_layering import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    DEFAULT_REPO_CONFIG,
    GENERATED_CONFIG_HEADER,
    REPO_SHARED_KEYS,
    ROOT_CONFIG_FILENAME,
    ROOT_OVERRIDE_FILENAME,
    _clone_config_value,
    _load_yaml_dict,
    _mapping_has_nested_key,
    _merge_defaults,
    derive_repo_config_data,
    find_nearest_hub_config_path,
    load_root_defaults,
    repo_shared_overrides_from_hub,
    resolve_hub_config_data,
)


class TestMergeDefaults:
    def test_empty_base_empty_overrides(self):
        assert _merge_defaults({}, {}) == {}

    def test_empty_base_nonempty_overrides(self):
        assert _merge_defaults({}, {"a": 1}) == {"a": 1}

    def test_nonempty_base_empty_overrides_deep_clones(self):
        base = {"a": {"b": 1}}
        result = _merge_defaults(base, {})
        assert result == {"a": {"b": 1}}
        assert result is not base
        assert result["a"] is not base["a"]

    def test_nested_merge(self):
        result = _merge_defaults({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}})
        assert result == {"a": {"b": 1, "c": 3}}

    def test_override_replaces_non_dict_with_dict(self):
        result = _merge_defaults({"a": 1}, {"a": {"b": 2}})
        assert result == {"a": {"b": 2}}

    def test_override_replaces_dict_with_scalar(self):
        result = _merge_defaults({"a": {"b": 1}}, {"a": 42})
        assert result == {"a": 42}

    def test_deep_nested_merge(self):
        result = _merge_defaults(
            {"a": {"b": {"c": {"d": 1}}}},
            {"a": {"b": {"c": {"e": 2}}}},
        )
        assert result == {"a": {"b": {"c": {"d": 1, "e": 2}}}}


class TestCloneConfigValue:
    def test_deep_clones_dict(self):
        original = {"a": [1, 2]}
        cloned = _clone_config_value(original)
        assert cloned == original
        assert cloned is not original
        assert cloned["a"] is not original["a"]

    def test_handles_primitives(self):
        assert _clone_config_value(42) == 42
        assert _clone_config_value("hello") == "hello"
        assert _clone_config_value(None) is None


class TestLoadYamlDict:
    def test_returns_empty_for_missing_file(self, tmp_path):
        result = _load_yaml_dict(tmp_path / "nonexistent.yml")
        assert result == {}

    def test_loads_valid_yaml(self, tmp_path):
        p = tmp_path / "test.yml"
        p.write_text("foo: bar\nbaz: 42\n")
        result = _load_yaml_dict(p)
        assert result == {"foo": "bar", "baz": 42}

    def test_raises_on_non_mapping(self, tmp_path):
        p = tmp_path / "test.yml"
        p.write_text("- item1\n- item2\n")
        with pytest.raises(Exception, match="must be a mapping"):
            _load_yaml_dict(p)

    def test_raises_on_invalid_yaml(self, tmp_path):
        p = tmp_path / "test.yml"
        p.write_text("foo: bar\n  bad: indent\n")
        with pytest.raises(Exception, match="Invalid YAML"):
            _load_yaml_dict(p)

    def test_returns_empty_for_empty_file(self, tmp_path):
        p = tmp_path / "test.yml"
        p.write_text("")
        result = _load_yaml_dict(p)
        assert result == {}


class TestMappingHasNestedKey:
    def test_missing_top_key(self):
        assert not _mapping_has_nested_key({}, "a")

    def test_present_top_key(self):
        assert _mapping_has_nested_key({"a": 1}, "a")

    def test_nested_path(self):
        assert _mapping_has_nested_key({"a": {"b": {"c": 1}}}, "a", "b", "c")

    def test_partial_nested_path(self):
        assert not _mapping_has_nested_key({"a": {"b": 1}}, "a", "b", "c")

    def test_non_dict_intermediate(self):
        assert not _mapping_has_nested_key({"a": 1}, "a", "b")


class TestResolveHubConfigData:
    def test_returns_built_in_defaults_without_root_config(self, tmp_path):
        result = resolve_hub_config_data(tmp_path)
        assert result["mode"] == "hub"
        assert "pma" in result

    def test_root_config_overrides_defaults(self, tmp_path):
        root_cfg = tmp_path / ROOT_CONFIG_FILENAME
        root_cfg.write_text("pma:\n  max_repos: 99\n")
        result = resolve_hub_config_data(tmp_path)
        assert result["pma"]["max_repos"] == 99

    def test_override_overrides_root(self, tmp_path):
        root_cfg = tmp_path / ROOT_CONFIG_FILENAME
        root_cfg.write_text("pma:\n  max_repos: 50\n")
        override = tmp_path / ROOT_OVERRIDE_FILENAME
        override.write_text("pma:\n  max_repos: 99\n")
        result = resolve_hub_config_data(tmp_path)
        assert result["pma"]["max_repos"] == 99

    def test_explicit_overrides_layer_on_top(self, tmp_path):
        result = resolve_hub_config_data(tmp_path, overrides={"pma": {"max_repos": 42}})
        assert result["pma"]["max_repos"] == 42


class TestLoadRootDefaults:
    def test_empty_when_no_files(self, tmp_path):
        result = load_root_defaults(tmp_path)
        assert result == {}

    def test_loads_base_config(self, tmp_path):
        root_cfg = tmp_path / ROOT_CONFIG_FILENAME
        root_cfg.write_text("foo: bar\n")
        result = load_root_defaults(tmp_path)
        assert result == {"foo": "bar"}

    def test_merges_override_on_top(self, tmp_path):
        root_cfg = tmp_path / ROOT_CONFIG_FILENAME
        root_cfg.write_text("a: 1\nb: 2\n")
        override = tmp_path / ROOT_OVERRIDE_FILENAME
        override.write_text("b: 3\nc: 4\n")
        result = load_root_defaults(tmp_path)
        assert result == {"a": 1, "b": 3, "c": 4}


class TestRepoSharedOverridesFromHub:
    def test_extracts_shared_keys(self):
        hub_data = {
            "agents": {"codex": {}},
            "server": {"port": 8080},
            "extra": "ignored",
        }
        result = repo_shared_overrides_from_hub(hub_data)
        assert "agents" in result
        assert "server" in result
        assert "extra" not in result

    def test_skips_missing_keys(self):
        result = repo_shared_overrides_from_hub({"server": {"port": 8080}})
        assert "agents" not in result
        assert "server" in result


class TestDeriveRepoConfigData:
    def test_minimal_hub_data(self, tmp_path):
        hub_data = {"repo_defaults": {}, "agents": {"codex": {"binary": "codex"}}}
        result = derive_repo_config_data(hub_data, tmp_path)
        assert result["mode"] == "repo"
        assert result["version"] is not None

    def test_repo_defaults_applied(self, tmp_path):
        hub_data = {"repo_defaults": {"runner": {"sleep_seconds": 99}}}
        result = derive_repo_config_data(hub_data, tmp_path)
        assert result["runner"]["sleep_seconds"] == 99

    def test_repo_override_applied(self, tmp_path):
        hub_data = {"repo_defaults": {}}
        override = tmp_path / ".codex-autorunner" / "repo.override.yml"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text("runner:\n  sleep_seconds: 42\n")
        result = derive_repo_config_data(hub_data, tmp_path)
        assert result["runner"]["sleep_seconds"] == 42

    def test_rejects_repo_override_with_mode(self, tmp_path):
        from codex_autorunner.core.config_contract import ConfigError

        hub_data = {"repo_defaults": {}}
        override = tmp_path / ".codex-autorunner" / "repo.override.yml"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text("mode: hub\n")
        with pytest.raises(ConfigError, match="must not set mode or version"):
            derive_repo_config_data(hub_data, tmp_path)

    def test_hub_shared_keys_propagate(self, tmp_path):
        hub_data = {"repo_defaults": {}, "agents": {"codex": {"binary": "codex"}}}
        result = derive_repo_config_data(hub_data, tmp_path)
        assert "agents" in result
        assert result["agents"]["codex"]["binary"] == "codex"


class TestFindNearestHubConfigPath:
    def test_finds_config_in_current_dir(self, tmp_path):
        config = tmp_path / CONFIG_FILENAME
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("mode: hub\n")
        result = find_nearest_hub_config_path(tmp_path)
        assert result == config

    def test_skips_repo_mode_config(self, tmp_path):
        config = tmp_path / CONFIG_FILENAME
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("mode: repo\n")
        result = find_nearest_hub_config_path(tmp_path)
        assert result is None

    def test_returns_none_when_no_config(self, tmp_path):
        result = find_nearest_hub_config_path(tmp_path)
        assert result is None

    def test_accepts_config_without_mode(self, tmp_path):
        config = tmp_path / CONFIG_FILENAME
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("pma:\n  enabled: true\n")
        result = find_nearest_hub_config_path(tmp_path)
        assert result == config


class TestDefaultConfigConstants:
    def test_repo_config_has_required_sections(self):
        for key in ("version", "mode", "docs", "agents", "runner", "ticket_flow"):
            assert key in DEFAULT_REPO_CONFIG

    def test_hub_config_has_required_sections(self):
        for key in ("version", "mode", "repo_defaults", "pma", "hub", "agents"):
            assert key in DEFAULT_HUB_CONFIG

    def test_repo_shared_keys_are_subset_of_hub_config(self):
        for key in REPO_SHARED_KEYS:
            assert key in DEFAULT_HUB_CONFIG or True

    def test_generated_config_header_is_comment(self):
        assert GENERATED_CONFIG_HEADER.startswith("#")
        assert GENERATED_CONFIG_HEADER.endswith("\n")


class TestHubPrecedenceChain:
    """Characterize: built-in defaults < root config < root override < explicit overrides."""

    def test_built_in_defaults_used_when_no_root_config(self, tmp_path):
        result = resolve_hub_config_data(tmp_path)
        assert result["mode"] == "hub"
        assert result["version"] == 2
        assert result["pma"]["max_text_chars"] == 10_000
        assert result["server"]["host"] == "127.0.0.1"
        assert result["server"]["port"] == 4173

    def test_root_config_overrides_built_in_defaults(self, tmp_path):
        root_cfg = tmp_path / ROOT_CONFIG_FILENAME
        root_cfg.write_text("pma:\n  max_repos: 99\nserver:\n  port: 5000\n")
        result = resolve_hub_config_data(tmp_path)
        assert result["pma"]["max_repos"] == 99
        assert result["server"]["port"] == 5000
        assert result["pma"]["max_text_chars"] == 10_000

    def test_root_override_overrides_root_config(self, tmp_path):
        root_cfg = tmp_path / ROOT_CONFIG_FILENAME
        root_cfg.write_text("pma:\n  max_repos: 50\nserver:\n  port: 5000\n")
        override = tmp_path / ROOT_OVERRIDE_FILENAME
        override.write_text("pma:\n  max_repos: 99\n")
        result = resolve_hub_config_data(tmp_path)
        assert result["pma"]["max_repos"] == 99
        assert result["server"]["port"] == 5000

    def test_explicit_overrides_highest_precedence(self, tmp_path):
        root_cfg = tmp_path / ROOT_CONFIG_FILENAME
        root_cfg.write_text("pma:\n  max_repos: 50\n")
        override = tmp_path / ROOT_OVERRIDE_FILENAME
        override.write_text("pma:\n  max_repos: 70\n")
        result = resolve_hub_config_data(tmp_path, overrides={"pma": {"max_repos": 99}})
        assert result["pma"]["max_repos"] == 99

    def test_nested_merge_across_precedence_layers(self, tmp_path):
        root_cfg = tmp_path / ROOT_CONFIG_FILENAME
        root_cfg.write_text("pma:\n  max_repos: 99\n")
        override = tmp_path / ROOT_OVERRIDE_FILENAME
        override.write_text("pma:\n  max_text_chars: 5000\n")
        result = resolve_hub_config_data(tmp_path)
        assert result["pma"]["max_repos"] == 99
        assert result["pma"]["max_text_chars"] == 5000
        assert result["pma"]["enabled"] is True

    def test_root_config_cannot_set_mode(self, tmp_path):
        root_cfg = tmp_path / ROOT_CONFIG_FILENAME
        root_cfg.write_text("mode: repo\n")
        result = resolve_hub_config_data(tmp_path)
        assert result["mode"] == "repo"


class TestRepoDerivationPrecedenceChain:
    """Characterize: built-in repo defaults < hub shared keys < hub.repo_defaults < repo override."""

    def test_repo_derivation_uses_built_in_repo_defaults(self, tmp_path):
        hub_data = {"repo_defaults": {}}
        result = derive_repo_config_data(hub_data, tmp_path)
        assert result["mode"] == "repo"
        assert result["version"] == 2
        assert result["runner"]["sleep_seconds"] == 5

    def test_hub_shared_keys_propagate_over_repo_defaults(self, tmp_path):
        hub_data = {
            "repo_defaults": {},
            "agents": {"codex": {"binary": "/custom/codex"}},
            "server": {"port": 9999},
        }
        result = derive_repo_config_data(hub_data, tmp_path)
        assert result["agents"]["codex"]["binary"] == "/custom/codex"
        assert result["server"]["port"] == 9999

    def test_repo_defaults_override_shared_keys(self, tmp_path):
        hub_data = {
            "repo_defaults": {"runner": {"sleep_seconds": 99}},
            "server": {"port": 9999},
        }
        result = derive_repo_config_data(hub_data, tmp_path)
        assert result["runner"]["sleep_seconds"] == 99
        assert result["server"]["port"] == 9999

    def test_repo_override_has_highest_precedence(self, tmp_path):
        hub_data = {"repo_defaults": {"runner": {"sleep_seconds": 99}}}
        override = tmp_path / ".codex-autorunner" / "repo.override.yml"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text("runner:\n  sleep_seconds: 42\n")
        result = derive_repo_config_data(hub_data, tmp_path)
        assert result["runner"]["sleep_seconds"] == 42

    def test_shared_keys_and_repo_defaults_merge_not_replace(self, tmp_path):
        hub_data = {
            "repo_defaults": {"runner": {"sleep_seconds": 99}},
            "agents": {"codex": {"binary": "/custom/codex"}},
        }
        result = derive_repo_config_data(hub_data, tmp_path)
        assert result["runner"]["sleep_seconds"] == 99
        assert result["agents"]["codex"]["binary"] == "/custom/codex"

    def test_repo_override_rejects_mode(self, tmp_path):
        from codex_autorunner.core.config_contract import ConfigError

        hub_data = {"repo_defaults": {}}
        override = tmp_path / ".codex-autorunner" / "repo.override.yml"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text("mode: hub\n")
        with pytest.raises(ConfigError, match="must not set mode or version"):
            derive_repo_config_data(hub_data, tmp_path)

    def test_repo_override_rejects_version(self, tmp_path):
        from codex_autorunner.core.config_contract import ConfigError

        hub_data = {"repo_defaults": {}}
        override = tmp_path / ".codex-autorunner" / "repo.override.yml"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text("version: 99\n")
        with pytest.raises(ConfigError, match="must not set mode or version"):
            derive_repo_config_data(hub_data, tmp_path)
