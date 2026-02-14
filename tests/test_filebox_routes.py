from fastapi.testclient import TestClient

from codex_autorunner.core import filebox
from codex_autorunner.server import create_hub_app


def test_hub_filebox_delete_removes_only_resolved_file(hub_env) -> None:
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
