from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes import file_chat as file_chat_routes


def test_ticket_chat_route_passes_selected_profile_into_execution(
    tmp_path, monkeypatch
):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_ticketchat001"\nagent: hermes\nprofile: m4-pma\ndone: false\ntitle: Demo\n---\n\nBody\n',
        encoding="utf-8",
    )

    observed = {}

    async def fake_execute_file_chat(
        _request,
        _repo_root,
        target,
        message,
        *,
        agent,
        profile,
        model=None,
        reasoning=None,
        on_meta=None,
        on_usage=None,
    ):
        observed.update(
            {
                "target": target.target,
                "message": message,
                "agent": agent,
                "profile": profile,
                "model": model,
                "reasoning": reasoning,
            }
        )
        return {"status": "ok", "agent": agent, "profile": profile}

    monkeypatch.setattr(
        file_chat_routes,
        "extracted_execute_file_chat",
        fake_execute_file_chat,
    )

    app = FastAPI()
    app.include_router(file_chat_routes.build_file_chat_routes())
    app.state.engine = SimpleNamespace(repo_root=tmp_path)
    app.state.config = SimpleNamespace(
        agent_profiles=lambda agent_id: (
            {"m4-pma": object()} if agent_id == "hermes" else {}
        ),
        agent_default_profile=lambda _agent_id: None,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/tickets/1/chat",
            json={
                "message": "Use the selected profile",
                "agent": "hermes",
                "profile": "m4-pma",
                "model": "free-form-model",
                "reasoning": "high",
            },
        )

    assert response.status_code == 200
    assert observed == {
        "target": "ticket:1",
        "message": "Use the selected profile",
        "agent": "hermes",
        "profile": "m4-pma",
        "model": "free-form-model",
        "reasoning": "high",
    }


def test_ticket_chat_route_rejects_invalid_profile_before_stream_start(
    tmp_path, monkeypatch
):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_ticketchatbadprofile001"\nagent: hermes\ndone: false\ntitle: Demo\n---\n\nBody\n',
        encoding="utf-8",
    )

    app = FastAPI()
    app.include_router(file_chat_routes.build_file_chat_routes())
    app.state.engine = SimpleNamespace(repo_root=tmp_path)
    app.state.config = SimpleNamespace(
        agent_profiles=lambda agent_id: (
            {"m4-pma": object()} if agent_id == "hermes" else {}
        ),
        agent_default_profile=lambda _agent_id: None,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/tickets/1/chat",
            json={
                "message": "Use the selected profile",
                "agent": "hermes",
                "profile": "bad-profile",
                "stream": True,
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "profile is invalid"
