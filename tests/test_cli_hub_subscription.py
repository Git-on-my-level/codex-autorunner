import json

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.automation import (
    EXECUTOR_MANAGED_THREAD_TURN,
    PMA_SUBSCRIPTION_RULE_PREFIX,
    AutomationRule,
    AutomationStore,
)
from codex_autorunner.core.automation.models import (
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
)

runner = CliRunner()


def _seed_subscription(
    hub_root,
    *,
    thread_id: str,
    lane_id: str,
    event_types: list[str],
) -> dict:
    store = AutomationStore(hub_root)
    subscription_id = f"sub-{thread_id}"
    rule = store.upsert_rule(
        AutomationRule.create(
            rule_id=f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}",
            name=f"Subscription {thread_id}",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"kind": "lifecycle_event", "event_types": event_types},
            target_policy=TARGET_POLICY_HUB,
            target={"thread_id": thread_id},
            executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
            executor={"lane_id": lane_id, "message_text": "Follow up"},
            metadata={
                "purpose": "managed_thread_lifecycle_subscription",
                "legacy_subscription_id": subscription_id,
            },
        )
    )
    return {
        "subscription_id": subscription_id,
        "rule_id": rule.rule_id,
        "thread_id": thread_id,
        "lane_id": lane_id,
        "event_types": event_types,
    }


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
    store = AutomationStore(hub_env.hub_root)
    assert store.set_rule_enabled(cancelled["rule_id"], False) is not None

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
    assert "Cancelled" in result.output
    assert subscription["subscription_id"][:8] in result.output

    store = AutomationStore(hub_env.hub_root)
    cancelled = store.get_rule(subscription["rule_id"])
    assert cancelled is not None
    assert cancelled.enabled is False


def test_hub_subscription_purge_previews_disabled_rules_without_deleting(
    hub_env,
) -> None:
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
    store = AutomationStore(hub_env.hub_root)
    assert store.set_rule_enabled(cancelled["rule_id"], False) is not None

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
    assert store.get_rule(active["rule_id"]) is not None
    assert store.get_rule(cancelled["rule_id"]) is not None

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

    assert apply_result.exit_code != 0, apply_result.output
    assert "disabled generalized rules" in apply_result.output
    assert store.get_rule(active["rule_id"]) is not None
    assert store.get_rule(cancelled["rule_id"]) is not None
