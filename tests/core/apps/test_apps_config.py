from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.config import (
    DEFAULT_HUB_CONFIG,
    ConfigError,
    load_hub_config,
    load_repo_config,
)
from codex_autorunner.core.config_parsers import _parse_apps_config


def test_parse_apps_config_valid() -> None:
    parsed = _parse_apps_config(
        {
            "enabled": True,
            "repos": [
                {
                    "id": "local",
                    "url": "https://example.com/apps.git",
                    "trusted": True,
                    "default_ref": "stable",
                }
            ],
        },
        {},
    )

    assert parsed.enabled is True
    assert len(parsed.repos) == 1
    assert parsed.repos[0].id == "local"
    assert parsed.repos[0].url == "https://example.com/apps.git"
    assert parsed.repos[0].trusted is True
    assert parsed.repos[0].default_ref == "stable"


def test_parse_apps_config_disabled() -> None:
    parsed = _parse_apps_config({"enabled": False, "repos": []}, {})

    assert parsed.enabled is False
    assert parsed.repos == []


def test_parse_apps_config_rejects_duplicate_repo_ids() -> None:
    with pytest.raises(ConfigError, match=r"apps\.repos\[1\]\.id must be unique"):
        _parse_apps_config(
            {
                "repos": [
                    {"id": "dup", "url": "https://example.com/one.git"},
                    {"id": "dup", "url": "https://example.com/two.git"},
                ]
            },
            {},
        )


@pytest.mark.parametrize(
    ("cfg", "pattern"),
    [
        ({"repos": "bad"}, r"apps\.repos must be a list"),
        ({"repos": [123]}, r"apps\.repos\[0\] must be a mapping"),
        ({"repos": [{"id": "x"}]}, r"apps\.repos\[0\]\.url must be a non-empty string"),
        (
            {"repos": [{"id": "", "url": "https://example.com/apps.git"}]},
            r"apps\.repos\[0\]\.id must be a non-empty string",
        ),
    ],
)
def test_parse_apps_config_rejects_invalid_repo_values(cfg, pattern: str) -> None:
    with pytest.raises(ConfigError, match=pattern):
        _parse_apps_config(cfg, {})


def test_default_loaded_config_exposes_apps_shape(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)

    repo_root = hub_root / "worktrees" / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)

    hub_config = load_hub_config(hub_root)
    repo_config = load_repo_config(repo_root, hub_path=hub_root)

    assert hub_config.apps.enabled is True
    assert hub_config.apps.repos
    assert hub_config.apps.repos[0].id == "blessed"
    assert hub_config.apps.repos[0].url == (
        "https://github.com/Git-on-my-level/blessed-car-apps"
    )
    assert repo_config.apps.enabled is True
    assert repo_config.apps.repos == hub_config.apps.repos


def test_default_hub_config_keeps_template_and_app_catalogs_separate() -> None:
    assert DEFAULT_HUB_CONFIG["templates"]["repos"][0]["id"] == "blessed"
    assert DEFAULT_HUB_CONFIG["templates"]["repos"][0]["url"] == (
        "https://github.com/Git-on-my-level/car-ticket-templates"
    )

    assert DEFAULT_HUB_CONFIG["apps"]["repos"][0]["id"] == "blessed"
    assert DEFAULT_HUB_CONFIG["apps"]["repos"][0]["url"] == (
        "https://github.com/Git-on-my-level/blessed-car-apps"
    )
