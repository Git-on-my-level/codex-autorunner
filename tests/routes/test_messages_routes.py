from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from tests.support.web_test_helpers import build_messages_app

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore


def _write_dispatch_history(repo_root: Path, run_id: str, seq: int = 1) -> None:
    entry_dir = (
        repo_root
        / ".codex-autorunner"
        / "runs"
        / run_id
        / "dispatch_history"
        / f"{seq:04d}"
    )
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "DISPATCH.md").write_text(
        "---\nmode: pause\ntitle: Review\n---\n\nPlease review this change.\n",
        encoding="utf-8",
    )
    (entry_dir / "design.md").write_text("draft", encoding="utf-8")


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
        },
        state={},
        metadata={},
    )
    store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)


def test_messages_active_and_reply_archive(tmp_path, monkeypatch):
    repo_root = Path(tmp_path)
    run_id = "11111111-1111-1111-1111-111111111111"

    _seed_paused_run(repo_root, run_id)
    _write_dispatch_history(repo_root, run_id, seq=1)

    app = build_messages_app(repo_root, monkeypatch)

    with TestClient(app) as client:
        active = client.get("/api/messages/active")
        assert active.status_code == 200
        payload = active.json()
        assert payload["active"] is True
        assert payload["run_id"] == run_id
        assert payload["dispatch"]["title"] == "Review"

        threads = client.get("/api/messages/threads").json()["conversations"]
        assert len(threads) == 1
        assert threads[0]["run_id"] == run_id

        detail = client.get(f"/api/messages/threads/{run_id}").json()
        assert detail["run"]["id"] == run_id
        assert detail["dispatch_history"][0]["seq"] == 1
        assert detail["reply_history"] == []

        resp = client.post(
            f"/api/messages/{run_id}/reply",
            data={"body": "LGTM"},
            files=[("files", ("note.txt", b"hello", "text/plain"))],
        )
        assert resp.status_code == 200
        assert resp.json()["seq"] == 1

        detail2 = client.get(f"/api/messages/threads/{run_id}").json()
        assert detail2["reply_history"][0]["seq"] == 1
        assert detail2["reply_history"][0]["reply"]["body"].strip() == "LGTM"

        file_url = detail2["reply_history"][0]["files"][0]["url"]
        fetched = client.get(file_url)
        assert fetched.status_code == 200
        assert fetched.content == b"hello"


def test_reply_archive_rejects_relative_workspace_root(tmp_path, monkeypatch):
    repo_root = Path(tmp_path)
    run_id = "22222222-2222-2222-2222-222222222222"

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    store.create_flow_run(
        run_id,
        "ticket_flow",
        input_data={
            "workspace_root": ".",
        },
        state={},
        metadata={},
    )
    store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)
    _write_dispatch_history(repo_root, run_id, seq=1)

    app = build_messages_app(repo_root, monkeypatch)

    with TestClient(app) as client:
        resp = client.post(
            f"/api/messages/{run_id}/reply",
            data={"body": "Check paths"},
        )
        assert resp.status_code == 409
        assert "non-absolute workspace_root" in resp.json()["detail"]


def test_messages_active_returns_false_when_no_active_run(tmp_path, monkeypatch):
    repo_root = Path(tmp_path)

    app = build_messages_app(repo_root, monkeypatch)

    with TestClient(app) as client:
        active = client.get("/api/messages/active")
        assert active.status_code == 200
        payload = active.json()
        assert payload["active"] is False


def test_messages_threads_returns_empty_list_when_no_runs(tmp_path, monkeypatch):
    repo_root = Path(tmp_path)

    app = build_messages_app(repo_root, monkeypatch)

    with TestClient(app) as client:
        threads = client.get("/api/messages/threads")
        assert threads.status_code == 200
        assert threads.json()["conversations"] == []


def test_reply_archive_preserves_file_url_within_workspace(tmp_path, monkeypatch):
    repo_root = Path(tmp_path)
    run_id = "33333333-3333-3333-3333-333333333333"

    _seed_paused_run(repo_root, run_id)
    _write_dispatch_history(repo_root, run_id, seq=1)

    app = build_messages_app(repo_root, monkeypatch)

    with TestClient(app) as client:
        resp = client.post(
            f"/api/messages/{run_id}/reply",
            data={"body": "See attached"},
            files=[("files", ("report.txt", b"data", "text/plain"))],
        )
        assert resp.status_code == 200

        detail = client.get(f"/api/messages/threads/{run_id}").json()
        assert len(detail["reply_history"]) == 1
        file_entry = detail["reply_history"][0]["files"][0]
        assert ".." not in file_entry["url"]
        assert not file_entry["url"].startswith("/")

        fetched = client.get(f"/{file_entry['url']}")
        assert fetched.status_code == 200
        assert fetched.content == b"data"


def test_reply_archive_rejects_reply_to_unknown_run(tmp_path, monkeypatch):
    repo_root = Path(tmp_path)

    app = build_messages_app(repo_root, monkeypatch)

    with TestClient(app) as client:
        resp = client.post(
            "/api/messages/nonexistent-run-id/reply",
            data={"body": "test"},
        )
        assert resp.status_code in (404, 409, 500)
