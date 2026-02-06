from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.web.app import create_repo_app


def _client_for_repo(repo_root: Path) -> TestClient:
    seed_repo_files(repo_root, git_required=False)
    (repo_root / ".git").mkdir(exist_ok=True)
    app = create_repo_app(repo_root)
    return TestClient(app)


def test_contextspace_read_rejects_non_utf8(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    contextspace_dir = repo_root / ".codex-autorunner" / "contextspace"
    contextspace_dir.mkdir(parents=True, exist_ok=True)
    (contextspace_dir / "binary.bin").write_bytes(b"\xf3\xff\x00\x01")

    res = client.get("/api/contextspace/file", params={"path": "binary.bin"})
    assert res.status_code == 400
    assert "utf-8" in res.json()["detail"].lower()
