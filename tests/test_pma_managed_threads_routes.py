from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration import (
    ActiveWorkSummary,
    OrchestrationBindingStore,
    ThreadTarget,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes.pma_routes import managed_threads
from tests.conftest import write_test_config

pytestmark = pytest.mark.slow


def _disable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def _set_default_terminal_followup(hub_root: Path, enabled: bool) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["managed_thread_terminal_followup_default"] = enabled
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def _repo_owner(hub_env) -> dict[str, str]:
    return {"resource_kind": "repo", "resource_id": hub_env.repo_id}


def test_create_managed_thread_with_repo_owner(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
                "name": "Primary thread",
            },
        )

    assert resp.status_code == 200
    thread = resp.json()["thread"]
    assert thread["agent"] == "codex"
    assert thread["repo_id"] == hub_env.repo_id
    assert thread["resource_kind"] == "repo"
    assert thread["resource_id"] == hub_env.repo_id
    assert thread["workspace_root"] == str(hub_env.repo_root.resolve())
    assert thread["name"] == "Primary thread"
    assert thread["backend_thread_id"] is None
    assert thread["status"] == "idle"
    assert thread["operator_status"] == "idle"
    assert thread["is_reusable"] is True
    assert thread["lifecycle_status"] == "active"
    assert thread["status_reason"] == "thread_created"
    assert thread["status_terminal"] is False
    assert thread["context_profile"] == "car_ambient"
    assert thread["approval_mode"] == "yolo"
    assert thread["managed_thread_id"]
    store = PmaThreadStore(hub_env.hub_root)
    stored = store.get_thread(thread["managed_thread_id"])
    assert stored is not None
    assert stored.get("backend_thread_id") is None
    notification = resp.json().get("notification") or {}
    subscription = notification.get("subscription") or {}
    assert subscription.get("thread_id") == thread["managed_thread_id"]

    automation_store = app.state.hub_supervisor.get_pma_automation_store()
    subscriptions = automation_store.list_subscriptions(
        thread_id=thread["managed_thread_id"]
    )
    assert len(subscriptions) == 1


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
    assert thread["resource_kind"] is None
    assert thread["resource_id"] is None
    assert thread["workspace_root"] == str((hub_env.hub_root / rel_workspace).resolve())
    assert thread["name"] == "Workspace thread"
    assert thread["context_profile"] == "car_ambient"
    assert thread["approval_mode"] == "yolo"


def test_build_managed_thread_orchestration_service_reuses_cached_harnesses(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_hub_app(hub_env.hub_root)
    created: list[object] = []

    def _make_harness(_ctx: object) -> object:
        harness = object()
        created.append(harness)
        return harness

    descriptor = AgentDescriptor(
        id="opencode",
        name="OpenCode",
        capabilities=frozenset({"durable_threads", "message_turns"}),
        make_harness=_make_harness,
    )
    monkeypatch.setattr(
        managed_threads,
        "get_registered_agents",
        lambda: {"opencode": descriptor},
    )

    request_a = SimpleNamespace(app=SimpleNamespace(state=app.state))
    request_b = SimpleNamespace(app=SimpleNamespace(state=app.state))

    service_a = managed_threads.build_managed_thread_orchestration_service(request_a)
    service_b = managed_threads.build_managed_thread_orchestration_service(request_b)

    harness_a = service_a.harness_factory("opencode")
    harness_b = service_b.harness_factory("opencode")

    assert harness_a is harness_b
    assert len(created) == 1


def test_create_managed_thread_with_agent_workspace_owner(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_hub_app(hub_env.hub_root)
    monkeypatch.setattr(
        "codex_autorunner.core.hub.probe_agent_workspace_runtime",
        lambda _config, _workspace: {
            "status": "ready",
            "message": "ZeroClaw runtime is ready",
        },
    )
    workspace = app.state.hub_supervisor.create_agent_workspace(
        workspace_id="zc-main",
        runtime="zeroclaw",
        display_name="ZeroClaw Main",
    )

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={
                "resource_kind": "agent_workspace",
                "resource_id": workspace.id,
                "name": "Workspace-owned thread",
            },
        )

    assert resp.status_code == 200
    thread = resp.json()["thread"]
    assert thread["agent"] == "zeroclaw"
    assert thread["repo_id"] is None
    assert thread["resource_kind"] == "agent_workspace"
    assert thread["resource_id"] == workspace.id
    assert thread["workspace_root"] == str(workspace.path.resolve())
    assert thread["context_profile"] == "none"
    assert thread["approval_mode"] == "yolo"


def test_create_and_resume_managed_thread_reject_backend_thread_id_input(
    hub_env,
) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
                "backend_thread_id": "thread-backend-1",
            },
        )
        resume_resp = client.post(
            "/hub/pma/threads/thread-1/resume",
            json={"backend_thread_id": "thread-backend-2"},
        )

    assert create_resp.status_code == 422
    assert resume_resp.status_code == 422


def test_create_managed_thread_accepts_explicit_context_profile(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
                "context_profile": "car_core",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["thread"]["context_profile"] == "car_core"


def test_create_managed_thread_accepts_explicit_approval_mode(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
                "approval_mode": "safe",
            },
        )

    assert resp.status_code == 200
    thread = resp.json()["thread"]
    assert thread["approval_mode"] == "safe"

    store = PmaThreadStore(hub_env.hub_root)
    stored = store.get_thread(thread["managed_thread_id"])
    assert stored is not None
    assert stored["metadata"]["approval_mode"] == "safe"


def test_create_managed_thread_rejects_mismatched_agent_workspace_runtime(
    hub_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_hub_app(hub_env.hub_root)
    monkeypatch.setattr(
        "codex_autorunner.core.hub.probe_agent_workspace_runtime",
        lambda _config, _workspace: {
            "status": "ready",
            "message": "ZeroClaw runtime is ready",
        },
    )
    workspace = app.state.hub_supervisor.create_agent_workspace(
        workspace_id="zc-main",
        runtime="zeroclaw",
        display_name="ZeroClaw Main",
    )

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "resource_kind": "agent_workspace",
                "resource_id": workspace.id,
            },
        )

    assert resp.status_code == 400
    assert "agent workspace runtime" in (resp.json().get("detail") or "").lower()


def test_create_managed_thread_rejects_incompatible_agent_workspace_runtime(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_hub_app(hub_env.hub_root)
    monkeypatch.setattr(
        "codex_autorunner.core.hub.probe_agent_workspace_runtime",
        lambda _config, _workspace: {
            "status": "ready",
            "message": "ZeroClaw runtime is ready",
        },
    )
    workspace = app.state.hub_supervisor.create_agent_workspace(
        workspace_id="zc-main",
        runtime="zeroclaw",
        display_name="ZeroClaw Main",
    )
    monkeypatch.setattr(
        app.state.hub_supervisor,
        "get_agent_workspace_runtime_readiness",
        lambda _workspace_id: {
            "status": "incompatible",
            "message": "ZeroClaw CLI is incompatible",
        },
    )

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={
                "resource_kind": "agent_workspace",
                "resource_id": workspace.id,
            },
        )

    assert resp.status_code == 400
    assert "incompatible" in (resp.json().get("detail") or "").lower()


def test_create_managed_thread_rejects_disabled_agent_workspace(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    workspace = app.state.hub_supervisor.create_agent_workspace(
        workspace_id="zc-main",
        runtime="zeroclaw",
        display_name="ZeroClaw Main",
        enabled=False,
    )

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={
                "resource_kind": "agent_workspace",
                "resource_id": workspace.id,
            },
        )

    assert resp.status_code == 400
    assert "disabled" in (resp.json().get("detail") or "").lower()


def test_create_managed_thread_rejects_unknown_agent(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "bogus",
                **_repo_owner(hub_env),
                "name": "Invalid thread",
            },
        )

    assert resp.status_code == 422
    assert "codex" in str(resp.json())
    assert "zeroclaw" in str(resp.json())


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
                **_repo_owner(hub_env),
                "notify_on": "invalid",
            },
        )
        assert create_resp.status_code == 422
        assert "notify_on" in str(create_resp.json())

        after_resp = client.get("/hub/pma/threads")
        assert after_resp.status_code == 200
        after_count = len(after_resp.json().get("threads") or [])

    assert after_count == before_count


def test_create_managed_thread_rejects_legacy_repo_owner_alias(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )

    assert resp.status_code == 422
    assert "repo_id" in str(resp.json())


def test_create_managed_thread_rejects_missing_or_both_inputs(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        missing = client.post("/hub/pma/threads", json={"agent": "codex"})
        both = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
                "workspace_root": str(hub_env.repo_root),
            },
        )

    assert missing.status_code == 400
    assert "Exactly one of resource owner or workspace_root is required" in (
        missing.json().get("detail") or ""
    )
    assert both.status_code == 400
    assert "Exactly one of resource owner or workspace_root is required" in (
        both.json().get("detail") or ""
    )


def test_list_managed_threads_returns_created_thread(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
                "name": "List me",
            },
        )
        assert create_resp.status_code == 200
        created_id = create_resp.json()["thread"]["managed_thread_id"]

        list_resp = client.get(
            "/hub/pma/threads",
            params={"agent": "codex", **_repo_owner(hub_env), "limit": 200},
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
            json={"agent": "codex", **_repo_owner(hub_env)},
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


def test_list_managed_threads_includes_ticket_flow_threads_for_repo(hub_env) -> None:
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="ticket-flow:codex",
        metadata={
            "thread_kind": "ticket_flow",
            "flow_type": "ticket_flow",
            "run_id": "run-123",
        },
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        list_resp = client.get(
            "/hub/pma/threads",
            params={**_repo_owner(hub_env), "limit": 200},
        )

    assert list_resp.status_code == 200
    threads = list_resp.json()["threads"]
    thread = next(
        item
        for item in threads
        if item["managed_thread_id"] == created["managed_thread_id"]
    )
    assert thread["name"] == "ticket-flow:codex"
    assert thread["repo_id"] == hub_env.repo_id
    assert thread["resource_kind"] == "repo"
    assert thread["resource_id"] == hub_env.repo_id


def test_get_managed_thread_returns_created_thread(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
            },
        )
        assert create_resp.status_code == 200
        created = create_resp.json()["thread"]

        get_resp = client.get(f"/hub/pma/threads/{created['managed_thread_id']}")

    assert get_resp.status_code == 200
    fetched = get_resp.json()["thread"]
    assert fetched["managed_thread_id"] == created["managed_thread_id"]


def test_managed_thread_routes_expose_chat_binding_metadata(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
                "name": "Chat-bound thread",
            },
        )
        assert create_resp.status_code == 200
        created = create_resp.json()["thread"]
        thread_id = created["managed_thread_id"]
        assert created["chat_bound"] is False
        assert created["cleanup_protected"] is False

        OrchestrationBindingStore(hub_env.hub_root).upsert_binding(
            surface_kind="telegram",
            surface_key="telegram:-1001:root",
            thread_target_id=thread_id,
            agent_id="codex",
            repo_id=hub_env.repo_id,
            mode="reuse",
        )

        get_resp = client.get(f"/hub/pma/threads/{thread_id}")
        list_resp = client.get(
            "/hub/pma/threads",
            params={"agent": "codex", **_repo_owner(hub_env), "limit": 200},
        )

    assert get_resp.status_code == 200
    fetched = get_resp.json()["thread"]
    assert fetched["chat_bound"] is True
    assert fetched["binding_kind"] == "telegram"
    assert fetched["binding_id"] == "telegram:-1001:root"
    assert fetched["binding_count"] == 1
    assert fetched["binding_kinds"] == ["telegram"]
    assert fetched["binding_ids"] == ["telegram:-1001:root"]
    assert fetched["cleanup_protected"] is True

    listed = next(
        item
        for item in list_resp.json()["threads"]
        if item["managed_thread_id"] == thread_id
    )
    assert listed["chat_bound"] is True
    assert listed["binding_kind"] == "telegram"
    assert listed["cleanup_protected"] is True
    assert fetched["repo_id"] == hub_env.repo_id
    assert fetched["resource_kind"] == "repo"
    assert fetched["resource_id"] == hub_env.repo_id


def test_create_managed_thread_succeeds_when_binding_metadata_lookup_fails(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_hub_app(hub_env.hub_root)
    monkeypatch.setattr(
        managed_threads,
        "active_chat_binding_metadata_by_thread",
        lambda *, hub_root: (_ for _ in ()).throw(
            RuntimeError("binding db unavailable")
        ),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
                "name": "Degraded metadata thread",
            },
        )

    assert resp.status_code == 200
    thread = resp.json()["thread"]
    assert thread["managed_thread_id"]
    assert thread["chat_bound"] is False
    assert thread["binding_kind"] is None
    assert thread["binding_count"] == 0
    assert thread["cleanup_protected"] is False
    assert thread["operator_status"] == "idle"
    assert thread["is_reusable"] is True


def test_create_managed_thread_notify_on_terminal_creates_subscription(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
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


def test_create_managed_thread_terminal_followup_false_opts_out(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
                "terminal_followup": False,
            },
        )
        assert create_resp.status_code == 200
        payload = create_resp.json()
        thread = payload["thread"]
        assert "notification" not in payload

    automation_store = app.state.hub_supervisor.get_pma_automation_store()
    subscriptions = automation_store.list_subscriptions(
        thread_id=thread["managed_thread_id"]
    )
    assert subscriptions == []


def test_create_managed_thread_respects_config_disabled_default_followup(
    hub_env,
) -> None:
    _set_default_terminal_followup(hub_env.hub_root, False)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        payload = create_resp.json()
        thread = payload["thread"]
        assert "notification" not in payload

    automation_store = app.state.hub_supervisor.get_pma_automation_store()
    subscriptions = automation_store.list_subscriptions(
        thread_id=thread["managed_thread_id"]
    )
    assert subscriptions == []


def test_create_managed_thread_with_explicit_notify_lane_requires_subscription(
    hub_env,
) -> None:
    app = create_hub_app(hub_env.hub_root)

    class PartialAutomationStore:
        def create_subscription(self) -> None:
            return None

    app.state.hub_supervisor.get_pma_automation_store = lambda: PartialAutomationStore()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
                "notify_lane": "pma:lane-next",
            },
        )

    assert create_resp.status_code == 503


def test_create_managed_thread_default_followup_ignores_partial_automation_store(
    hub_env,
) -> None:
    app = create_hub_app(hub_env.hub_root)

    class PartialAutomationStore:
        def create_subscription(self) -> None:
            return None

    app.state.hub_supervisor.get_pma_automation_store = lambda: PartialAutomationStore()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )

    assert create_resp.status_code == 200
    payload = create_resp.json()
    assert "notification" not in payload


def test_managed_thread_routes_respect_pma_enabled_flag(hub_env) -> None:
    _disable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        list_resp = client.get("/hub/pma/threads")
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )

    assert list_resp.status_code == 404
    assert create_resp.status_code == 404


def test_resume_managed_thread_starts_fresh_backend_on_next_send(hub_env) -> None:
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
            self.runtime_instance_id = "runtime-test-1"

        async def start(self) -> None:
            return None

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

    class FakeEvents:
        async def stream(self, conversation_id: str, turn_id: str):
            _ = conversation_id, turn_id
            if False:
                yield ""

    app.state.app_server_events = FakeEvents()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        archive_resp = client.post(f"/hub/pma/threads/{managed_thread_id}/archive")
        assert archive_resp.status_code == 200

        resume_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/resume", json={}
        )
        assert resume_resp.status_code == 200
        resumed_thread = resume_resp.json()["thread"]
        assert resumed_thread["status"] == "idle"
        assert resumed_thread["operator_status"] == "idle"
        assert resumed_thread["is_reusable"] is True
        assert resumed_thread["lifecycle_status"] == "active"
        assert resumed_thread["backend_thread_id"] is None

        send_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "message after resume"},
        )
        assert send_resp.status_code == 200
        payload = send_resp.json()
        assert payload["status"] == "ok"
        assert payload["backend_thread_id"] == "backend-thread-1"

        get_resp = client.get(f"/hub/pma/threads/{managed_thread_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["thread"]["status"] == "completed"
        assert get_resp.json()["thread"]["operator_status"] == "reusable"
        assert get_resp.json()["thread"]["is_reusable"] is True
        assert get_resp.json()["thread"]["lifecycle_status"] == "active"

    assert fake_supervisor.client.resume_calls == []


def test_resume_managed_thread_without_backend_binding_reactivates_thread(
    hub_env,
) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        archive_resp = client.post(f"/hub/pma/threads/{managed_thread_id}/archive")
        assert archive_resp.status_code == 200

        resume_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/resume",
            json={},
        )

    assert resume_resp.status_code == 200
    resumed_thread = resume_resp.json()["thread"]
    assert resumed_thread["managed_thread_id"] == managed_thread_id
    assert resumed_thread["lifecycle_status"] == "active"
    assert resumed_thread["backend_thread_id"] is None


def test_managed_thread_crud_routes_use_orchestration_service(
    hub_env, monkeypatch
) -> None:
    class FakeService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.thread = ThreadTarget(
                thread_target_id="thread-orch-1",
                agent_id="codex",
                backend_thread_id="backend-thread-1",
                repo_id=hub_env.repo_id,
                workspace_root=str(hub_env.repo_root.resolve()),
                display_name="Orchestrated thread",
                status="idle",
                lifecycle_status="active",
                status_reason="thread_created",
                status_changed_at="2026-03-13T00:00:00Z",
                status_terminal=False,
            )

        def create_thread_target(
            self,
            agent_id,
            workspace_root,
            *,
            repo_id=None,
            resource_kind=None,
            resource_id=None,
            display_name=None,
            metadata=None,
        ):
            self.calls.append(
                (
                    "create",
                    {
                        "agent_id": agent_id,
                        "workspace_root": str(workspace_root),
                        "repo_id": repo_id,
                        "resource_kind": resource_kind,
                        "resource_id": resource_id,
                        "display_name": display_name,
                        "metadata": metadata,
                    },
                )
            )
            self.thread = ThreadTarget(
                thread_target_id=self.thread.thread_target_id,
                agent_id=agent_id,
                repo_id=repo_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                workspace_root=str(workspace_root),
                display_name=display_name,
                status="idle",
                lifecycle_status="active",
                status_reason="thread_created",
                status_changed_at="2026-03-13T00:00:00Z",
                status_terminal=False,
            )
            return self.thread

        def list_thread_targets(
            self,
            *,
            agent_id=None,
            lifecycle_status=None,
            runtime_status=None,
            repo_id=None,
            resource_kind=None,
            resource_id=None,
            limit=200,
        ):
            self.calls.append(
                (
                    "list",
                    {
                        "agent_id": agent_id,
                        "lifecycle_status": lifecycle_status,
                        "runtime_status": runtime_status,
                        "repo_id": repo_id,
                        "resource_kind": resource_kind,
                        "resource_id": resource_id,
                        "limit": limit,
                    },
                )
            )
            return [self.thread]

        def get_thread_target(self, thread_target_id):
            self.calls.append(("get", {"thread_target_id": thread_target_id}))
            if thread_target_id != self.thread.thread_target_id:
                return None
            return self.thread

        def resume_thread_target(
            self,
            thread_target_id,
        ):
            self.calls.append(("resume", {"thread_target_id": thread_target_id}))
            self.thread = ThreadTarget(
                **{
                    **self.thread.to_dict(),
                    "status": "idle",
                    "lifecycle_status": "active",
                    "status_reason": "thread_resumed",
                    "status_terminal": False,
                }
            )
            return self.thread

        def archive_thread_target(self, thread_target_id):
            self.calls.append(("archive", {"thread_target_id": thread_target_id}))
            self.thread = ThreadTarget(
                **{
                    **self.thread.to_dict(),
                    "status": "archived",
                    "lifecycle_status": "archived",
                    "status_reason": "thread_archived",
                    "status_terminal": True,
                }
            )
            return self.thread

    fake_service = FakeService()
    monkeypatch.setattr(
        managed_threads,
        "build_managed_thread_orchestration_service",
        lambda request: fake_service,
    )
    monkeypatch.setattr(
        managed_threads.PmaThreadStore,
        "append_action",
        lambda self, action_type, *, managed_thread_id=None, payload_json=None: 1,
    )

    app = create_hub_app(hub_env.hub_root)
    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                **_repo_owner(hub_env),
                "name": "Orchestrated thread",
            },
        )
        list_resp = client.get(
            "/hub/pma/threads",
            params={"agent": "codex", "status": "idle", **_repo_owner(hub_env)},
        )
        get_resp = client.get("/hub/pma/threads/thread-orch-1")
        resume_resp = client.post("/hub/pma/threads/thread-orch-1/resume", json={})
        archive_resp = client.post("/hub/pma/threads/thread-orch-1/archive")

    assert create_resp.status_code == 200
    assert list_resp.status_code == 200
    assert get_resp.status_code == 200
    assert resume_resp.status_code == 200
    assert archive_resp.status_code == 200

    created = create_resp.json()["thread"]
    assert created["managed_thread_id"] == "thread-orch-1"
    assert created["workspace_root"] == str(hub_env.repo_root.resolve())
    assert list_resp.json()["threads"][0]["managed_thread_id"] == "thread-orch-1"
    assert get_resp.json()["thread"]["managed_thread_id"] == "thread-orch-1"
    assert resume_resp.json()["thread"]["backend_thread_id"] is None
    assert archive_resp.json()["thread"]["lifecycle_status"] == "archived"
    assert archive_resp.json()["thread"]["status"] == "archived"
    assert created["operator_status"] == "idle"
    assert created["is_reusable"] is True
    assert archive_resp.json()["thread"]["operator_status"] == "archived"
    assert archive_resp.json()["thread"]["is_reusable"] is False

    assert fake_service.calls == [
        (
            "create",
            {
                "agent_id": "codex",
                "workspace_root": str(hub_env.repo_root.resolve()),
                "repo_id": hub_env.repo_id,
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
                "display_name": "Orchestrated thread",
                "metadata": {
                    "context_profile": "car_ambient",
                    "approval_mode": "yolo",
                },
            },
        ),
        (
            "list",
            {
                "agent_id": "codex",
                "lifecycle_status": None,
                "runtime_status": "idle",
                "repo_id": hub_env.repo_id,
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
                "limit": 200,
            },
        ),
        ("get", {"thread_target_id": "thread-orch-1"}),
        ("get", {"thread_target_id": "thread-orch-1"}),
        ("resume", {"thread_target_id": "thread-orch-1"}),
        ("get", {"thread_target_id": "thread-orch-1"}),
        ("archive", {"thread_target_id": "thread-orch-1"}),
    ]


def test_list_bindings_work_route_returns_busy_work_summaries(
    hub_env, monkeypatch
) -> None:
    class FakeService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def list_active_work_summaries(
            self,
            *,
            agent_id=None,
            repo_id=None,
            resource_kind=None,
            resource_id=None,
            limit=200,
        ):
            self.calls.append(
                (
                    "list_active_work_summaries",
                    {
                        "agent_id": agent_id,
                        "repo_id": repo_id,
                        "resource_kind": resource_kind,
                        "resource_id": resource_id,
                        "limit": limit,
                    },
                )
            )
            return [
                ActiveWorkSummary(
                    thread_target_id="thread-orch-1",
                    agent_id="codex",
                    repo_id=hub_env.repo_id,
                    resource_kind="repo",
                    resource_id=hub_env.repo_id,
                    workspace_root=str(hub_env.repo_root.resolve()),
                    display_name="Busy thread",
                    lifecycle_status="active",
                    runtime_status="completed",
                    execution_id="turn-queued-1",
                    execution_status="queued",
                    queued_count=1,
                    message_preview="Follow-up queued",
                    binding_count=2,
                    surface_kinds=("discord", "telegram"),
                )
            ]

    fake_service = FakeService()
    monkeypatch.setattr(
        managed_threads,
        "build_managed_thread_orchestration_service",
        lambda request: fake_service,
    )

    app = create_hub_app(hub_env.hub_root)
    with TestClient(app) as client:
        resp = client.get(
            "/hub/pma/bindings/work",
            params={"agent": "codex", **_repo_owner(hub_env), "limit": 25},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "summaries": [
            {
                "thread_target_id": "thread-orch-1",
                "agent_id": "codex",
                "repo_id": hub_env.repo_id,
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
                "workspace_root": str(hub_env.repo_root.resolve()),
                "display_name": "Busy thread",
                "lifecycle_status": "active",
                "runtime_status": "completed",
                "execution_id": "turn-queued-1",
                "execution_status": "queued",
                "queued_count": 1,
                "message_preview": "Follow-up queued",
                "binding_count": 2,
                "surface_kinds": ["discord", "telegram"],
            }
        ]
    }
    assert fake_service.calls == [
        (
            "list_active_work_summaries",
            {
                "agent_id": "codex",
                "repo_id": hub_env.repo_id,
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
                "limit": 25,
            },
        )
    ]
