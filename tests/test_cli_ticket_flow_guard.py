from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.core.config import load_hub_config


def test_ticket_flow_start_rejects_unregistered_worktree(tmp_path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    hub_config = load_hub_config(hub_root)
    repo_root = hub_config.worktrees_root / "orphan--branch"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "start",
            "--repo",
            str(repo_root),
            "--hub",
            str(hub_root),
        ],
    )
    assert result.exit_code == 1
    first_line = result.output.splitlines()[0]
    assert (
        first_line
        == "Repo not registered in hub manifest. Run `car hub scan` or create via `car hub worktree create`."
    )
    assert str(hub_root) in result.output
    assert str(repo_root) in result.output
