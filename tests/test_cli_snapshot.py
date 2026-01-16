from pathlib import Path

import pytest

from codex_autorunner.cli import app

pytest.importorskip("typer")
CliRunner = pytest.importorskip("typer.testing").CliRunner


runner = CliRunner()


def test_snapshot_invokes_generate_snapshot(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_autorunner.core.snapshot import SnapshotService

    # Update config to have app_server.command
    config_path = repo / "codex-autorunner.yml"
    if config_path.exists():
        content = config_path.read_text()
        if "app_server:" not in content:
            with config_path.open("a") as f:
                f.write("\napp_server:\n  command: ['echo', 'hello']\n")

    calls = []

    async def mock_generate(self):
        calls.append(self)

    monkeypatch.setattr(SnapshotService, "generate_snapshot", mock_generate)

    result = runner.invoke(app, ["snapshot", "--repo", str(repo)])
    assert result.exit_code == 0, result.stdout
    assert calls, "expected generate_snapshot to be called"
    assert "Snapshot written to .codex-autorunner/SNAPSHOT.md" in result.stdout
