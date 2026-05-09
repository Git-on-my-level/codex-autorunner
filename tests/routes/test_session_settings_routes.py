from __future__ import annotations

import json
from unittest.mock import PropertyMock, patch

import pytest
from fastapi.testclient import TestClient
from tests.conftest import write_test_config

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.surfaces.web.app import create_repo_app
from codex_autorunner.surfaces.web.runner_manager import RunnerManager


@pytest.fixture(scope="module")
def _settings_env(tmp_path_factory):
    repo_root = tmp_path_factory.mktemp("repo")
    hub_root = repo_root
    seed_hub_files(hub_root, force=True)
    seed_repo_files(repo_root, git_required=False)
    (repo_root / ".git").mkdir(exist_ok=True)
    write_test_config(
        hub_root / CONFIG_FILENAME, json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    )
    app = create_repo_app(repo_root)
    yield TestClient(app), repo_root


def test_session_settings_round_trip_persists_values(_settings_env) -> None:
    client, _repo_root = _settings_env

    client.post(
        "/api/session/settings",
        json={
            "autorunner_model_override": "",
            "autorunner_effort_override": "",
            "autorunner_approval_policy": "",
            "autorunner_sandbox_mode": "",
            "autorunner_workspace_write_network": None,
            "runner_stop_after_runs": None,
        },
    )

    initial = client.get("/api/session/settings")
    assert initial.status_code == 200
    assert initial.json() == {
        "autorunner_model_override": None,
        "autorunner_model_overrides": {},
        "autorunner_effort_override": None,
        "autorunner_approval_policy": None,
        "autorunner_sandbox_mode": None,
        "autorunner_workspace_write_network": None,
        "runner_stop_after_runs": None,
    }

    response = client.post(
        "/api/session/settings",
        json={
            "autorunner_model_override": "gpt-5.4",
            "autorunner_effort_override": "high",
            "autorunner_approval_policy": "never",
            "autorunner_sandbox_mode": "workspaceWrite",
            "autorunner_workspace_write_network": True,
            "runner_stop_after_runs": 4,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "autorunner_model_override": "gpt-5.4",
        "autorunner_model_overrides": {"codex": "gpt-5.4"},
        "autorunner_effort_override": "high",
        "autorunner_approval_policy": "never",
        "autorunner_sandbox_mode": "workspaceWrite",
        "autorunner_workspace_write_network": True,
        "runner_stop_after_runs": 4,
    }

    refreshed = client.get("/api/session/settings")
    assert refreshed.status_code == 200
    assert refreshed.json() == response.json()


def test_session_settings_allow_clearing_values(_settings_env) -> None:
    client, _repo_root = _settings_env

    seeded = client.post(
        "/api/session/settings",
        json={
            "autorunner_model_override": "gpt-5.4",
            "autorunner_effort_override": "high",
            "autorunner_approval_policy": "never",
            "autorunner_sandbox_mode": "workspaceWrite",
            "autorunner_workspace_write_network": False,
            "runner_stop_after_runs": 2,
        },
    )
    assert seeded.status_code == 200

    cleared = client.post(
        "/api/session/settings",
        json={
            "autorunner_model_override": "",
            "autorunner_effort_override": "",
            "autorunner_approval_policy": "",
            "autorunner_sandbox_mode": "",
            "autorunner_workspace_write_network": None,
            "runner_stop_after_runs": None,
        },
    )

    assert cleared.status_code == 200
    assert cleared.json() == {
        "autorunner_model_override": None,
        "autorunner_model_overrides": {},
        "autorunner_effort_override": None,
        "autorunner_approval_policy": None,
        "autorunner_sandbox_mode": None,
        "autorunner_workspace_write_network": None,
        "runner_stop_after_runs": None,
    }


def test_session_settings_reject_changes_while_run_is_active(_settings_env) -> None:
    client, _repo_root = _settings_env

    with patch.object(RunnerManager, "running", new_callable=PropertyMock) as running:
        running.return_value = True
        response = client.post(
            "/api/session/settings",
            json={"autorunner_model_override": "gpt-5.4"},
        )

    assert response.status_code == 409
    assert "Cannot change autorunner settings while a run is active" in response.text


def test_session_settings_rejects_invalid_runtime_preferences(_settings_env) -> None:
    client, _repo_root = _settings_env

    invalid_approval = client.post(
        "/api/session/settings",
        json={"autorunner_approval_policy": "sometimes"},
    )
    assert invalid_approval.status_code == 400
    assert "approval policy must be never or unlessTrusted" in invalid_approval.text

    invalid_sandbox = client.post(
        "/api/session/settings",
        json={"autorunner_sandbox_mode": "readOnly"},
    )
    assert invalid_sandbox.status_code == 400
    assert (
        "sandbox mode must be dangerFullAccess or workspaceWrite"
        in invalid_sandbox.text
    )

    invalid_network = client.post(
        "/api/session/settings",
        json={"autorunner_workspace_write_network": "yes"},
    )
    assert invalid_network.status_code == 422

    invalid_runs = client.post(
        "/api/session/settings",
        json={"runner_stop_after_runs": 0},
    )
    assert invalid_runs.status_code == 400
    assert "runner_stop_after_runs must be a positive integer" in invalid_runs.text


def test_session_settings_apply_all_runtime_preferences_directly(_settings_env) -> None:
    client, _repo_root = _settings_env

    response = client.post(
        "/api/session/settings",
        json={
            "autorunner_model_override": "gpt-5.5",
            "autorunner_effort_override": "high",
            "autorunner_approval_policy": "unlessTrusted",
            "autorunner_sandbox_mode": "workspaceWrite",
            "autorunner_workspace_write_network": True,
            "runner_stop_after_runs": 5,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "autorunner_model_override": "gpt-5.5",
        "autorunner_model_overrides": {"codex": "gpt-5.5"},
        "autorunner_effort_override": "high",
        "autorunner_approval_policy": "unlessTrusted",
        "autorunner_sandbox_mode": "workspaceWrite",
        "autorunner_workspace_write_network": True,
        "runner_stop_after_runs": 5,
    }


def test_session_settings_persist_per_agent_model_defaults(_settings_env) -> None:
    client, _repo_root = _settings_env

    response = client.post(
        "/api/session/settings",
        json={
            "autorunner_model_overrides": {
                "codex": "gpt-5.5",
                "opencode": "zai-coding-plan/glm-5.1",
                "hermes": "",
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["autorunner_model_override"] == "gpt-5.5"
    assert response.json()["autorunner_model_overrides"] == {
        "codex": "gpt-5.5",
        "opencode": "zai-coding-plan/glm-5.1",
    }


def test_session_settings_approval_routes_are_removed(_settings_env) -> None:
    client, _repo_root = _settings_env

    assert client.get("/api/session/settings/approvals").status_code == 404
    assert (
        client.post(
            "/api/session/settings/approvals",
            json={"autorunner_model_override": "gpt-5.4"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/session/settings/approvals/approval-1/decision",
            json={"decision": "approve"},
        ).status_code
        == 404
    )
