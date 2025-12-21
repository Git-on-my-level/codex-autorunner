import json
from pathlib import Path

import pytest
import yaml

from codex_autorunner.config import (
    CONFIG_FILENAME,
    DEFAULT_REPO_CONFIG,
    ConfigError,
    load_config,
)


def _write_config(repo_root: Path, data: dict) -> None:
    config_path = repo_root / CONFIG_FILENAME
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )


def test_terminal_idle_timeout_loaded(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["terminal"]["idle_timeout_seconds"] = 900
    _write_config(repo_root, cfg)

    config = load_config(repo_root)

    assert config.terminal_idle_timeout_seconds == 900


def test_terminal_idle_timeout_rejects_negative(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["terminal"]["idle_timeout_seconds"] = -5
    _write_config(repo_root, cfg)

    with pytest.raises(ConfigError):
        load_config(repo_root)
