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
