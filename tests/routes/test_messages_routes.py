from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from tests.support.web_test_helpers import build_messages_app

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.surfaces.web.routes import messages as messages_routes


def _write_dispatch_history(
    repo_root: Path,
    run_id: str,
    seq: int = 1,
    *,
    mode: str = "pause",
    title: str = "Review",
    body: str = "Please review this change.",
) -> None:
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
        f"---\nmode: {mode}\ntitle: {title}\n---\n\n{body}\n",
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


def test_active_message_prefers_pause_handoff_over_newer_turn_summary(
    tmp_path, monkeypatch
):
    repo_root = Path(tmp_path)
    run_id = "55555555-5555-4555-8555-555555555555"

    _seed_paused_run(repo_root, run_id)
    _write_dispatch_history(
        repo_root,
        run_id,
        seq=1,
        mode="pause",
        title="Choose direction",
        body="Which path should the agent take?",
    )
    _write_dispatch_history(
        repo_root,
        run_id,
        seq=2,
        mode="turn_summary",
        title="Turn summary",
        body="The agent paused for input.",
    )

    app = build_messages_app(repo_root, monkeypatch)

    with TestClient(app) as client:
        active = client.get("/api/messages/active")

    assert active.status_code == 200
    payload = active.json()
    assert payload["active"] is True
    assert payload["seq"] == 1
    assert payload["dispatch"]["is_handoff"] is True
    assert payload["dispatch"]["title"] == "Choose direction"


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


def test_reply_and_resume_writes_live_reply_before_resume(tmp_path, monkeypatch):
    repo_root = Path(tmp_path)
    run_id = "44444444-4444-4444-4444-444444444444"

    _seed_paused_run(repo_root, run_id)
    _write_dispatch_history(repo_root, run_id, seq=1)

    observed = {}

    async def fake_resume(root: Path, requested_run_id: str, *, force: bool = False):
        observed["root"] = root
        observed["run_id"] = requested_run_id
        observed["force"] = force
        live_reply = repo_root / ".codex-autorunner" / "runs" / run_id / "USER_REPLY.md"
        observed["live_reply"] = live_reply.read_text(encoding="utf-8")
        with FlowStore(repo_root / ".codex-autorunner" / "flows.db") as store:
            store.update_flow_run_status(run_id, FlowRunStatus.RUNNING)
            record = store.get_flow_run(run_id)
            assert record is not None
            return record

    monkeypatch.setattr(messages_routes, "resume_ticket_flow_run", fake_resume)

    app = build_messages_app(repo_root, monkeypatch)

    with TestClient(app) as client:
        resp = client.post(
            f"/api/messages/{run_id}/reply-and-resume",
            data={"body": "Proceed with option B"},
        )

    assert resp.status_code == 200
    assert resp.json()["run_status"] == "running"
    assert observed["run_id"] == run_id
    assert observed["force"] is False
    assert "Proceed with option B" in observed["live_reply"]


def test_reply_and_resume_validates_tickets_before_writing_reply(tmp_path, monkeypatch):
    repo_root = Path(tmp_path)
    run_id = "66666666-6666-4666-8666-666666666666"

    _seed_paused_run(repo_root, run_id)
    _write_dispatch_history(repo_root, run_id, seq=1)
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nticket_id: [broken\n---\n\nBody\n",
        encoding="utf-8",
    )

    async def fail_resume(*_args, **_kwargs):
        raise AssertionError("reply-and-resume should validate before resume")

    monkeypatch.setattr(messages_routes, "resume_ticket_flow_run", fail_resume)

    app = build_messages_app(repo_root, monkeypatch)

    with TestClient(app) as client:
        resp = client.post(
            f"/api/messages/{run_id}/reply-and-resume",
            data={"body": "Proceed anyway"},
        )

    assert resp.status_code == 400
    assert resp.json()["detail"]["message"] == "Ticket validation failed"
    live_reply = repo_root / ".codex-autorunner" / "runs" / run_id / "USER_REPLY.md"
    assert not live_reply.exists()


def test_reply_archive_rejects_reply_to_unknown_run(tmp_path, monkeypatch):
    repo_root = Path(tmp_path)

    app = build_messages_app(repo_root, monkeypatch)

    with TestClient(app) as client:
        resp = client.post(
            "/api/messages/nonexistent-run-id/reply",
            data={"body": "test"},
        )
        assert resp.status_code in (404, 409, 500)
