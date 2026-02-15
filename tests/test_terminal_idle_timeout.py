from pathlib import Path

import pytest
from tests.conftest import write_test_config

from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    ConfigError,
    load_repo_config,
)


def test_terminal_idle_timeout_loaded(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "terminal": {"idle_timeout_seconds": 900}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()
    config = load_repo_config(repo_root, hub_path=hub_root)
    assert config.terminal_idle_timeout_seconds == 900


def test_terminal_idle_timeout_rejects_negative(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "terminal": {"idle_timeout_seconds": -5}},
    )

    with pytest.raises(ConfigError):
        load_repo_config(hub_root / "repo", hub_path=hub_root)
