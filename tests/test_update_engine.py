from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from codex_autorunner.core.update.detect import SupervisorIdentity
from codex_autorunner.core.update.engine import UpdateEngine, UpdateEngineConfig
from codex_autorunner.core.update.supervisors import RestartResult, UpdateServices


def _linux_identity(*, hub_root: Path) -> SupervisorIdentity:
    return SupervisorIdentity(
        backend="systemd-user",
        scope="user",
        unit_name="car-hub",
        label=None,
        hub_pid=1234,
        hub_root=hub_root,
        exec_start_or_program="/home/user/.local/bin/car serve",
        routes_through_car_wrapper=True,
        routes_through_current_venv=False,
        is_container=False,
    )


def test_engine_refuses_cutover_without_routing_or_allow_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    status_path = tmp_path / ".codex-autorunner" / "update_status.json"
    lock_path = tmp_path / ".codex-autorunner" / "update.lock"

    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.prepare_update_source",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.resolve_executable",
        lambda _cmd: "/usr/bin/tool",
    )
    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.detect_supervisor_identity",
        lambda **_kwargs: SupervisorIdentity(
            backend="systemd-user",
            scope="user",
            unit_name="car-hub",
            label=None,
            hub_pid=1,
            hub_root=tmp_path / "hub",
            exec_start_or_program="/usr/bin/python3 -m codex_autorunner",
            routes_through_car_wrapper=False,
            routes_through_current_venv=False,
            is_container=False,
        ),
    )

    config = UpdateEngineConfig(
        repo_url="https://example.com/repo.git",
        repo_ref="main",
        update_dir=tmp_path / "cache",
        update_target="web",
        update_backend="systemd-user",
        allow_in_place=False,
    )
    UpdateEngine(
        config,
        logger=logging.getLogger("test"),
        status_path=status_path,
        lock_path=lock_path,
    ).run()

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert payload.get("error_type") == "cutover_routing_refused"


def test_engine_rollback_writes_status_on_health_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    status_path = tmp_path / ".codex-autorunner" / "update_status.json"
    lock_path = tmp_path / ".codex-autorunner" / "update.lock"
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.prepare_update_source",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.resolve_executable",
        lambda _cmd: "/usr/bin/tool",
    )
    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.detect_supervisor_identity",
        lambda **_kwargs: _linux_identity(hub_root=hub_root),
    )

    class _FakeInstaller:
        def __init__(self, **_kwargs) -> None:
            pass

        def create_staged_venv(self) -> Path:
            staged = tmp_path / "staged"
            staged.mkdir()
            (staged / "bin").mkdir(parents=True)
            return staged

        def build_wheel(self, *_args) -> Path:
            wheel = tmp_path / "pkg.whl"
            wheel.write_text("wheel", encoding="utf-8")
            return wheel

        def install_wheel(self, *_args) -> None:
            return None

        def validate_candidate(self, *_args) -> None:
            return None

    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.StagedInstaller",
        _FakeInstaller,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.ensure_playwright_chromium",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.snapshot_orchestration_db_transaction",
        lambda *_args, **_kwargs: type(
            "Txn",
            (),
            {"snapshot": type("Snap", (), {"snapshot_dir": str(tmp_path / "snap")})()},
        )(),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.UpdateEngine._refresh_managed_repos",
        lambda *_args, **_kwargs: None,
    )

    class _FakeSupervisor:
        def restart(self, _services: UpdateServices) -> RestartResult:
            return RestartResult(success=True, method_used="fake")

    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.UpdateEngine._build_supervisor",
        lambda *_args, **_kwargs: _FakeSupervisor(),
    )

    class _FailHealth:
        def wait_for_hub_health(self, **_kwargs):
            return type("R", (), {"ok": False, "message": "down"})()

    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.UpdateEngine._build_health_checker",
        lambda *_args, **_kwargs: _FailHealth(),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.wait_hub_before_chat",
        lambda *_args, **_kwargs: type("R", (), {"ok": True})(),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.restore_orchestration_db_snapshot",
        lambda *_args, **_kwargs: None,
    )

    pipx_root = tmp_path / ".local" / "pipx"
    venvs = pipx_root / "venvs"
    live = venvs / "codex-autorunner"
    live.mkdir(parents=True)
    (venvs / "codex-autorunner.current").symlink_to(live)
    monkeypatch.setenv("PIPX_ROOT", str(pipx_root))

    config = UpdateEngineConfig(
        repo_url="https://example.com/repo.git",
        repo_ref="main",
        update_dir=tmp_path / "cache",
        update_target="web",
        update_backend="systemd-user",
        identity_hint={"hub_root": str(hub_root)},
    )
    UpdateEngine(
        config,
        logger=logging.getLogger("test"),
        status_path=status_path,
        lock_path=lock_path,
    ).run()

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "rollback"
