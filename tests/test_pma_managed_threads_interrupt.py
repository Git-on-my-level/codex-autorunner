from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration.runtime_bindings import (
    clear_runtime_thread_binding,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.server import create_hub_app
from tests.conftest import write_test_config


def _enable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


@pytest.mark.slow
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
            json={
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]
        store = PmaThreadStore(hub_env.hub_root)
        turn = store.create_turn(managed_thread_id, prompt="running turn")
        managed_turn_id = turn["managed_turn_id"]
        store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
        store.set_turn_backend_turn_id(managed_turn_id, "backend-turn-1")
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


@pytest.mark.slow
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
            json={
                "agent": "opencode",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]
        store = PmaThreadStore(hub_env.hub_root)
        turn = store.create_turn(managed_thread_id, prompt="running opencode turn")
        managed_turn_id = turn["managed_turn_id"]
        store.set_thread_backend_id(managed_thread_id, "session-123")
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


@pytest.mark.slow
def test_interrupt_managed_thread_rejects_without_running_turn(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        interrupt_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/interrupt",
        )

    assert interrupt_resp.status_code == 409
    assert "running turn" in (interrupt_resp.json().get("detail") or "").lower()


@pytest.mark.slow
def test_interrupt_managed_thread_recovers_when_runtime_binding_is_lost_after_restart(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def notify_transition(self, payload: dict[str, object]) -> None:
            _ = payload

    class FakeSupervisor:
        async def get_client(self, hub_root: Path):
            _ = hub_root
            raise AssertionError("interrupt client should not be requested")

    app.state.hub_supervisor.get_pma_automation_store = lambda: FakeAutomationStore()
    app.state.hub_supervisor.process_pma_automation_now = lambda **_kwargs: None
    app.state.app_server_supervisor = FakeSupervisor()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

    store = PmaThreadStore(hub_env.hub_root)
    turn = store.create_turn(managed_thread_id, prompt="running after restart")
    managed_turn_id = turn["managed_turn_id"]
    store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
    store.set_turn_backend_turn_id(managed_turn_id, "backend-turn-1")
    clear_runtime_thread_binding(hub_env.hub_root, managed_thread_id)

    with TestClient(app) as client:
        interrupt_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/interrupt",
        )

    assert interrupt_resp.status_code == 409
    assert "running turn" in (interrupt_resp.json().get("detail") or "").lower()
    updated_turn = store.get_turn(managed_thread_id, managed_turn_id)
    assert updated_turn is not None
    assert updated_turn["status"] == "error"
    assert updated_turn["error"] == "Backend thread missing from orchestration state"


@pytest.mark.slow
def test_interrupt_managed_thread_notifies_automation_failure(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.transitions: list[dict[str, object]] = []

        def notify_transition(self, payload: dict[str, object]) -> None:
            self.transitions.append(dict(payload))

    class FakeClient:
        async def turn_interrupt(
            self, turn_id: str, *, thread_id: str | None = None
        ) -> None:
            _ = turn_id, thread_id

    class FakeSupervisor:
        async def get_client(self, hub_root: Path):
            _ = hub_root
            return FakeClient()

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store
    app.state.app_server_supervisor = FakeSupervisor()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]
        store = PmaThreadStore(hub_env.hub_root)
        turn = store.create_turn(managed_thread_id, prompt="running turn")
        managed_turn_id = turn["managed_turn_id"]
        store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
        store.set_turn_backend_turn_id(managed_turn_id, "backend-turn-1")
        interrupt_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/interrupt",
        )

    assert interrupt_resp.status_code == 200
    assert len(fake_store.transitions) == 1
    transition = fake_store.transitions[0]
    assert transition["thread_id"] == managed_thread_id
    assert transition["repo_id"] == hub_env.repo_id
    assert transition["from_state"] == "running"
    assert transition["to_state"] == "interrupted"
    assert transition["reason"] == "managed_turn_interrupted"


@pytest.mark.slow
def test_interrupt_managed_thread_skips_failed_side_effects_when_turn_already_finished(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.transitions: list[dict[str, object]] = []

        def notify_transition(self, payload: dict[str, object]) -> None:
            self.transitions.append(dict(payload))

    class FakeClient:
        async def turn_interrupt(
            self, turn_id: str, *, thread_id: str | None = None
        ) -> None:
            _ = turn_id, thread_id

    class FakeSupervisor:
        async def get_client(self, hub_root: Path):
            _ = hub_root
            return FakeClient()

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store
    app.state.app_server_supervisor = FakeSupervisor()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]
        store = PmaThreadStore(hub_env.hub_root)
        turn = store.create_turn(managed_thread_id, prompt="running turn")
        managed_turn_id = turn["managed_turn_id"]
        store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
        store.set_turn_backend_turn_id(managed_turn_id, "backend-turn-1")

        original_mark_turn_finished = PmaThreadStore.mark_turn_finished

        def finish_then_report_not_interrupted(
            self: PmaThreadStore, managed_turn_id_arg: str
        ) -> bool:
            assert managed_turn_id_arg == managed_turn_id
            assert (
                original_mark_turn_finished(
                    self,
                    managed_turn_id_arg,
                    status="ok",
                    assistant_text="completed before interrupt persisted",
                )
                is True
            )
            return False

        monkeypatch.setattr(
            PmaThreadStore,
            "mark_turn_interrupted",
            finish_then_report_not_interrupted,
        )
        interrupt_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/interrupt",
        )

    assert interrupt_resp.status_code == 200
    payload = interrupt_resp.json()
    assert payload["status"] == "error"
    assert payload["interrupt_state"] == "failed"
    assert payload["managed_turn_id"] == managed_turn_id
    assert len(fake_store.transitions) == 0

    updated_turn = store.get_turn(managed_thread_id, managed_turn_id)
    assert updated_turn is not None
    assert updated_turn["status"] == "ok"
    assert updated_turn["assistant_text"] == "completed before interrupt persisted"


@pytest.mark.slow
def test_interrupt_managed_thread_recovers_stale_backend_thread(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeClient:
        async def turn_interrupt(
            self, turn_id: str, *, thread_id: str | None = None
        ) -> None:
            _ = turn_id, thread_id
            raise RuntimeError("thread not found: backend-thread-1")

    class FakeSupervisor:
        async def get_client(self, hub_root: Path):
            _ = hub_root
            return FakeClient()

    app.state.app_server_supervisor = FakeSupervisor()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]
        store = PmaThreadStore(hub_env.hub_root)
        turn = store.create_turn(managed_thread_id, prompt="running")
        managed_turn_id = turn["managed_turn_id"]
        store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
        store.set_turn_backend_turn_id(managed_turn_id, "backend-turn-1")
        interrupt_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/interrupt",
        )

    assert interrupt_resp.status_code == 200
    payload = interrupt_resp.json()
    assert payload["status"] == "ok"
    assert payload["interrupt_state"] == "recovered_lost_backend"
    assert payload["backend_error"] is None
    updated_turn = store.get_turn(managed_thread_id, managed_turn_id)
    assert updated_turn is not None
    assert updated_turn["status"] == "interrupted"
    assert updated_turn["error"] is None
    updated_binding = store.get_thread_runtime_binding(managed_thread_id)
    assert updated_binding is None
