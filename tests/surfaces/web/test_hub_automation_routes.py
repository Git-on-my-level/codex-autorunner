from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.automation import AutomationStore
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
        },
    )

    assert created.status_code == 200
    automation = created.json()["automation"]
    assert automation["executor_kind"] == "ticket_flow"
    assert automation["enabled"] is False
    assert automation["target_policy"] == "new_automation_worktree"
    rule_id = automation["id"]

    run = client.post(f"/hub/automations/{rule_id}/run")
    assert run.status_code == 200
    assert run.json()["jobs_created"] == 1

    jobs = AutomationStore(hub_root).list_jobs(rule_id=rule_id)
    assert len(jobs) == 1
    assert jobs[0].executor["kind"] == "ticket_flow"


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
