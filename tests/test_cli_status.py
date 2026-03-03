import json
import shlex

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_repo_files
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
    assert "recommended_actions" in parsed
    assert isinstance(parsed["recommended_actions"], list)
    assert parsed["recommended_actions"][0].startswith("car ticket-flow status --repo ")
    assert parsed["recommended_actions"][1].startswith("car sessions --repo ")

    assert parsed["repo"] == str(repo)


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
    assert "Recommended actions:" in output

    assert str(repo) in output


def test_status_recommendations_quote_repo_paths_with_spaces(tmp_path) -> None:
    repo = tmp_path / "repo with space"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    seed_repo_files(repo, git_required=False)

    result = runner.invoke(app, ["status", "--repo", str(repo), "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)

    quoted_repo = shlex.quote(str(repo))
    assert parsed["recommended_actions"][0] == (
        f"car ticket-flow status --repo {quoted_repo}"
    )
    assert parsed["recommended_actions"][1] == f"car sessions --repo {quoted_repo}"
