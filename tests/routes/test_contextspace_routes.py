from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.web.app import create_repo_app


def _client_for_repo(repo_root: Path) -> TestClient:
    seed_hub_files(repo_root.parent, force=True)
    seed_repo_files(repo_root, git_required=False)
    (repo_root / ".git").mkdir(exist_ok=True)
    app = create_repo_app(repo_root)
    return TestClient(app)


def test_contextspace_rejects_invalid_doc_kind(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    res = client.put("/api/contextspace/binary", json={"content": "nope"})
    assert res.status_code == 400
    assert res.json()["detail"] == "invalid contextspace doc kind"
