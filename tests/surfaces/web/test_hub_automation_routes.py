import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.automation import (
    AutomationRule,
    AutomationSchedule,
    AutomationStore,
)
from codex_autorunner.core.automation.models import (
    EXECUTOR_MANAGED_THREAD_TURN,
    EXECUTOR_PUBLISH_OPERATION,
    JOB_FAILED,
    JOB_SUCCEEDED,
    SCHEDULE_ONE_SHOT,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_SCHEDULE,
)
from codex_autorunner.core.automation.product import automation_overview
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes.automations import _automation_target_options


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
    assert automation["executor_kind"] == "managed_thread_turn"
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
    assert automation["message_source"] == "executor.message_text"
    assert "Run a security scan" in automation["message_preview"]
    assert automation["target_summary"]["repo_id"] == "repo-1"
    assert automation["executor_summary"]["kind"] == "managed_thread_turn"
    assert automation["raw_links"]["control_plane_rule"].endswith(automation["id"])

    listing = client.get("/hub/automations")
    assert listing.status_code == 200
    assert listing.json()["summary"]["paused"] >= 1
    presets = {preset["id"]: preset for preset in listing.json()["presets"]}
    security_preset = presets["security_scan_pr"]
    assert security_preset["schedule"]["kind"] == "daily"
    assert security_preset["executor_kind"] == "managed_thread_turn"
    assert security_preset["target_policy"] == "hub"
    assert security_preset["policy"]["max_attempts"] == 3
    assert "{repo_id}" in security_preset["prompt_template"]


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

    listing = client.get("/hub/automations")
    presets = {preset["id"]: preset for preset in listing.json()["presets"]}
    weekly_preset = presets["weekly_ticket_flow"]
    assert weekly_preset["schedule"] == {
        "kind": "weekly",
        "timezone": "UTC",
        "hour": 10,
        "minute": 0,
        "weekday": 0,
    }
    assert weekly_preset["executor_kind"] == "ticket_flow"
    assert weekly_preset["target_policy"] == "new_automation_worktree"
    assert "{ticket_id}" in weekly_preset["ticket_body_template"]


def test_hub_automation_workspace_batches_jobs_and_target_options(
    tmp_path, monkeypatch
):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    store = AutomationStore(hub_root)
    rule_ids: list[str] = []
    for index in range(2):
        rule = store.upsert_rule(
            AutomationRule.create(
                rule_id=f"rule-{index}",
                name=f"Rule {index}",
                enabled=index == 0,
                trigger_kind=TRIGGER_KIND_SCHEDULE,
                trigger={"event_types": ["schedule.fire"]},
                target_policy=TARGET_POLICY_HUB,
                target={"repo_id": f"repo-{index}"},
                executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
                executor={"message": f"Run rule {index}"},
            )
        )
        rule_ids.append(rule.rule_id)
        store.upsert_schedule(
            AutomationSchedule.create(
                schedule_id=f"schedule-{index}",
                rule_id=rule.rule_id,
                schedule_kind="daily",
                next_fire_at=f"2026-01-0{index + 1}T00:00:00Z",
                schedule={"hour": index, "minute": 0},
            )
        )
        event = store.create_event(
            event_id=f"event-{index}",
            event_type="schedule.fire",
            source="test",
            target={"rule_id": rule.rule_id},
            payload={},
        )
        job_total = 30 if index == 0 else 3
        for job_index in range(job_total):
            store.create_job(
                job_id=f"job-{index}-{job_index:02d}",
                rule_id=rule.rule_id,
                event_id=event.event_id,
                state=(
                    JOB_FAILED
                    if index == 0 and job_index == job_total - 1
                    else JOB_SUCCEEDED
                ),
                dedupe_key=f"dedupe-{index}-{job_index}",
                target=rule.target,
                executor=rule.executor,
                created_at=f"2026-01-01T00:{job_index:02d}:00Z",
            )

    def fail_per_rule_query(*args, **kwargs):
        raise AssertionError("per-rule automation overview query was called")

    monkeypatch.setattr(store, "list_schedules", fail_per_rule_query)
    monkeypatch.setattr(store, "list_jobs", fail_per_rule_query)
    monkeypatch.setattr(store, "count_jobs", fail_per_rule_query)

    overview = automation_overview(store)

    rows = {row["id"]: row for row in overview["automations"]}
    assert overview["summary"] == {
        "total": 2,
        "active": 1,
        "paused": 1,
        "failed_jobs": 1,
    }
    assert rows["rule-0"]["job_count"] == 30
    assert rows["rule-1"]["job_count"] == 3
    assert len(rows["rule-0"]["jobs"]) == 25
    assert rows["rule-0"]["last_job"]["state"] == "failed"
    assert rows["rule-1"]["schedule"]["schedule_id"] == "schedule-1"

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/read-models/automations/workspace")

    assert response.status_code == 200
    workspace = response.json()
    workspace_rows = {row["id"]: row for row in workspace["automations"]}
    assert workspace["summary"]["failed_jobs"] == 1
    assert len(workspace_rows["rule-0"]["jobs"]) == 25
    assert "executor" in workspace_rows["rule-0"]
    assert "target_options" in workspace
    assert set(workspace["agent_defaults"]) == {
        "default_agent",
        "default_profile",
        "default_model",
        "default_reasoning",
    }
    assert workspace["generated_at"]
    index = client.get("/hub/read-models/automations/workspace-index")
    assert index.status_code == 200
    index_workspace = index.json()
    index_rows = {row["id"]: row for row in index_workspace["automations"]}
    assert len(index_rows["rule-0"]["jobs"]) == 0
    assert "executor" not in index_rows["rule-0"]
    assert "target_options" not in index_workspace
    assert (
        client.get(
            "/hub/read-models/automations/workspace-index?include_target_options=true"
        ).json()["target_options"]
        is not None
    )
    assert client.get("/hub/read-models/automations/target-options").status_code == 200

    context = SimpleNamespace(
        supervisor=SimpleNamespace(
            list_repos=lambda use_cache=True: [
                SimpleNamespace(
                    id="repo-1",
                    display_name="Repo One",
                    kind="repo",
                    exists_on_disk=True,
                    initialized=True,
                ),
                SimpleNamespace(
                    id="repo-1--feature",
                    display_name="Feature worktree",
                    kind="worktree",
                    exists_on_disk=False,
                    initialized=True,
                ),
            ]
        )
    )
    target_options = _automation_target_options(context)
    assert target_options == [
        {"id": "repo-1", "label": "Repo One", "kind": "repo"},
        {
            "id": "repo-1--feature",
            "label": "Feature worktree",
            "kind": "worktree",
            "disabled": True,
        },
    ]


def test_hub_automation_workspace_tolerates_unknown_executor_kind(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    store = AutomationStore(hub_root)
    store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-future",
            name="Future executor",
            enabled=True,
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_HUB,
            executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
            executor={"message": "Known before upgrade"},
        )
    )
    with open_orchestration_sqlite(hub_root) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_automation_rules
                   SET executor_kind = ?,
                       executor_json = ?
                 WHERE rule_id = ?
                """,
                (
                    "agent_task_turn",
                    json.dumps({"kind": "agent_task_turn", "prompt": "Future task"}),
                    "rule-future",
                ),
            )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/read-models/automations/workspace")
    run = client.post("/hub/automations/rule-future/run")

    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["automations"]}
    row = rows["rule-future"]
    assert row["executor_kind"] == "agent_task_turn"
    assert row["executor"]["kind"] == "agent_task_turn"
    assert row["known_executor"] is False
    assert row["executable"] is False
    assert row["editable"]["can_run_now"] is False
    assert row["executor_summary"]["known_executor"] is False
    assert {item["code"] for item in row["diagnostics"]} >= {
        "AUTOMATION_EXECUTOR_KIND_UNSUPPORTED"
    }
    assert run.status_code == 400
    assert "AUTOMATION_EXECUTOR_KIND_UNSUPPORTED" in run.json()["detail"]


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


def test_hub_automations_delete_requires_paused(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))
    created = client.post(
        "/hub/automations",
        json={"preset": "security_scan_pr", "repo_id": "repo-1"},
    )
    rule_id = created.json()["automation"]["id"]

    # Created as paused by default — delete should succeed.
    missing = client.delete("/hub/automations/does-not-exist")
    assert missing.status_code == 404

    client.post(f"/hub/automations/{rule_id}/enabled", json={"enabled": True})
    blocked = client.delete(f"/hub/automations/{rule_id}")
    assert blocked.status_code == 400
    assert blocked.json()["detail"]["code"] == "AUTOMATION_DELETE_REQUIRES_PAUSED"

    client.post(f"/hub/automations/{rule_id}/enabled", json={"enabled": False})
    deleted = client.delete(f"/hub/automations/{rule_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == rule_id

    # Subsequent get should 404.
    after = client.get(f"/hub/automations/{rule_id}")
    assert after.status_code == 404


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
            executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
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
            executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
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
