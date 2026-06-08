from __future__ import annotations

from pathlib import Path

from codex_autorunner.core import update_runner


def test_update_runner_skips_checks_by_default(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def fake_system_update_worker(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "codex_autorunner.core.update.runner._system_update_worker",
        fake_system_update_worker,
    )

    result = update_runner.main(
        [
            "--repo-url",
            "https://example.test/repo.git",
            "--update-dir",
            str(tmp_path / "updates"),
            "--log-path",
            str(tmp_path / "update.log"),
        ]
    )

    assert result == 0
    assert captured["skip_checks"] is True


def test_update_runner_allows_checks_explicitly(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def fake_system_update_worker(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "codex_autorunner.core.update.runner._system_update_worker",
        fake_system_update_worker,
    )

    result = update_runner.main(
        [
            "--repo-url",
            "https://example.test/repo.git",
            "--update-dir",
            str(tmp_path / "updates"),
            "--log-path",
            str(tmp_path / "update.log"),
            "--no-skip-checks",
        ]
    )

    assert result == 0
    assert captured["skip_checks"] is False
