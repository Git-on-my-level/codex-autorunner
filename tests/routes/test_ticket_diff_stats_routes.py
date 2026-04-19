import uuid

from fastapi.testclient import TestClient
from tests.support.web_test_helpers import build_flow_app

from codex_autorunner.core.flows.models import FlowEventType, FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore


def test_ticket_list_keeps_diff_stats_for_latest_completed_run(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_id = "tkt_diffstats001"
    ticket_path.write_text(
        f'---\nticket_id: "{ticket_id}"\nagent: codex\ndone: false\ntitle: Demo\n---\n\nBody\n',
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
            "ticket_id": ticket_id,
            "insertions": 12,
            "deletions": 3,
            "files_changed": 2,
        },
    )
    store.close()

    app = build_flow_app(tmp_path, monkeypatch)

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


def test_ticket_list_keeps_diff_stats_when_newer_run_has_no_events(
    tmp_path, monkeypatch
):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_id = "tkt_diffstats002"
    ticket_path.write_text(
        f'---\nticket_id: "{ticket_id}"\nagent: codex\ndone: false\ntitle: Demo\n---\n\nBody\n',
        encoding="utf-8",
    )
    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    store = FlowStore(db_path)
    store.initialize()

    older_run_id = str(uuid.uuid4())
    store.create_flow_run(
        run_id=older_run_id, flow_type="ticket_flow", input_data={}, state={}
    )
    store.update_flow_run_status(older_run_id, FlowRunStatus.COMPLETED)
    store.create_event(
        event_id=str(uuid.uuid4()),
        run_id=older_run_id,
        event_type=FlowEventType.DIFF_UPDATED,
        data={
            "ticket_id": ticket_id,
            "insertions": 12,
            "deletions": 3,
            "files_changed": 2,
        },
    )

    newer_run_id = str(uuid.uuid4())
    store.create_flow_run(
        run_id=newer_run_id, flow_type="ticket_flow", input_data={}, state={}
    )
    store.update_flow_run_status(newer_run_id, FlowRunStatus.COMPLETED)
    store.close()

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["tickets"][0]["diff_stats"] == {
            "insertions": 12,
            "deletions": 3,
            "files_changed": 2,
        }


def test_ticket_list_matches_diff_stats_by_stable_ticket_identity(
    tmp_path, monkeypatch
):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_key = "tkt_rename123"
    ticket_path = ticket_dir / "TICKET-002.md"
    ticket_path.write_text(
        "---\n"
        "agent: codex\n"
        "done: false\n"
        f'ticket_id: "{ticket_key}"\n'
        "title: Demo\n"
        "---\n\n"
        "Body\n",
        encoding="utf-8",
    )

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
            "ticket_id": ".codex-autorunner/tickets/TICKET-001.md",
            "ticket_path": ".codex-autorunner/tickets/TICKET-001.md",
            "ticket_key": ticket_key,
            "insertions": 8,
            "deletions": 5,
            "files_changed": 1,
        },
    )
    store.close()

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        payload = resp.json()
        assert (
            payload["tickets"][0]["path"] == ".codex-autorunner/tickets/TICKET-002.md"
        )
        assert payload["tickets"][0]["diff_stats"] == {
            "insertions": 8,
            "deletions": 5,
            "files_changed": 1,
        }


def test_ticket_list_ignores_legacy_path_stats_when_ticket_has_stable_id(
    tmp_path, monkeypatch
):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_key = "tkt_reused_path"
    ticket_path = ticket_dir / "TICKET-002.md"
    ticket_path.write_text(
        "---\n"
        "agent: codex\n"
        "done: false\n"
        f'ticket_id: "{ticket_key}"\n'
        "title: Demo\n"
        "---\n\n"
        "Body\n",
        encoding="utf-8",
    )

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
            "ticket_id": ".codex-autorunner/tickets/TICKET-002.md",
            "insertions": 99,
            "deletions": 44,
            "files_changed": 7,
        },
    )
    store.create_event(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        event_type=FlowEventType.DIFF_UPDATED,
        data={
            "ticket_key": ticket_key,
            "ticket_path": ".codex-autorunner/tickets/TICKET-001.md",
            "ticket_id": ".codex-autorunner/tickets/TICKET-001.md",
            "insertions": 8,
            "deletions": 5,
            "files_changed": 1,
        },
    )
    store.close()

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["tickets"][0]["diff_stats"] == {
            "insertions": 8,
            "deletions": 5,
            "files_changed": 1,
        }
