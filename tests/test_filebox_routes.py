from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core import filebox
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app

pytestmark = pytest.mark.slow


@dataclass(frozen=True)
class _FileboxEnv:
    hub_root: Path
    repo_id: str
    repo_root: Path
    app: object
    client: TestClient


@pytest.fixture(scope="module")
def _filebox_env(tmp_path_factory):
    hub_root = tmp_path_factory.mktemp("hub")
    seed_hub_files(hub_root, force=True)
    repo_id = "repo"
    repo_root = hub_root / "worktrees" / repo_id
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)
    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(hub_config.manifest_path, manifest, hub_root)
    app = create_hub_app(hub_root)
    yield _FileboxEnv(
        hub_root=hub_root,
        repo_id=repo_id,
        repo_root=repo_root,
        app=app,
        client=TestClient(app),
    )


def test_hub_filebox_delete_ignores_legacy_duplicates(_filebox_env) -> None:
    env = _filebox_env
    filebox.ensure_structure(env.repo_root)
    (filebox.outbox_dir(env.repo_root) / "shared.txt").write_bytes(b"primary")
    legacy_topic_pending = (
        env.repo_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-1"
        / "outbox"
        / "pending"
    )
    legacy_topic_pending.mkdir(parents=True, exist_ok=True)
    (legacy_topic_pending / "shared.txt").write_bytes(b"legacy")

    resp = env.client.delete(f"/hub/filebox/{env.repo_id}/outbox/shared.txt")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert not (filebox.outbox_dir(env.repo_root) / "shared.txt").exists()
    assert (legacy_topic_pending / "shared.txt").exists()


def test_hub_filebox_legacy_only_file_returns_404(_filebox_env) -> None:
    env = _filebox_env
    legacy_topic_pending = (
        env.repo_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-1"
        / "outbox"
        / "pending"
    )
    legacy_topic_pending.mkdir(parents=True, exist_ok=True)
    (legacy_topic_pending / "legacy.txt").write_bytes(b"legacy")

    resp = env.client.get(f"/hub/filebox/{env.repo_id}/outbox/legacy.txt")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "File not found"}


def test_filebox_upload_invalid_filename_parity_between_repo_and_hub(
    _filebox_env,
) -> None:
    env = _filebox_env
    client = TestClient(env.app, raise_server_exceptions=False)
    files = {"../bad.txt": ("safe.txt", b"x", "text/plain")}

    repo_resp = client.post(f"/repos/{env.repo_id}/api/filebox/inbox", files=files)
    hub_resp = client.post(f"/hub/filebox/{env.repo_id}/inbox", files=files)

    assert repo_resp.status_code == 400
    assert hub_resp.status_code == 400
    assert repo_resp.json() == {"detail": "Invalid filename"}
    assert hub_resp.json() == {"detail": "Invalid filename"}


@pytest.mark.parametrize("method", ["get", "delete"])
def test_filebox_missing_file_parity_between_repo_and_hub(
    _filebox_env, method: str
) -> None:
    env = _filebox_env
    request = getattr(env.client, method)

    repo_resp = request(f"/repos/{env.repo_id}/api/filebox/inbox/missing.txt")
    hub_resp = request(f"/hub/filebox/{env.repo_id}/inbox/missing.txt")

    assert repo_resp.status_code == 404
    assert hub_resp.status_code == 404
    assert repo_resp.json() == {"detail": "File not found"}
    assert hub_resp.json() == {"detail": "File not found"}


def test_filebox_invalid_box_parity_between_repo_and_hub(_filebox_env) -> None:
    env = _filebox_env

    repo_resp = env.client.get(f"/repos/{env.repo_id}/api/filebox/not-a-box")
    hub_resp = env.client.get(f"/hub/filebox/{env.repo_id}/not-a-box/missing.txt")

    assert repo_resp.status_code == 400
    assert hub_resp.status_code == 400
    assert repo_resp.json() == {"detail": "Invalid box"}
    assert hub_resp.json() == {"detail": "Invalid box"}
