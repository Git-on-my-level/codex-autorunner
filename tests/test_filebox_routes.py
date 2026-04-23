import pytest
from fastapi.testclient import TestClient

from codex_autorunner.core import filebox
from codex_autorunner.server import create_hub_app

pytestmark = pytest.mark.slow


def test_hub_filebox_delete_ignores_legacy_duplicates(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    filebox.ensure_structure(hub_env.repo_root)
    (filebox.outbox_dir(hub_env.repo_root) / "shared.txt").write_bytes(b"primary")
    legacy_topic_pending = (
        hub_env.repo_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-1"
        / "outbox"
        / "pending"
    )
    legacy_topic_pending.mkdir(parents=True, exist_ok=True)
    (legacy_topic_pending / "shared.txt").write_bytes(b"legacy")

    resp = client.delete(f"/hub/filebox/{hub_env.repo_id}/outbox/shared.txt")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert not (filebox.outbox_dir(hub_env.repo_root) / "shared.txt").exists()
    assert (legacy_topic_pending / "shared.txt").exists()


def test_hub_filebox_legacy_only_file_returns_404(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    legacy_topic_pending = (
        hub_env.repo_root
        / ".codex-autorunner"
        / "uploads"
        / "telegram-files"
        / "topic-1"
        / "outbox"
        / "pending"
    )
    legacy_topic_pending.mkdir(parents=True, exist_ok=True)
    (legacy_topic_pending / "legacy.txt").write_bytes(b"legacy")

    resp = client.get(f"/hub/filebox/{hub_env.repo_id}/outbox/legacy.txt")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "File not found"}


def test_filebox_upload_invalid_filename_parity_between_repo_and_hub(hub_env) -> None:
    client = TestClient(create_hub_app(hub_env.hub_root), raise_server_exceptions=False)
    files = {"../bad.txt": ("safe.txt", b"x", "text/plain")}

    repo_resp = client.post(f"/repos/{hub_env.repo_id}/api/filebox/inbox", files=files)
    hub_resp = client.post(f"/hub/filebox/{hub_env.repo_id}/inbox", files=files)

    assert repo_resp.status_code == 400
    assert hub_resp.status_code == 400
    assert repo_resp.json() == {"detail": "Invalid filename"}
    assert hub_resp.json() == {"detail": "Invalid filename"}


@pytest.mark.parametrize("method", ["get", "delete"])
def test_filebox_missing_file_parity_between_repo_and_hub(hub_env, method: str) -> None:
    client = TestClient(create_hub_app(hub_env.hub_root))
    request = getattr(client, method)

    repo_resp = request(f"/repos/{hub_env.repo_id}/api/filebox/inbox/missing.txt")
    hub_resp = request(f"/hub/filebox/{hub_env.repo_id}/inbox/missing.txt")

    assert repo_resp.status_code == 404
    assert hub_resp.status_code == 404
    assert repo_resp.json() == {"detail": "File not found"}
    assert hub_resp.json() == {"detail": "File not found"}


def test_filebox_invalid_box_parity_between_repo_and_hub(hub_env) -> None:
    client = TestClient(create_hub_app(hub_env.hub_root))

    repo_resp = client.get(f"/repos/{hub_env.repo_id}/api/filebox/not-a-box")
    hub_resp = client.get(f"/hub/filebox/{hub_env.repo_id}/not-a-box/missing.txt")

    assert repo_resp.status_code == 400
    assert hub_resp.status_code == 400
    assert repo_resp.json() == {"detail": "Invalid box"}
    assert hub_resp.json() == {"detail": "Invalid box"}
