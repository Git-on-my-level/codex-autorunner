import json
import os
from pathlib import Path

import pytest
import yaml

from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_REPO_CONFIG,
    REPO_OVERRIDE_FILENAME,
    ConfigError,
    load_hub_config,
    load_repo_config,
    resolve_env_for_root,
)


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_load_hub_config_prefers_config_over_root_overrides(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    _write_yaml(hub_root / "codex-autorunner.yml", {"server": {"port": 5000}})
    _write_yaml(hub_root / "codex-autorunner.override.yml", {"server": {"port": 6000}})

    config_dir = hub_root / ".codex-autorunner"
    config_dir.mkdir()
    _write_yaml(
        config_dir / "config.yml",
        {"mode": "hub", "server": {"port": 7000}},
    )

    config = load_hub_config(hub_root)
    assert config.server_port == 7000


def test_load_hub_config_uses_root_override_when_config_missing(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    _write_yaml(hub_root / "codex-autorunner.yml", {"server": {"port": 5000}})
    _write_yaml(hub_root / "codex-autorunner.override.yml", {"server": {"port": 6000}})

    config_dir = hub_root / ".codex-autorunner"
    config_dir.mkdir()
    _write_yaml(config_dir / "config.yml", {"mode": "hub"})

    config = load_hub_config(hub_root)
    assert config.server_port == 6000


def test_load_repo_config_inherits_hub_shared_settings(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "agents": {"opencode": {"binary": "/opt/opencode"}},
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    config = load_repo_config(repo_root, hub_path=hub_root)
    assert config.agent_binary("opencode") == "/opt/opencode"


def test_repo_override_file_overrides_repo_defaults(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {"runner": {"sleep_seconds": 5}},
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()
    _write_yaml(
        repo_root / REPO_OVERRIDE_FILENAME,
        {"runner": {"sleep_seconds": 11}},
    )

    config = load_repo_config(repo_root, hub_path=hub_root)
    assert config.runner_sleep_seconds == 11


def test_repo_override_rejects_mode_and_version(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub"},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()
    _write_yaml(
        repo_root / REPO_OVERRIDE_FILENAME,
        {"mode": "repo", "version": 2},
    )

    with pytest.raises(ConfigError):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_env_overrides_hub_env(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub"},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    (hub_root / ".env").write_text("CAR_DOTENV_TEST=hub\n", encoding="utf-8")
    (repo_root / ".env").write_text("CAR_DOTENV_TEST=repo\n", encoding="utf-8")

    previous = os.environ.get("CAR_DOTENV_TEST")
    try:
        load_repo_config(repo_root, hub_path=hub_root)
        assert os.environ.get("CAR_DOTENV_TEST") == "repo"
    finally:
        if previous is None:
            os.environ.pop("CAR_DOTENV_TEST", None)
        else:
            os.environ["CAR_DOTENV_TEST"] = previous


def test_resolve_env_for_root_isolated(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".env").write_text("CAR_DOTENV_TEST=repo\n", encoding="utf-8")

    base_env = {"CAR_DOTENV_TEST": "hub"}
    previous = os.environ.get("CAR_DOTENV_TEST")
    env = resolve_env_for_root(repo_root, base_env=base_env)
    assert env["CAR_DOTENV_TEST"] == "repo"
    assert os.environ.get("CAR_DOTENV_TEST") == previous


def test_repo_docs_reject_absolute_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["docs"]["active_context"] = "/tmp/active_context.md"
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"docs": cfg["docs"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_docs_reject_parent_segments(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["docs"]["spec"] = "../spec.md"
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"docs": cfg["docs"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_log_rejects_absolute_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["log"]["path"] = "/tmp/codex.log"
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"log": cfg["log"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError, match="log.path"):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_log_rejects_parent_segments(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["log"]["path"] = "../codex.log"
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"log": cfg["log"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError, match="log.path"):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_server_log_rejects_absolute_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["server_log"] = {"path": "/tmp/server.log"}
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"server_log": cfg["server_log"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError, match="server_log.path"):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_server_log_rejects_parent_segments(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["server_log"] = {"path": "../server.log"}
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"server_log": cfg["server_log"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError, match="server_log.path"):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_log_accepts_valid_relative_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {"log": {"path": ".codex-autorunner/codex.log"}},
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    config = load_repo_config(repo_root, hub_path=hub_root)
    assert config.log.path.name == "codex.log"


def test_hub_log_rejects_absolute_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "hub": {
                "repos_root": "repos",
                "worktrees_root": "worktrees",
                "manifest": "manifest.yml",
                "discover_depth": 1,
                "auto_init_missing": False,
                "log": {
                    "path": "/tmp/codex.log",
                    "max_bytes": 10485760,
                    "backup_count": 5,
                },
            },
        },
    )

    with pytest.raises(ConfigError, match="log.path"):
        load_hub_config(hub_root)


def test_hub_server_log_rejects_parent_segments(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "hub": {
                "repos_root": "repos",
                "worktrees_root": "worktrees",
                "manifest": "manifest.yml",
                "discover_depth": 1,
                "auto_init_missing": False,
                "log": {
                    "path": ".codex-autorunner/codex.log",
                    "max_bytes": 10485760,
                    "backup_count": 5,
                },
            },
            "server_log": {
                "path": "../server.log",
                "max_bytes": 10485760,
                "backup_count": 5,
            },
        },
    )

    with pytest.raises(ConfigError, match="server_log.path"):
        load_hub_config(hub_root)


def test_static_assets_validation_rejects_negative_max_cache_entries(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "static_assets": {
                "cache_root": ".codex-autorunner/static-cache",
                "max_cache_entries": -1,
                "max_cache_age_days": 30,
            },
        },
    )

    with pytest.raises(
        ConfigError, match="static_assets.max_cache_entries must be >= 0"
    ):
        load_hub_config(hub_root)


def test_static_assets_validation_rejects_negative_max_cache_age_days(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "static_assets": {
                "cache_root": ".codex-autorunner/static-cache",
                "max_cache_entries": 5,
                "max_cache_age_days": -1,
            },
        },
    )

    with pytest.raises(
        ConfigError, match="static_assets.max_cache_age_days must be >= 0"
    ):
        load_hub_config(hub_root)


def test_static_assets_validation_allows_null_max_cache_age_days(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "static_assets": {
                "cache_root": ".codex-autorunner/static-cache",
                "max_cache_entries": 5,
                "max_cache_age_days": None,
            },
        },
    )

    load_hub_config(hub_root)


def test_housekeeping_validation_rejects_invalid_interval_seconds(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "housekeeping": {
                "enabled": True,
                "interval_seconds": 0,
            },
        },
    )

    with pytest.raises(ConfigError, match="housekeeping.interval_seconds must be > 0"):
        load_hub_config(hub_root)


def test_housekeeping_validation_rejects_negative_min_file_age_seconds(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "housekeeping": {
                "enabled": True,
                "interval_seconds": 3600,
                "min_file_age_seconds": -1,
            },
        },
    )

    with pytest.raises(
        ConfigError, match="housekeeping.min_file_age_seconds must be >= 0"
    ):
        load_hub_config(hub_root)
