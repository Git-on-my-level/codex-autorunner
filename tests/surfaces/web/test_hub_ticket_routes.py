from pathlib import Path

from fastapi.testclient import TestClient
from tests.support.web_test_helpers import create_test_hub_supervisor
from tests.surfaces.web._hub_test_support import init_git_repo, seed_flow_run

from codex_autorunner.core.flows import FlowRunStatus
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
    assert rows["tkt_repo_001"]["frontmatter_yaml"].startswith(
        'ticket_id: "tkt_repo_001"\n'
    )
    assert rows["tkt_repo_001"]["hub_root"] == str(hub_root)
    assert rows["tkt_repo_001"]["workspace_root"] == str(base.path)
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


def test_hub_tickets_enriches_current_ticket_with_live_run_metadata(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    base = supervisor.create_repo("base")
    run_id = "22222222-2222-4222-8222-222222222222"
    _write_ticket(
        base.path,
        "TICKET-001.md",
        'ticket_id: "tkt_base"\ntitle: "Base"\nagent: codex\ndone: false\n',
        "Base body",
    )
    seed_flow_run(
        base.path,
        run_id=run_id,
        status=FlowRunStatus.RUNNING,
        diff_events=[
            {
                "ticket_id": "tkt_base",
                "ticket_path": ".codex-autorunner/tickets/TICKET-001.md",
                "insertions": 5,
                "deletions": 2,
                "files_changed": 1,
            },
            {
                "ticket_id": "tkt_base",
                "ticket_path": ".codex-autorunner/tickets/TICKET-001.md",
                "insertions": 3,
                "deletions": 0,
                "files_changed": 2,
            },
        ],
        started_at="2026-03-13T08:00:00Z",
        state={
            "ticket_engine": {
                "current_ticket": ".codex-autorunner/tickets/TICKET-001.md",
                "status": "running",
            }
        },
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/tickets?repo=base")
    assert response.status_code == 200

    row = response.json()["tickets"][0]
    assert row["run_id"] == run_id
    assert row["status"] == "running"
    assert row["diff_stats"] == {
        "insertions": 8,
        "deletions": 2,
        "files_changed": 3,
    }
    assert row["duration_seconds"] is not None


def test_hub_tickets_marks_lint_errors_invalid_not_failed(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    base = supervisor.create_repo("base")
    _write_ticket(
        base.path,
        "TICKET-001.md",
        'title: "Needs repair"\ndone: false\n',
        "Base body",
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/tickets?repo=base")
    assert response.status_code == 200

    row = response.json()["tickets"][0]
    assert row["errors"]
    assert row["status"] == "invalid"


def test_hub_tickets_marks_duplicate_ticket_numbers_invalid(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    base = supervisor.create_repo("base")
    for name, title in (
        ("TICKET-001-alpha.md", "Alpha"),
        ("TICKET-001-beta.md", "Beta"),
    ):
        _write_ticket(
            base.path,
            name,
            f'ticket_id: "{title.lower()}"\ntitle: "{title}"\nagent: codex\ndone: false\n',
            "Body",
        )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/tickets?repo=base")
    assert response.status_code == 200

    rows = response.json()["tickets"]
    assert [row["status"] for row in rows] == ["invalid", "invalid"]
    assert all(
        any("Duplicate ticket index 001" in error for error in row["errors"])
        for row in rows
    )


def test_hub_tickets_repo_and_worktree_filters_do_not_mix_owners(
    tmp_path: Path,
) -> None:
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
    assert [row["ticket_id"] for row in rows] == ["tkt_base"]
    assert rows[0]["workspace_kind"] == "repo"

    worktree_response = client.get(f"/hub/tickets?worktree={worktree.id}")
    assert worktree_response.status_code == 200
    worktree_rows = worktree_response.json()["tickets"]
    assert [row["ticket_id"] for row in worktree_rows] == ["tkt_child"]
    assert worktree_rows[0]["workspace_kind"] == "worktree"


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
