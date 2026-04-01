from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.surfaces.web.app import create_repo_app


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
    contextspace_dir = snapshot_root / "contextspace"
    contextspace_dir.mkdir(parents=True, exist_ok=True)
    (contextspace_dir / "active_context.md").write_text(
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
    seed_hub_files(repo_root, force=True)
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
    assert "snap-with-meta" in snapshot_ids
    assert "snap-no-meta" not in snapshot_ids

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
        "/api/archive/tree", params={"snapshot_id": "../snap1", "path": "contextspace"}
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
        "/api/archive/tree", params={"snapshot_id": "snap1", "path": "contextspace"}
    )
    assert tree.status_code == 200
    nodes = {node["path"]: node for node in tree.json()["nodes"]}
    assert "contextspace/active_context.md" in nodes
    assert nodes["contextspace/active_context.md"]["type"] == "file"

    read = client.get(
        "/api/archive/file",
        params={"snapshot_id": "snap1", "path": "contextspace/active_context.md"},
    )
    assert read.status_code == 200
    assert read.text.strip() == "Archived context"

    download = client.get(
        "/api/archive/download",
        params={"snapshot_id": "snap1", "path": "contextspace/active_context.md"},
    )
    assert download.status_code == 200
    assert download.content == b"Archived context"


def test_local_archive_tree_reads_any_archived_run_content(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    run_root = repo_root / ".codex-autorunner" / "archive" / "runs" / "run-123"
    (run_root / "contextspace").mkdir(parents=True, exist_ok=True)
    (run_root / "contextspace" / "active_context.md").write_text(
        "Local archived context", encoding="utf-8"
    )
    (run_root / "flow_state" / "chat").mkdir(parents=True, exist_ok=True)
    (run_root / "flow_state" / "chat" / "outbound.jsonl").write_text(
        "{}", encoding="utf-8"
    )

    tree = client.get("/api/archive/local/tree", params={"run_id": "run-123"})
    assert tree.status_code == 200
    nodes = {node["path"]: node for node in tree.json()["nodes"]}
    assert "contextspace" in nodes
    assert "flow_state" in nodes

    read = client.get(
        "/api/archive/local/file",
        params={"run_id": "run-123", "path": "contextspace/active_context.md"},
    )
    assert read.status_code == 200
    assert read.text.strip() == "Local archived context"


def test_local_archive_symlink_escape_is_rejected(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    outside_root = tmp_path / "outside-run"
    (outside_root / "contextspace").mkdir(parents=True, exist_ok=True)
    (outside_root / "contextspace" / "active_context.md").write_text(
        "Escaped local archived context", encoding="utf-8"
    )

    link_root = repo_root / ".codex-autorunner" / "archive" / "runs"
    link_root.mkdir(parents=True, exist_ok=True)
    try:
        (link_root / "run-escape").symlink_to(outside_root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    tree = client.get("/api/archive/local/tree", params={"run_id": "run-escape"})
    assert tree.status_code == 400

    read = client.get(
        "/api/archive/local/file",
        params={"run_id": "run-escape", "path": "contextspace/active_context.md"},
    )
    assert read.status_code == 400

    download = client.get(
        "/api/archive/local/download",
        params={"run_id": "run-escape", "path": "contextspace/active_context.md"},
    )
    assert download.status_code == 400
