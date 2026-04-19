from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.support.web_test_helpers import build_flow_app

from codex_autorunner.core.flows.models import FlowEventType, FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.surfaces.web.routes import base as base_routes


def test_ticket_flow_runs_endpoint_returns_empty_list_on_fresh_repo(
    tmp_path, monkeypatch
):
    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/runs?flow_type=ticket_flow")
        assert resp.status_code == 200
        assert resp.json() == []


def test_ticket_list_endpoint_returns_empty_list_when_no_tickets(tmp_path, monkeypatch):
    (tmp_path / ".codex-autorunner" / "tickets").mkdir(parents=True)

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["tickets"] == []


def test_repo_health_is_ok_when_tickets_dir_exists(tmp_path):
    (tmp_path / ".codex-autorunner" / "tickets").mkdir(parents=True)

    app = FastAPI()
    app.state.config = object()
    app.state.engine = SimpleNamespace(repo_root=Path(tmp_path))

    app.include_router(base_routes.build_base_routes(static_dir=Path(tmp_path)))

    with TestClient(app) as client:
        resp = client.get("/api/repo/health")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "ok"
        assert payload["tickets"]["status"] == "ok"


def test_ticket_list_returns_body_even_when_frontmatter_invalid(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-007.md"
    ticket_path.write_text(
        "---\nagent: codex\n# done is missing on purpose\n---\n\nDescribe the task details here...\n",
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        payload = resp.json()
        assert len(payload["tickets"]) == 1
        ticket = payload["tickets"][0]
        assert ticket["index"] == 7
        assert "Describe the task details here" in (ticket["body"] or "")
        assert ticket["errors"]


def test_get_ticket_by_index(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-002.md"
    ticket_path.write_text(
        '---\nticket_id: "tkt_get002"\nagent: codex\ndone: false\ntitle: Demo\n---\n\nBody here\n',
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets/2")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["index"] == 2
        assert payload["frontmatter"]["agent"] == "codex"
        assert "Body here" in payload["body"]
        assert isinstance(payload.get("chat_key"), str)
        assert payload["chat_key"]


def test_create_ticket_sets_ticket_id_and_stable_chat_key(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        created = client.post(
            "/api/flows/ticket_flow/tickets",
            json={"agent": "codex", "title": "Demo", "body": "Body"},
        )
        assert created.status_code == 200
        payload = created.json()
        fm = payload["frontmatter"] or {}
        extra = fm.get("extra") if isinstance(fm.get("extra"), dict) else {}
        ticket_id = fm.get("ticket_id") or extra.get("ticket_id")
        assert isinstance(ticket_id, str) and ticket_id.startswith("tkt_")
        first_chat_key = payload.get("chat_key")
        assert isinstance(first_chat_key, str) and ticket_id in first_chat_key

        update_content = f"""---
agent: "codex"
done: true
ticket_id: "{ticket_id}"
title: "Demo"
---

Body updated
"""
        updated = client.put(
            f"/api/flows/ticket_flow/tickets/{payload['index']}",
            json={"content": update_content},
        )
        assert updated.status_code == 200
        updated_payload = updated.json()
        assert updated_payload.get("chat_key") == first_chat_key


def test_create_ticket_appends_after_highest_index_when_gaps_exist(
    tmp_path, monkeypatch
):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_gap001"\nagent: codex\ndone: false\n---\n\nOne\n',
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-003.md").write_text(
        '---\nticket_id: "tkt_gap003"\nagent: codex\ndone: false\n---\n\nThree\n',
        encoding="utf-8",
    )
    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        created = client.post(
            "/api/flows/ticket_flow/tickets",
            json={"agent": "codex", "title": "Demo", "body": "Body"},
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["index"] == 4
        assert (ticket_dir / "TICKET-004.md").exists()
        assert not (ticket_dir / "TICKET-002.md").exists()


def test_create_ticket_canonicalizes_hermes_alias_input(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        created = client.post(
            "/api/flows/ticket_flow/tickets",
            json={"agent": "hermes-m4-pma", "title": "Demo", "body": "Body"},
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["frontmatter"]["agent"] == "hermes"
        assert payload["frontmatter"]["profile"] == "m4-pma"


def test_create_ticket_rejects_unknown_keys(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        created = client.post(
            "/api/flows/ticket_flow/tickets",
            json={"agent": "codex", "titel": "Demo", "body": "Body"},
        )
        assert created.status_code == 422
        detail = created.json()["detail"]
        assert any(item["loc"][-1] == "titel" for item in detail)


def test_get_ticket_by_index_returns_body_on_invalid_frontmatter(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-003.md"
    ticket_path.write_text(
        "---\nagent: codex\n# missing done\n---\n\nStill show body\n",
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets/3")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["index"] == 3
        assert payload["frontmatter"].get("agent") == "codex"
        assert "Still show body" in (payload["body"] or "")


def test_update_ticket_allows_colon_titles_and_models(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-004.md"
    ticket_path.write_text(
        "---\nticket_id: tkt_update004\nagent: codex\ndone: false\n---\n\nBody\n",
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    content = """---
ticket_id: "tkt_update004"
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
    (tmp_path / ".codex-autorunner" / "tickets").mkdir(parents=True)
    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets/99")
        assert resp.status_code == 404


def test_dispatch_history_returns_empty_when_no_run(tmp_path, monkeypatch):
    (tmp_path / ".codex-autorunner" / "tickets").mkdir(parents=True)
    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/runs/run-nonexistent/dispatch_history")
        assert resp.status_code == 404


def test_ticket_list_endpoint_stable_when_dispatch_history_has_gaps(
    tmp_path, monkeypatch
):
    import uuid as _uuid

    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_id = "tkt_gapdispatch"
    ticket_path.write_text(
        f'---\nticket_id: "{ticket_id}"\nagent: codex\ndone: false\ntitle: Demo\n---\n\nBody\n',
        encoding="utf-8",
    )
    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    store = FlowStore(db_path)
    store.initialize()

    run_id = str(_uuid.uuid4())
    store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}, state={}
    )
    store.update_flow_run_status(run_id, FlowRunStatus.COMPLETED)
    store.create_event(
        event_id=str(_uuid.uuid4()),
        run_id=run_id,
        event_type=FlowEventType.DIFF_UPDATED,
        data={
            "ticket_key": ticket_id,
            "ticket_path": ".codex-autorunner/tickets/TICKET-001.md",
            "insertions": 5,
            "deletions": 2,
            "files_changed": 1,
        },
    )
    store.create_event(
        event_id=str(_uuid.uuid4()),
        run_id=run_id,
        event_type=FlowEventType.DIFF_UPDATED,
        data={
            "ticket_key": ticket_id,
            "ticket_path": ".codex-autorunner/tickets/TICKET-001.md",
            "insertions": 10,
            "deletions": 1,
            "files_changed": 3,
        },
    )
    store.close()

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        payload = resp.json()
        assert len(payload["tickets"]) == 1
        stats = payload["tickets"][0]["diff_stats"]
        assert stats["insertions"] == 5 + 10
        assert stats["deletions"] == 2 + 1
        assert stats["files_changed"] == 1 + 3


def test_ticket_flow_runs_endpoint_returns_completed_run(tmp_path, monkeypatch):
    import uuid as _uuid

    (tmp_path / ".codex-autorunner" / "tickets").mkdir(parents=True)
    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    store = FlowStore(db_path)
    store.initialize()

    run_id = str(_uuid.uuid4())
    store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}, state={}
    )
    store.update_flow_run_status(run_id, FlowRunStatus.COMPLETED)
    store.close()

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/runs?flow_type=ticket_flow")
        assert resp.status_code == 200
        runs = resp.json()
        assert len(runs) >= 1
        found = [r for r in runs if r["id"] == run_id]
        assert len(found) == 1
        assert found[0]["status"] == "completed"
        assert found[0]["flow_type"] == "ticket_flow"


def test_ticket_list_returns_ascending_numeric_order(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-003.md").write_text(
        '---\nticket_id: "tkt_order003"\nagent: codex\ndone: false\ntitle: Three\n---\n\nThree\n',
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_order001"\nagent: codex\ndone: false\ntitle: One\n---\n\nOne\n',
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002.md").write_text(
        '---\nticket_id: "tkt_order002"\nagent: codex\ndone: false\ntitle: Two\n---\n\nTwo\n',
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        indices = [t["index"] for t in resp.json()["tickets"]]
        assert indices == sorted(indices)


def test_user_agent_ticket_is_visible_but_not_auto_executable(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_user001"\nagent: user\ndone: false\ntitle: Manual task\n---\n\nDo this manually\n',
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/api/flows/ticket_flow/tickets")
        assert resp.status_code == 200
        tickets = resp.json()["tickets"]
        assert len(tickets) == 1
        assert tickets[0]["frontmatter"]["agent"] == "user"


def test_ticket_update_preserves_chat_key_stability(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        created = client.post(
            "/api/flows/ticket_flow/tickets",
            json={"agent": "codex", "title": "Test", "body": "Original"},
        )
        assert created.status_code == 200
        original_chat_key = created.json()["chat_key"]
        ticket_id = created.json()["frontmatter"].get("ticket_id") or created.json()[
            "frontmatter"
        ].get("extra", {}).get("ticket_id")
        index = created.json()["index"]

        updated_content = f'---\nagent: "codex"\ndone: false\nticket_id: "{ticket_id}"\ntitle: "Updated"\n---\n\nUpdated body\n'
        updated = client.put(
            f"/api/flows/ticket_flow/tickets/{index}",
            json={"content": updated_content},
        )
        assert updated.status_code == 200
        assert updated.json()["chat_key"] == original_chat_key
