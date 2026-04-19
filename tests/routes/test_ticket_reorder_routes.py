import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from tests.support.web_test_helpers import build_flow_app

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore


def test_reorder_ticket_moves_source_before_destination(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nticket_id: tkt_reorder001\nagent: codex\ndone: false\ntitle: One\n---\n\nBody 1\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002.md").write_text(
        "---\nticket_id: tkt_reorder002\nagent: codex\ndone: false\ntitle: Two\n---\n\nBody 2\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-003.md").write_text(
        "---\nticket_id: tkt_reorder003\nagent: codex\ndone: false\ntitle: Three\n---\n\nBody 3\n",
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

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
        "---\nticket_id: tkt_active001\nagent: codex\ndone: false\ntitle: One\n---\n\nBody 1\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002.md").write_text(
        "---\nticket_id: tkt_active002\nagent: codex\ndone: false\ntitle: Two\n---\n\nBody 2\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-003.md").write_text(
        "---\nticket_id: tkt_active003\nagent: codex\ndone: false\ntitle: Three\n---\n\nBody 3\n",
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

    app = build_flow_app(tmp_path, monkeypatch)

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


def test_reorder_ticket_does_not_overwrite_malformed_frontmatter(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    malformed = (
        "---\n"
        "agent: codex\n"
        "title: Broken\n"
        "# done is missing on purpose\n"
        "---\n\n"
        "Body 1\n"
    )
    (ticket_dir / "TICKET-001.md").write_text(malformed, encoding="utf-8")
    (ticket_dir / "TICKET-002.md").write_text(
        "---\nticket_id: tkt_reorder_ok\nagent: codex\ndone: false\ntitle: Two\n---\n\nBody 2\n",
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.post(
            "/api/flows/ticket_flow/tickets/reorder",
            json={
                "source_index": 2,
                "destination_index": 1,
                "place_after": False,
            },
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "error"
        assert payload["lint_errors"]

    rewritten = (ticket_dir / "TICKET-002.md").read_text(encoding="utf-8")
    assert "# done is missing on purpose" in rewritten
    assert "ticket_id:" not in rewritten
