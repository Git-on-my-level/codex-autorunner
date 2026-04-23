from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core import drafts as draft_utils
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.core.state import now_iso
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes import file_chat as file_chat_routes


@dataclass(frozen=True)
class _DraftEnv:
    client: TestClient
    repo_id: str
    repo_root: Path


@pytest.fixture(scope="module")
def _draft_env(tmp_path_factory):
    hub_root = tmp_path_factory.mktemp("hub")
    seed_hub_files(hub_root, force=True)
    repo_id = "repo"
    repo_root = hub_root / "worktrees" / repo_id
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)
    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(hub_config.manifest_path, manifest, hub_root)
    app = create_hub_app(hub_root)
    yield _DraftEnv(
        client=TestClient(app),
        repo_id=repo_id,
        repo_root=repo_root,
    )


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


def test_pending_returns_hash_and_stale_flag(_draft_env) -> None:
    repo_root = _draft_env.repo_root
    contextspace_path = (
        repo_root / ".codex-autorunner" / "contextspace" / "active_context.md"
    )
    before = "line 1\n"
    after = "line 1\nline 2\n"
    _write_file(contextspace_path, before)
    _seed_draft(repo_root, "contextspace:active_context", before, after)

    res = _draft_env.client.get(
        f"/repos/{_draft_env.repo_id}/api/file-chat/pending",
        params={"target": "contextspace:active_context"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["base_hash"] == draft_utils.hash_content(before)
    assert data["current_hash"] == draft_utils.hash_content(before)
    assert data["is_stale"] is False


def test_apply_respects_force_and_clears_draft(_draft_env) -> None:
    repo_root = _draft_env.repo_root
    contextspace_path = (
        repo_root / ".codex-autorunner" / "contextspace" / "decisions.md"
    )
    before = "original\n"
    draft_after = "drafted\n"
    _write_file(contextspace_path, before)
    target, draft = _seed_draft(
        repo_root, "contextspace:decisions", before, draft_after
    )

    _write_file(contextspace_path, "external change\n")

    res_conflict = _draft_env.client.post(
        f"/repos/{_draft_env.repo_id}/api/file-chat/apply",
        json={"target": target.target},
    )
    assert res_conflict.status_code == 409

    res_force = _draft_env.client.post(
        f"/repos/{_draft_env.repo_id}/api/file-chat/apply",
        json={"target": target.target, "force": True},
    )
    assert res_force.status_code == 200
    assert contextspace_path.read_text() == draft["content"]

    res_pending = _draft_env.client.get(
        f"/repos/{_draft_env.repo_id}/api/file-chat/pending",
        params={"target": target.target},
    )
    assert res_pending.status_code == 404


def test_discard_clears_draft_and_keeps_file_unchanged(_draft_env) -> None:
    repo_root = _draft_env.repo_root
    contextspace_path = repo_root / ".codex-autorunner" / "contextspace" / "spec.md"
    before = "spec v1\n"
    after = "spec v2\n"
    _write_file(contextspace_path, before)
    target, _draft = _seed_draft(repo_root, "contextspace:spec", before, after)

    res = _draft_env.client.post(
        f"/repos/{_draft_env.repo_id}/api/file-chat/discard",
        json={"target": target.target},
    )
    assert res.status_code == 200
    assert res.json()["content"] == before
    assert contextspace_path.read_text(encoding="utf-8") == before

    pending = _draft_env.client.get(
        f"/repos/{_draft_env.repo_id}/api/file-chat/pending",
        params={"target": target.target},
    )
    assert pending.status_code == 404
    assert not draft_utils.load_state(repo_root).get("drafts", {})


def test_workspace_write_invalidates_draft(_draft_env) -> None:
    repo_root = _draft_env.repo_root
    contextspace_path = repo_root / ".codex-autorunner" / "contextspace" / "spec.md"
    before = "spec v1\n"
    after = "spec v2\n"
    _write_file(contextspace_path, before)
    _seed_draft(repo_root, "contextspace:spec", before, after)

    res = _draft_env.client.put(
        f"/repos/{_draft_env.repo_id}/api/contextspace/spec",
        json={"content": "direct edit\n"},
    )
    assert res.status_code == 200

    pending = _draft_env.client.get(
        f"/repos/{_draft_env.repo_id}/api/file-chat/pending",
        params={"target": "contextspace:spec"},
    )
    assert pending.status_code == 404

    state = draft_utils.load_state(repo_root)
    assert not state.get("drafts", {})


def test_ticket_chat_apply_and_discard_wrappers_match_generic_contract(
    _draft_env,
) -> None:
    repo_root = _draft_env.repo_root
    ticket_path = repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md"
    before = "---\ntitle: Ticket\nagent: codex\ndone: false\ngoal: test\n---\n\nbody\n"
    after = (
        "---\ntitle: Ticket\nagent: codex\ndone: false\ngoal: test\n---\n\nupdated\n"
    )
    _write_file(ticket_path, before)

    _seed_draft(repo_root, "ticket:1", before, after)
    discard_res = _draft_env.client.post(
        f"/repos/{_draft_env.repo_id}/api/tickets/1/chat/discard"
    )
    assert discard_res.status_code == 200
    assert discard_res.json()["content"] == before
    assert ticket_path.read_text(encoding="utf-8") == before

    pending_after_discard = _draft_env.client.get(
        f"/repos/{_draft_env.repo_id}/api/tickets/1/chat/pending"
    )
    assert pending_after_discard.status_code == 404

    _seed_draft(repo_root, "ticket:1", before, after)
    _write_file(ticket_path, "externally changed\n")

    conflict_res = _draft_env.client.post(
        f"/repos/{_draft_env.repo_id}/api/tickets/1/chat/apply"
    )
    assert conflict_res.status_code == 409

    force_res = _draft_env.client.post(
        f"/repos/{_draft_env.repo_id}/api/tickets/1/chat/apply",
        json={"force": True},
    )
    assert force_res.status_code == 200
    assert force_res.json()["content"] == after
    assert ticket_path.read_text(encoding="utf-8") == after

    pending_after_apply = _draft_env.client.get(
        f"/repos/{_draft_env.repo_id}/api/tickets/1/chat/pending"
    )
    assert pending_after_apply.status_code == 404


def test_ticket_new_thread_resets_instance_scoped_registry_key(_draft_env) -> None:
    repo_root = _draft_env.repo_root
    ticket_path = repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md"
    _write_file(ticket_path, "---\nagent: codex\ndone: false\n---\n\nbody\n")

    target = file_chat_routes._parse_target(repo_root, "ticket:1")
    thread_key = f"file_chat.{target.state_key}"
    threads_file = repo_root / ".codex-autorunner" / "app_server_threads.json"
    threads_file.parent.mkdir(parents=True, exist_ok=True)
    threads_file.write_text(
        f'{{"version": 1, "threads": {{"{thread_key}": "thr_test_1"}}}}\n',
        encoding="utf-8",
    )

    res = _draft_env.client.post(
        f"/repos/{_draft_env.repo_id}/api/tickets/1/chat/new-thread"
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["status"] == "ok"
    assert payload["key"] == thread_key
    assert payload["cleared"] is True
    assert thread_key not in threads_file.read_text(encoding="utf-8")


def test_ticket_pending_returns_stale_flag_when_file_changed(_draft_env) -> None:
    repo_root = _draft_env.repo_root
    ticket_path = repo_root / ".codex-autorunner" / "tickets" / "TICKET-002.md"
    before = "---\ntitle: T2\nagent: codex\ndone: false\n---\n\noriginal\n"
    after = "---\ntitle: T2\nagent: codex\ndone: false\n---\n\nchanged\n"
    _write_file(ticket_path, before)
    _seed_draft(repo_root, "ticket:2", before, after)

    pending_fresh = _draft_env.client.get(
        f"/repos/{_draft_env.repo_id}/api/tickets/2/chat/pending"
    )
    assert pending_fresh.status_code == 200
    assert pending_fresh.json()["is_stale"] is False

    _write_file(ticket_path, "external modification\n")

    pending_stale = _draft_env.client.get(
        f"/repos/{_draft_env.repo_id}/api/tickets/2/chat/pending"
    )
    assert pending_stale.status_code == 200
    data = pending_stale.json()
    assert data["is_stale"] is True
    assert data["base_hash"] != data["current_hash"]
