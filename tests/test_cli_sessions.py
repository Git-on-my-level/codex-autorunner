from pathlib import Path

from tests.conftest import write_test_config

from codex_autorunner.cli import _resolve_repo_api_path
from codex_autorunner.core.config import CONFIG_FILENAME


def test_resolve_repo_api_path_prefixes_repo_id(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    config_path = hub_root / CONFIG_FILENAME
    write_test_config(
        config_path,
        {"mode": "hub", "hub": {"manifest": ".codex-autorunner/manifest.yml"}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    write_test_config(
        manifest_path,
        {
            "version": 2,
            "repos": [
                {
                    "id": "repo",
                    "path": "repo",
                    "kind": "base",
                    "enabled": True,
                    "auto_run": False,
                }
            ],
        },
    )

    resolved = _resolve_repo_api_path(repo_root, None, "/api/sessions")
    assert resolved == "/repos/repo/api/sessions"


def test_resolve_repo_api_path_falls_back_without_manifest(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    config_path = hub_root / CONFIG_FILENAME
    write_test_config(config_path, {"mode": "hub"})

    repo_root = hub_root / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()

    resolved = _resolve_repo_api_path(repo_root, None, "/api/sessions")
    assert resolved == "/api/sessions"
