from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import OrchestrationBindingStore
from codex_autorunner.surfaces.web.services.repo_worktree_read_models import (
    RepoWorktreeReadModelService,
)


def test_scoped_chats_repairs_legacy_worktree_rows(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    worktree_root = hub_root / "worktrees" / "base--feature"
    worktree_root.mkdir(parents=True)
    thread_store = ManagedThreadStore(hub_root)
    thread = thread_store.create_thread(
        "codex",
        worktree_root,
        repo_id="base--feature",
        resource_kind="repo",
        resource_id="base--feature",
        name="discord:channel-1",
    )
    thread_id = str(thread["managed_thread_id"])
    OrchestrationBindingStore(hub_root).upsert_binding(
        surface_kind="discord",
        surface_key="channel-1",
        thread_target_id=thread_id,
        agent_id="codex",
        repo_id="base--feature",
        resource_kind="repo",
        resource_id="base--feature",
        mode="repo",
    )
    snapshot = SimpleNamespace(
        id="base--feature",
        kind="worktree",
        worktree_of="base",
        path=worktree_root,
    )
    context = SimpleNamespace(
        config=SimpleNamespace(root=hub_root),
        supervisor=SimpleNamespace(list_repos=lambda use_cache=True: [snapshot]),
    )
    service = RepoWorktreeReadModelService(
        context,
        mount_manager=SimpleNamespace(),
        enricher=SimpleNamespace(),
    )

    chats = service._scoped_chats(
        owner_kind="worktree",
        owner_id="base--feature",
        limit=10,
    )

    assert [chat["thread_target_id"] for chat in chats] == [thread_id]
    assert chats[0]["resource_kind"] == "worktree"
    assert chats[0]["resource_id"] == "base--feature"
