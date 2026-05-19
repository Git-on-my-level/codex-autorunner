from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.automation import (
    AutomationRule,
    AutomationSchedule,
    AutomationStore,
)
from codex_autorunner.core.automation.models import (
    EXECUTOR_PMA_TURN,
    EXECUTOR_PUBLISH_OPERATION,
    SCHEDULE_ONE_SHOT,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_SCHEDULE,
)
from codex_autorunner.server import create_hub_app


def test_hub_automations_create_security_scan_saved_disabled(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    response = client.post(
        "/hub/automations",
        json={
            "preset": "security_scan_pr",
            "repo_id": "repo-1",
            "timezone": "UTC",
            "hour": 8,
            "agent": "codex",
            "model": "gpt-5.4",
            "reasoning": "medium",
        },
    )

    assert response.status_code == 200
    automation = response.json()["automation"]
    assert automation["kind"] == "security_scan_pr"
    assert automation["executor_kind"] == "pma_turn"
    assert automation["enabled"] is False
    assert automation["schedule"]["schedule_kind"] == "daily"
    assert automation["target"]["repo_id"] == "repo-1"
    assert automation["executor"]["agent"] == "codex"
    assert automation["executor"]["model"] == "gpt-5.4"
    assert automation["executor"]["reasoning"] == "medium"
    assert automation["product_api_version"] == 1
    assert automation["schedule_editor"] == {
        "kind": "daily",
        "editable": True,
        "fields": {"timezone": "UTC", "hour": 8, "minute": 0},
        "timezone": "UTC",
        "next_fire_at": automation["schedule"]["next_fire_at"],
        "last_fire_at": None,
        "state": "active",
        "summary": automation["schedule_editor"]["summary"],
    }
    assert automation["message_source"] == "executor.message"
    assert "Run a security scan" in automation["message_preview"]
    assert automation["target_summary"]["repo_id"] == "repo-1"
    assert automation["executor_summary"]["kind"] == "pma_turn"
    assert automation["raw_links"]["control_plane_rule"].endswith(automation["id"])

    listing = client.get("/hub/automations")
    assert listing.status_code == 200
    assert listing.json()["summary"]["paused"] >= 1


def test_hub_automations_create_weekly_ticket_flow_and_run_now(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    created = client.post(
        "/hub/automations",
        json={
            "preset": "weekly_ticket_flow",
            "repo_id": "repo-1",
            "timezone": "UTC",
            "weekday": 0,
            "hour": 10,
            "ticket_body": "Run the customized weekly maintenance ticket.",
        },
    )

    assert created.status_code == 200
    automation = created.json()["automation"]
    assert automation["executor_kind"] == "ticket_flow"
    assert automation["enabled"] is False
    assert automation["target_policy"] == "new_automation_worktree"
    assert (
        automation["executor"]["ticket_pack"]["tickets"][0]["content"]
        == "Run the customized weekly maintenance ticket."
    )
    rule_id = automation["id"]

    run = client.post(f"/hub/automations/{rule_id}/run")
    assert run.status_code == 200
    assert run.json()["jobs_created"] == 1
    second_run = client.post(f"/hub/automations/{rule_id}/run")
    assert second_run.status_code == 200
    assert second_run.json()["jobs_created"] == 1

    jobs = AutomationStore(hub_root).list_jobs(rule_id=rule_id)
    assert len(jobs) == 2
    assert jobs[0].executor["kind"] == "ticket_flow"
    assert jobs[0].dedupe_key != jobs[1].dedupe_key

    for _ in range(24):
        extra_run = client.post(f"/hub/automations/{rule_id}/run")
        assert extra_run.status_code == 200
        assert extra_run.json()["jobs_created"] == 1

    detail = client.get(f"/hub/automations/{rule_id}")
    assert detail.status_code == 200
    assert detail.json()["automation"]["job_count"] == 26
    job_rows = detail.json()["automation"]["jobs"]
    assert len(job_rows) == 25
    assert detail.json()["automation"]["last_job"]["job_id"] == job_rows[0]["job_id"]
    assert "executor" not in job_rows[0]
    assert "payload" not in job_rows[0]


def test_hub_automations_pause_and_resume(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/automations",
        json={"preset": "security_scan_pr", "repo_id": "repo-1"},
    )
    rule_id = created.json()["automation"]["id"]

    paused = client.post(f"/hub/automations/{rule_id}/enabled", json={"enabled": False})
    resumed = client.post(f"/hub/automations/{rule_id}/enabled", json={"enabled": True})

    assert paused.status_code == 200
    assert paused.json()["automation"]["enabled"] is False
    assert resumed.status_code == 200
    assert resumed.json()["automation"]["enabled"] is True


def test_hub_automations_get_and_update_details(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/automations",
        json={"preset": "security_scan_pr", "repo_id": "repo-1", "hour": 9},
    )
    rule_id = created.json()["automation"]["id"]

    detail = client.get(f"/hub/automations/{rule_id}")
    updated = client.patch(
        f"/hub/automations/{rule_id}",
        json={
            "name": "Morning scan",
            "prompt": "Run the focused morning security check.",
            "timezone": "UTC",
            "hour": 6,
            "minute": 30,
            "agent": "opencode",
            "model": "claude-sonnet-4",
            "reasoning": "high",
            "metadata": {"description": "Updated from detail view"},
        },
    )

    assert detail.status_code == 200
    assert detail.json()["automation"]["id"] == rule_id
    assert updated.status_code == 200
    automation = updated.json()["automation"]
    assert automation["name"] == "Morning scan"
    assert (
        automation["executor"]["message"] == "Run the focused morning security check."
    )
    assert automation["executor"]["agent"] == "opencode"
    assert automation["executor"]["model"] == "claude-sonnet-4"
    assert automation["executor"]["reasoning"] == "high"
    assert automation["metadata"]["description"] == "Updated from detail view"
    assert automation["schedule"]["schedule"]["hour"] == 6
    assert automation["schedule"]["schedule"]["minute"] == 30
    assert automation["schedule_editor"]["fields"] == {
        "timezone": "UTC",
        "hour": 6,
        "minute": 30,
    }


def test_hub_automations_update_ticket_body(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/automations",
        json={"preset": "weekly_ticket_flow", "repo_id": "repo-1"},
    )
    rule_id = created.json()["automation"]["id"]

    response = client.patch(
        f"/hub/automations/{rule_id}",
        json={"ticket_body": "Run the updated weekly maintenance ticket."},
    )

    assert response.status_code == 200
    tickets = response.json()["automation"]["executor"]["ticket_pack"]["tickets"]
    assert tickets[0]["content"] == "Run the updated weekly maintenance ticket."


def test_hub_automations_reject_raw_product_updates(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/automations",
        json={"preset": "security_scan_pr", "repo_id": "repo-1"},
    )
    rule_id = created.json()["automation"]["id"]

    response = client.patch(
        f"/hub/automations/{rule_id}",
        json={"executor": {"message": "raw edit"}, "policy": {"max_attempts": 1}},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "AUTOMATION_PRODUCT_UNSUPPORTED_RAW_FIELDS"
    assert detail["fields"] == ["executor", "policy"]


def test_hub_automations_project_pma_one_shot_timer(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    store = AutomationStore(hub_root)
    rule = store.upsert_rule(
        AutomationRule.create(
            rule_id="builtin:pma:timer:timer-1",
            name="PMA timer timer-1",
            enabled=True,
            system_owned=True,
            trigger_kind=TRIGGER_KIND_SCHEDULE,
            trigger={"schedule_kind": SCHEDULE_ONE_SHOT},
            target_policy=TARGET_POLICY_HUB,
            target={"repo_id": "repo-1", "thread_target_id": "thread-1"},
            executor_kind=EXECUTOR_PMA_TURN,
            executor={
                "lane_id": "pma:default",
                "source": "timer",
                "wake_up_kind": "pma_timer",
                "message": "Automation wake-up received.\nsource: timer\ntimer_id: timer-1",
            },
            metadata={
                "builtin": True,
                "purpose": "pma_timer",
                "legacy_timer_id": "timer-1",
            },
        )
    )
    store.upsert_schedule(
        AutomationSchedule.create(
            schedule_id="pma-timer:timer-1",
            rule_id=rule.rule_id,
            schedule_kind=SCHEDULE_ONE_SHOT,
            next_fire_at="2026-01-02T00:00:00Z",
            schedule={"legacy_timer_id": "timer-1", "timer_kind": "one_shot"},
        )
    )
    client = TestClient(create_hub_app(hub_root))

    response = client.get(f"/hub/automations/{rule.rule_id}")

    assert response.status_code == 200
    automation = response.json()["automation"]
    assert automation["schedule_editor"]["kind"] == "one_shot"
    assert automation["schedule_editor"]["fields"] == {"due_at": "2026-01-02T00:00:00Z"}
    assert automation["editable"]["can_edit_schedule"] is False
    assert automation["managed"]["managed"] is True
    assert automation["managed"]["legacy"] is True
    assert {item["code"] for item in automation["diagnostics"]} >= {
        "AUTOMATION_LEGACY_MIGRATED",
        "AUTOMATION_ONE_SHOT_SCHEDULE",
    }


def test_hub_automations_project_pma_lifecycle_reaction(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    store = AutomationStore(hub_root)
    rule = store.upsert_rule(
        AutomationRule.create(
            rule_id="builtin:pma:reactive-lifecycle:test",
            name="Built-in PMA lifecycle reaction",
            enabled=True,
            system_owned=True,
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["lifecycle.flow_failed"]},
            filters={"event.payload.pma_dispatch_action": {"not_in": ["ignore"]}},
            target_policy=TARGET_POLICY_HUB,
            target={"repo_id": "{{ event.repo_id }}"},
            executor_kind=EXECUTOR_PMA_TURN,
            executor={
                "lane_id": "pma:default",
                "message": "Lifecycle event received.\ntype: {{ event.event_type }}",
            },
            metadata={"builtin": True, "purpose": "pma_reactive_lifecycle"},
        )
    )
    client = TestClient(create_hub_app(hub_root))

    response = client.get(f"/hub/automations/{rule.rule_id}")

    assert response.status_code == 200
    automation = response.json()["automation"]
    assert automation["trigger_summary"]["event_types"] == ["lifecycle.flow_failed"]
    assert automation["message_source"] == "executor.message"
    assert automation["message"]["template"] is True
    assert automation["managed"]["reason"] == (
        "System-managed automation: pma_reactive_lifecycle"
    )


def test_hub_automations_project_github_scm_reaction_actions(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    store = AutomationStore(hub_root)
    rule = store.upsert_rule(
        AutomationRule.create(
            rule_id="builtin:github:scm-reactions",
            name="Built-in GitHub SCM reactions",
            enabled=True,
            system_owned=True,
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["scm.github.pull_request_review.submitted"]},
            filters={"event.payload.review_state": "changes_requested"},
            target_policy=TARGET_POLICY_HUB,
            target={"repo_id": "{{ event.repo_id }}"},
            executor_kind=EXECUTOR_PUBLISH_OPERATION,
            executor={
                "operation_kind": "enqueue_managed_turn",
                "actions": {
                    "source": "automation_event.payload.actions",
                    "kind": "scm_reaction",
                    "message": {
                        "template": (
                            "SCM review requires follow-up for PR "
                            "{{ event.payload.pr_number }}."
                        )
                    },
                },
            },
            metadata={"builtin": True, "purpose": "github_scm_reaction"},
        )
    )
    client = TestClient(create_hub_app(hub_root))

    response = client.get(f"/hub/automations/{rule.rule_id}")

    assert response.status_code == 200
    automation = response.json()["automation"]
    assert automation["executor_summary"]["kind"] == "publish_operation"
    assert automation["action_preview"]["source"] == "automation_event.payload.actions"
    assert automation["action_preview"]["kind"] == "scm_reaction"
    assert automation["message_source"] == "executor.actions.message.template"
    assert "SCM review requires follow-up" in automation["message_preview"]
