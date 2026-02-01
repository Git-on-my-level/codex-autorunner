import json
from pathlib import Path
from typing import Optional

import yaml
from fastapi.testclient import TestClient

from codex_autorunner.core.app_server_threads import PMA_KEY, PMA_OPENCODE_KEY
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.server import create_hub_app


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _enable_pma(
    hub_root: Path, *, model: Optional[str] = None, reasoning: Optional[str] = None
) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    if model is not None:
        cfg["pma"]["model"] = model
    if reasoning is not None:
        cfg["pma"]["reasoning"] = reasoning
    _write_config(hub_root / CONFIG_FILENAME, cfg)


def test_pma_agents_endpoint(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.get("/hub/pma/agents")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload.get("agents"), list)
    assert payload.get("default") in {agent.get("id") for agent in payload["agents"]}


def test_pma_chat_requires_message(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={})
    assert resp.status_code == 400


def test_pma_routes_disabled_by_default(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    assert client.get("/hub/pma/agents").status_code in {403, 404}
    assert client.post("/hub/pma/chat", json={"message": "hi"}).status_code in {
        403,
        404,
    }


def test_pma_chat_applies_model_reasoning_defaults(hub_env) -> None:
    _enable_pma(hub_env.hub_root, model="test-model", reasoning="high")

    app = create_hub_app(hub_env.hub_root)

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        def __init__(self) -> None:
            self.turn_kwargs = None

        async def thread_resume(self, thread_id: str) -> None:
            return None

        async def thread_start(self, root: str) -> dict:
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            self.turn_kwargs = turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    assert app.state.app_server_supervisor.client.turn_kwargs == {
        "model": "test-model",
        "effort": "high",
    }


def test_pma_thread_reset_clears_registry(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    registry = app.state.app_server_threads
    registry.set_thread_id(PMA_KEY, "thread-codex")
    registry.set_thread_id(PMA_OPENCODE_KEY, "thread-opencode")

    client = TestClient(app)
    resp = client.post("/hub/pma/thread/reset", json={"agent": "opencode"})
    assert resp.status_code == 200
    assert registry.get_thread_id(PMA_KEY) == "thread-codex"
    assert registry.get_thread_id(PMA_OPENCODE_KEY) is None

    resp = client.post("/hub/pma/thread/reset", json={"agent": "all"})
    assert resp.status_code == 200
    assert registry.get_thread_id(PMA_KEY) is None
