import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.manifest import MANIFEST_HEADER, load_manifest, save_manifest

runner = CliRunner()


def _seed_hub_with_base_and_worktree(hub_root: Path) -> tuple[Path, str, str]:
    seed_hub_files(hub_root, force=True)
    base_dir = hub_root / "repos" / "base"
    wt_dir = hub_root / "worktrees" / "base--feature"
    base_dir.mkdir(parents=True, exist_ok=True)
    wt_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    base = manifest.ensure_repo(hub_root, base_dir, repo_id="base", kind="base")
    worktree = manifest.ensure_repo(
        hub_root,
        wt_dir,
        repo_id="base--feature",
        kind="worktree",
        worktree_of=base.id,
        branch="feature",
    )
    save_manifest(manifest_path, manifest, hub_root)
    return manifest_path, base.id, worktree.id


def test_hub_destination_show_reports_inherited_effective_destination(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    manifest_path, base_id, worktree_id = _seed_hub_with_base_and_worktree(hub_root)

    manifest = load_manifest(manifest_path, hub_root)
    base = manifest.get(base_id)
    assert base is not None
    base.destination = {"kind": "docker", "image": "ghcr.io/acme/base:latest"}
    save_manifest(manifest_path, manifest, hub_root)

    result = runner.invoke(
        app,
        ["hub", "destination", "show", worktree_id, "--json", "--path", str(hub_root)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["repo_id"] == worktree_id
    assert payload["configured_destination"] is None
    assert payload["source"] == "base"
    assert payload["effective_destination"] == {
        "kind": "docker",
        "image": "ghcr.io/acme/base:latest",
    }


def test_hub_destination_set_docker_updates_manifest_and_preserves_header(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    manifest_path, base_id, _ = _seed_hub_with_base_and_worktree(hub_root)

    result = runner.invoke(
        app,
        [
            "hub",
            "destination",
            "set",
            base_id,
            "docker",
            "--image",
            "busybox:latest",
            "--name",
            "car-demo",
            "--env",
            "CAR_*",
            "--env",
            "PATH",
            "--mount",
            "/tmp/src:/tmp/target",
            "--json",
            "--path",
            str(hub_root),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["repo_id"] == base_id
    assert payload["source"] == "repo"
    assert payload["effective_destination"]["kind"] == "docker"

    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert manifest_text.startswith(MANIFEST_HEADER)
    manifest_data = yaml.safe_load(manifest_text)
    repo_data = next(item for item in manifest_data["repos"] if item["id"] == base_id)
    assert repo_data["destination"] == {
        "kind": "docker",
        "image": "busybox:latest",
        "container_name": "car-demo",
        "env_passthrough": ["CAR_*", "PATH"],
        "mounts": [{"source": "/tmp/src", "target": "/tmp/target"}],
    }


def test_hub_destination_set_local_rejects_worktree_override(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    manifest_path, base_id, worktree_id = _seed_hub_with_base_and_worktree(hub_root)

    manifest = load_manifest(manifest_path, hub_root)
    base = manifest.get(base_id)
    assert base is not None
    base.destination = {"kind": "docker", "image": "ghcr.io/acme/base:latest"}
    save_manifest(manifest_path, manifest, hub_root)

    set_result = runner.invoke(
        app,
        [
            "hub",
            "destination",
            "set",
            worktree_id,
            "local",
            "--json",
            "--path",
            str(hub_root),
        ],
    )
    assert set_result.exit_code != 0
    assert "only be configured on base repos" in set_result.output

    show_result = runner.invoke(
        app,
        ["hub", "destination", "show", worktree_id, "--json", "--path", str(hub_root)],
    )
    assert show_result.exit_code == 0
    show_payload = json.loads(show_result.output)
    assert show_payload["configured_destination"] is None
    assert show_payload["effective_destination"] == {
        "kind": "docker",
        "image": "ghcr.io/acme/base:latest",
    }
    assert show_payload["source"] == "base"
