from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes.pma_routes import tail_stream
from tests.pma_support import _enable_pma

pytestmark = pytest.mark.slow


def test_pma_automation_subscription_endpoints(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.created_payloads: list[dict[str, Any]] = []
            self.list_filters: list[dict[str, Any]] = []
            self.deleted_ids: list[str] = []

        def create_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.created_payloads.append(dict(payload))
            return {"subscription_id": "sub-1", **payload}

        def list_subscriptions(self, **filters: Any) -> list[dict[str, Any]]:
            self.list_filters.append(dict(filters))
            return [{"subscription_id": "sub-1", "thread_id": "thread-1"}]

        def cancel_subscription(self, subscription_id: str) -> dict[str, Any]:
            self.deleted_ids.append(subscription_id)
            return {"deleted": True}

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/subscriptions",
            json={
                "thread_id": "thread-1",
                "from_state": "running",
                "to_state": "completed",
                "reason": "manual",
                "timestamp": "2026-03-01T12:00:00Z",
            },
        )
        assert create_resp.status_code == 200
        created = create_resp.json()["subscription"]
        assert created["subscription_id"] == "sub-1"
        assert created["thread_id"] == "thread-1"

        list_resp = client.get(
            "/hub/pma/automation/subscriptions",
            params={"thread_id": "thread-1", "limit": 5},
        )
        assert list_resp.status_code == 200
        listed = list_resp.json()["subscriptions"]
        assert listed and listed[0]["subscription_id"] == "sub-1"

        delete_resp = client.delete("/hub/pma/subscriptions/sub-1")
        assert delete_resp.status_code == 200
        assert delete_resp.json()["status"] == "ok"
        assert delete_resp.json()["subscription_id"] == "sub-1"

    assert fake_store.created_payloads
    assert fake_store.created_payloads[0]["from_state"] == "running"
    assert fake_store.created_payloads[0]["to_state"] == "completed"
    assert (
        fake_store.list_filters
        and fake_store.list_filters[0]["thread_id"] == "thread-1"
    )
    assert fake_store.deleted_ids == ["sub-1"]


@pytest.mark.parametrize(
    ("request_body", "expected_event_types"),
    [
        (
            {
                "event_type": "managed_thread_completed",
                "thread_id": "thread-1",
            },
            ["managed_thread_completed"],
        ),
        (
            {
                "event_types": [
                    "managed_thread_completed",
                    "managed_thread_failed",
                ],
                "thread_id": "thread-2",
            },
            ["managed_thread_completed", "managed_thread_failed"],
        ),
    ],
)
def test_pma_automation_subscription_create_normalizes_event_type_aliases(
    hub_env,
    request_body: dict[str, Any],
    expected_event_types: list[str],
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.created_payloads: list[dict[str, Any]] = []

        def create_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.created_payloads.append(dict(payload))
            return {"subscription_id": "sub-event-types-1", **payload}

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post("/hub/pma/subscriptions", json=request_body)

    assert create_resp.status_code == 200
    assert fake_store.created_payloads == [
        {
            "thread_id": request_body["thread_id"],
            "event_types": expected_event_types,
        }
    ]


def test_pma_automation_subscription_create_accepts_nested_filter_payload(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.created_payloads: list[dict[str, Any]] = []

        def create_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.created_payloads.append(dict(payload))
            return {"subscription_id": "sub-filter-1", **payload}

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/subscriptions",
            json={
                "event_types": ["managed_thread_completed"],
                "filter": {"thread_id": "thread-filter-1", "repoId": "repo-filter-1"},
            },
        )

    assert create_resp.status_code == 200
    assert fake_store.created_payloads == [
        {
            "event_types": ["managed_thread_completed"],
            "thread_id": "thread-filter-1",
            "repo_id": "repo-filter-1",
        }
    ]


def test_pma_automation_subscription_create_rejects_unknown_filter_keys(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def create_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"subscription_id": "should-not-be-created", **payload}

    app.state.hub_supervisor.get_pma_automation_store = lambda: FakeAutomationStore()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/subscriptions",
            json={
                "event_types": ["managed_thread_completed"],
                "filter": {"thread_id": "thread-filter-1", "workspace_id": "ws-1"},
            },
        )

    assert create_resp.status_code == 400
    assert "Unsupported subscription filter keys" in create_resp.json()["detail"]
    assert "workspace_id" in create_resp.json()["detail"]


def test_pma_automation_subscription_create_rejects_conflicting_filter_values(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def create_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"subscription_id": "should-not-be-created", **payload}

    app.state.hub_supervisor.get_pma_automation_store = lambda: FakeAutomationStore()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/subscriptions",
            json={
                "event_types": ["managed_thread_completed"],
                "thread_id": "thread-top-level",
                "filter": {"thread_id": "thread-filter"},
            },
        )

    assert create_resp.status_code == 400
    assert "Conflicting values for thread_id" in create_resp.json()["detail"]


def test_pma_automation_timer_endpoints(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.created_payloads: list[dict[str, Any]] = []
            self.list_filters: list[dict[str, Any]] = []
            self.touched: list[tuple[str, dict[str, Any]]] = []
            self.cancelled: list[tuple[str, dict[str, Any]]] = []

        def create_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.created_payloads.append(dict(payload))
            return {"timer_id": "timer-1", **payload}

        def list_timers(self, **filters: Any) -> list[dict[str, Any]]:
            self.list_filters.append(dict(filters))
            return [{"timer_id": "timer-1", "thread_id": "thread-1"}]

        def touch_timer(self, timer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.touched.append((timer_id, dict(payload)))
            return {"timer_id": timer_id, "touched": True}

        def cancel_timer(
            self, timer_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            self.cancelled.append((timer_id, dict(payload)))
            return {"timer_id": timer_id, "cancelled": True}

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_type": "one_shot",
                "delay_seconds": 1800,
                "lane_id": "pma:lane-next",
                "thread_id": "thread-1",
                "from_state": "running",
                "to_state": "failed",
                "reason": "timeout",
            },
        )
        assert create_resp.status_code == 200
        created = create_resp.json()["timer"]
        assert created["timer_id"] == "timer-1"
        assert created["thread_id"] == "thread-1"

        list_resp = client.get(
            "/hub/pma/automation/timers",
            params={"thread_id": "thread-1", "limit": 20},
        )
        assert list_resp.status_code == 200
        listed = list_resp.json()["timers"]
        assert listed and listed[0]["timer_id"] == "timer-1"

        touch_resp = client.post(
            "/hub/pma/timers/timer-1/touch",
            json={"reason": "heartbeat"},
        )
        assert touch_resp.status_code == 200
        assert touch_resp.json()["timer_id"] == "timer-1"

        cancel_resp = client.post(
            "/hub/pma/timers/timer-1/cancel",
            json={"reason": "done"},
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["timer_id"] == "timer-1"

    assert fake_store.created_payloads
    assert fake_store.created_payloads[0]["timer_type"] == "one_shot"
    assert fake_store.created_payloads[0]["delay_seconds"] == 1800
    assert fake_store.created_payloads[0]["lane_id"] == "pma:lane-next"
    assert fake_store.created_payloads[0]["to_state"] == "failed"
    assert (
        fake_store.list_filters
        and fake_store.list_filters[0]["thread_id"] == "thread-1"
    )
    assert fake_store.touched == [("timer-1", {"reason": "heartbeat"})]
    assert fake_store.cancelled == [("timer-1", {"reason": "done"})]


def test_pma_automation_timer_create_accepts_nested_filter_payload(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.created_payloads: list[dict[str, Any]] = []

        def create_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.created_payloads.append(dict(payload))
            return {"timer_id": "timer-filter-1", **payload}

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_type": "one_shot",
                "delay_seconds": 30,
                "filter": {"threadId": "thread-filter-1", "repo_id": "repo-filter-1"},
            },
        )

    assert create_resp.status_code == 200
    assert fake_store.created_payloads == [
        {
            "timer_type": "one_shot",
            "delay_seconds": 30,
            "thread_id": "thread-filter-1",
            "repo_id": "repo-filter-1",
        }
    ]


def test_pma_automation_timer_create_rejects_unknown_filter_keys(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def create_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"timer_id": "should-not-be-created", **payload}

    app.state.hub_supervisor.get_pma_automation_store = lambda: FakeAutomationStore()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_type": "one_shot",
                "delay_seconds": 30,
                "filter": {"thread_id": "thread-filter-1", "workspace_id": "ws-1"},
            },
        )

    assert create_resp.status_code == 400
    assert "Unsupported timer filter keys" in create_resp.json()["detail"]
    assert "workspace_id" in create_resp.json()["detail"]


def test_pma_automation_timer_create_rejects_conflicting_filter_values(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def create_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"timer_id": "should-not-be-created", **payload}

    app.state.hub_supervisor.get_pma_automation_store = lambda: FakeAutomationStore()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_type": "one_shot",
                "delay_seconds": 30,
                "thread_id": "thread-top-level",
                "filter": {"thread_id": "thread-filter"},
            },
        )

    assert create_resp.status_code == 400
    assert "Conflicting values for thread_id" in create_resp.json()["detail"]


def test_pma_automation_timer_touch_rejects_unknown_keys(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def touch_timer(self, timer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {"timer_id": timer_id, "payload": dict(payload)}

    app.state.hub_supervisor.get_pma_automation_store = lambda: FakeAutomationStore()

    with TestClient(app) as client:
        response = client.post(
            "/hub/pma/timers/timer-1/touch",
            json={"reason": "heartbeat", "oops": True},
        )

    assert response.status_code == 400
    assert "Unsupported timer touch keys" in response.json()["detail"]


def test_pma_automation_timer_cancel_rejects_unknown_keys(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def cancel_timer(
            self, timer_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            return {"timer_id": timer_id, "payload": dict(payload)}

    app.state.hub_supervisor.get_pma_automation_store = lambda: FakeAutomationStore()

    with TestClient(app) as client:
        response = client.post(
            "/hub/pma/timers/timer-1/cancel",
            json={"reason": "done", "unexpected": "value"},
        )

    assert response.status_code == 400
    assert "Unsupported timer cancel keys" in response.json()["detail"]


def test_pma_automation_subscription_alias_endpoint_supports_kwargs_only_store(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.create_calls: list[dict[str, Any]] = []
            self.deleted_ids: list[str] = []

        def create_subscription(
            self, *, thread_id: Optional[str] = None, lane_id: Optional[str] = None
        ) -> dict[str, Any]:
            self.create_calls.append({"thread_id": thread_id, "lane_id": lane_id})
            return {
                "subscription_id": "sub-alias-1",
                "thread_id": thread_id,
                "lane_id": lane_id,
            }

        def cancel_subscription(self, subscription_id: str) -> bool:
            self.deleted_ids.append(subscription_id)
            return True

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/automation/subscriptions",
            json={"thread_id": "thread-alias", "lane_id": "pma:lane-alias"},
        )
        assert create_resp.status_code == 200
        subscription = create_resp.json()["subscription"]
        assert subscription["subscription_id"] == "sub-alias-1"
        assert subscription["thread_id"] == "thread-alias"
        assert subscription["lane_id"] == "pma:lane-alias"

        delete_resp = client.delete("/hub/pma/automation/subscriptions/sub-alias-1")
        assert delete_resp.status_code == 200
        delete_payload = delete_resp.json()
        assert delete_payload["status"] == "ok"
        assert delete_payload["subscription_id"] == "sub-alias-1"

    assert fake_store.create_calls == [
        {"thread_id": "thread-alias", "lane_id": "pma:lane-alias"}
    ]
    assert fake_store.deleted_ids == ["sub-alias-1"]


def test_pma_automation_timer_alias_endpoint_supports_fallback_method_signatures(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.create_calls: list[dict[str, Any]] = []
            self.cancelled_ids: list[str] = []

        def create_timer(
            self,
            *,
            timer_type: Optional[str] = None,
            idle_seconds: Optional[int] = None,
        ) -> dict[str, Any]:
            self.create_calls.append(
                {"timer_type": timer_type, "idle_seconds": idle_seconds}
            )
            return {
                "timer_id": "timer-alias-1",
                "timer_type": timer_type,
                "idle_seconds": idle_seconds,
            }

        def cancel_timer(self, timer_id: str) -> bool:
            self.cancelled_ids.append(timer_id)
            return True

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/automation/timers",
            json={"timer_type": "watchdog", "idle_seconds": 45},
        )
        assert create_resp.status_code == 200
        timer = create_resp.json()["timer"]
        assert timer["timer_id"] == "timer-alias-1"
        assert timer["timer_type"] == "watchdog"
        assert timer["idle_seconds"] == 45

        cancel_resp = client.delete("/hub/pma/automation/timers/timer-alias-1")
        assert cancel_resp.status_code == 200
        cancel_payload = cancel_resp.json()
        assert cancel_payload["status"] == "ok"
        assert cancel_payload["timer_id"] == "timer-alias-1"

    assert fake_store.create_calls == [{"timer_type": "watchdog", "idle_seconds": 45}]
    assert fake_store.cancelled_ids == ["timer-alias-1"]


def test_pma_automation_watchdog_timer_create(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.created_payloads: list[dict[str, Any]] = []

        def create_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.created_payloads.append(dict(payload))
            return {"timer_id": "watchdog-1", **payload}

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_type": "watchdog",
                "idle_seconds": 300,
                "thread_id": "thread-1",
                "reason": "watchdog_stalled",
            },
        )
        assert create_resp.status_code == 200
        payload = create_resp.json()["timer"]
        assert payload["timer_id"] == "watchdog-1"

    assert fake_store.created_payloads
    assert fake_store.created_payloads[0]["timer_type"] == "watchdog"
    assert fake_store.created_payloads[0]["idle_seconds"] == 300


def test_pma_automation_timer_rejects_invalid_due_at(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeAutomationStore:
        def __init__(self) -> None:
            self.created_payloads: list[dict[str, Any]] = []

        def create_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.created_payloads.append(dict(payload))
            return {"timer_id": "timer-1", **payload}

    fake_store = FakeAutomationStore()
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        response = client.post(
            "/hub/pma/timers",
            json={"timer_type": "one_shot", "due_at": "not-a-timestamp"},
        )
        assert response.status_code == 422

    assert fake_store.created_payloads == []


def test_pma_automation_timer_rejects_unknown_subscription_id(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        response = client.post(
            "/hub/pma/timers",
            json={
                "timer_type": "one_shot",
                "delay_seconds": 60,
                "subscription_id": "missing-sub",
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Unknown subscription_id: missing-sub"


def test_pma_orchestration_service_integration_for_thread_operations(
    hub_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_pma(hub_env.hub_root)

    class FakeService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_thread_target(self, thread_target_id: str):
            self.calls.append(f"get_thread_target:{thread_target_id}")
            return None

        def get_running_execution(self, thread_target_id: str):
            self.calls.append(f"get_running_execution:{thread_target_id}")
            return None

        def get_latest_execution(self, thread_target_id: str):
            self.calls.append(f"get_latest_execution:{thread_target_id}")
            return None

    fake_service = FakeService()
    monkeypatch.setattr(
        tail_stream,
        "build_managed_thread_orchestration_service",
        lambda request: fake_service,
    )
    app = create_hub_app(hub_env.hub_root)

    client = TestClient(app)

    client.get("/hub/pma/threads/thread-1/status")
    assert any(
        call.startswith("get_thread_target:thread-1") for call in fake_service.calls
    )

    fake_service.calls.clear()
    client.get("/hub/pma/threads/thread-1/tail")
    assert any(
        call.startswith("get_thread_target:thread-1") for call in fake_service.calls
    )
