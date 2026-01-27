from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.routes import flows as flows_routes
from codex_autorunner.routes import messages as messages_routes


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
            "runs_dir": ".codex-autorunner/runs",
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

    monkeypatch.setattr(messages_routes, "find_repo_root", lambda: repo_root)
    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    app = FastAPI()
    app.state.repo_id = "repo"
    app.include_router(messages_routes.build_messages_routes())
    app.include_router(flows_routes.build_flow_routes())

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
