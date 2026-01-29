from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.core import drafts as draft_utils
from codex_autorunner.core.state import now_iso
from codex_autorunner.routes import file_chat as file_chat_routes
from codex_autorunner.server import create_hub_app


@pytest.fixture()
def client(hub_env):
    app = create_hub_app(hub_env.hub_root)
    return TestClient(app)


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_draft(repo_root: Path, target_raw: str, before: str, after: str):
    target = file_chat_routes._parse_target(repo_root, target_raw)
    patch = file_chat_routes._build_patch(target.rel_path, before, after)
    draft = {
        "content": after,
        "patch": patch,
        "agent_message": "draft",
        "created_at": now_iso(),
        "base_hash": draft_utils.hash_content(before),
        "target": target.target,
        "rel_path": target.rel_path,
    }
    draft_utils.save_state(repo_root, {"drafts": {target.state_key: draft}})
    return target, draft


def test_pending_returns_hash_and_stale_flag(client: TestClient, hub_env, repo: Path):
    repo_root = repo
    workspace_path = repo_root / ".codex-autorunner" / "workspace" / "active_context.md"
    before = "line 1\n"
    after = "line 1\nline 2\n"
    _write_file(workspace_path, before)
    _seed_draft(repo_root, "workspace:active_context", before, after)

    res = client.get(
        f"/repos/{hub_env.repo_id}/api/file-chat/pending",
        params={"target": "workspace:active_context"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["base_hash"] == draft_utils.hash_content(before)
    assert data["current_hash"] == draft_utils.hash_content(before)
    assert data["is_stale"] is False


def test_apply_respects_force_and_clears_draft(client: TestClient, hub_env, repo: Path):
    repo_root = repo
    workspace_path = repo_root / ".codex-autorunner" / "workspace" / "decisions.md"
    before = "original\n"
    draft_after = "drafted\n"
    _write_file(workspace_path, before)
    target, draft = _seed_draft(repo_root, "workspace:decisions", before, draft_after)

    # Simulate external change to make draft stale
    _write_file(workspace_path, "external change\n")

    res_conflict = client.post(
        f"/repos/{hub_env.repo_id}/api/file-chat/apply",
        json={"target": target.target},
    )
    assert res_conflict.status_code == 409

    res_force = client.post(
        f"/repos/{hub_env.repo_id}/api/file-chat/apply",
        json={"target": target.target, "force": True},
    )
    assert res_force.status_code == 200
    assert workspace_path.read_text() == draft["content"]

    # Draft should be removed
    res_pending = client.get(
        f"/repos/{hub_env.repo_id}/api/file-chat/pending",
        params={"target": target.target},
    )
    assert res_pending.status_code == 404


def test_workspace_write_invalidates_draft(client: TestClient, hub_env, repo: Path):
    repo_root = repo
    workspace_path = repo_root / ".codex-autorunner" / "workspace" / "spec.md"
    before = "spec v1\n"
    after = "spec v2\n"
    _write_file(workspace_path, before)
    _seed_draft(repo_root, "workspace:spec", before, after)

    # Direct write through workspace API should invalidate draft
    res = client.put(
        f"/repos/{hub_env.repo_id}/api/workspace/spec",
        json={"content": "direct edit\n"},
    )
    assert res.status_code == 200

    pending = client.get(
        f"/repos/{hub_env.repo_id}/api/file-chat/pending",
        params={"target": "workspace:spec"},
    )
    assert pending.status_code == 404

    # State file should no longer have the draft
    state = draft_utils.load_state(repo_root)
    assert not state.get("drafts", {})
