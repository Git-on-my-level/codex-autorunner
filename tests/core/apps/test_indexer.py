from __future__ import annotations

from pathlib import Path

import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.apps import get_app_by_ref, index_apps
from codex_autorunner.core.config import CONFIG_FILENAME, load_hub_config
from codex_autorunner.core.git_utils import run_git


def _init_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], repo_path, check=True)
    run_git(["config", "user.email", "test@example.com"], repo_path, check=True)
    run_git(["config", "user.name", "Test User"], repo_path, check=True)
    run_git(["checkout", "-b", "main"], repo_path, check=True)


def _commit_repo(repo_path: Path, message: str) -> str:
    run_git(["add", "."], repo_path, check=True)
    run_git(["commit", "-m", message], repo_path, check=True)
    return (run_git(["rev-parse", "HEAD"], repo_path, check=True).stdout or "").strip()


def _configure_apps_repo(hub_root: Path, app_repo: Path) -> None:
    config_path = hub_root / CONFIG_FILENAME
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["apps"] = {
        "enabled": True,
        "repos": [
            {
                "id": "local",
                "url": str(app_repo),
                "trusted": True,
                "default_ref": "main",
            }
        ],
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def test_index_apps_discovers_valid_app_and_skips_invalid_manifest(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)

    app_repo = tmp_path / "app_repo"
    _init_repo(app_repo)
    (app_repo / "apps" / "hello").mkdir(parents=True)
    (app_repo / "apps" / "hello" / "car-app.yaml").write_text(
        """schema_version: 1
id: local.hello
name: Hello App
version: 1.2.3
description: Valid test app.
entrypoint:
  template: templates/bootstrap.md
tools:
  check:
    argv: ["python3", "scripts/check.py"]
""",
        encoding="utf-8",
    )
    (app_repo / "apps" / "broken").mkdir(parents=True)
    (app_repo / "apps" / "broken" / "car-app.yaml").write_text(
        "schema_version: 1\nname: Broken App\nversion: 0.0.1\n",
        encoding="utf-8",
    )
    commit_sha = _commit_repo(app_repo, "add apps")

    _configure_apps_repo(hub_root, app_repo)
    hub_config = load_hub_config(hub_root)

    apps = index_apps(hub_config, hub_root)

    assert len(apps) == 1
    app_info = apps[0]
    assert app_info.repo_id == "local"
    assert app_info.path == "apps/hello"
    assert app_info.ref == "main"
    assert app_info.app_id == "local.hello"
    assert app_info.app_version == "1.2.3"
    assert app_info.app_name == "Hello App"
    assert app_info.description == "Valid test app."
    assert app_info.commit_sha == commit_sha
    assert app_info.manifest_sha
    assert app_info.trusted is True


def test_get_app_by_ref_defaults_to_repo_default_ref(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)

    app_repo = tmp_path / "app_repo"
    _init_repo(app_repo)
    (app_repo / "apps" / "hello").mkdir(parents=True)
    (app_repo / "apps" / "hello" / "car-app.yaml").write_text(
        """schema_version: 1
id: local.hello
name: Hello App
version: 1.0.0
""",
        encoding="utf-8",
    )
    _commit_repo(app_repo, "add app")

    _configure_apps_repo(hub_root, app_repo)
    hub_config = load_hub_config(hub_root)

    app_info = get_app_by_ref(hub_config, hub_root, "local:apps/hello")

    assert app_info is not None
    assert app_info.ref == "main"
    assert app_info.app_id == "local.hello"
