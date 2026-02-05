"""Tests for flows routes security, particularly path traversal protection."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.routes import flows as flows_routes


def _seed_paused_run(repo_root: Path, run_id: str) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    store.create_flow_run(
        run_id,
        "ticket_flow",
        input_data={
            "workspace_root": str(repo_root),
            "runs_dir": ".codex-autorunner/runs",
        },
        state={},
        metadata={},
    )
    store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)


def _create_reply_file(
    repo_root: Path, run_id: str, seq: str, filename: str, content: str = "test content"
) -> Path:
    reply_dir = (
        repo_root / ".codex-autorunner" / "runs" / run_id / "reply_history" / seq
    )
    reply_dir.mkdir(parents=True, exist_ok=True)
    file_path = reply_dir / filename
    file_path.write_text(content, encoding="utf-8")
    return file_path


def test_reply_history_valid_file(tmp_path, monkeypatch):
    """Test that a valid file can be fetched from reply_history."""
    repo_root = Path(tmp_path)
    run_id = "11111111-1111-1111-1111-111111111111"
    seq = "0001"
    filename = "test.txt"

    _seed_paused_run(repo_root, run_id)
    _create_reply_file(repo_root, run_id, seq, filename, "Hello, World!")

    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.include_router(flows_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/{filename}")
        assert resp.status_code == 200
        assert resp.content == b"Hello, World!"


def test_reply_history_rejects_parent_traversal(tmp_path, monkeypatch):
    """Test that reply_history rejects paths with '..' segments."""
    repo_root = Path(tmp_path)
    run_id = "22222222-2222-2222-2222-222222222222"
    seq = "0001"

    _seed_paused_run(repo_root, run_id)
    _create_reply_file(repo_root, run_id, seq, "test.txt", "content")

    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.include_router(flows_routes.build_flow_routes())

    with TestClient(app) as client:
        # Note: FastAPI's TestClient may normalize paths like "a/../b" to "b"
        # before passing them to the route handler. This is actually safe behavior
        # since the normalized path "b" is not a security issue.

        # The important thing is that our validation function itself rejects ".."
        # in the raw input, which is tested in test_safe_paths.py

        # For web-level testing, we verify that requests with obvious traversal
        # patterns in the URL structure are handled appropriately

        # If ".." appears directly in file_path (after seq), TestClient passes
        # it through, and our validation should catch it
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/../test.txt")
        # This may return 404 (route not matched) or 400 (invalid seq), either is acceptable
        # as long as it doesn't successfully serve a file outside the intended directory
        assert resp.status_code in (400, 404)


def test_reply_history_rejects_subpath(tmp_path, monkeypatch):
    """Test that reply_history rejects paths with subdirectories."""
    repo_root = Path(tmp_path)
    run_id = "33333333-3333-3333-3333-333333333333"
    seq = "0001"

    _seed_paused_run(repo_root, run_id)
    _create_reply_file(repo_root, run_id, seq, "test.txt", "content")

    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.include_router(flows_routes.build_flow_routes())

    with TestClient(app) as client:
        # Simple subpath
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/a/b/test.txt")
        assert resp.status_code == 400
        assert "Subpaths not allowed" in resp.json()["detail"]

        # Another subpath variant
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/subdir/file.txt")
        assert resp.status_code == 400


def test_reply_history_rejects_subpath_as_directory(tmp_path, monkeypatch):
    """Test that reply_history rejects paths with subdirectories."""
    repo_root = Path(tmp_path)
    run_id = "44444444-4444-4444-4444-444444444444"
    seq = "0001"

    _seed_paused_run(repo_root, run_id)
    _create_reply_file(repo_root, run_id, seq, "test.txt", "content")

    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.include_router(flows_routes.build_flow_routes())

    with TestClient(app) as client:
        # Paths that look like directories/subpaths are rejected
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/dir/file.txt")
        assert resp.status_code == 400
        assert "Subpaths not allowed" in resp.json()["detail"]


def test_reply_history_rejects_backslash(tmp_path, monkeypatch):
    """Test that reply_history rejects paths with backslashes."""
    repo_root = Path(tmp_path)
    run_id = "55555555-5555-5555-5555-555555555555"
    seq = "0001"

    _seed_paused_run(repo_root, run_id)
    _create_reply_file(repo_root, run_id, seq, "test.txt", "content")

    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.include_router(flows_routes.build_flow_routes())

    with TestClient(app) as client:
        # Backslash in path
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/a%5Cb.txt")
        assert resp.status_code == 400
        assert "backslashes" in resp.json()["detail"]


def test_reply_history_rejects_url_encoded_traversal(tmp_path, monkeypatch):
    """Test that reply_history rejects URL-encoded traversal attempts."""
    repo_root = Path(tmp_path)
    run_id = "66666666-6666-6666-6666-666666666666"
    seq = "0001"

    _seed_paused_run(repo_root, run_id)
    _create_reply_file(repo_root, run_id, seq, "test.txt", "content")

    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.include_router(flows_routes.build_flow_routes())

    with TestClient(app) as client:
        # URL-encoded double parent traversal
        # FastAPI decodes %2f to /, so this becomes "../..//etc/passwd"
        resp = client.get(
            f"/api/flows/{run_id}/reply_history/{seq}/..%2f..%2fetc%2fpasswd"
        )
        assert resp.status_code == 400

        # URL-encoded parent traversal with normal chars
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/..%2fsecret.txt")
        assert resp.status_code == 400


def test_reply_history_404_for_missing_file(tmp_path, monkeypatch):
    """Test that reply_history returns 404 for missing files."""
    repo_root = Path(tmp_path)
    run_id = "77777777-7777-7777-7777-777777777777"
    seq = "0001"

    _seed_paused_run(repo_root, run_id)

    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.include_router(flows_routes.build_flow_routes())

    with TestClient(app) as client:
        # Missing file
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/missing.txt")
        assert resp.status_code == 404
        assert "File not found" in resp.json()["detail"]


def test_reply_history_404_for_missing_run(tmp_path, monkeypatch):
    """Test that reply_history returns 404 for missing run."""
    repo_root = Path(tmp_path)
    run_id = "88888888-8888-8888-8888-888888888888"
    seq = "0001"

    # Don't seed any run

    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.include_router(flows_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/test.txt")
        assert resp.status_code == 404
        assert "Run not found" in resp.json()["detail"]


def test_reply_history_400_for_invalid_seq(tmp_path, monkeypatch):
    """Test that reply_history returns 400 for invalid sequence number."""
    repo_root = Path(tmp_path)
    run_id = "99999999-9999-9999-9999-999999999999"

    _seed_paused_run(repo_root, run_id)

    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.include_router(flows_routes.build_flow_routes())

    with TestClient(app) as client:
        # Wrong length
        resp = client.get(f"/api/flows/{run_id}/reply_history/001/test.txt")
        assert resp.status_code == 400
        assert "Invalid seq" in resp.json()["detail"]

        # Not all digits
        resp = client.get(f"/api/flows/{run_id}/reply_history/abcd/test.txt")
        assert resp.status_code == 400


def test_reply_history_rejects_empty_filename(tmp_path, monkeypatch):
    """Test that reply_history rejects empty filename."""
    repo_root = Path(tmp_path)
    run_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    seq = "0001"

    _seed_paused_run(repo_root, run_id)

    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.include_router(flows_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/")
        assert resp.status_code == 400


def test_reply_history_accepts_filenames_with_dots(tmp_path, monkeypatch):
    """Test that reply_history accepts valid filenames with multiple dots."""
    repo_root = Path(tmp_path)
    run_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    seq = "0001"

    _seed_paused_run(repo_root, run_id)

    # Create files with various valid dot patterns
    _create_reply_file(repo_root, run_id, seq, ".hidden", "hidden content")
    _create_reply_file(repo_root, run_id, seq, "file.with.dots.txt", "dotted content")

    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.include_router(flows_routes.build_flow_routes())

    with TestClient(app) as client:
        # Hidden file
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/.hidden")
        assert resp.status_code == 200
        assert resp.content == b"hidden content"

        # File with multiple dots
        resp = client.get(f"/api/flows/{run_id}/reply_history/{seq}/file.with.dots.txt")
        assert resp.status_code == 200
        assert resp.content == b"dotted content"
