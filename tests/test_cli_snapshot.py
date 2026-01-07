from pathlib import Path

import pytest

from codex_autorunner.cli import app

pytest.importorskip("typer")
CliRunner = pytest.importorskip("typer.testing").CliRunner


runner = CliRunner()


def test_snapshot_invokes_generate_snapshot(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def fake_generate_snapshot(engine):
        calls.append(engine)
        return type(
            "R",
            (),
            {"state": {}, "truncated": False},
        )()

    monkeypatch.setattr(
        "codex_autorunner.cli.generate_snapshot", fake_generate_snapshot
    )

    result = runner.invoke(app, ["snapshot", "--repo", str(repo)])
    assert result.exit_code == 0, result.stdout
    assert calls, "expected generate_snapshot to be called"
    assert "Snapshot written to .codex-autorunner/SNAPSHOT.md" in result.stdout
