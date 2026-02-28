import json
import os
from pathlib import Path

import pytest
import yaml

from codex_autorunner.bootstrap import GITIGNORE_CONTENT, seed_repo_files
from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
    load_repo_config,
)
from codex_autorunner.discovery import discover_and_init
from codex_autorunner.manifest import (
    MANIFEST_HEADER,
    load_manifest,
    sanitize_repo_id,
    save_manifest,
)
from tests.conftest import write_test_config


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


def test_manifest_roundtrip_preserves_destination(tmp_path: Path):
    hub_root = tmp_path / "hub"
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    repo_dir = hub_root / "projects" / "demo-repo"
    repo_dir.mkdir(parents=True)
    repo = manifest.ensure_repo(hub_root, repo_dir)
    repo.destination = {"kind": "docker", "image": "ghcr.io/acme/demo:latest"}
    save_manifest(manifest_path, manifest, hub_root)

    loaded = load_manifest(manifest_path, hub_root)
    loaded_repo = loaded.get(repo.id)
    assert loaded_repo is not None
    assert loaded_repo.destination == {
        "kind": "docker",
        "image": "ghcr.io/acme/demo:latest",
    }

    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert data["repos"][0]["destination"] == {
        "kind": "docker",
        "image": "ghcr.io/acme/demo:latest",
    }


def test_load_manifest_ignores_invalid_destination_shapes(tmp_path: Path):
    hub_root = tmp_path / "hub"
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "\n".join(
            [
                "version: 2",
                "repos:",
                "  - id: base",
                "    path: workspace/base",
                "    enabled: true",
                "    auto_run: false",
                "    kind: base",
                "    destination: not-a-dict",
                "  - id: wt",
                "    path: worktrees/wt",
                "    enabled: true",
                "    auto_run: false",
                "    kind: worktree",
                "    worktree_of: base",
                "    destination:",
                "      image: missing-kind",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = load_manifest(manifest_path, hub_root)
    base = manifest.get("base")
    worktree = manifest.get("wt")
    assert base is not None
    assert worktree is not None
    assert base.destination is None
    assert worktree.destination is None


def test_save_manifest_atomic_write_keeps_header_and_uses_replace(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root = tmp_path / "hub"
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    repo_dir = hub_root / "projects" / "demo-repo"
    repo_dir.mkdir(parents=True)
    manifest.ensure_repo(hub_root, repo_dir, repo_id="demo")

    real_replace = os.replace
    seen: dict[str, Path] = {}

    def _capture_replace(src: Path, dst: Path) -> None:
        seen["src"] = src
        seen["dst"] = dst
        real_replace(src, dst)

    monkeypatch.setattr("codex_autorunner.manifest.os.replace", _capture_replace)

    save_manifest(manifest_path, manifest, hub_root)

    text = manifest_path.read_text(encoding="utf-8")
    assert text.startswith(MANIFEST_HEADER)
    assert seen["dst"] == manifest_path
    assert seen["src"].parent == manifest_path.parent
    assert seen["src"].name.startswith(f".{manifest_path.name}.")
    assert seen["src"].suffix == ".tmp"


def test_save_manifest_atomic_write_replace_failure_leaves_existing_manifest_intact(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root = tmp_path / "hub"
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    initial_content = "version: 2\nrepos: []\n"
    manifest_path.write_text(initial_content, encoding="utf-8")

    manifest = load_manifest(manifest_path, hub_root)
    repo_dir = hub_root / "projects" / "demo-repo"
    repo_dir.mkdir(parents=True)
    manifest.ensure_repo(hub_root, repo_dir, repo_id="demo")

    def _fail_replace(_src: Path, _dst: Path) -> None:
        raise OSError("replace interrupted")

    monkeypatch.setattr("codex_autorunner.manifest.os.replace", _fail_replace)

    with pytest.raises(OSError, match="replace interrupted"):
        save_manifest(manifest_path, manifest, hub_root)

    assert manifest_path.read_text(encoding="utf-8") == initial_content
    assert not list(manifest_path.parent.glob(f".{manifest_path.name}.*.tmp"))


def test_discovery_adds_repo_and_autoinits(tmp_path: Path):
    hub_root = tmp_path / "hub"
    config = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    config["hub"]["repos_root"] = "workspace"
    config_path = hub_root / CONFIG_FILENAME
    write_test_config(config_path, config)

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
    write_test_config(config_path, config)

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


def test_discovery_skips_hub_root_repo_by_default(tmp_path: Path):
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    (hub_root / ".git").mkdir(parents=True, exist_ok=True)
    seed_repo_files(hub_root, force=False, git_required=False)

    config = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    config["hub"]["repos_root"] = "."
    config_path = hub_root / CONFIG_FILENAME
    write_test_config(config_path, config)

    hub_config = load_hub_config(hub_root)
    _, records = discover_and_init(hub_config)

    assert not any(r.absolute_path == hub_root.resolve() for r in records)

    manifest_data = yaml.safe_load(
        (hub_root / ".codex-autorunner" / "manifest.yml").read_text()
    )
    assert not any(r["path"] == "." for r in manifest_data["repos"])


def test_discovery_includes_hub_root_repo_when_enabled(tmp_path: Path):
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    (hub_root / ".git").mkdir(parents=True, exist_ok=True)
    seed_repo_files(hub_root, force=False, git_required=False)

    config = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    config["hub"]["repos_root"] = "."
    config["hub"]["include_root_repo"] = True
    config_path = hub_root / CONFIG_FILENAME
    write_test_config(config_path, config)

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


def test_discovery_removes_existing_hub_root_repo_when_disabled(tmp_path: Path):
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    (hub_root / ".git").mkdir(parents=True, exist_ok=True)
    seed_repo_files(hub_root, force=False, git_required=False)

    config = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    config["hub"]["repos_root"] = "."
    config["hub"]["include_root_repo"] = False
    config_path = hub_root / CONFIG_FILENAME
    write_test_config(config_path, config)

    manifest = load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)
    manifest.ensure_repo(hub_root, hub_root, repo_id="hub", display_name="hub")
    save_manifest(hub_root / ".codex-autorunner" / "manifest.yml", manifest, hub_root)

    hub_config = load_hub_config(hub_root)
    manifest, records = discover_and_init(hub_config)

    assert not any(r.absolute_path == hub_root.resolve() for r in records)
    assert not any(entry.path.as_posix() == "." for entry in manifest.repos)


def test_load_repo_config_uses_nearest_hub_config(tmp_path: Path):
    hub_root = tmp_path / "hub"
    write_test_config(hub_root / CONFIG_FILENAME, DEFAULT_HUB_CONFIG)
    repo_dir = hub_root / "child"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    seed_repo_files(repo_dir, force=False)

    hub_cfg = load_hub_config(hub_root)
    assert hub_cfg.mode == "hub"

    repo_cfg = load_repo_config(repo_dir / "nested" / "path")
    assert repo_cfg.mode == "repo"
    assert repo_cfg.root == repo_dir.resolve()
