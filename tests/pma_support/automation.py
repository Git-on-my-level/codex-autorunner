from typing import Any

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.core.automation import (
    EXECUTOR_PMA_OPERATOR_TURN,
    PMA_SUBSCRIPTION_RULE_PREFIX,
    PMA_TIMER_RULE_PREFIX,
    PMA_TIMER_SCHEDULE_PREFIX,
    AutomationSchedule,
    AutomationStore,
)
from codex_autorunner.core.pma_automation_store import PmaAutomationStore
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes.pma_routes import tail_stream
from tests.pma_support import _enable_pma

pytestmark = pytest.mark.slow


def test_pma_automation_subscription_endpoints(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/subscriptions",
            json={
                "event_types": ["managed_thread_completed"],
                "lane_id": "pma:lane-next",
                "from_state": "running",
                "to_state": "completed",
                "reason": "manual",
                "timestamp": "2026-03-01T12:00:00Z",
            },
        )
        assert create_resp.status_code == 200
        created = create_resp.json()["subscription"]
        subscription_id = created["subscription_id"]
        assert subscription_id
        assert created["lane_id"] == "pma:lane-next"

        list_resp = client.get(
            "/hub/pma/automation/subscriptions",
            params={"lane_id": "pma:lane-next", "limit": 5},
        )
        assert list_resp.status_code == 200
        listed = list_resp.json()["subscriptions"]
        assert any(row["subscription_id"] == subscription_id for row in listed)

        delete_resp = client.delete(f"/hub/pma/subscriptions/{subscription_id}")
        assert delete_resp.status_code == 200
        assert delete_resp.json()["status"] == "ok"
        assert delete_resp.json()["subscription_id"] == subscription_id

    rule = AutomationStore(hub_env.hub_root).get_rule(
        f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}"
    )
    assert rule is not None
    assert rule.enabled is False


@pytest.mark.parametrize(
    ("request_body", "expected_event_types"),
    [
        (
            {
                "event_type": "managed_thread_completed",
                "lane_id": "pma:event-alias-1",
            },
            ["lifecycle.flow_completed"],
        ),
        (
            {
                "event_types": [
                    "managed_thread_completed",
                    "managed_thread_failed",
                ],
                "lane_id": "pma:event-alias-2",
            },
            ["lifecycle.flow_completed", "lifecycle.flow_failed"],
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

    with TestClient(app) as client:
        create_resp = client.post("/hub/pma/subscriptions", json=request_body)

    assert create_resp.status_code == 200
    subscription = create_resp.json()["subscription"]
    assert subscription["event_types"] == expected_event_types
    assert subscription["lane_id"] == request_body["lane_id"]


def test_pma_automation_subscription_create_accepts_nested_filter_payload(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/subscriptions",
            json={
                "event_types": ["managed_thread_completed"],
                "filter": {"laneId": "pma:filter-1", "repoId": "repo-filter-1"},
            },
        )

    assert create_resp.status_code == 200
    subscription = create_resp.json()["subscription"]
    assert subscription["event_types"] == ["managed_thread_completed"]
    assert subscription["lane_id"] == "pma:filter-1"
    assert subscription["repo_id"] == "repo-filter-1"


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

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_id": "timer-1",
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

    schedule = AutomationStore(hub_env.hub_root).get_schedule(
        f"{PMA_TIMER_SCHEDULE_PREFIX}timer-1"
    )
    assert schedule is not None
    assert schedule.state == "cancelled"


def test_pma_automation_list_endpoints_include_unified_read_model(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        sub_resp = client.post(
            "/hub/pma/subscriptions",
            json={
                "event_types": ["flow_completed"],
                "repo_id": "repo-1",
                "run_id": "run-1",
                "lane_id": "pma:default",
                "idempotency_key": "route-sub-unified",
            },
        )
        assert sub_resp.status_code == 200

        timer_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_type": "one_shot",
                "due_at": "2026-01-01T00:00:00Z",
                "thread_id": "thread-1",
                "idempotency_key": "route-timer-unified",
            },
        )
        assert timer_resp.status_code == 200

        list_subs = client.get("/hub/pma/subscriptions").json()
        list_timers = client.get("/hub/pma/timers").json()

    sub_rules = list_subs["unified"]["rules"]
    timer_rules = list_timers["unified"]["rules"]
    timer_schedules = list_timers["unified"]["schedules"]
    assert any(
        rule["metadata"].get("purpose") == "managed_thread_lifecycle_subscription"
        for rule in sub_rules
    )
    assert any(
        rule["metadata"].get("purpose") == "managed_thread_timer"
        for rule in timer_rules
    )
    assert timer_schedules and timer_schedules[0]["schedule_kind"] == "one_shot"


def test_pma_automation_created_rules_are_visible_through_control_plane(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/subscriptions",
            json={
                "event_types": ["flow_completed"],
                "repo_id": "repo-control-plane",
                "run_id": "run-control-plane",
                "idempotency_key": "route-sub-control-plane",
            },
        )
        assert create_resp.status_code == 200
        rule_id = (
            f"{PMA_SUBSCRIPTION_RULE_PREFIX}"
            f"{create_resp.json()['subscription']['subscription_id']}"
        )

        list_resp = client.post(
            "/hub/api/control-plane/automations/rules/query",
            json={"enabled": True},
        )
        detail_resp = client.get(f"/hub/api/control-plane/automations/rules/{rule_id}")

    assert list_resp.status_code == 200
    assert any(rule["rule_id"] == rule_id for rule in list_resp.json()["rules"])
    assert detail_resp.status_code == 200
    assert detail_resp.json()["rule"]["metadata"]["purpose"] == (
        "managed_thread_lifecycle_subscription"
    )


def test_pma_automation_real_create_routes_write_unified_records_only(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        sub_resp = client.post(
            "/hub/pma/subscriptions",
            json={
                "event_types": ["flow_completed"],
                "repo_id": "repo-unified-create",
                "run_id": "run-unified-create",
                "idempotency_key": "route-sub-unified-only",
            },
        )
        assert sub_resp.status_code == 200
        subscription_id = sub_resp.json()["subscription"]["subscription_id"]

        timer_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_type": "one_shot",
                "due_at": "2026-01-01T00:00:00Z",
                "subscription_id": subscription_id,
                "thread_id": "thread-unified-create",
                "idempotency_key": "route-timer-unified-only",
            },
        )
        assert timer_resp.status_code == 200
        timer_id = timer_resp.json()["timer"]["timer_id"]

        touch_resp = client.post(
            f"/hub/pma/timers/{timer_id}/touch",
            json={"due_at": "2026-01-01T01:00:00Z", "reason": "heartbeat"},
        )
        assert touch_resp.status_code == 200
        assert touch_resp.json()["touched"] is True

    automation_store = AutomationStore(hub_env.hub_root)
    assert (
        automation_store.get_rule(f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}")
        is not None
    )
    schedule = automation_store.get_schedule(f"{PMA_TIMER_SCHEDULE_PREFIX}{timer_id}")
    assert schedule is not None
    assert schedule.next_fire_at == "2026-01-01T01:00:00Z"
    assert (
        PmaAutomationStore(hub_env.hub_root).list_subscriptions(include_inactive=True)
        == []
    )
    assert PmaAutomationStore(hub_env.hub_root).list_timers(include_inactive=True) == []


def test_pma_automation_cancel_subscription_can_disable_unified_only_rule(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    automation_store = AutomationStore(hub_env.hub_root)
    subscription_id = "unified-sub-only-1"
    rule_id = f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}"
    automation_store.create_rule(
        rule_id=rule_id,
        name="Unified-only PMA subscription",
        enabled=True,
        system_owned=True,
        trigger_kind="event",
        trigger={"event_types": ["lifecycle.flow_completed"]},
        target_policy="hub",
        target={"thread_id": "thread-unified"},
        executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
        executor={"lane_id": "pma:default"},
        metadata={
            "purpose": "pma_lifecycle_subscription",
            "subscription_id": subscription_id,
        },
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        response = client.delete(f"/hub/pma/subscriptions/{subscription_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] is True
    assert automation_store.get_rule(rule_id).enabled is False


def test_pma_automation_cancel_subscription_leaves_legacy_row_read_only(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    legacy_store = PmaAutomationStore(hub_env.hub_root)
    created = legacy_store.create_subscription(
        {
            "event_types": ["flow_completed"],
            "repo_id": "repo-legacy-cancel",
            "idempotency_key": "legacy-sub-cancel-read-only",
        }
    )
    subscription_id = created["subscription"]["subscription_id"]
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        response = client.delete(f"/hub/pma/subscriptions/{subscription_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] is True
    assert payload["unified_deleted"] is True
    assert payload["legacy_deleted"] is False
    legacy_rows = PmaAutomationStore(hub_env.hub_root).list_subscriptions(
        include_inactive=True
    )
    assert {row["subscription_id"]: row["state"] for row in legacy_rows} == {
        subscription_id: "active"
    }
    rule = AutomationStore(hub_env.hub_root).get_rule(
        f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}"
    )
    assert rule is not None
    assert rule.enabled is False


def test_pma_automation_cancel_timer_can_cancel_unified_only_schedule(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    automation_store = AutomationStore(hub_env.hub_root)
    timer_id = "unified-timer-only-1"
    rule_id = f"{PMA_TIMER_RULE_PREFIX}{timer_id}"
    schedule_id = f"{PMA_TIMER_SCHEDULE_PREFIX}{timer_id}"
    automation_store.create_rule(
        rule_id=rule_id,
        name="Unified-only PMA timer",
        enabled=True,
        system_owned=True,
        trigger_kind="event",
        trigger={"event_types": ["schedule.fire"]},
        target_policy="hub",
        target={"thread_id": "thread-unified"},
        executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
        executor={"lane_id": "pma:default", "wake_up_kind": "pma_timer"},
        metadata={"purpose": "pma_timer", "timer_id": timer_id},
    )
    automation_store.upsert_schedule(
        AutomationSchedule.create(
            schedule_id=schedule_id,
            rule_id=rule_id,
            schedule_kind="one_shot",
            next_fire_at="2026-01-01T00:00:00Z",
            schedule={
                "timer_id": timer_id,
                "payload": {"timer_id": timer_id, "thread_id": "thread-unified"},
            },
            state="active",
        )
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        response = client.delete(f"/hub/pma/timers/{timer_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cancelled"] is True
    assert automation_store.get_rule(rule_id).enabled is False
    schedule = automation_store.get_schedule(schedule_id)
    assert schedule.state == "cancelled"
    assert schedule.next_fire_at is None


def test_pma_automation_cancel_timer_leaves_legacy_row_read_only(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    legacy_store = PmaAutomationStore(hub_env.hub_root)
    created = legacy_store.create_timer(
        {
            "timer_type": "one_shot",
            "due_at": "2026-01-01T00:00:00Z",
            "thread_id": "thread-legacy-cancel",
            "idempotency_key": "legacy-timer-cancel-read-only",
        }
    )
    timer_id = created["timer"]["timer_id"]
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        response = client.delete(f"/hub/pma/timers/{timer_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cancelled"] is True
    assert payload["unified_deleted"] is True
    assert payload["legacy_deleted"] is False
    legacy_rows = PmaAutomationStore(hub_env.hub_root).list_timers(
        include_inactive=True
    )
    assert {row["timer_id"]: row["state"] for row in legacy_rows} == {
        timer_id: "pending"
    }
    schedule = AutomationStore(hub_env.hub_root).get_schedule(
        f"{PMA_TIMER_SCHEDULE_PREFIX}{timer_id}"
    )
    assert schedule is not None
    assert schedule.state == "cancelled"
    assert schedule.next_fire_at is None


def test_pma_automation_timer_create_accepts_nested_filter_payload(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_id": "timer-filter-1",
                "timer_type": "one_shot",
                "delay_seconds": 30,
                "filter": {"threadId": "thread-filter-1", "repo_id": "repo-filter-1"},
            },
        )

    assert create_resp.status_code == 200
    timer = create_resp.json()["timer"]
    assert timer["timer_id"] == "timer-filter-1"
    assert timer["timer_type"] == "one_shot"
    assert timer["thread_id"] == "thread-filter-1"
    assert timer["repo_id"] == "repo-filter-1"


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

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/automation/subscriptions",
            json={
                "event_types": ["managed_thread_completed"],
                "lane_id": "pma:lane-alias",
            },
        )
        assert create_resp.status_code == 200
        subscription = create_resp.json()["subscription"]
        subscription_id = subscription["subscription_id"]
        assert subscription_id
        assert subscription["lane_id"] == "pma:lane-alias"

        delete_resp = client.delete(
            f"/hub/pma/automation/subscriptions/{subscription_id}"
        )
        assert delete_resp.status_code == 200
        delete_payload = delete_resp.json()
        assert delete_payload["status"] == "ok"
        assert delete_payload["subscription_id"] == subscription_id


def test_pma_automation_timer_alias_endpoint_supports_fallback_method_signatures(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/automation/timers",
            json={
                "timer_id": "timer-alias-1",
                "timer_type": "watchdog",
                "idle_seconds": 45,
            },
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


def test_pma_automation_watchdog_timer_create(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/timers",
            json={
                "timer_id": "watchdog-1",
                "timer_type": "watchdog",
                "idle_seconds": 300,
                "thread_id": "thread-1",
                "reason": "watchdog_stalled",
            },
        )
        assert create_resp.status_code == 200
        payload = create_resp.json()["timer"]
        assert payload["timer_id"] == "watchdog-1"
        assert payload["timer_type"] == "watchdog"
        assert payload["idle_seconds"] == 300


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
