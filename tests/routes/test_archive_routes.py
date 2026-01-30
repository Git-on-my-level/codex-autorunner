from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.web.app import create_repo_app


def _write_snapshot(
    repo_root: Path,
    worktree_id: str,
    snapshot_id: str,
    *,
    with_meta: bool = False,
) -> Path:
    snapshot_root = (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "worktrees"
        / worktree_id
        / snapshot_id
    )
    snapshot_root.mkdir(parents=True, exist_ok=True)
    workspace_dir = snapshot_root / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "active_context.md").write_text(
        "Archived context", encoding="utf-8"
    )
    if with_meta:
        meta = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "created_at": "2026-01-30T03:15:22Z",
            "status": "complete",
            "base_repo_id": "base",
            "worktree_repo_id": worktree_id,
            "worktree_of": "base",
            "branch": "feature/archive-viewer",
            "head_sha": "deadbeef",
            "source": {
                "path": "worktrees/example",
                "copied_paths": [],
                "missing_paths": [],
            },
            "summary": {"file_count": 1, "total_bytes": 12},
            "note": "unit test",
        }
        (snapshot_root / "META.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
    return snapshot_root


def _client_for_repo(repo_root: Path) -> TestClient:
    seed_repo_files(repo_root, git_required=False)
    (repo_root / ".git").mkdir(exist_ok=True)
    app = create_repo_app(repo_root)
    return TestClient(app)


def test_archive_snapshots_list_and_detail(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    _write_snapshot(repo_root, "wt1", "snap-no-meta", with_meta=False)
    _write_snapshot(repo_root, "wt2", "snap-with-meta", with_meta=True)

    res = client.get("/api/archive/snapshots")
    assert res.status_code == 200
    payload = res.json()
    snapshot_ids = {item["snapshot_id"] for item in payload["snapshots"]}
    assert {"snap-no-meta", "snap-with-meta"} <= snapshot_ids

    detail = client.get(
        "/api/archive/snapshots/snap-no-meta", params={"worktree_repo_id": "wt1"}
    )
    assert detail.status_code == 200
    data = detail.json()
    assert data["snapshot"]["snapshot_id"] == "snap-no-meta"
    assert data["meta"] is None

    detail2 = client.get(
        "/api/archive/snapshots/snap-with-meta", params={"worktree_repo_id": "wt2"}
    )
    assert detail2.status_code == 200
    assert detail2.json()["snapshot"]["status"] == "complete"


def test_archive_missing_snapshot_returns_404(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    res = client.get("/api/archive/snapshots/missing")
    assert res.status_code == 404


def test_archive_traversal_is_rejected(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    _write_snapshot(repo_root, "wt1", "snap1", with_meta=False)

    res = client.get(
        "/api/archive/tree", params={"snapshot_id": "../snap1", "path": "workspace"}
    )
    assert res.status_code == 400

    res = client.get(
        "/api/archive/tree", params={"snapshot_id": "snap1", "path": "../secret"}
    )
    assert res.status_code == 400

    res = client.get(
        "/api/archive/file",
        params={"snapshot_id": "snap1", "path": "C:windows/system.ini"},
    )
    assert res.status_code == 400


def test_archive_tree_and_file_reads(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    _write_snapshot(repo_root, "wt1", "snap1", with_meta=False)

    tree = client.get(
        "/api/archive/tree", params={"snapshot_id": "snap1", "path": "workspace"}
    )
    assert tree.status_code == 200
    nodes = {node["path"]: node for node in tree.json()["nodes"]}
    assert "workspace/active_context.md" in nodes
    assert nodes["workspace/active_context.md"]["type"] == "file"

    read = client.get(
        "/api/archive/file",
        params={"snapshot_id": "snap1", "path": "workspace/active_context.md"},
    )
    assert read.status_code == 200
    assert read.text.strip() == "Archived context"

    download = client.get(
        "/api/archive/download",
        params={"snapshot_id": "snap1", "path": "workspace/active_context.md"},
    )
    assert download.status_code == 200
    assert download.content == b"Archived context"
