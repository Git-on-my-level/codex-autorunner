"""Snapshot tests for default config structures.

These tests ensure that any changes to default config output are intentional
by comparing against golden JSON files.
"""

import json
from pathlib import Path

from codex_autorunner.core.config import (
    DEFAULT_HUB_CONFIG,
    DEFAULT_REPO_CONFIG,
)


def _get_fixtures_dir() -> Path:
    """Get the fixtures directory."""
    return Path(__file__).parent / "fixtures"


def test_default_repo_config_snapshot():
    """DEFAULT_REPO_CONFIG matches golden snapshot."""
    fixtures_dir = _get_fixtures_dir()
    snapshot_path = fixtures_dir / "default_repo_config.v2.json"

    expected = json.loads(snapshot_path.read_text())
    actual = json.loads(json.dumps(DEFAULT_REPO_CONFIG, sort_keys=True, indent=2))

    assert actual == expected, (
        f"DEFAULT_REPO_CONFIG has changed. "
        f"If intentional, update {snapshot_path.relative_to(Path.cwd())} with:\n"
        f"  echo '{json.dumps(DEFAULT_REPO_CONFIG, sort_keys=True, indent=2)}' > {snapshot_path}"
    )


def test_default_hub_config_snapshot():
    """DEFAULT_HUB_CONFIG matches golden snapshot."""
    fixtures_dir = _get_fixtures_dir()
    snapshot_path = fixtures_dir / "default_hub_config.v2.json"

    expected = json.loads(snapshot_path.read_text())
    actual = json.loads(json.dumps(DEFAULT_HUB_CONFIG, sort_keys=True, indent=2))

    assert actual == expected, (
        f"DEFAULT_HUB_CONFIG has changed. "
        f"If intentional, update {snapshot_path.relative_to(Path.cwd())} with:\n"
        f"  echo '{json.dumps(DEFAULT_HUB_CONFIG, sort_keys=True, indent=2)}' > {snapshot_path}"
    )
