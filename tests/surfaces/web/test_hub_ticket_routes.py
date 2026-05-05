from pathlib import Path

from fastapi.testclient import TestClient
from tests.support.web_test_helpers import create_test_hub_supervisor
from tests.surfaces.web._hub_test_support import init_git_repo

from codex_autorunner.server import create_hub_app


def _write_ticket(repo_root: Path, name: str, frontmatter: str, body: str) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / name).write_text(
        f"---\n{frontmatter}---\n\n{body}\n", encoding="utf-8"
    )


def test_hub_tickets_projects_repo_and_worktree_owned_queues(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    base = supervisor.create_repo("base")
    init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/tickets",
        start_point="HEAD",
    )

    _write_ticket(
        base.path,
        "TICKET-001.md",
        'ticket_id: "tkt_repo_001"\ntitle: "Repo ticket"\nagent: codex\ndone: false\n',
        "Repo body",
    )
    _write_ticket(
        worktree.path,
        "TICKET-002.md",
        'ticket_id: "tkt_worktree_002"\ntitle: "Worktree ticket"\nagent: codex\ndone: true\n',
        "Worktree body",
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/tickets")
    assert response.status_code == 200

    rows = {row["ticket_id"]: row for row in response.json()["tickets"]}
    assert (
        rows["tkt_repo_001"]
        | {
            "workspace_kind": "repo",
            "workspace_id": "base",
            "repo_id": "base",
            "worktree_id": None,
            "ticket_number": 1,
            "ticket_path": ".codex-autorunner/tickets/TICKET-001.md",
            "status": "idle",
        }
        == rows["tkt_repo_001"]
    )
    assert (
        rows["tkt_worktree_002"]
        | {
            "workspace_kind": "worktree",
            "workspace_id": worktree.id,
            "repo_id": "base",
            "worktree_id": worktree.id,
            "ticket_number": 2,
            "ticket_path": ".codex-autorunner/tickets/TICKET-002.md",
            "status": "done",
        }
        == rows["tkt_worktree_002"]
    )


def test_hub_tickets_filters_to_requested_owner(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    base = supervisor.create_repo("base")
    other = supervisor.create_repo("other")
    _write_ticket(
        base.path,
        "TICKET-001.md",
        'ticket_id: "tkt_base"\ntitle: "Base"\nagent: codex\ndone: false\n',
        "Base body",
    )
    _write_ticket(
        other.path,
        "TICKET-001.md",
        'ticket_id: "tkt_other"\ntitle: "Other"\nagent: codex\ndone: false\n',
        "Other body",
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/tickets?repo=base")
    assert response.status_code == 200

    assert [row["ticket_id"] for row in response.json()["tickets"]] == ["tkt_base"]


def test_hub_tickets_repo_filter_includes_child_worktrees(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    base = supervisor.create_repo("base")
    init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/tickets",
        start_point="HEAD",
    )
    _write_ticket(
        base.path,
        "TICKET-001.md",
        'ticket_id: "tkt_base"\ntitle: "Base"\nagent: codex\ndone: false\n',
        "Base body",
    )
    _write_ticket(
        worktree.path,
        "TICKET-002.md",
        'ticket_id: "tkt_child"\ntitle: "Child"\nagent: codex\ndone: false\n',
        "Child body",
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/tickets?repo=base")
    assert response.status_code == 200

    rows = response.json()["tickets"]
    assert [row["ticket_id"] for row in rows] == ["tkt_base", "tkt_child"]
    assert rows[1]["workspace_kind"] == "worktree"
    assert rows[1]["repo_id"] == "base"


def test_hub_ticket_projection_ids_are_workspace_qualified(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    first = supervisor.create_repo("first")
    second = supervisor.create_repo("second")
    for repo in (first, second):
        _write_ticket(
            repo.path,
            "TICKET-001.md",
            'title: "Duplicate path"\nagent: codex\ndone: false\n',
            "Body",
        )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/tickets")
    assert response.status_code == 200

    rows = response.json()["tickets"]
    assert len({row["id"] for row in rows}) == 2
    assert {row["id"].split(":", 2)[0] for row in rows} == {"repo"}
    assert {row["workspace_id"] for row in rows} == {"first", "second"}
