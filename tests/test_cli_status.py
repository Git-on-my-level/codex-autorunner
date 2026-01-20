import json

from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()


def test_status_emits_valid_json(repo) -> None:
    """Test that car status --json emits valid JSON with required fields."""
    result = runner.invoke(app, ["status", "--repo", str(repo), "--json"])

    assert result.exit_code == 0

    output = result.output
    parsed = json.loads(output)

    assert "repo" in parsed
    assert "status" in parsed
    assert "last_run_id" in parsed
    assert "last_exit_code" in parsed
    assert "last_run_started_at" in parsed
    assert "last_run_finished_at" in parsed
    assert "runner_pid" in parsed
    assert "session_id" in parsed
    assert "session_record" in parsed
    assert "opencode_session_id" in parsed
    assert "opencode_record" in parsed
    assert "outstanding_todos" in parsed

    assert parsed["repo"] == str(repo)
    assert isinstance(parsed["outstanding_todos"], int)


def test_status_without_json_outputs_human_readable(repo) -> None:
    """Test that car status without --json still outputs human-readable text."""
    result = runner.invoke(app, ["status", "--repo", str(repo)])

    assert result.exit_code == 0

    output = result.output

    assert "Repo:" in output
    assert "Status:" in output
    assert "Last run id:" in output
    assert "Last exit code:" in output
    assert "Last start:" in output
    assert "Last finish:" in output
    assert "Runner pid:" in output
    assert "Outstanding TODO items:" in output

    assert str(repo) in output
