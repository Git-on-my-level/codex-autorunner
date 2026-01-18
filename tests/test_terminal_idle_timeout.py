from pathlib import Path

import pytest
import yaml

from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    ConfigError,
    load_repo_config,
)


def _write_config(root: Path, data: dict) -> None:
    config_path = root / CONFIG_FILENAME
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )


def test_terminal_idle_timeout_loaded(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_config(
        hub_root,
        {"mode": "hub", "terminal": {"idle_timeout_seconds": 900}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()
    config = load_repo_config(repo_root, hub_path=hub_root)
    assert config.terminal_idle_timeout_seconds == 900


def test_terminal_idle_timeout_rejects_negative(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    _write_config(
        hub_root,
        {"mode": "hub", "terminal": {"idle_timeout_seconds": -5}},
    )

    with pytest.raises(ConfigError):
        load_repo_config(hub_root / "repo", hub_path=hub_root)
