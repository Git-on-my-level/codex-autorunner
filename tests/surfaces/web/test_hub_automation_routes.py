import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.automation import (
    AutomationChildExecutionEdge,
    AutomationEvent,
    AutomationJob,
    AutomationRule,
    AutomationSchedule,
    AutomationStore,
)
from codex_autorunner.core.automation.models import (
    EXECUTOR_AGENT_TASK_TURN,
    EXECUTOR_PMA_OPERATOR_TURN,
    EXECUTOR_PUBLISH_OPERATION,
    JOB_FAILED,
    JOB_SUCCEEDED,
    LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
    SCHEDULE_ONE_SHOT,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_SCHEDULE,
)
from codex_autorunner.core.automation.product import automation_overview
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.orchestration.turn_execution_contract import (
    TurnExecutionOrigin,
    TurnExecutionRequest,
)
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes.automations import _automation_target_options
from codex_autorunner.surfaces.web.services.pma.managed_thread_send_runtime import (
    _prepare_automation_child_for_managed_send,
    _upsert_automation_child_edge_for_send,
)


def test_hub_automations_create_security_scan_saved_disabled(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    response = client.post(
        "/hub/automations",
        json={
            "preset": "security_scan_pr",
            "execution_mode": EXECUTOR_AGENT_TASK_TURN,
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
    assert automation["execution_mode"] == EXECUTOR_AGENT_TASK_TURN
    assert automation["executor_kind"] == EXECUTOR_AGENT_TASK_TURN
    assert automation["enabled"] is False
    assert automation["schedule"]["schedule_kind"] == "daily"
    assert automation["target"]["repo_id"] == "repo-1"
    assert automation["executor"]["agent"] == "codex"
    assert automation["executor"]["model"] == "gpt-5.4"
    assert automation["executor"]["reasoning"] == "medium"
    assert automation["direct_runtime_contract"]["agent"] == "codex"
    assert automation["direct_runtime_contract"]["model"] == "gpt-5.4"
    assert automation["coordinator_runtime_contract"] is None
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
    assert automation["executor_summary"]["kind"] == EXECUTOR_AGENT_TASK_TURN
    assert automation["raw_links"]["control_plane_rule"].endswith(automation["id"])

    listing = client.get("/hub/automations")
    assert listing.status_code == 200
    assert listing.json()["summary"]["paused"] >= 1
    presets = {preset["id"]: preset for preset in listing.json()["presets"]}
    security_preset = presets["security_scan_pr"]
    assert security_preset["schedule"]["kind"] == "daily"
    assert security_preset["executor_kind"] == EXECUTOR_AGENT_TASK_TURN
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


def test_hub_automations_create_pma_operator_security_scan_and_run_now(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    created = client.post(
        "/hub/automations",
        json={
            "preset": "security_scan_pr",
            "execution_mode": EXECUTOR_PMA_OPERATOR_TURN,
            "repo_id": "repo-1",
            "agent": "codex",
            "model": "gpt-5.5",
            "worker_child_policy": {"default_authoritative": True},
        },
    )

    assert created.status_code == 200
    automation = created.json()["automation"]
    assert automation["execution_mode"] == EXECUTOR_PMA_OPERATOR_TURN
    assert automation["executor_kind"] == EXECUTOR_PMA_OPERATOR_TURN
    assert automation["coordinator_runtime_contract"]["agent"] == "codex"
    assert automation["coordinator_runtime_contract"]["model"] == "gpt-5.5"
    assert automation["direct_runtime_contract"] is None
    assert automation["worker_child_policy"] == {"default_authoritative": True}

    rule_id = automation["id"]
    run = client.post(f"/hub/automations/{rule_id}/run")

    assert run.status_code == 200
    assert run.json()["jobs_created"] == 1
    jobs = AutomationStore(hub_root).list_jobs(rule_id=rule_id)
    assert jobs[0].executor["kind"] == EXECUTOR_PMA_OPERATOR_TURN


def test_managed_thread_send_records_canonical_automation_child_edge(tmp_path):
    hub_root = tmp_path / "hub"
    store = AutomationStore(hub_root)
    store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-pma-parent",
            name="PMA parent",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_HUB,
            executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-pma-parent", event_type="manual.run")
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-pma-parent",
            rule_id="rule-pma-parent",
            event_id="event-pma-parent",
            state="running",
            target={"policy": TARGET_POLICY_HUB},
            executor={"kind": EXECUTOR_PMA_OPERATOR_TURN},
            available_at="2026-01-01T00:00:00Z",
            created_at="2026-01-01T00:00:00Z",
            dedupe_key="job-pma-parent",
        )
    )
    request = TurnExecutionRequest(
        request_id="turn-worker",
        target_id="thread-worker",
        target_kind="thread",
        workspace_root=str(tmp_path),
        request_kind="message",
        busy_policy="queue",
        prompt_text="Do worker task",
        agent="opencode",
        profile="security",
        model="zai-coding-plan/glm-5.1",
        model_payload={"providerID": "zai-coding-plan", "modelID": "glm-5.1"},
        reasoning="high",
        approval_policy="never",
        sandbox_policy="dangerFullAccess",
        origin=TurnExecutionOrigin(
            kind="surface",
            source_id="web:thread-worker",
            surface_kind="web",
            surface_key="thread-worker",
        ),
    )

    plan = _prepare_automation_child_for_managed_send(
        hub_root,
        {
            "parent_job_id": "job-pma-parent",
            "authoritative_for_parent_completion": True,
        },
        turn_request=request,
        thread=SimpleNamespace(backend_runtime_instance_id="runtime-1"),
    )
    assert plan is not None
    _upsert_automation_child_edge_for_send(
        hub_root, plan, managed_turn_id="turn-worker"
    )

    edge = store.list_child_execution_edges("job-pma-parent")[0]
    assert edge.child_kind == "agent_task"
    assert edge.child_id == "turn-worker"
    assert edge.requested_runtime.agent == "opencode"
    assert edge.requested_runtime.model == "zai-coding-plan/glm-5.1"
    assert edge.requested_runtime.provider_payload == {
        "providerID": "zai-coding-plan",
        "modelID": "glm-5.1",
    }
    assert edge.actual_runtime is None


def test_managed_thread_send_rejects_orphan_automation_child_edge(tmp_path):
    hub_root = tmp_path / "hub"
    request = TurnExecutionRequest(
        request_id="turn-worker",
        target_id="thread-worker",
        target_kind="thread",
        workspace_root=str(tmp_path),
        request_kind="message",
        busy_policy="queue",
        prompt_text="Do worker task",
        agent="opencode",
        model="zai-coding-plan/glm-5.1",
        model_payload={"providerID": "zai-coding-plan", "modelID": "glm-5.1"},
        approval_policy="never",
        sandbox_policy="dangerFullAccess",
        origin=TurnExecutionOrigin(
            kind="surface",
            source_id="web:thread-worker",
            surface_kind="web",
            surface_key="thread-worker",
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        _prepare_automation_child_for_managed_send(
            hub_root,
            {"parent_job_id": "missing-job"},
            turn_request=request,
            thread=SimpleNamespace(backend_runtime_instance_id="runtime-1"),
        )

    assert exc_info.value.status_code == 404
    assert AutomationStore(hub_root).list_child_execution_edges("missing-job") == []


def test_hub_automations_reject_invalid_product_runtime_combinations(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    client = TestClient(create_hub_app(hub_root))

    missing_agent = client.post(
        "/hub/automations",
        json={"preset": "security_scan_pr", "repo_id": "repo-1"},
    )
    wrong_model_runtime = client.post(
        "/hub/automations",
        json={
            "preset": "security_scan_pr",
            "repo_id": "repo-1",
            "agent": "codex",
            "model": "zai-coding-plan/glm-5.1",
        },
    )
    worker_policy_without_pma = client.post(
        "/hub/automations",
        json={
            "preset": "security_scan_pr",
            "repo_id": "repo-1",
            "agent": "codex",
            "worker_child_policy": {"default_authoritative": True},
        },
    )

    assert missing_agent.status_code == 400
    assert "requires requested_runtime.agent" in missing_agent.json()["detail"]
    assert wrong_model_runtime.status_code == 400
    assert "OpenCode provider/model" in wrong_model_runtime.json()["detail"]
    assert worker_policy_without_pma.status_code == 400
    assert "worker_child_policy" in worker_policy_without_pma.json()["detail"]


def test_hub_automation_workspace_batches_jobs_and_target_options(
    tmp_path, monkeypatch
):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    store = AutomationStore(hub_root)
    rule_ids: list[str] = []
    for index in range(2):
        rule = store.upsert_rule(
            AutomationRule.hydrate_persisted(
                rule_id=f"rule-{index}",
                name=f"Rule {index}",
                enabled=index == 0,
                trigger_kind=TRIGGER_KIND_SCHEDULE,
                trigger={"event_types": ["schedule.fire"]},
                target_policy=TARGET_POLICY_HUB,
                target={"repo_id": f"repo-{index}"},
                executor_kind=LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
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


def test_hub_automation_jobs_project_effective_state_and_child_runtime(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    store = AutomationStore(hub_root)
    rule = store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-effective",
            name="Effective state rule",
            enabled=True,
            trigger_kind=TRIGGER_KIND_SCHEDULE,
            trigger={"event_types": ["schedule.fire"]},
            target_policy=TARGET_POLICY_HUB,
            target={"repo_id": "repo-1"},
            executor_kind=EXECUTOR_AGENT_TASK_TURN,
            executor={
                "message": "Run direct task",
                "requested_runtime": {"agent": "codex", "model": "gpt-5.4"},
                "agent": "codex",
                "model": "gpt-5.4",
            },
        )
    )
    event = store.create_event(
        event_id="event-effective",
        event_type="schedule.fire",
        source="test",
        target={"rule_id": rule.rule_id},
        payload={},
    )
    store.create_job(
        job_id="job-effective",
        rule_id=rule.rule_id,
        event_id=event.event_id,
        state="running",
        dedupe_key="dedupe-effective",
        target=rule.target,
        executor=rule.executor,
        created_at="2026-01-01T00:00:00Z",
    )
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="job-effective",
            child_kind="agent_task",
            child_id="thread-effective",
            authoritative_for_parent_completion=True,
            requested_runtime={"agent": "codex", "model": "gpt-5.4"},
            actual_runtime={"agent": "codex", "model": "gpt-5.4"},
            terminal_state="succeeded",
            terminal_observed_at="2026-01-01T00:05:00Z",
        )
    )
    client = TestClient(create_hub_app(hub_root))

    response = client.get(f"/hub/automations/{rule.rule_id}")

    assert response.status_code == 200
    job = response.json()["automation"]["last_job"]
    assert job["raw_state"] == "running"
    assert job["effective_state"] == "succeeded"
    assert job["terminal_reason"].startswith("Derived from authoritative child")
    assert job["children"][0]["child_id"] == "thread-effective"
    assert job["runtime_contract"]["requested"]["agent"] == "codex"
    assert job["policy_violations"][0]["code"] == "AUTOMATION_PARENT_STATE_STALE"


def test_hub_automation_jobs_project_pma_coordinator_and_worker_runtime(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    store = AutomationStore(hub_root)
    rule = store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-pma-runtime",
            name="PMA runtime rule",
            enabled=True,
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_HUB,
            target={"repo_id": "repo-1"},
            executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
            executor={
                "kind": EXECUTOR_PMA_OPERATOR_TURN,
                "message": "Coordinate follow-up",
                "requested_runtime": {"agent": "codex", "model": "gpt-5.5"},
                "worker_child_policy": {"default_authoritative": True},
            },
        )
    )
    event = store.create_event(
        event_id="event-pma-runtime",
        event_type="manual.run",
        source="test",
        target={"rule_id": rule.rule_id},
        payload={},
    )
    store.create_job(
        job_id="job-pma-runtime",
        rule_id=rule.rule_id,
        event_id=event.event_id,
        state="running",
        dedupe_key="dedupe-pma-runtime",
        target=rule.target,
        executor=rule.executor,
        created_at="2026-01-01T00:00:00Z",
    )
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="job-pma-runtime",
            child_kind="pma_operator",
            child_id="pma-item-runtime",
            authoritative_for_parent_completion=False,
            requested_runtime={"agent": "codex", "model": "gpt-5.5"},
            actual_runtime={"agent": "codex", "model": "gpt-5.5"},
            terminal_state="succeeded",
            terminal_observed_at="2026-01-01T00:02:00Z",
        )
    )
    store.upsert_child_execution_edge(
        AutomationChildExecutionEdge.create(
            parent_job_id="job-pma-runtime",
            child_kind="agent_task",
            child_id="worker-runtime",
            authoritative_for_parent_completion=True,
            requested_runtime={
                "agent": "opencode",
                "model": "zai-coding-plan/glm-5.1",
            },
            actual_runtime={
                "agent": "opencode",
                "model": "zai-coding-plan/glm-5.1",
            },
            terminal_state="succeeded",
            terminal_observed_at="2026-01-01T00:05:00Z",
        )
    )
    client = TestClient(create_hub_app(hub_root))

    response = client.get(f"/hub/automations/{rule.rule_id}")

    assert response.status_code == 200
    automation = response.json()["automation"]
    assert automation["execution_mode"] == EXECUTOR_PMA_OPERATOR_TURN
    assert automation["coordinator_runtime_contract"]["agent"] == "codex"
    assert automation["coordinator_runtime_contract"]["model"] == "gpt-5.5"
    assert automation["direct_runtime_contract"] is None
    assert automation["executor_summary"]["agent"] is None
    job = automation["last_job"]
    assert job["effective_state"] == JOB_SUCCEEDED
    children = {child["child_id"]: child for child in job["children"]}
    assert children["pma-item-runtime"]["child_kind"] == "pma_operator"
    assert children["pma-item-runtime"]["requested_runtime"]["agent"] == "codex"
    assert children["worker-runtime"]["child_kind"] == "agent_task"
    assert children["worker-runtime"]["requested_runtime"]["agent"] == "opencode"


def test_hub_automation_workspace_tolerates_unknown_executor_kind(tmp_path):
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    store = AutomationStore(hub_root)
    store.upsert_rule(
        AutomationRule.hydrate_persisted(
            rule_id="rule-future",
            name="Future executor",
            enabled=True,
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_HUB,
            executor_kind=LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
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
                    "hypothetical_future_executor",
                    json.dumps(
                        {
                            "kind": "hypothetical_future_executor",
                            "prompt": "Future task",
                        }
                    ),
                    "rule-future",
                ),
            )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/read-models/automations/workspace")
    run = client.post("/hub/automations/rule-future/run")

    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["automations"]}
    row = rows["rule-future"]
    assert row["executor_kind"] == "hypothetical_future_executor"
    assert row["executor"]["kind"] == "hypothetical_future_executor"
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
        json={"preset": "security_scan_pr", "repo_id": "repo-1", "agent": "codex"},
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
        json={"preset": "security_scan_pr", "repo_id": "repo-1", "agent": "codex"},
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
        json={
            "preset": "security_scan_pr",
            "repo_id": "repo-1",
            "hour": 9,
            "agent": "codex",
        },
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
            "model": "anthropic/claude-sonnet-4",
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
    assert automation["executor"]["model"] == "anthropic/claude-sonnet-4"
    assert automation["executor"]["reasoning"] == "high"
    assert automation["direct_runtime_contract"]["agent"] == "opencode"
    assert automation["direct_runtime_contract"]["model"] == "anthropic/claude-sonnet-4"
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
        json={"preset": "security_scan_pr", "repo_id": "repo-1", "agent": "codex"},
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
        AutomationRule.hydrate_persisted(
            rule_id="builtin:pma:timer:timer-1",
            name="PMA timer timer-1",
            enabled=True,
            system_owned=True,
            trigger_kind=TRIGGER_KIND_SCHEDULE,
            trigger={"schedule_kind": SCHEDULE_ONE_SHOT},
            target_policy=TARGET_POLICY_HUB,
            target={"repo_id": "repo-1", "thread_target_id": "thread-1"},
            executor_kind=LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
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
            executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
            executor={
                "lane_id": "pma:default",
                "requested_runtime": {"agent": "codex", "model": "gpt-5.5"},
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
    assert automation["execution_mode"] == EXECUTOR_PMA_OPERATOR_TURN
    assert automation["coordinator_runtime_contract"]["agent"] == "codex"
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
