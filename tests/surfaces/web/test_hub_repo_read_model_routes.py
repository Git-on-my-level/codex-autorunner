from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from tests.surfaces.web._hub_test_support import write_discord_binding_rows

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.services.repo_worktree_read_models import (
    RepoWorktreeReadModelService,
)


def _add_workspace(
    hub_root: Path,
    *,
    repo_id: str,
    kind: str = "base",
    worktree_of: str | None = None,
) -> Path:
    root = hub_root / "worktrees" / repo_id
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    seed_repo_files(root, git_required=False)
    config = load_hub_config(hub_root)
    manifest = load_manifest(config.manifest_path, hub_root)
    manifest.ensure_repo(
        hub_root,
        root,
        repo_id=repo_id,
        display_name=repo_id,
        kind=kind,
        worktree_of=worktree_of,
        branch=f"feature/{repo_id}" if kind == "worktree" else None,
    )
    save_manifest(config.manifest_path, manifest, hub_root)
    return root


def _write_tickets(workspace_root: Path, count: int) -> None:
    tickets_dir = workspace_root / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        (tickets_dir / f"TICKET-{index:03d}.md").write_text(
            f"---\ntitle: Ticket {index:03d}\nagent: codex\ndone: false\n---\n\nbody\n",
            encoding="utf-8",
        )


def test_repo_worktree_topology_and_runtime_snapshots_are_windowed(hub_env) -> None:
    for index in range(8):
        _add_workspace(
            hub_env.hub_root,
            repo_id=f"{hub_env.repo_id}--wt-{index:02d}",
            kind="worktree",
            worktree_of=hub_env.repo_id,
        )

    client = TestClient(create_hub_app(hub_env.hub_root))
    topology = client.get(
        "/hub/read-models/repo-worktree/topology",
        params={"kind": "worktree", "limit": 7},
    )
    runtime = client.get(
        "/hub/read-models/repo-worktree/runtime",
        params={"kind": "worktree", "limit": 7},
    )

    assert topology.status_code == 200
    assert runtime.status_code == 200
    topology_payload = topology.json()
    runtime_payload = runtime.json()
    assert topology_payload["contractVersion"] == "web-read-models.v1"
    assert topology_payload["kind"] == "repo_worktree.topology.snapshot"
    assert len(topology_payload["worktrees"]) == 7
    assert topology_payload["window"]["nextCursor"] == "7"
    assert "repos" not in topology_payload["worktrees"][0]
    assert runtime_payload["kind"] == "repo_worktree.runtime.snapshot"
    assert len(runtime_payload["runtime"]) == 7


def test_archive_worktree_is_non_destructive_and_projects_archive_state(
    hub_env,
) -> None:
    worktree_root = _add_workspace(
        hub_env.hub_root,
        repo_id="repo-00--archive-me",
        kind="worktree",
        worktree_of=hub_env.repo_id,
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    archive = client.post(
        "/hub/worktrees/archive",
        json={"worktreeRepoId": "repo-00--archive-me", "archived": True},
    )
    assert archive.status_code == 200
    assert archive.json()["archive_state"] == "archived"
    assert worktree_root.exists()

    topology = client.get(
        "/hub/read-models/repo-worktree/topology",
        params={"kind": "worktree", "limit": 20},
    )
    topology.raise_for_status()
    worktree = next(
        item
        for item in topology.json()["worktrees"]
        if item["worktreeId"] == "repo-00--archive-me"
    )
    assert worktree["archived"] is True
    assert worktree["archiveState"] == "archived"

    unarchive = client.post(
        "/hub/worktrees/archive",
        json={"worktreeRepoId": "repo-00--archive-me", "archived": False},
    )
    assert unarchive.status_code == 200
    assert unarchive.json()["archive_state"] == "active"
    assert worktree_root.exists()


def test_repo_worktree_topology_surfaces_chat_bound_channel_display(
    hub_env,
) -> None:
    worktree_root = _add_workspace(
        hub_env.hub_root,
        repo_id="repo-00--discord-chat",
        kind="worktree",
        worktree_of=hub_env.repo_id,
    )
    write_discord_binding_rows(
        hub_env.hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        rows=[
            {
                "channel_id": "chan-bound",
                "guild_id": "guild-1",
                "workspace_path": str(worktree_root.resolve()),
                "repo_id": None,
                "resource_kind": None,
                "resource_id": None,
                "pma_enabled": 0,
                "agent": "codex",
                "updated_at": "2026-01-01T00:00:01Z",
            }
        ],
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/repo-worktree/topology",
        params={"kind": "worktree", "limit": 20},
    )

    assert response.status_code == 200
    worktree = next(
        item
        for item in response.json()["worktrees"]
        if item["worktreeId"] == "repo-00--discord-chat"
    )
    assert worktree["chatBound"] is True
    assert worktree["chatBindingCount"] == 1
    assert worktree["chatBindingSources"] == {"discord": 1}
    assert worktree["chatBindingDisplayNames"] == ["guild:guild-1 / #chan-bound"]


def test_repo_detail_child_worktrees_preserve_chat_binding_metadata(
    hub_env,
) -> None:
    worktree_root = _add_workspace(
        hub_env.hub_root,
        repo_id="repo-00--detail-discord-chat",
        kind="worktree",
        worktree_of=hub_env.repo_id,
    )
    write_discord_binding_rows(
        hub_env.hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        rows=[
            {
                "channel_id": "chan-detail-bound",
                "guild_id": "guild-1",
                "workspace_path": str(worktree_root.resolve()),
                "repo_id": None,
                "resource_kind": None,
                "resource_id": None,
                "pma_enabled": 0,
                "agent": "codex",
                "updated_at": "2026-01-01T00:00:01Z",
            }
        ],
    )

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(f"/hub/read-models/repos/{hub_env.repo_id}/detail")

    assert response.status_code == 200
    child = next(
        item
        for item in response.json()["topology"]["children"]
        if item["id"] == "repo-00--detail-discord-chat"
    )
    assert child["chat_bound"] is True
    assert child["chat_bound_thread_count"] == 1
    assert child["chat_binding_sources"] == {"discord": 1}
    assert child["chat_binding_display_names"] == ["guild:guild-1 / #chan-detail-bound"]


def test_worktree_detail_snapshot_is_scoped_and_does_not_include_global_tickets(
    hub_env,
) -> None:
    worktree_root = _add_workspace(
        hub_env.hub_root,
        repo_id="repo--feature",
        kind="worktree",
        worktree_of=hub_env.repo_id,
    )
    other_root = _add_workspace(hub_env.hub_root, repo_id="other")
    _write_tickets(worktree_root, 501)
    _write_tickets(other_root, 30)

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/worktrees/repo--feature/detail",
        params={
            "ticket_limit": 25,
            "run_limit": 3,
            "chat_limit": 3,
            "artifact_limit": 3,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["contractVersion"] == "web-read-models.v1"
    assert payload["kind"] == "repo_worktree.detail.snapshot"
    assert payload["ownerKind"] == "worktree"
    assert payload["parentLinks"]["repo_id"] == hub_env.repo_id
    assert len(payload["ticketQueue"]) == 25
    assert {ticket["workspace_id"] for ticket in payload["ticketQueue"]} == {
        "repo--feature"
    }
    assert payload["scopedTickets"] == payload["ticketQueue"]
    assert payload["ticketWindow"]["limit"] == 25
    assert payload["ticketWindow"]["totalEstimate"] == 501
    assert payload["ticketWindow"]["totalIsExact"] is True
    assert payload["ticketWindow"]["nextCursor"] == "25"


def test_repo_detail_ticket_queue_is_bounded_by_default(hub_env) -> None:
    _write_tickets(hub_env.repo_root, 550)

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(f"/hub/read-models/repos/{hub_env.repo_id}/detail")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["ticketQueue"]) == 100
    assert payload["ticketWindow"]["limit"] == 100
    assert payload["ticketWindow"]["totalEstimate"] == 550
    assert payload["scopedTickets"] == payload["ticketQueue"]


def test_repo_detail_ticket_queue_supports_cursor_windows(hub_env) -> None:
    _write_tickets(hub_env.repo_root, 120)

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        f"/hub/read-models/repos/{hub_env.repo_id}/detail",
        params={"ticket_limit": 25, "ticket_cursor": "25"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["ticketQueue"]) == 25
    assert payload["ticketQueue"][0]["frontmatter"]["title"] == "Ticket 026"
    assert payload["ticketQueue"][-1]["frontmatter"]["title"] == "Ticket 050"
    assert payload["scopedTickets"] == payload["ticketQueue"]
    assert payload["ticketWindow"]["limit"] == 25
    assert payload["ticketWindow"]["previousCursor"] == "0"
    assert payload["ticketWindow"]["nextCursor"] == "50"
    assert payload["ticketWindow"]["totalEstimate"] == 120


def test_ticket_detail_snapshot_uses_owner_scoped_ticket_queue(hub_env) -> None:
    _write_tickets(hub_env.repo_root, 12)
    other_root = _add_workspace(hub_env.hub_root, repo_id="other")
    _write_tickets(other_root, 12)

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/tickets/7",
        params={"owner_kind": "repo", "owner_id": hub_env.repo_id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["contractVersion"] == "web-read-models.v1"
    assert payload["kind"] == "ticket.detail.snapshot"
    assert payload["ticket"]["routeId"] == "7"
    assert payload["ticketDetail"]["workspace_id"] == hub_env.repo_id
    assert {ticket["workspace_id"] for ticket in payload["ticketQueue"]} == {
        hub_env.repo_id
    }
    assert payload["legacyTicket"]["workspace_id"] == hub_env.repo_id
    assert {ticket["workspace_id"] for ticket in payload["scopedTickets"]} == {
        hub_env.repo_id
    }


def test_ticket_detail_snapshot_bounds_owner_queue(hub_env) -> None:
    _write_tickets(hub_env.repo_root, 150)

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/tickets/1",
        params={"owner_kind": "repo", "owner_id": hub_env.repo_id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["ticketQueue"]) == 100
    assert len(payload["scopedTickets"]) == 100
    assert payload["ticket"]["routeId"] == "1"
    assert len(payload["siblings"]) > 0


def test_ticket_detail_snapshot_window_includes_selected_ticket(hub_env) -> None:
    _write_tickets(hub_env.repo_root, 150)

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/tickets/120",
        params={"owner_kind": "repo", "owner_id": hub_env.repo_id, "ticket_limit": 25},
    )

    assert response.status_code == 200
    payload = response.json()
    titles = [ticket["frontmatter"]["title"] for ticket in payload["ticketQueue"]]
    assert len(titles) == 25
    assert "Ticket 120" in titles
    assert "Ticket 001" not in titles
    assert payload["ticket"]["routeId"] == "120"
    assert payload["scopedTickets"] == payload["ticketQueue"]


def test_ticket_detail_assembly_runs_blocking_work_off_event_loop(hub_env) -> None:
    """Regression guard: ticket_detail must delegate blocking reads to worker threads."""
    _write_tickets(hub_env.repo_root, 5)

    # _snapshot_by_id runs synchronously on the event-loop thread; the blocking
    # helpers must run on a different thread via asyncio.to_thread.
    event_loop_thread_ids: list[int] = []
    scoped_tickets_thread_ids: list[int] = []
    scoped_chats_thread_ids: list[int] = []
    scoped_runs_thread_ids: list[int] = []

    original_snapshot_by_id = RepoWorktreeReadModelService._snapshot_by_id
    original_scoped_tickets = RepoWorktreeReadModelService._scoped_tickets
    original_scoped_chats = RepoWorktreeReadModelService._scoped_chats
    original_scoped_runs = RepoWorktreeReadModelService._scoped_runs

    def spy_snapshot_by_id(self, repo_id: str) -> object:
        event_loop_thread_ids.append(threading.get_ident())
        return original_snapshot_by_id(self, repo_id)

    def spy_scoped_tickets(self, snapshot: object) -> list[dict]:
        scoped_tickets_thread_ids.append(threading.get_ident())
        return original_scoped_tickets(self, snapshot)

    def spy_scoped_chats(self, **kwargs: object) -> list[dict]:
        scoped_chats_thread_ids.append(threading.get_ident())
        return original_scoped_chats(self, **kwargs)

    def spy_scoped_runs(self, workspace_root: Path, **kwargs: object) -> list[dict]:
        scoped_runs_thread_ids.append(threading.get_ident())
        return original_scoped_runs(self, workspace_root, **kwargs)

    with (
        patch.object(
            RepoWorktreeReadModelService, "_snapshot_by_id", spy_snapshot_by_id
        ),
        patch.object(
            RepoWorktreeReadModelService, "_scoped_tickets", spy_scoped_tickets
        ),
        patch.object(RepoWorktreeReadModelService, "_scoped_chats", spy_scoped_chats),
        patch.object(RepoWorktreeReadModelService, "_scoped_runs", spy_scoped_runs),
    ):
        client = TestClient(create_hub_app(hub_env.hub_root))
        response = client.get(
            "/hub/read-models/tickets/1",
            params={"owner_kind": "repo", "owner_id": hub_env.repo_id},
        )

    assert response.status_code == 200
    assert event_loop_thread_ids, "_snapshot_by_id was never called"
    event_loop_tid = event_loop_thread_ids[0]

    assert scoped_tickets_thread_ids, "_scoped_tickets was never called"
    assert scoped_chats_thread_ids, "_scoped_chats was never called"
    assert scoped_runs_thread_ids, "_scoped_runs was never called"

    assert all(
        tid != event_loop_tid for tid in scoped_tickets_thread_ids
    ), "_scoped_tickets ran on the event-loop thread instead of a worker thread"
    assert all(
        tid != event_loop_tid for tid in scoped_chats_thread_ids
    ), "_scoped_chats ran on the event-loop thread instead of a worker thread"
    assert all(
        tid != event_loop_tid for tid in scoped_runs_thread_ids
    ), "_scoped_runs ran on the event-loop thread instead of a worker thread"
