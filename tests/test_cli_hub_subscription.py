import json

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.pma_automation_store import PmaAutomationStore

runner = CliRunner()


def _seed_subscription(
    hub_root,
    *,
    thread_id: str,
    lane_id: str,
    event_types: list[str],
) -> dict:
    store = PmaAutomationStore(hub_root)
    return store.create_subscription(
        {
            "thread_id": thread_id,
            "lane_id": lane_id,
            "event_types": event_types,
        }
    )["subscription"]


def test_hub_subscription_list_renders_table_and_state_filter(hub_env) -> None:
    active = _seed_subscription(
        hub_env.hub_root,
        thread_id="thread-active",
        lane_id="pma:lane-active",
        event_types=["completed"],
    )
    cancelled = _seed_subscription(
        hub_env.hub_root,
        thread_id="thread-cancelled",
        lane_id="pma:lane-cancelled",
        event_types=["failed"],
    )
    store = PmaAutomationStore(hub_env.hub_root)
    assert store.cancel_subscription(cancelled["subscription_id"]) is True

    result = runner.invoke(
        app,
        ["hub", "subscription", "list", "--path", str(hub_env.hub_root)],
    )

    assert result.exit_code == 0, result.output
    assert "Subscriptions (2) state=all" in result.output
    assert "ID" in result.output
    assert "STATE" in result.output
    assert active["subscription_id"] in result.output
    assert cancelled["subscription_id"] in result.output
    assert "thread-active" in result.output
    assert "thread-cancelled" in result.output

    filtered = runner.invoke(
        app,
        [
            "hub",
            "subscription",
            "list",
            "--path",
            str(hub_env.hub_root),
            "--state",
            "active",
        ],
    )

    assert filtered.exit_code == 0, filtered.output
    assert "Subscriptions (1) state=active" in filtered.output
    assert active["subscription_id"] in filtered.output
    assert cancelled["subscription_id"] not in filtered.output


def test_hub_subscription_cancel_marks_subscription_cancelled(hub_env) -> None:
    subscription = _seed_subscription(
        hub_env.hub_root,
        thread_id="thread-cancel-me",
        lane_id="pma:lane-cancel-me",
        event_types=["completed"],
    )

    result = runner.invoke(
        app,
        [
            "hub",
            "subscription",
            "cancel",
            "--path",
            str(hub_env.hub_root),
            "--id",
            subscription["subscription_id"],
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"Cancelled subscription {subscription['subscription_id']}" in result.output

    store = PmaAutomationStore(hub_env.hub_root)
    subscriptions = store.list_subscriptions(include_inactive=True)
    cancelled = next(
        entry
        for entry in subscriptions
        if entry["subscription_id"] == subscription["subscription_id"]
    )
    assert cancelled["state"] == "cancelled"


def test_hub_subscription_purge_supports_dry_run_and_apply(hub_env) -> None:
    active = _seed_subscription(
        hub_env.hub_root,
        thread_id="thread-keep",
        lane_id="pma:lane-keep",
        event_types=["completed"],
    )
    cancelled = _seed_subscription(
        hub_env.hub_root,
        thread_id="thread-purge",
        lane_id="pma:lane-purge",
        event_types=["failed"],
    )
    store = PmaAutomationStore(hub_env.hub_root)
    store.create_timer(
        {
            "subscription_id": cancelled["subscription_id"],
            "thread_id": "thread-purge",
            "reason": "subscription-timer",
        }
    )
    store.enqueue_wakeup(
        source="lifecycle_subscription",
        subscription_id=cancelled["subscription_id"],
        thread_id="thread-purge",
        reason="subscription-wakeup",
        idempotency_key="purge-wakeup-1",
    )
    assert store.cancel_subscription(cancelled["subscription_id"]) is True

    dry_run = runner.invoke(
        app,
        [
            "hub",
            "subscription",
            "purge",
            "--path",
            str(hub_env.hub_root),
            "--state",
            "cancelled",
            "--dry-run",
            "--json",
        ],
    )

    assert dry_run.exit_code == 0, dry_run.output
    dry_run_payload = json.loads(dry_run.stdout)
    assert dry_run_payload["dry_run"] is True
    assert dry_run_payload["count"] == 1
    assert dry_run_payload["subscription_ids"] == [cancelled["subscription_id"]]
    remaining_after_dry_run = store.list_subscriptions(include_inactive=True)
    assert len(remaining_after_dry_run) == 2

    apply_result = runner.invoke(
        app,
        [
            "hub",
            "subscription",
            "purge",
            "--path",
            str(hub_env.hub_root),
            "--state",
            "cancelled",
            "--json",
        ],
    )

    assert apply_result.exit_code == 0, apply_result.output
    apply_payload = json.loads(apply_result.stdout)
    assert apply_payload["dry_run"] is False
    assert apply_payload["count"] == 1
    assert apply_payload["subscription_ids"] == [cancelled["subscription_id"]]

    remaining = store.list_subscriptions(include_inactive=True)
    assert [entry["subscription_id"] for entry in remaining] == [
        active["subscription_id"]
    ]
    assert store.list_timers(include_inactive=True) == []
    assert store.list_wakeups() == []
