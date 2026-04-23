from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from tests.conftest import write_test_config

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.surfaces.web.app import create_repo_app
from codex_autorunner.surfaces.web.schemas import TemplateRepoUpdateRequest


@pytest.fixture(scope="module")
def _validation_env(tmp_path_factory):
    repo_root = tmp_path_factory.mktemp("repo")
    hub_root = repo_root
    seed_hub_files(hub_root, force=True)
    seed_repo_files(repo_root, git_required=False)
    (repo_root / ".git").mkdir(exist_ok=True)
    config = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    config["templates"] = {
        "enabled": True,
        "repos": [
            {
                "id": "existing",
                "url": "https://github.com/acme/templates.git",
                "trusted": True,
                "default_ref": "main",
            }
        ],
    }
    write_test_config(hub_root / CONFIG_FILENAME, config)
    app = create_repo_app(repo_root)
    yield TestClient(app), repo_root


def test_session_settings_rejects_unknown_keys(_validation_env) -> None:
    client, _repo_root = _validation_env

    response = client.post(
        "/api/session/settings",
        json={
            "autorunner_model_override": "gpt-5.4",
            "autorunner_model_overide": "typo",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"][-1] == "autorunner_model_overide" for item in detail)


def test_template_repo_create_rejects_unknown_keys(_validation_env) -> None:
    client, _repo_root = _validation_env

    response = client.post(
        "/api/templates/repos",
        json={
            "id": "new-repo",
            "url": "https://github.com/acme/new-templates.git",
            "trusted": True,
            "unexpected": "value",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"][-1] == "unexpected" for item in detail)


def test_template_repo_update_rejects_unknown_keys(_validation_env) -> None:
    client, _repo_root = _validation_env

    response = client.put(
        "/api/templates/repos/existing",
        json={"url": "https://github.com/acme/updated.git", "unexpected": "value"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"][-1] == "unexpected" for item in detail)


def test_template_repo_update_schema_accepts_default_ref_alias() -> None:
    payload = TemplateRepoUpdateRequest.model_validate({"defaultRef": "stable"})

    assert payload.default_ref == "stable"
    assert payload.model_dump(exclude_unset=True) == {"default_ref": "stable"}


def test_template_apply_rejects_unknown_keys(_validation_env) -> None:
    client, _repo_root = _validation_env

    response = client.post(
        "/api/templates/apply",
        json={
            "template": "existing:tickets/TEMPLATE.md",
            "set_agentt": "codex",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"][-1] == "set_agentt" for item in detail)


def test_run_start_rejects_unknown_keys(_validation_env) -> None:
    client, _repo_root = _validation_env

    response = client.post(
        "/api/run/start",
        json={"once": True, "agnt": "codex"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"][-1] == "agnt" for item in detail)
