from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.server import create_hub_app
from tests.conftest import write_test_config


def _disable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def test_create_managed_thread_with_repo_id(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "repo_id": hub_env.repo_id,
                "name": "Primary thread",
                "backend_thread_id": "thread-backend-1",
            },
        )

    assert resp.status_code == 200
    thread = resp.json()["thread"]
    assert thread["agent"] == "codex"
    assert thread["repo_id"] == hub_env.repo_id
    assert thread["workspace_root"] == str(hub_env.repo_root.resolve())
    assert thread["name"] == "Primary thread"
    assert thread["backend_thread_id"] == "thread-backend-1"
    assert thread["status"] == "idle"
    assert thread["lifecycle_status"] == "active"
    assert thread["status_reason"] == "thread_created"
    assert thread["status_terminal"] is False
    assert thread["managed_thread_id"]


def test_create_managed_thread_with_workspace_root(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    rel_workspace = str(Path("worktrees") / hub_env.repo_id)

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "opencode",
                "workspace_root": rel_workspace,
                "name": "Workspace thread",
            },
        )

    assert resp.status_code == 200
    thread = resp.json()["thread"]
    assert thread["agent"] == "opencode"
    assert thread["repo_id"] is None
    assert thread["workspace_root"] == str((hub_env.hub_root / rel_workspace).resolve())
    assert thread["name"] == "Workspace thread"


def test_create_managed_thread_rejects_invalid_notify_on_without_side_effect(
    hub_env,
) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        before_resp = client.get("/hub/pma/threads")
        assert before_resp.status_code == 200
        before_count = len(before_resp.json().get("threads") or [])

        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "repo_id": hub_env.repo_id,
                "notify_on": "invalid",
            },
        )
        assert create_resp.status_code == 400
        assert "notify_on" in (create_resp.json().get("detail") or "")

        after_resp = client.get("/hub/pma/threads")
        assert after_resp.status_code == 200
        after_count = len(after_resp.json().get("threads") or [])

    assert after_count == before_count


def test_create_managed_thread_rejects_missing_or_both_inputs(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        missing = client.post("/hub/pma/threads", json={"agent": "codex"})
        both = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "repo_id": hub_env.repo_id,
                "workspace_root": str(hub_env.repo_root),
            },
        )

    assert missing.status_code == 400
    assert "Exactly one of repo_id or workspace_root is required" in (
        missing.json().get("detail") or ""
    )
    assert both.status_code == 400
    assert "Exactly one of repo_id or workspace_root is required" in (
        both.json().get("detail") or ""
    )


def test_list_managed_threads_returns_created_thread(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "repo_id": hub_env.repo_id,
                "name": "List me",
            },
        )
        assert create_resp.status_code == 200
        created_id = create_resp.json()["thread"]["managed_thread_id"]

        list_resp = client.get(
            "/hub/pma/threads",
            params={"agent": "codex", "repo_id": hub_env.repo_id, "limit": 200},
        )

    assert list_resp.status_code == 200
    threads = list_resp.json()["threads"]
    assert isinstance(threads, list)
    assert any(thread["managed_thread_id"] == created_id for thread in threads)


def test_list_managed_threads_supports_normalized_status_filter(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200

        ready_resp = client.get("/hub/pma/threads", params={"status": "idle"})
        active_resp = client.get(
            "/hub/pma/threads",
            params={"lifecycle_status": "active"},
        )

    assert ready_resp.status_code == 200
    assert len(ready_resp.json()["threads"]) == 1
    assert active_resp.status_code == 200
    assert len(active_resp.json()["threads"]) == 1


def test_get_managed_thread_returns_created_thread(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "repo_id": hub_env.repo_id,
            },
        )
        assert create_resp.status_code == 200
        created = create_resp.json()["thread"]

        get_resp = client.get(f"/hub/pma/threads/{created['managed_thread_id']}")

    assert get_resp.status_code == 200
    fetched = get_resp.json()["thread"]
    assert fetched["managed_thread_id"] == created["managed_thread_id"]
    assert fetched["repo_id"] == hub_env.repo_id


def test_create_managed_thread_notify_on_terminal_creates_subscription(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "repo_id": hub_env.repo_id,
                "notify_on": "terminal",
                "notify_lane": "pma:lane-next",
                "notify_once": True,
            },
        )
        assert create_resp.status_code == 200
        payload = create_resp.json()
        thread = payload["thread"]
        notification = payload.get("notification") or {}
        subscription = notification.get("subscription") or {}
        assert subscription.get("thread_id") == thread["managed_thread_id"]
        assert subscription.get("lane_id") == "pma:lane-next"

    automation_store = app.state.hub_supervisor.get_pma_automation_store()
    subscriptions = automation_store.list_subscriptions(
        thread_id=thread["managed_thread_id"]
    )
    assert len(subscriptions) == 1


def test_managed_thread_routes_respect_pma_enabled_flag(hub_env) -> None:
    _disable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        list_resp = client.get("/hub/pma/threads")
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )

    assert list_resp.status_code == 404
    assert create_resp.status_code == 404


def test_resume_managed_thread_allows_send_without_new_backend_thread(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "backend-turn-1"

        async def wait(self, timeout=None):
            _ = timeout
            return type(
                "Result",
                (),
                {
                    "agent_messages": ["assistant output"],
                    "raw_events": [],
                    "errors": [],
                },
            )()

    class FakeClient:
        def __init__(self) -> None:
            self.resume_calls: list[str] = []
            self.thread_start_calls = 0
            self.turn_start_calls: list[dict[str, str]] = []

        async def thread_resume(self, thread_id: str) -> None:
            self.resume_calls.append(thread_id)

        async def thread_start(self, root: str) -> dict[str, str]:
            _ = root
            self.thread_start_calls += 1
            return {"id": f"backend-thread-{self.thread_start_calls}"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = approval_policy, sandbox_policy, turn_kwargs
            self.turn_start_calls.append({"thread_id": thread_id, "prompt": prompt})
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    fake_supervisor = FakeSupervisor()
    app.state.app_server_supervisor = fake_supervisor
    app.state.app_server_events = object()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        archive_resp = client.post(f"/hub/pma/threads/{managed_thread_id}/archive")
        assert archive_resp.status_code == 200

        resumed_backend_id = "backend-thread-manual"
        resume_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/resume",
            json={"backend_thread_id": resumed_backend_id},
        )
        assert resume_resp.status_code == 200
        resumed_thread = resume_resp.json()["thread"]
        assert resumed_thread["status"] == "idle"
        assert resumed_thread["lifecycle_status"] == "active"
        assert resumed_thread["backend_thread_id"] == resumed_backend_id

        send_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "message after resume"},
        )
        assert send_resp.status_code == 200
        payload = send_resp.json()
        assert payload["status"] == "ok"
        assert payload["backend_thread_id"] == resumed_backend_id

        get_resp = client.get(f"/hub/pma/threads/{managed_thread_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["thread"]["status"] == "completed"
        assert get_resp.json()["thread"]["lifecycle_status"] == "active"

    assert fake_supervisor.client.resume_calls == [resumed_backend_id]
    assert fake_supervisor.client.thread_start_calls == 0
    assert len(fake_supervisor.client.turn_start_calls) == 1
