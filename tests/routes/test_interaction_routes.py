from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from tests.conftest import write_test_config

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.interaction_inbox import (
    InteractionInboxStore,
    InteractionOption,
    InteractionPrompt,
    default_interaction_inbox_path,
)
from codex_autorunner.surfaces.web.app import create_repo_app


@pytest.fixture()
def interaction_env(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    seed_hub_files(repo_root, force=True)
    seed_repo_files(repo_root, git_required=False)
    (repo_root / ".git").mkdir(exist_ok=True)
    write_test_config(
        repo_root / CONFIG_FILENAME, json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    )
    app = create_repo_app(repo_root)
    return TestClient(app), app


def test_interaction_routes_list_and_respond(interaction_env) -> None:
    client, app = interaction_env
    store = InteractionInboxStore(
        default_interaction_inbox_path(app.state.engine.state_path)
    )
    store.upsert_prompt(
        InteractionPrompt(
            id="question-1",
            kind="single_choice_question",
            title="Question",
            message="Pick one",
            owner={"kind": "repo", "id": "repo"},
            target_scope={"kind": "thread", "key": "thread:1"},
            requester_user_id="user-1",
            options=(
                InteractionOption(id="a", label="A"),
                InteractionOption(id="b", label="B"),
            ),
        )
    )

    listed = client.get("/api/interactions/prompts")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["prompts"]] == ["question-1"]

    decided = client.post(
        "/api/interactions/prompts/question-1/response",
        json={"actor_user_id": "user-1", "response": {"option_id": "b"}},
    )
    assert decided.status_code == 200
    prompt = decided.json()["prompt"]
    assert prompt["status"] == "answered"
    assert prompt["response"]["option_id"] == "b"


def test_interaction_routes_reject_unauthorized_actor(interaction_env) -> None:
    client, app = interaction_env
    store = InteractionInboxStore(
        default_interaction_inbox_path(app.state.engine.state_path)
    )
    store.upsert_prompt(
        InteractionPrompt(
            id="approval-1",
            kind="approval",
            title="Approval",
            message="Approve?",
            owner={"kind": "repo", "id": "repo"},
            target_scope={"kind": "run", "key": "run:1"},
            requester_user_id="user-1",
            options=(
                InteractionOption(id="approve", label="Approve"),
                InteractionOption(id="decline", label="Decline"),
            ),
        )
    )

    response = client.post(
        "/api/interactions/prompts/approval-1/response",
        json={"actor_user_id": "user-2", "response": {"decision": "approve"}},
    )

    assert response.status_code == 403
