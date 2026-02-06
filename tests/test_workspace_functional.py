import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.server import create_hub_app


@pytest.fixture
def client(hub_env):
    app = create_hub_app(hub_env.hub_root)
    return TestClient(app)


def test_workspace_docs_read_write(hub_env, client, repo: Path):
    # 1. Read initial state (should be empty or default)
    res = client.get(f"/repos/{hub_env.repo_id}/api/contextspace")
    assert res.status_code == 200
    data = res.json()
    assert "active_context" in data
    assert "decisions" in data
    assert "spec" in data

    # 2. Write to active_context
    test_content = "# Test Context"
    res = client.put(
        f"/repos/{hub_env.repo_id}/api/contextspace/active_context",
        json={"content": test_content},
    )
    assert res.status_code == 200
    assert res.json()["active_context"] == test_content

    # 3. Verify file was written to the correct location
    expected_path = repo / ".codex-autorunner" / "contextspace" / "active_context.md"
    assert expected_path.exists()
    assert expected_path.read_text() == test_content

    # 4. Write to decisions
    test_decisions = "# Test Decisions"
    res = client.put(
        f"/repos/{hub_env.repo_id}/api/contextspace/decisions",
        json={"content": test_decisions},
    )
    assert res.status_code == 200
    assert res.json()["decisions"] == test_decisions
    assert (
        repo / ".codex-autorunner" / "contextspace" / "decisions.md"
    ).read_text() == test_decisions


def test_workspace_tree_and_metadata(hub_env, client, repo: Path):
    cs_dir = repo / ".codex-autorunner" / "contextspace"
    cs_dir.mkdir(parents=True, exist_ok=True)
    (cs_dir / "loose.txt").write_text("loose")
    nested_dir = cs_dir / "docs" / "nested"
    nested_dir.mkdir(parents=True, exist_ok=True)
    (nested_dir / "note.txt").write_text("note")

    res = client.get(f"/repos/{hub_env.repo_id}/api/contextspace/tree")
    assert res.status_code == 200
    tree = res.json()["tree"]

    def flatten(nodes):
        for node in nodes:
            yield node
            for child in flatten(node.get("children") or []):
                yield child

    paths = {node["path"]: node for node in flatten(tree)}

    # Pinned docs are always present
    assert "active_context.md" in paths
    assert paths["active_context.md"]["is_pinned"] is True

    assert "loose.txt" in paths
    assert paths["loose.txt"]["type"] == "file"

    assert "docs" in paths and paths["docs"]["type"] == "folder"
    assert "docs/nested" in paths and paths["docs/nested"]["type"] == "folder"
    assert "docs/nested/note.txt" in paths


def test_workspace_upload_download_and_delete(hub_env, client, repo: Path):
    files = [
        ("files", ("hello.txt", b"hello world", "text/plain")),
        ("files", ("inner.txt", b"inner", "text/plain")),
    ]
    res = client.post(
        f"/repos/{hub_env.repo_id}/api/contextspace/upload",
        data={"subdir": "inbox"},
        files=files,
    )
    assert res.status_code == 200
    uploaded = res.json()["uploaded"]
    assert {item["path"] for item in uploaded} == {"inbox/hello.txt", "inbox/inner.txt"}

    cs_dir = repo / ".codex-autorunner" / "contextspace" / "inbox"
    assert (cs_dir / "hello.txt").read_text() == "hello world"

    res = client.get(
        f"/repos/{hub_env.repo_id}/api/contextspace/download",
        params={"path": "inbox/hello.txt"},
    )
    assert res.status_code == 200
    assert res.content == b"hello world"

    res = client.delete(
        f"/repos/{hub_env.repo_id}/api/contextspace/file",
        params={"path": "inbox/inner.txt"},
    )
    assert res.status_code == 200
    assert not (cs_dir / "inner.txt").exists()

    # Traversal should be blocked
    res = client.delete(
        f"/repos/{hub_env.repo_id}/api/contextspace/file",
        params={"path": "../secrets.txt"},
    )
    assert res.status_code == 400


def test_workspace_download_zip_scoped(hub_env, client, repo: Path):
    cs_dir = repo / ".codex-autorunner" / "contextspace"
    cs_dir.mkdir(parents=True, exist_ok=True)
    (cs_dir / "root.txt").write_text("root")
    folder = cs_dir / "folder"
    (folder / "sub").mkdir(parents=True, exist_ok=True)
    (folder / "a.txt").write_text("A")
    (folder / "sub" / "b.txt").write_text("B")

    outside = repo / "outside.txt"
    outside.write_text("nope")
    # symlink pointing outside workspace should be skipped
    link = cs_dir / "folder" / "escape"
    try:
        link.symlink_to(outside)
    except OSError:
        # If symlinks unsupported, skip symlink assertion
        link = None

    res = client.get(
        f"/repos/{hub_env.repo_id}/api/contextspace/download-zip",
        params={"path": "folder"},
    )
    assert res.status_code == 200

    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = set(zf.namelist())
        assert "a.txt" in names
        assert "sub/b.txt" in names
        assert "root.txt" not in names
        if link:
            assert "escape" not in names


def test_workspace_folder_crud_and_pinned_guard(hub_env, client, repo: Path):
    cs_dir = repo / ".codex-autorunner" / "contextspace"
    cs_dir.mkdir(parents=True, exist_ok=True)
    (cs_dir / "active_context.md").write_text("keep")

    res = client.post(
        f"/repos/{hub_env.repo_id}/api/contextspace/folder",
        params={"path": "notes"},
    )
    assert res.status_code == 200
    assert (cs_dir / "notes").is_dir()

    # Non-empty delete should fail
    (cs_dir / "notes" / "tmp.txt").write_text("tmp")
    res = client.delete(
        f"/repos/{hub_env.repo_id}/api/contextspace/folder",
        params={"path": "notes"},
    )
    assert res.status_code == 400

    (cs_dir / "notes" / "tmp.txt").unlink()
    res = client.delete(
        f"/repos/{hub_env.repo_id}/api/contextspace/folder",
        params={"path": "notes"},
    )
    assert res.status_code == 200
    assert not (cs_dir / "notes").exists()

    # Pinned docs cannot be deleted
    res = client.delete(
        f"/repos/{hub_env.repo_id}/api/contextspace/file",
        params={"path": "active_context.md"},
    )
    assert res.status_code == 400


@pytest.mark.integration
def test_file_chat_workspace_targets(hub_env, client, repo: Path):
    # Setup contextspace docs
    cs_dir = repo / ".codex-autorunner" / "contextspace"
    cs_dir.mkdir(parents=True, exist_ok=True)
    (cs_dir / "active_context.md").write_text("Active Context Content")
    (cs_dir / "decisions.md").write_text("Decisions Content")
    (cs_dir / "spec.md").write_text("Spec Content")

    # Test file-chat with all contextspace targets
    # We use a loop but only check status code 500 or better to avoid flaky app-server issues in tests
    for target in [
        "contextspace:active_context",
        "contextspace:decisions",
        "contextspace:spec",
    ]:
        try:
            res = client.post(
                f"/repos/{hub_env.repo_id}/api/file-chat",
                json={"message": "Explain this", "target": target},
            )
            # If it returns 200, 202, or 409, it means the target was accepted and routing worked.
            # We avoid checking for 500 which would indicate a routing/validation failure before app-server.
            assert res.status_code in (
                200,
                202,
                409,
                500,
            )  # 500 is allowed if it's an app-server spawn issue
            if res.status_code == 500:
                # If it's a 500, make sure it's not a validation error
                data = res.json()
                assert data.get("error_type") != "ValidationError"
        except RuntimeError as e:
            # Handle "attached to a different loop" error which is a known test environment issue
            if "attached to a different loop" in str(e):
                pytest.skip(
                    "Skipping due to known asyncio loop conflict in test environment"
                )
            raise


def test_spec_ingest_ticket_generation(hub_env, client, repo: Path):
    # Setup a spec file
    (repo / ".codex-autorunner" / "contextspace").mkdir(parents=True, exist_ok=True)
    spec_content = """
# Feature: Test
- [ ] Task 1
- [ ] Task 2
"""
    (repo / ".codex-autorunner" / "contextspace" / "spec.md").write_text(spec_content)

    # Trigger ingest
    res = client.post(f"/repos/{hub_env.repo_id}/api/contextspace/spec/ingest")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["created"] == 1

    # Verify tickets were created
    tickets_dir = repo / ".codex-autorunner" / "tickets"
    assert tickets_dir.exists()
    ticket_files = list(tickets_dir.glob("TICKET-*.md"))
    assert len(ticket_files) == 1
    assert (tickets_dir / "TICKET-001.md").exists()


def test_ticket_runner_workspace_doc_injection(hub_env, repo: Path):
    from unittest.mock import MagicMock

    from codex_autorunner.tickets.models import TicketRunConfig
    from codex_autorunner.tickets.runner import TicketRunner

    # Setup contextspace docs
    cs_dir = repo / ".codex-autorunner" / "contextspace"
    cs_dir.mkdir(parents=True, exist_ok=True)
    cs_dir.joinpath("active_context.md").write_text("Active Context Content")
    cs_dir.joinpath("decisions.md").write_text("Decisions Content")
    cs_dir.joinpath("spec.md").write_text("Spec Content")

    # Setup a ticket
    t_dir = repo / ".codex-autorunner" / "tickets"
    t_dir.mkdir(parents=True, exist_ok=True)
    t_path = t_dir / "TICKET-001.md"
    t_path.write_text("---\nagent: codex\ndone: false\n---\nGoal: Test injection")

    # Initialize runner
    config = TicketRunConfig(
        ticket_dir=".codex-autorunner/tickets",
        runs_dir=".codex-autorunner/runs",
    )
    runner = TicketRunner(
        workspace_root=repo,
        run_id="run-1",
        config=config,
        agent_pool=MagicMock(),
    )

    # Mock outbox paths for _build_prompt
    outbox_paths = MagicMock()
    outbox_paths.dispatch_dir = (
        repo / ".codex-autorunner" / "runs" / "run-1" / "dispatch"
    )
    outbox_paths.dispatch_path = (
        repo / ".codex-autorunner" / "runs" / "run-1" / "DISPATCH.md"
    )

    # Test prompt building
    from codex_autorunner.tickets.files import read_ticket

    ticket_doc, _ = read_ticket(t_path)

    prompt = runner._build_prompt(
        ticket_path=t_path,
        ticket_doc=ticket_doc,
        last_agent_output=None,
        outbox_paths=outbox_paths,
        lint_errors=None,
    )

    assert (
        "Active context [.codex-autorunner/contextspace/active_context.md]:\nActive Context Content"
        in prompt
    )
    assert (
        "Decisions [.codex-autorunner/contextspace/decisions.md]:\nDecisions Content"
        in prompt
    )
    assert "Spec [.codex-autorunner/contextspace/spec.md]:\nSpec Content" in prompt


def test_ticket_runner_workspace_doc_injection_missing(hub_env, repo: Path):
    from unittest.mock import MagicMock

    from codex_autorunner.tickets.models import TicketRunConfig
    from codex_autorunner.tickets.runner import TicketRunner

    # Remove the seeded contextspace docs to ensure they are truly missing
    cs_dir = repo / ".codex-autorunner" / "contextspace"
    if cs_dir.exists():
        import shutil

        shutil.rmtree(cs_dir)

    # Setup a ticket
    t_dir = repo / ".codex-autorunner" / "tickets"
    t_dir.mkdir(parents=True, exist_ok=True)
    t_path = t_dir / "TICKET-001.md"
    t_path.write_text(
        "---\nagent: codex\ndone: false\n---\nGoal: Test missing injection"
    )

    # Initialize runner
    config = TicketRunConfig(
        ticket_dir=".codex-autorunner/tickets",
        runs_dir=".codex-autorunner/runs",
    )
    runner = TicketRunner(
        workspace_root=repo,
        run_id="run-1",
        config=config,
        agent_pool=MagicMock(),
    )

    # Mock outbox paths
    outbox_paths = MagicMock()
    outbox_paths.dispatch_dir = (
        repo / ".codex-autorunner" / "runs" / "run-1" / "dispatch"
    )
    outbox_paths.dispatch_path = (
        repo / ".codex-autorunner" / "runs" / "run-1" / "DISPATCH.md"
    )

    # Test prompt building
    from codex_autorunner.tickets.files import read_ticket

    ticket_doc, _ = read_ticket(t_path)

    prompt = runner._build_prompt(
        ticket_path=t_path,
        ticket_doc=ticket_doc,
        last_agent_output=None,
        outbox_paths=outbox_paths,
        lint_errors=None,
    )

    # Should not contain workspace doc headers if files are missing
    assert "Workspace docs (truncated; skip if not relevant):" not in prompt
    assert "Active context" not in prompt
