import json
from pathlib import Path

import yaml

from codex_autorunner.bootstrap import GITIGNORE_CONTENT, seed_repo_files
from codex_autorunner.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_config,
)
from codex_autorunner.discovery import discover_and_init
from codex_autorunner.manifest import load_manifest, save_manifest


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

    hub_config = load_config(hub_root)
    manifest, records = discover_and_init(hub_config)  # type: ignore[arg-type]

    entry = next(r for r in records if r.repo.id == "demo")
    assert entry.added_to_manifest is True
    assert entry.initialized is True
    assert (repo_dir / ".codex-autorunner" / "config.yml").exists()
    gitignore = (repo_dir / ".codex-autorunner" / ".gitignore").read_text(
        encoding="utf-8"
    )
    assert gitignore == GITIGNORE_CONTENT

    manifest_data = yaml.safe_load(
        (hub_root / ".codex-autorunner" / "manifest.yml").read_text()
    )
    assert manifest_data["repos"][0]["path"] == "workspace/demo"
    assert manifest_data["repos"][0]["kind"] == "base"


def test_nearest_config_prefers_repo_over_hub(tmp_path: Path):
    hub_root = tmp_path / "hub"
    _write_config(hub_root / CONFIG_FILENAME, DEFAULT_HUB_CONFIG)
    repo_dir = hub_root / "child"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    seed_repo_files(repo_dir, force=False)

    hub_cfg = load_config(hub_root)
    assert hub_cfg.mode == "hub"

    repo_cfg = load_config(repo_dir / "nested" / "path")
    assert repo_cfg.mode == "repo"
    assert repo_cfg.root == repo_dir.resolve()
