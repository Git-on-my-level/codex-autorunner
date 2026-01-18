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
    cfg["docs"]["todo"] = "/tmp/TODO.md"
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
    cfg["docs"]["summary"] = "../SUMMARY.md"
    _write_yaml(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"docs": cfg["docs"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError):
        load_repo_config(repo_root, hub_path=hub_root)
