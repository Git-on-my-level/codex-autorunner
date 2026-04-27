from pathlib import Path

import yaml

from codex_autorunner.core.config_layering import (
    GENERATED_CONFIG_HEADER,
    PMA_DEFAULT_MAX_TEXT_CHARS,
    PMA_LEGACY_GENERATED_MAX_TEXT_CHARS,
    ROOT_CONFIG_FILENAME,
    ROOT_OVERRIDE_FILENAME,
    resolve_hub_config_data,
)
from codex_autorunner.core.generated_hub_config import (
    _sparsify_generated_config_mapping,
    build_generated_hub_config,
    normalize_generated_hub_config,
    render_hub_config_yaml,
    save_hub_config_data,
)
from tests.conftest import write_test_config


class TestSparsifyGeneratedConfigMapping:
    def test_empty_explicit_returns_empty(self) -> None:
        result = _sparsify_generated_config_mapping({}, {"a": 1})
        assert result == {}

    def test_key_not_in_defaults_included(self) -> None:
        result = _sparsify_generated_config_mapping({"extra": 42}, {"a": 1})
        assert result == {"extra": 42}

    def test_key_matching_default_excluded(self) -> None:
        result = _sparsify_generated_config_mapping({"a": 1}, {"a": 1})
        assert result == {}

    def test_key_differing_from_default_included(self) -> None:
        result = _sparsify_generated_config_mapping({"a": 2}, {"a": 1})
        assert result == {"a": 2}

    def test_preserve_keys_always_included(self) -> None:
        result = _sparsify_generated_config_mapping(
            {"version": 2},
            {"version": 2},
            preserve_keys=("version",),
        )
        assert result == {"version": 2}

    def test_nested_dict_sparsified_recursively(self) -> None:
        defaults = {"server": {"host": "127.0.0.1", "port": 4173}}
        explicit = {"server": {"host": "127.0.0.1", "port": 5000}}
        result = _sparsify_generated_config_mapping(explicit, defaults)
        assert result == {"server": {"port": 5000}}

    def test_nested_dict_empty_after_sparsify_omitted(self) -> None:
        defaults = {"server": {"host": "127.0.0.1", "port": 4173}}
        explicit = {"server": {"host": "127.0.0.1", "port": 4173}}
        result = _sparsify_generated_config_mapping(explicit, defaults)
        assert result == {}


class TestBuildGeneratedHubConfig:
    def test_empty_overrides_produces_minimal_output(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / ".codex-autorunner" / "config.yml", {"mode": "hub"}
        )

        result = build_generated_hub_config(hub_root)
        assert result == {"version": 2, "mode": "hub"}

    def test_overrides_differing_from_defaults_included(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / ".codex-autorunner" / "config.yml", {"mode": "hub"}
        )

        overrides = {"server": {"port": 9999}}
        result = build_generated_hub_config(hub_root, overrides=overrides)
        assert result["version"] == 2
        assert result["mode"] == "hub"
        assert result["server"]["port"] == 9999

    def test_overrides_matching_defaults_sparsified_away(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / ".codex-autorunner" / "config.yml", {"mode": "hub"}
        )

        overrides = {"server": {"port": 4173}}
        result = build_generated_hub_config(hub_root, overrides=overrides)
        assert "server" not in result


class TestRenderHubConfigYaml:
    def test_generated_adds_header(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub"})

        output = render_hub_config_yaml(config_path, {"mode": "hub"}, generated=True)
        assert output.startswith(GENERATED_CONFIG_HEADER)

    def test_non_generated_returns_raw_yaml(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub"})

        data = {"mode": "hub", "custom": "value"}
        output = render_hub_config_yaml(config_path, data, generated=False)
        assert not output.startswith(GENERATED_CONFIG_HEADER)
        assert yaml.safe_load(output) == data


class TestSaveHubConfigData:
    def test_writes_generated_config_to_disk(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub"})

        save_hub_config_data(config_path, {"mode": "hub"}, generated=True)

        content = config_path.read_text(encoding="utf-8")
        assert content.startswith(GENERATED_CONFIG_HEADER)
        data = yaml.safe_load(content)
        assert data["mode"] == "hub"

    def test_normalizes_legacy_discord_intents_before_writing(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub"})

        save_hub_config_data(
            config_path,
            {"mode": "hub", "discord_bot": {"intents": 513}},
            generated=False,
        )

        persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert persisted["discord_bot"]["intents"] == 33281


class TestNormalizeGeneratedHubConfig:
    def test_upgrades_stale_pma_800(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "version": 2,
            "mode": "hub",
            "pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS},
        }
        config_path.write_text(
            GENERATED_CONFIG_HEADER + yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

        normalize_generated_hub_config(config_path)

        persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert persisted == {"version": 2, "mode": "hub"}

    def test_preserves_explicit_root_pma_800(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / "codex-autorunner.yml",
            {"pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS}},
        )
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "version": 2,
            "mode": "hub",
            "pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS},
        }
        config_path.write_text(
            GENERATED_CONFIG_HEADER + yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

        result = normalize_generated_hub_config(config_path)

        persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert persisted == {"version": 2, "mode": "hub"}
        assert "pma" not in result

    def test_non_generated_config_loaded_as_is(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub", "custom": "data"})

        result = normalize_generated_hub_config(config_path)
        assert result == {"mode": "hub", "custom": "data"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        config_path = tmp_path / "nonexistent" / "config.yml"
        result = normalize_generated_hub_config(config_path)
        assert result == {}


class TestSparseRenderingCharacterization:
    """Characterize that generated configs are sparse: only values that differ from inherited defaults are persisted."""

    def test_default_matching_values_sparsified_away(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub"})

        overrides = {
            "mode": "hub",
            "version": 2,
            "pma": {"max_text_chars": PMA_DEFAULT_MAX_TEXT_CHARS},
        }
        result = build_generated_hub_config(hub_root, overrides=overrides)
        assert result == {"version": 2, "mode": "hub"}

    def test_non_default_values_preserved_in_sparse_output(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub"})

        overrides = {"pma": {"max_repos": 5, "max_text_chars": 9999}}
        result = build_generated_hub_config(hub_root, overrides=overrides)
        assert result["version"] == 2
        assert result["mode"] == "hub"
        assert result["pma"]["max_repos"] == 5
        assert result["pma"]["max_text_chars"] == 9999

    def test_sparse_output_renders_with_generated_header(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub"})

        data = {"mode": "hub", "server": {"port": 9999}}
        output = render_hub_config_yaml(config_path, data, generated=True)
        assert output.startswith(GENERATED_CONFIG_HEADER)
        parsed = yaml.safe_load(output)
        assert parsed["mode"] == "hub"
        assert parsed["server"]["port"] == 9999

    def test_non_generated_render_preserves_all_keys(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub"})

        data = {"mode": "hub", "pma": {"max_text_chars": PMA_DEFAULT_MAX_TEXT_CHARS}}
        output = render_hub_config_yaml(config_path, data, generated=False)
        assert not output.startswith(GENERATED_CONFIG_HEADER)
        parsed = yaml.safe_load(output)
        assert parsed == data

    def test_version_and_mode_always_preserved_in_sparse(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub"})

        overrides = {"version": 2, "mode": "hub"}
        result = build_generated_hub_config(hub_root, overrides=overrides)
        assert result["version"] == 2
        assert result["mode"] == "hub"

    def test_nested_partial_override_preserves_sparse(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub"})

        overrides = {"pma": {"max_repos": 5}}
        result = build_generated_hub_config(hub_root, overrides=overrides)
        assert result["version"] == 2
        assert result["mode"] == "hub"
        assert result["pma"] == {"max_repos": 5}
        assert "max_text_chars" not in result.get("pma", {})

    def test_save_and_reload_preserves_sparse_shape(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        write_test_config(config_path, {"mode": "hub"})

        save_hub_config_data(
            config_path, {"mode": "hub", "server": {"port": 9999}}, generated=True
        )
        content = config_path.read_text(encoding="utf-8")
        assert content.startswith(GENERATED_CONFIG_HEADER)
        parsed = yaml.safe_load(content)
        assert parsed == {"version": 2, "mode": "hub", "server": {"port": 9999}}


class TestLegacyPmaRewriteCharacterization:
    """Characterize the generated-config PMA max_text_chars=800 legacy rewrite as a compatibility path."""

    def test_generated_config_with_stale_pma_800_upgraded_when_no_root_override(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "version": 2,
            "mode": "hub",
            "pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS},
        }
        config_path.write_text(
            GENERATED_CONFIG_HEADER + yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

        result = normalize_generated_hub_config(config_path)
        persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert persisted == {"version": 2, "mode": "hub"}
        assert "pma" not in result

    def test_generated_config_preserves_explicit_root_pma_800(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / ROOT_CONFIG_FILENAME,
            {"pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS}},
        )
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "version": 2,
            "mode": "hub",
            "pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS},
        }
        config_path.write_text(
            GENERATED_CONFIG_HEADER + yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

        result = normalize_generated_hub_config(config_path)
        persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert persisted == {"version": 2, "mode": "hub"}
        assert "pma" not in result

    def test_generated_config_preserves_explicit_override_pma_800(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / ROOT_OVERRIDE_FILENAME,
            {"pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS}},
        )
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "version": 2,
            "mode": "hub",
            "pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS},
        }
        config_path.write_text(
            GENERATED_CONFIG_HEADER + yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

        result = normalize_generated_hub_config(config_path)
        persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert persisted == {"version": 2, "mode": "hub"}
        assert "pma" not in result

    def test_non_generated_pma_800_not_rewritten(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "mode": "hub",
            "pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS},
        }
        config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

        result = normalize_generated_hub_config(config_path)
        assert result["pma"]["max_text_chars"] == PMA_LEGACY_GENERATED_MAX_TEXT_CHARS

    def test_generated_config_non_800_pma_preserved(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "version": 2,
            "mode": "hub",
            "pma": {"max_text_chars": 5000},
        }
        config_path.write_text(
            GENERATED_CONFIG_HEADER + yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

        result = normalize_generated_hub_config(config_path)
        assert result["pma"]["max_text_chars"] == 5000

    def test_full_hub_load_upgrades_stale_pma_via_generated_config(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "version": 2,
            "mode": "hub",
            "pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS},
        }
        config_path.write_text(
            GENERATED_CONFIG_HEADER + yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

        merged = resolve_hub_config_data(hub_root, overrides=raw)
        assert merged["pma"]["max_text_chars"] == PMA_LEGACY_GENERATED_MAX_TEXT_CHARS

        from codex_autorunner.core.config import load_hub_config

        config = load_hub_config(hub_root)
        assert config.pma.max_text_chars == PMA_DEFAULT_MAX_TEXT_CHARS

    def test_full_hub_load_preserves_explicit_root_pma_800(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / ROOT_CONFIG_FILENAME,
            {"pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS}},
        )
        config_path = hub_root / ".codex-autorunner" / "config.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "version": 2,
            "mode": "hub",
            "pma": {"max_text_chars": PMA_LEGACY_GENERATED_MAX_TEXT_CHARS},
        }
        config_path.write_text(
            GENERATED_CONFIG_HEADER + yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

        from codex_autorunner.core.config import load_hub_config

        config = load_hub_config(hub_root)
        assert config.pma.max_text_chars == PMA_LEGACY_GENERATED_MAX_TEXT_CHARS
