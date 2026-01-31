from fastapi.testclient import TestClient

from codex_autorunner.server import create_hub_app


def test_pma_agents_endpoint(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.get("/hub/pma/agents")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload.get("agents"), list)
    assert payload.get("default") in {agent.get("id") for agent in payload["agents"]}


def test_pma_chat_requires_message(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={})
    assert resp.status_code == 400
