from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes import file_chat as file_chat_routes

pytestmark = pytest.mark.slow


@pytest.fixture
def client(hub_env):
    app = create_hub_app(hub_env.hub_root)
    return TestClient(app)


def test_workspace_docs_read_write(hub_env, client, repo: Path):
    res = client.get(f"/repos/{hub_env.repo_id}/api/contextspace")
    assert res.status_code == 200
    data = res.json()
    assert set(data) == {"active_context", "decisions", "spec"}

    active_context = "# Test Context"
    decisions = "# Test Decisions"

    res = client.put(
        f"/repos/{hub_env.repo_id}/api/contextspace/active_context",
        json={"content": active_context},
    )
    assert res.status_code == 200
    assert res.json()["active_context"] == active_context

    res = client.put(
        f"/repos/{hub_env.repo_id}/api/contextspace/decisions",
        json={"content": decisions},
    )
    assert res.status_code == 200
    assert res.json()["decisions"] == decisions

    contextspace_dir = repo / ".codex-autorunner" / "contextspace"
    assert (contextspace_dir / "active_context.md").read_text() == active_context
    assert (contextspace_dir / "decisions.md").read_text() == decisions


def test_workspace_rejects_invalid_doc_kind(hub_env, client):
    res = client.put(
        f"/repos/{hub_env.repo_id}/api/contextspace/notes",
        json={"content": "nope"},
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "invalid contextspace doc kind"


def test_file_chat_workspace_targets(repo: Path):
    contextspace_dir = repo / ".codex-autorunner" / "contextspace"
    contextspace_dir.mkdir(parents=True, exist_ok=True)
    (contextspace_dir / "active_context.md").write_text("Active Context Content")
    (contextspace_dir / "decisions.md").write_text("Decisions Content")
    (contextspace_dir / "spec.md").write_text("Spec Content")

    for target in [
        "contextspace:active_context",
        "contextspace:decisions",
        "contextspace:spec",
        "contextspace:spec.md",
    ]:
        resolved = file_chat_routes._parse_target(repo, target)
        assert resolved.kind == "contextspace"
        assert resolved.path == contextspace_dir / resolved.id
