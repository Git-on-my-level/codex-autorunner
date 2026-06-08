import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core import filebox
from codex_autorunner.core.artifact_delivery import ArtifactDeliveryService
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.services.web_artifact_delivery import (
    drain_web_artifact_deliveries,
)

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


def test_hub_filebox_bulk_delete_only_clears_requested_box(_filebox_env) -> None:
    env = _filebox_env
    filebox.ensure_structure(env.repo_root)
    filebox.delete_regular_files(filebox.inbox_dir(env.repo_root))
    filebox.delete_regular_files(filebox.outbox_dir(env.repo_root))
    (filebox.inbox_dir(env.repo_root) / "upload.txt").write_bytes(b"upload")
    (filebox.outbox_dir(env.repo_root) / "reply.txt").write_bytes(b"reply")

    resp = env.client.delete(f"/hub/filebox/{env.repo_id}/inbox")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "deleted": ["upload.txt"],
        "deleted_count": 1,
    }
    assert not (filebox.inbox_dir(env.repo_root) / "upload.txt").exists()
    assert (filebox.outbox_dir(env.repo_root) / "reply.txt").exists()


def test_repo_filebox_bulk_delete_parity_with_hub(_filebox_env) -> None:
    env = _filebox_env
    filebox.ensure_structure(env.repo_root)
    filebox.delete_regular_files(filebox.inbox_dir(env.repo_root))
    (filebox.inbox_dir(env.repo_root) / "repo-upload.txt").write_bytes(b"upload")

    resp = env.client.delete(f"/repos/{env.repo_id}/api/filebox/inbox")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "deleted": ["repo-upload.txt"],
        "deleted_count": 1,
    }
    assert not (filebox.inbox_dir(env.repo_root) / "repo-upload.txt").exists()


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


def _enqueue_web_delivery(
    repo_root: Path, *, filename: str, conversation_key: str
) -> str:
    source = repo_root / filename
    source.write_bytes(b"<svg/>" if filename.endswith(".svg") else b"binarydata")
    service = ArtifactDeliveryService(repo_root)
    intent = service.enqueue_file(
        source,
        target_surface="web",
        target_conversation_key=conversation_key,
        workspace_scope=f"repo:{repo_root}",
    )
    return intent.delivery_id


def test_artifact_delivery_inline_disposition_allows_safe_image(_filebox_env) -> None:
    env = _filebox_env
    delivery_id = _enqueue_web_delivery(
        env.repo_root, filename="preview.png", conversation_key="managed_thread:disp"
    )

    resp = env.client.get(
        f"/hub/filebox/{env.repo_id}/artifacts/deliveries/{delivery_id}/download",
        params={"disposition": "inline"},
    )

    assert resp.status_code == 200
    assert resp.headers["Content-Disposition"].startswith("inline;")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_artifact_delivery_inline_disposition_forces_attachment_for_svg(
    _filebox_env,
) -> None:
    env = _filebox_env
    delivery_id = _enqueue_web_delivery(
        env.repo_root, filename="payload.svg", conversation_key="managed_thread:disp"
    )

    resp = env.client.get(
        f"/hub/filebox/{env.repo_id}/artifacts/deliveries/{delivery_id}/download",
        params={"disposition": "inline"},
    )

    assert resp.status_code == 200
    # Active markup must never be served inline from the hub origin (XSS guard).
    assert resp.headers["Content-Disposition"].startswith("attachment;")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_hub_artifact_deliveries_listing_for_repoless_thread(_filebox_env) -> None:
    env = _filebox_env
    # Hub-workspace deliveries resolve from the hub engine root, not a worktree.
    delivery_id = _enqueue_web_delivery(
        env.hub_root, filename="hublist.png", conversation_key="managed_thread:hub"
    )

    resp = env.client.get("/hub/artifacts/deliveries", params={"surface": "web"})

    assert resp.status_code == 200
    deliveries = resp.json()["deliveries"]
    match = next(d for d in deliveries if d["delivery_id"] == delivery_id)
    # Download URL must point at the hub route, not the unmounted /api route.
    assert "/hub/artifacts/deliveries/" in match["download_url"]
    assert match["download_url"].endswith("/download")

    dl = env.client.get(match["download_url"], params={"disposition": "inline"})
    assert dl.status_code == 200
    assert dl.headers["Content-Disposition"].startswith("inline;")


def test_drain_web_artifact_deliveries_marks_pending_sent(_filebox_env) -> None:
    env = _filebox_env
    conversation_key = "managed_thread:draintest"
    delivery_id = _enqueue_web_delivery(
        env.repo_root, filename="drained.bin", conversation_key=conversation_key
    )
    service = ArtifactDeliveryService(env.repo_root)
    pending = service.list_deliveries(
        states=("pending",),
        target_surface="web",
        target_conversation_key=conversation_key,
    )
    assert any(intent.delivery_id == delivery_id for intent in pending)

    asyncio.run(
        drain_web_artifact_deliveries(
            workspace_root=env.repo_root,
            managed_thread_id="draintest",
            logger=logging.getLogger("test.web_artifact_delivery"),
        )
    )

    sent = service.list_deliveries(
        states=("sent",),
        target_surface="web",
        target_conversation_key=conversation_key,
    )
    assert any(intent.delivery_id == delivery_id for intent in sent)
