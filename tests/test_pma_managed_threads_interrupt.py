from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.server import create_hub_app
from tests.conftest import write_test_config


def _enable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def test_interrupt_managed_thread_codex_marks_turn_interrupted(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeClient:
        def __init__(self) -> None:
            self.turn_interrupt_calls: list[tuple[str, str | None]] = []

        async def turn_interrupt(
            self, turn_id: str, *, thread_id: str | None = None
        ) -> None:
            self.turn_interrupt_calls.append((turn_id, thread_id))

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    fake_supervisor = FakeSupervisor()
    app.state.app_server_supervisor = fake_supervisor

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

    store = PmaThreadStore(hub_env.hub_root)
    turn = store.create_turn(managed_thread_id, prompt="running turn")
    managed_turn_id = turn["managed_turn_id"]
    store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
    store.set_turn_backend_turn_id(managed_turn_id, "backend-turn-1")

    with TestClient(app) as client:
        interrupt_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/interrupt",
        )

    assert interrupt_resp.status_code == 200
    payload = interrupt_resp.json()
    assert payload["status"] == "ok"
    assert payload["managed_turn_id"] == managed_turn_id
    assert fake_supervisor.client.turn_interrupt_calls == [
        ("backend-turn-1", "backend-thread-1")
    ]

    updated_turn = store.get_turn(managed_thread_id, managed_turn_id)
    assert updated_turn is not None
    assert updated_turn["status"] == "interrupted"
    assert updated_turn["finished_at"] is not None


def test_interrupt_managed_thread_opencode_marks_turn_interrupted(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeClient:
        def __init__(self) -> None:
            self.abort_calls: list[str] = []

        async def abort(self, session_id: str) -> None:
            self.abort_calls.append(session_id)

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    fake_supervisor = FakeSupervisor()
    app.state.opencode_supervisor = fake_supervisor

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "opencode", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

    store = PmaThreadStore(hub_env.hub_root)
    turn = store.create_turn(managed_thread_id, prompt="running opencode turn")
    managed_turn_id = turn["managed_turn_id"]
    store.set_thread_backend_id(managed_thread_id, "session-123")

    with TestClient(app) as client:
        interrupt_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/interrupt",
        )

    assert interrupt_resp.status_code == 200
    payload = interrupt_resp.json()
    assert payload["status"] == "ok"
    assert payload["managed_turn_id"] == managed_turn_id
    assert fake_supervisor.client.abort_calls == ["session-123"]

    updated_turn = store.get_turn(managed_thread_id, managed_turn_id)
    assert updated_turn is not None
    assert updated_turn["status"] == "interrupted"
    assert updated_turn["finished_at"] is not None


def test_interrupt_managed_thread_rejects_without_running_turn(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        interrupt_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/interrupt",
        )

    assert interrupt_resp.status_code == 409
    assert "running turn" in (interrupt_resp.json().get("detail") or "").lower()
