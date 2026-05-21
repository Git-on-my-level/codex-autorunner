from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes import file_chat as file_chat_routes
from codex_autorunner.surfaces.web.services import file_chat as file_chat_service


def test_file_chat_route_builds_canonical_turn_before_execution(tmp_path, monkeypatch):
    contextspace_dir = tmp_path / ".codex-autorunner" / "contextspace"
    contextspace_dir.mkdir(parents=True)
    (contextspace_dir / "spec.md").write_text("before\n", encoding="utf-8")

    observed = {}

    async def fake_execute_file_chat(
        _request,
        _repo_root,
        _target,
        _message,
        *,
        turn_request,
        **_kwargs,
    ):
        observed["turn_request"] = turn_request.to_dict()
        return {"status": "ok", "message": "done"}

    monkeypatch.setattr(
        file_chat_service,
        "execute_file_chat_agent_turn",
        fake_execute_file_chat,
    )

    app = FastAPI()
    app.include_router(file_chat_routes.build_file_chat_routes())
    app.state.engine = SimpleNamespace(repo_root=tmp_path)
    app.state.config = SimpleNamespace()

    with TestClient(app) as client:
        response = client.post(
            "/api/file-chat",
            json={
                "target": "contextspace:spec",
                "message": "Edit the spec",
                "client_turn_id": "client-route",
            },
        )

    assert response.status_code == 200
    assert observed["turn_request"]["request_id"] == "client-route"
    assert observed["turn_request"]["target_id"] == "contextspace_spec.md"
    assert observed["turn_request"]["origin"]["surface_kind"] == "web"
