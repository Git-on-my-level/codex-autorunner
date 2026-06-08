from __future__ import annotations

import os
import subprocess
import sys
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


def test_update_runner_resolves_from_source_before_stale_flat_module(
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old"
    old_core = old_root / "codex_autorunner" / "core"
    old_core.mkdir(parents=True)
    (old_root / "codex_autorunner" / "__init__.py").write_text("", encoding="utf-8")
    (old_core / "__init__.py").write_text("", encoding="utf-8")
    (old_core / "update.py").write_text("LEGACY = True\n", encoding="utf-8")

    repo_src = Path(__file__).resolve().parents[1] / "src"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(repo_src), str(old_root)]),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "codex_autorunner.core.update.runner",
            "--help",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Run codex-autorunner update worker" in result.stdout
