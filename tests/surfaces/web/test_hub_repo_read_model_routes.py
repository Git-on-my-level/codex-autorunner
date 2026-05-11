from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app


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
    for index in range(20):
        _add_workspace(hub_env.hub_root, repo_id=f"repo-{index:02d}")
    for index in range(20):
        _add_workspace(
            hub_env.hub_root,
            repo_id=f"repo-00--wt-{index:02d}",
            kind="worktree",
            worktree_of="repo-00",
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
    _write_tickets(worktree_root, 30)
    _write_tickets(other_root, 30)

    client = TestClient(create_hub_app(hub_env.hub_root))
    response = client.get(
        "/hub/read-models/worktrees/repo--feature/detail",
        params={
            "ticket_limit": 5,
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
    assert len(payload["scopedTickets"]) == 5
    assert {ticket["workspace_id"] for ticket in payload["scopedTickets"]} == {
        "repo--feature"
    }
    assert payload["ticketWindow"]["limit"] == 5


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
    assert payload["legacyTicket"]["workspace_id"] == hub_env.repo_id
    assert {ticket["workspace_id"] for ticket in payload["scopedTickets"]} == {
        hub_env.repo_id
    }
