from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.core.flows.models import FlowEventType, FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.routes import base as base_routes
from codex_autorunner.routes import flows as flow_routes


def test_ticket_flow_runs_endpoint_returns_empty_list_on_fresh_repo(
    tmp_path, monkeypatch
):
    """Ticket-first: /api/flows/runs must not 404/500 when no runs exist."""
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/runs?flow_type=ticket_flow")
        assert resp.status_code == 200
        assert resp.json() == []


def test_ticket_list_endpoint_returns_empty_list_when_no_tickets(tmp_path, monkeypatch):
    """Ticket-first: /api/flows/ticket_flow/tickets must never fail on empty dir."""

    (tmp_path / ".codex-autorunner" / "tickets").mkdir(parents=True)

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["tickets"] == []
        assert payload["ticket_dir"].endswith(".codex-autorunner/tickets")


def test_repo_health_is_ok_when_tickets_dir_exists(tmp_path):
    """Repo health should not be gated on legacy flows/docs initialization."""

    (tmp_path / ".codex-autorunner" / "tickets").mkdir(parents=True)

    app = FastAPI()
    # Minimal app state for repo_health.
    app.state.config = object()
    app.state.engine = SimpleNamespace(repo_root=Path(tmp_path))

    # build_base_routes requires a static_dir, but /api/repo/health does not use it.
    app.include_router(base_routes.build_base_routes(static_dir=Path(tmp_path)))

    with TestClient(app) as client:
        resp = client.get("/api/repo/health")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "ok"
        assert payload["tickets"]["status"] == "ok"


def test_ticket_list_returns_body_even_when_frontmatter_invalid(tmp_path, monkeypatch):
    """Broken frontmatter should still surface raw ticket content for repair."""

    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-007.md"
    ticket_path.write_text(
        "---\nagent: codex\n# done is missing on purpose\n---\n\nDescribe the task details here...\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        payload = resp.json()
        assert len(payload["tickets"]) == 1
        ticket = payload["tickets"][0]
        # Index is derived from filename even when lint fails.
        assert ticket["index"] == 7
        # Body should be present so the UI can show/repair it.
        assert "Describe the task details here" in (ticket["body"] or "")
        # Errors surface frontmatter problems.
        assert ticket["errors"]


def test_get_ticket_by_index(tmp_path, monkeypatch):
    """GET /api/flows/ticket_flow/tickets/{index} returns a single ticket."""

    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-002.md"
    ticket_path.write_text(
        "---\nagent: codex\ndone: false\ntitle: Demo\n---\n\nBody here\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets/2")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["index"] == 2
        assert payload["frontmatter"]["agent"] == "codex"
        assert "Body here" in payload["body"]


def test_get_ticket_by_index_returns_body_on_invalid_frontmatter(tmp_path, monkeypatch):
    """Single-ticket endpoint should mirror list behavior when frontmatter is broken."""

    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-003.md"
    ticket_path.write_text(
        "---\nagent: codex\n# missing done\n---\n\nStill show body\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets/3")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["index"] == 3
        # Invalid frontmatter should not block access; parsed fields may be partial.
        assert payload["frontmatter"].get("agent") == "codex"
        assert "Still show body" in (payload["body"] or "")


def test_update_ticket_allows_colon_titles_and_models(tmp_path, monkeypatch):
    """PUT /api/flows/ticket_flow/tickets/{index} should accept quoted scalars containing colons."""

    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-004.md"
    ticket_path.write_text(
        "---\nagent: codex\ndone: false\n---\n\nBody\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    content = """---
agent: \"opencode\"
done: false
title: \"TICKET-004: Review CLI lint error (issue #512)\"
model: \"zai-coding-plan/glm-4.7-aicoding\"
---

Updated body
"""

    with TestClient(app) as client:
        resp = client.put(
            "/api/flows/ticket_flow/tickets/4",
            json={"content": content},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["frontmatter"]["title"].startswith("TICKET-004: Review CLI")
        assert payload["frontmatter"]["agent"] == "opencode"
        assert payload["frontmatter"]["model"] == "zai-coding-plan/glm-4.7-aicoding"


def test_get_ticket_by_index_404(tmp_path, monkeypatch):
    """GET /api/flows/ticket_flow/tickets/{index} returns 404 when missing."""

    (tmp_path / ".codex-autorunner" / "tickets").mkdir(parents=True)
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets/99")
        assert resp.status_code == 404


def test_ticket_list_keeps_diff_stats_for_latest_completed_run(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\nagent: codex\ndone: false\ntitle: Demo\n---\n\nBody\n",
        encoding="utf-8",
    )
    rel_ticket_path = ".codex-autorunner/tickets/TICKET-001.md"

    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    store = FlowStore(db_path)
    store.initialize()

    run_id = str(uuid.uuid4())
    store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}, state={}
    )
    store.update_flow_run_status(run_id, FlowRunStatus.COMPLETED)
    store.create_event(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        event_type=FlowEventType.DIFF_UPDATED,
        data={
            "ticket_id": rel_ticket_path,
            "insertions": 12,
            "deletions": 3,
            "files_changed": 2,
        },
    )
    store.close()

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        payload = resp.json()
        assert len(payload["tickets"]) == 1
        assert payload["tickets"][0]["path"] == rel_ticket_path
        assert payload["tickets"][0]["diff_stats"] == {
            "insertions": 12,
            "deletions": 3,
            "files_changed": 2,
        }


def test_reorder_ticket_moves_source_before_destination(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: false\ntitle: One\n---\n\nBody 1\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002.md").write_text(
        "---\nagent: codex\ndone: false\ntitle: Two\n---\n\nBody 2\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-003.md").write_text(
        "---\nagent: codex\ndone: false\ntitle: Three\n---\n\nBody 3\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))
    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.post(
            "/api/flows/ticket_flow/tickets/reorder",
            json={
                "source_index": 3,
                "destination_index": 1,
                "place_after": False,
            },
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "ok"

        listed = client.get("/api/flows/ticket_flow/tickets")
        assert listed.status_code == 200
        names = [Path(ticket["path"]).name for ticket in listed.json()["tickets"]]
        assert names == ["TICKET-001.md", "TICKET-002.md", "TICKET-003.md"]
        first_ticket = (ticket_dir / "TICKET-001.md").read_text(encoding="utf-8")
        assert "title: Three" in first_ticket


def test_reorder_ticket_updates_active_run_current_ticket_path(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: false\ntitle: One\n---\n\nBody 1\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002.md").write_text(
        "---\nagent: codex\ndone: false\ntitle: Two\n---\n\nBody 2\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-003.md").write_text(
        "---\nagent: codex\ndone: false\ntitle: Three\n---\n\nBody 3\n",
        encoding="utf-8",
    )

    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    original_ticket = ".codex-autorunner/tickets/TICKET-003.md"
    run_id = str(uuid.uuid4())
    with FlowStore(db_path) as store:
        store.create_flow_run(
            run_id=run_id,
            flow_type="ticket_flow",
            input_data={},
            state={
                "current_ticket": original_ticket,
                "ticket_engine": {"current_ticket": original_ticket},
            },
        )
        store.update_flow_run_status(
            run_id,
            FlowRunStatus.PAUSED,
            state={
                "current_ticket": original_ticket,
                "ticket_engine": {"current_ticket": original_ticket},
            },
        )

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))
    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.post(
            "/api/flows/ticket_flow/tickets/reorder",
            json={
                "source_index": 3,
                "destination_index": 1,
                "place_after": False,
            },
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "ok"

    with FlowStore(db_path) as store:
        record = store.get_flow_run(run_id)
    assert record is not None
    assert (
        record.state.get("current_ticket") == ".codex-autorunner/tickets/TICKET-001.md"
    )
    ticket_engine = record.state.get("ticket_engine")
    assert isinstance(ticket_engine, dict)
    assert (
        ticket_engine.get("current_ticket") == ".codex-autorunner/tickets/TICKET-001.md"
    )
