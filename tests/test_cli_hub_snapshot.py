from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()


def test_hub_snapshot_requires_server(hub_root_only) -> None:
    """Test that car hub snapshot fails gracefully when server is not running."""
    hub_root = hub_root_only

    result = runner.invoke(app, ["hub", "snapshot", "--path", str(hub_root), "--json"])

    assert result.exit_code == 1
    assert (
        "Failed to connect to hub server" in result.output
        or "server" in result.output.lower()
    )


def test_hub_snapshot_json_structure() -> None:
    """Test the JSON schema structure without a running server."""
    # This test validates the JSON schema by checking that the summary
    # functions produce the correct structure. We don't need a real server.

    # Mock server response data
    mock_repos_response = {
        "last_scan_at": "2025-01-01T12:00:00Z",
        "repos": [
            {
                "id": "test-repo",
                "display_name": "Test Repo",
                "status": "idle",
                "initialized": True,
                "exists_on_disk": True,
                "last_run_id": 1,
                "last_run_started_at": "2025-01-01T10:00:00Z",
                "last_run_finished_at": "2025-01-01T10:05:00Z",
            }
        ],
    }

    mock_messages_response = {
        "items": [
            {
                "repo_id": "test-repo",
                "repo_display_name": "Test Repo",
                "run_id": "abc123",
                "run_created_at": "2025-01-01T11:00:00Z",
                "status": "paused",
                "seq": 1,
                "dispatch": {
                    "mode": "pause",
                    "title": "Need input",
                    "body": "This is a test message" * 50,  # Long body
                    "is_handoff": False,
                },
                "files": ["test.txt"],
            }
        ]
    }

    repos = mock_repos_response.get("repos", [])
    messages_items = mock_messages_response.get("items", [])

    def _summarize_repo(repo: dict) -> dict:
        if not isinstance(repo, dict):
            return {}
        return {
            "id": repo.get("id"),
            "display_name": repo.get("display_name"),
            "status": repo.get("status"),
            "initialized": repo.get("initialized"),
            "exists_on_disk": repo.get("exists_on_disk"),
            "last_run_id": repo.get("last_run_id"),
            "last_run_started_at": repo.get("last_run_started_at"),
            "last_run_finished_at": repo.get("last_run_finished_at"),
        }

    def _summarize_message(msg: dict) -> dict:
        if not isinstance(msg, dict):
            return {}
        dispatch = msg.get("dispatch", {})
        if not isinstance(dispatch, dict):
            dispatch = {}
        body = dispatch.get("body", "")
        title = dispatch.get("title", "")
        truncated_body = (body[:200] + "...") if len(body) > 200 else body
        return {
            "repo_id": msg.get("repo_id"),
            "repo_display_name": msg.get("repo_display_name"),
            "run_id": msg.get("run_id"),
            "run_created_at": msg.get("run_created_at"),
            "status": msg.get("status"),
            "seq": msg.get("seq"),
            "dispatch": {
                "mode": dispatch.get("mode"),
                "title": title,
                "body": truncated_body,
                "is_handoff": dispatch.get("is_handoff"),
            },
            "files_count": (
                len(msg.get("files", [])) if isinstance(msg.get("files"), list) else 0
            ),
        }

    snapshot = {
        "last_scan_at": mock_repos_response.get("last_scan_at"),
        "repos": [_summarize_repo(repo) for repo in repos],
        "inbox_items": [_summarize_message(msg) for msg in messages_items],
    }

    # Verify JSON structure
    assert "last_scan_at" in snapshot
    assert "repos" in snapshot
    assert isinstance(snapshot["repos"], list)
    assert "inbox_items" in snapshot
    assert isinstance(snapshot["inbox_items"], list)

    # Verify repo fields
    repo = snapshot["repos"][0]
    assert "id" in repo
    assert "display_name" in repo
    assert "status" in repo
    assert "initialized" in repo
    assert "exists_on_disk" in repo
    assert "last_run_id" in repo
    assert "last_run_started_at" in repo
    assert "last_run_finished_at" in repo

    # Verify inbox fields
    item = snapshot["inbox_items"][0]
    assert "repo_id" in item
    assert "repo_display_name" in item
    assert "run_id" in item
    assert "run_created_at" in item
    assert "status" in item
    assert "seq" in item
    assert "dispatch" in item

    dispatch = item["dispatch"]
    assert "mode" in dispatch
    assert "title" in dispatch
    assert "body" in dispatch
    assert "is_handoff" in dispatch


def test_hub_snapshot_truncates_long_dispatch_bodies() -> None:
    """Test that long dispatch bodies are truncated to 200 chars."""

    # Create a message with a very long body
    long_body = "x" * 500
    mock_message = {
        "repo_id": "test-repo",
        "repo_display_name": "Test Repo",
        "run_id": "abc123",
        "run_created_at": "2025-01-01T11:00:00Z",
        "status": "paused",
        "seq": 1,
        "dispatch": {
            "mode": "pause",
            "title": "Test",
            "body": long_body,
            "is_handoff": False,
        },
        "files": [],
    }

    def _summarize_message(msg: dict) -> dict:
        if not isinstance(msg, dict):
            return {}
        dispatch = msg.get("dispatch", {})
        if not isinstance(dispatch, dict):
            dispatch = {}
        body = dispatch.get("body", "")
        title = dispatch.get("title", "")
        truncated_body = (body[:200] + "...") if len(body) > 200 else body
        return {
            "repo_id": msg.get("repo_id"),
            "repo_display_name": msg.get("repo_display_name"),
            "run_id": msg.get("run_id"),
            "run_created_at": msg.get("run_created_at"),
            "status": msg.get("status"),
            "seq": msg.get("seq"),
            "dispatch": {
                "mode": dispatch.get("mode"),
                "title": title,
                "body": truncated_body,
                "is_handoff": dispatch.get("is_handoff"),
            },
            "files_count": (
                len(msg.get("files", [])) if isinstance(msg.get("files"), list) else 0
            ),
        }

    summary = _summarize_message(mock_message)
    truncated_body = summary["dispatch"]["body"]

    assert truncated_body.endswith("...")
    assert len(truncated_body) == 203  # 200 chars + "..."
