from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.apps import (
    AppInstallConflictError,
    AppInstallError,
    compute_bundle_sha,
    get_installed_app,
    install_app,
    installed_app_paths,
    list_installed_apps,
    load_app_lock,
)
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


def _write_valid_app(
    app_repo: Path,
    *,
    version: str = "1.0.0",
    description: str = "Installable app.",
    script_body: str = "print('hello')\n",
) -> None:
    app_root = app_repo / "apps" / "hello"
    app_root.mkdir(parents=True, exist_ok=True)
    (app_root / "car-app.yaml").write_text(
        f"""schema_version: 1
id: local.hello
name: Hello App
version: {version}
description: {description}
entrypoint:
  template: templates/bootstrap.md
tools:
  check:
    argv: ["python3", "scripts/check.py"]
""",
        encoding="utf-8",
    )
    (app_root / "templates").mkdir(exist_ok=True)
    (app_root / "templates" / "bootstrap.md").write_text(
        "# Bootstrap\n", encoding="utf-8"
    )
    (app_root / "scripts").mkdir(exist_ok=True)
    (app_root / "scripts" / "check.py").write_text(script_body, encoding="utf-8")


def _setup_install_env(tmp_path: Path) -> tuple[Path, Path, Path]:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    app_repo = tmp_path / "app_repo"
    _init_repo(app_repo)
    _configure_apps_repo(hub_root, app_repo)
    return hub_root, repo_root, app_repo


def test_install_valid_app_creates_repo_local_layout(tmp_path: Path) -> None:
    hub_root, repo_root, app_repo = _setup_install_env(tmp_path)
    _write_valid_app(app_repo)
    commit_sha = _commit_repo(app_repo, "add hello app")
    hub_config = load_hub_config(hub_root)

    result = install_app(hub_config, hub_root, repo_root, "local:apps/hello")
    paths = installed_app_paths(repo_root, "local.hello")

    assert result.changed is True
    assert result.app.app_id == "local.hello"
    assert paths.lock_path.exists()
    assert paths.bundle_root.exists()
    assert paths.state_root.exists()
    assert paths.artifacts_root.exists()
    assert paths.logs_root.exists()
    assert (paths.bundle_root / "car-app.yaml").exists()
    assert (paths.bundle_root / "templates" / "bootstrap.md").exists()
    assert (paths.bundle_root / "scripts" / "check.py").exists()
    assert result.app.lock.commit_sha == commit_sha
    assert result.app.bundle_verified is True


def test_lock_contents_and_lookup(tmp_path: Path) -> None:
    hub_root, repo_root, app_repo = _setup_install_env(tmp_path)
    _write_valid_app(app_repo, version="2.3.4", description="Lock metadata app.")
    commit_sha = _commit_repo(app_repo, "add hello app")
    hub_config = load_hub_config(hub_root)

    result = install_app(hub_config, hub_root, repo_root, "local:apps/hello")
    lock = load_app_lock(result.app.paths.lock_path)
    installed = get_installed_app(repo_root, "local.hello")

    assert lock.id == "local.hello"
    assert lock.version == "2.3.4"
    assert lock.source_repo_id == "local"
    assert lock.source_url == str(app_repo)
    assert lock.source_path == "apps/hello"
    assert lock.source_ref == "main"
    assert lock.commit_sha == commit_sha
    assert lock.manifest_sha
    assert lock.bundle_sha == compute_bundle_sha(result.app.paths.bundle_root)
    assert lock.trusted is True
    assert lock.installed_at.endswith("Z")
    assert installed is not None
    assert installed.lock == lock
    assert installed.bundle_verified is True


def test_compute_bundle_sha_is_deterministic(tmp_path: Path) -> None:
    bundle_one = tmp_path / "bundle-one"
    bundle_two = tmp_path / "bundle-two"
    for bundle in (bundle_one, bundle_two):
        (bundle / "scripts").mkdir(parents=True)
        (bundle / "templates").mkdir(parents=True)

    (bundle_one / "scripts" / "check.py").write_text("print('a')\n", encoding="utf-8")
    (bundle_one / "templates" / "bootstrap.md").write_text(
        "# Bootstrap\n", encoding="utf-8"
    )

    (bundle_two / "templates" / "bootstrap.md").write_text(
        "# Bootstrap\n", encoding="utf-8"
    )
    (bundle_two / "scripts" / "check.py").write_text("print('a')\n", encoding="utf-8")

    assert compute_bundle_sha(bundle_one) == compute_bundle_sha(bundle_two)


def test_idempotent_reinstall_when_lock_matches(tmp_path: Path) -> None:
    hub_root, repo_root, app_repo = _setup_install_env(tmp_path)
    _write_valid_app(app_repo)
    _commit_repo(app_repo, "add hello app")
    hub_config = load_hub_config(hub_root)

    first = install_app(hub_config, hub_root, repo_root, "local:apps/hello")
    second = install_app(hub_config, hub_root, repo_root, "local:apps/hello")

    assert first.changed is True
    assert second.changed is False
    assert second.app.lock.installed_at == first.app.lock.installed_at


def test_collision_without_force_fails_clearly(tmp_path: Path) -> None:
    hub_root, repo_root, app_repo = _setup_install_env(tmp_path)
    _write_valid_app(app_repo, version="1.0.0")
    _commit_repo(app_repo, "add hello v1")
    hub_config = load_hub_config(hub_root)
    install_app(hub_config, hub_root, repo_root, "local:apps/hello")

    _write_valid_app(app_repo, version="2.0.0", script_body="print('updated')\n")
    _commit_repo(app_repo, "update hello v2")

    with pytest.raises(AppInstallConflictError, match="already installed"):
        install_app(hub_config, hub_root, repo_root, "local:apps/hello")


def test_forced_reinstall_updates_existing_install(tmp_path: Path) -> None:
    hub_root, repo_root, app_repo = _setup_install_env(tmp_path)
    _write_valid_app(app_repo, version="1.0.0")
    _commit_repo(app_repo, "add hello v1")
    hub_config = load_hub_config(hub_root)
    first = install_app(hub_config, hub_root, repo_root, "local:apps/hello")

    _write_valid_app(app_repo, version="2.0.0", script_body="print('updated')\n")
    second_commit = _commit_repo(app_repo, "update hello v2")
    second = install_app(
        hub_config, hub_root, repo_root, "local:apps/hello", force=True
    )

    assert second.changed is True
    assert second.app.lock.version == "2.0.0"
    assert second.app.lock.commit_sha == second_commit
    assert second.app.lock.bundle_sha != first.app.lock.bundle_sha
    assert (second.app.paths.bundle_root / "scripts" / "check.py").read_text(
        encoding="utf-8"
    ) == "print('updated')\n"


def test_path_escape_symlink_is_not_materialized(tmp_path: Path) -> None:
    hub_root, repo_root, app_repo = _setup_install_env(tmp_path)
    _write_valid_app(app_repo)
    outside = app_repo / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (app_repo / "apps" / "hello" / "scripts" / "escape-link").symlink_to(
        "../../outside.txt"
    )
    _commit_repo(app_repo, "add hello app with symlink")
    hub_config = load_hub_config(hub_root)

    result = install_app(hub_config, hub_root, repo_root, "local:apps/hello")

    assert not (result.app.paths.bundle_root / "scripts" / "escape-link").exists()
    assert (result.app.paths.bundle_root / "scripts" / "check.py").exists()


def test_invalid_manifest_refuses_install(tmp_path: Path) -> None:
    hub_root, repo_root, app_repo = _setup_install_env(tmp_path)
    app_root = app_repo / "apps" / "hello"
    app_root.mkdir(parents=True, exist_ok=True)
    (app_root / "car-app.yaml").write_text(
        "schema_version: 1\nname: Broken App\nversion: 0.0.1\n",
        encoding="utf-8",
    )
    _commit_repo(app_repo, "add broken app")
    hub_config = load_hub_config(hub_root)

    with pytest.raises(AppInstallError, match="must be a string"):
        install_app(hub_config, hub_root, repo_root, "local:apps/hello")

    assert get_installed_app(repo_root, "local.hello") is None
    assert list_installed_apps(repo_root) == []
