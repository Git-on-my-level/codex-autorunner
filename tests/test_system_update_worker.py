from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import codex_autorunner.routes.system as system


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "both"),
        ("", "both"),
        ("ALL", "both"),
        ("web", "web"),
        ("ui", "web"),
        ("telegram", "telegram"),
        ("tg", "telegram"),
    ],
)
def test_normalize_update_target(raw: str | None, expected: str) -> None:
    assert system._normalize_update_target(raw) == expected


def test_update_lock_active_clears_stale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    lock_path = system._update_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 999999}), encoding="utf-8")

    monkeypatch.setattr(system, "_pid_is_running", lambda _pid: False)
    assert system._update_lock_active() is None
    assert not lock_path.exists()


def test_spawn_update_process_writes_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    calls: dict[str, object] = {}

    def fake_popen(cmd, cwd, start_new_session, stdout, stderr):  # type: ignore[no-untyped-def]
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        return object()

    monkeypatch.setattr(system.subprocess, "Popen", fake_popen)

    update_dir = tmp_path / "update"
    logger = logging.getLogger("test")
    system._spawn_update_process(
        repo_url="https://example.com/repo.git",
        repo_ref="main",
        update_dir=update_dir,
        logger=logger,
        update_target="web",
    )

    status_path = system._update_status_path()
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "running"
    assert "log_path" in payload
    cmd = calls["cmd"]
    assert "--repo-url" in cmd
    assert str(update_dir) in cmd


def test_system_update_worker_rejects_invalid_target(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    logger = logging.getLogger("test")
    update_dir = tmp_path / "update"

    system._system_update_worker(
        repo_url="https://example.com/repo.git",
        repo_ref="main",
        update_dir=update_dir,
        logger=logger,
        update_target="nope",
    )

    payload = json.loads(system._update_status_path().read_text(encoding="utf-8"))
    assert payload["status"] == "error"


def test_system_update_worker_missing_commands_releases_lock(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(system.shutil, "which", lambda _cmd: None)
    logger = logging.getLogger("test")
    update_dir = tmp_path / "update"

    system._system_update_worker(
        repo_url="https://example.com/repo.git",
        repo_ref="main",
        update_dir=update_dir,
        logger=logger,
        update_target="web",
    )

    payload = json.loads(system._update_status_path().read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert not system._update_lock_path().exists()
