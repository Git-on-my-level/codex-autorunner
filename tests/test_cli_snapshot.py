from pathlib import Path

import pytest

pytest.importorskip("typer")
CliRunner = pytest.importorskip("typer.testing").CliRunner

from codex_autorunner.cli import app


runner = CliRunner()


def test_snapshot_defaults_to_from_scratch_when_missing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def fake_load_snapshot(engine):
        return None

    def fake_generate_snapshot(engine, *, mode: str, max_chars: int, audience: str):
        calls.append((mode, max_chars, audience))
        return type(
            "R",
            (),
            {"state": {"mode": mode}, "truncated": False},
        )()

    monkeypatch.setattr("codex_autorunner.cli.load_snapshot", fake_load_snapshot)
    monkeypatch.setattr("codex_autorunner.cli.generate_snapshot", fake_generate_snapshot)

    result = runner.invoke(app, ["snapshot", "--repo", str(repo)])
    assert result.exit_code == 0, result.stdout
    assert calls == [("from_scratch", 12000, "overview")]


def test_snapshot_defaults_to_incremental_when_present(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def fake_load_snapshot(engine):
        return "# Existing snapshot\n"

    def fake_generate_snapshot(engine, *, mode: str, max_chars: int, audience: str):
        calls.append((mode, max_chars, audience))
        return type(
            "R",
            (),
            {"state": {"mode": mode}, "truncated": False},
        )()

    monkeypatch.setattr("codex_autorunner.cli.load_snapshot", fake_load_snapshot)
    monkeypatch.setattr("codex_autorunner.cli.generate_snapshot", fake_generate_snapshot)

    result = runner.invoke(app, ["snapshot", "--repo", str(repo)])
    assert result.exit_code == 0, result.stdout
    assert calls == [("incremental", 12000, "overview")]


def test_snapshot_from_scratch_flag_overrides_incremental(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def fake_load_snapshot(engine):
        return "# Existing snapshot\n"

    def fake_generate_snapshot(engine, *, mode: str, max_chars: int, audience: str):
        calls.append((mode, max_chars, audience))
        return type(
            "R",
            (),
            {"state": {"mode": mode}, "truncated": False},
        )()

    monkeypatch.setattr("codex_autorunner.cli.load_snapshot", fake_load_snapshot)
    monkeypatch.setattr("codex_autorunner.cli.generate_snapshot", fake_generate_snapshot)

    result = runner.invoke(app, ["snapshot", "--repo", str(repo), "--from-scratch"])
    assert result.exit_code == 0, result.stdout
    assert calls == [("from_scratch", 12000, "overview")]
