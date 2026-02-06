import json
from pathlib import Path

import yaml

from codex_autorunner.bootstrap import GITIGNORE_CONTENT, seed_repo_files
from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
    load_repo_config,
)
from codex_autorunner.discovery import discover_and_init
from codex_autorunner.manifest import load_manifest, sanitize_repo_id, save_manifest


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_manifest_creation_and_normalization(tmp_path: Path):
    hub_root = tmp_path / "hub"
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    assert manifest.repos == []
    repo_dir = hub_root / "projects" / "demo-repo"
    repo_dir.mkdir(parents=True)
    manifest.ensure_repo(hub_root, repo_dir)
    save_manifest(manifest_path, manifest, hub_root)

    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert data["repos"][0]["path"] == "projects/demo-repo"
    assert data["repos"][0]["kind"] == "base"


def test_discovery_adds_repo_and_autoinits(tmp_path: Path):
    hub_root = tmp_path / "hub"
    config = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    config["hub"]["repos_root"] = "workspace"
    config_path = hub_root / CONFIG_FILENAME
    _write_config(config_path, config)

    repos_root = hub_root / "workspace"
    repo_dir = repos_root / "demo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    hub_config = load_hub_config(hub_root)
    manifest, records = discover_and_init(hub_config)

    entry = next(r for r in records if r.repo.id == "demo")
    assert entry.added_to_manifest is True
    assert entry.initialized is True
    assert (repo_dir / ".codex-autorunner" / "state.sqlite3").exists()
    assert not (repo_dir / ".codex-autorunner" / "config.yml").exists()
    car_shim = repo_dir / "car"
    assert not car_shim.exists()
    local_car_shim = repo_dir / ".codex-autorunner" / "bin" / "car"
    assert local_car_shim.exists()
    assert local_car_shim.stat().st_mode & 0o111
    assert "codex_autorunner.cli" in local_car_shim.read_text(encoding="utf-8")
    gitignore = (repo_dir / ".codex-autorunner" / ".gitignore").read_text(
        encoding="utf-8"
    )
    assert gitignore == GITIGNORE_CONTENT

    manifest_data = yaml.safe_load(
        (hub_root / ".codex-autorunner" / "manifest.yml").read_text()
    )
    assert manifest_data["repos"][0]["path"] == "workspace/demo"
    assert manifest_data["repos"][0]["kind"] == "base"


def test_discovery_sanitizes_repo_ids(tmp_path: Path):
    hub_root = tmp_path / "hub"
    config = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    config["hub"]["repos_root"] = "workspace"
    config_path = hub_root / CONFIG_FILENAME
    _write_config(config_path, config)

    repos_root = hub_root / "workspace"
    repo_dir = repos_root / "demo#repo"
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    hub_config = load_hub_config(hub_root)
    manifest, records = discover_and_init(hub_config)

    entry = next(r for r in records if r.repo.display_name == "demo#repo")
    assert entry.repo.id == sanitize_repo_id("demo#repo")

    manifest_data = yaml.safe_load(
        (hub_root / ".codex-autorunner" / "manifest.yml").read_text()
    )
    repo_entry = next(r for r in manifest_data["repos"] if r["id"] == entry.repo.id)
    assert repo_entry["display_name"] == "demo#repo"


def test_discovery_includes_hub_root_repo(tmp_path: Path):
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    (hub_root / ".git").mkdir(parents=True, exist_ok=True)
    seed_repo_files(hub_root, force=False, git_required=False)

    config = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    config["hub"]["repos_root"] = "."
    config_path = hub_root / CONFIG_FILENAME
    _write_config(config_path, config)

    hub_config = load_hub_config(hub_root)
    _, records = discover_and_init(hub_config)

    record = next(r for r in records if r.absolute_path == hub_root.resolve())
    assert record.added_to_manifest is True
    assert record.initialized is True
    assert record.repo.path.as_posix() == "."
    assert record.repo.id == "hub"

    manifest_data = yaml.safe_load(
        (hub_root / ".codex-autorunner" / "manifest.yml").read_text()
    )
    root_entry = next(r for r in manifest_data["repos"] if r["path"] == ".")
    assert root_entry["id"] == "hub"


def test_load_repo_config_uses_nearest_hub_config(tmp_path: Path):
    hub_root = tmp_path / "hub"
    _write_config(hub_root / CONFIG_FILENAME, DEFAULT_HUB_CONFIG)
    repo_dir = hub_root / "child"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    seed_repo_files(repo_dir, force=False)

    hub_cfg = load_hub_config(hub_root)
    assert hub_cfg.mode == "hub"

    repo_cfg = load_repo_config(repo_dir / "nested" / "path")
    assert repo_cfg.mode == "repo"
    assert repo_cfg.root == repo_dir.resolve()
