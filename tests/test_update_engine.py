from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from codex_autorunner.core.update import engine as update_engine
from codex_autorunner.core.update.cutover import CutoverError, CutoverManager
from codex_autorunner.core.update.detect import SupervisorIdentity
from codex_autorunner.core.update.engine import (
    UpdateEngine,
    UpdateEngineConfig,
    resolve_pipx_layout,
)
from codex_autorunner.core.update.supervisors import RestartResult, UpdateServices

_PIPX_ENV_VARS = (
    "PIPX_ROOT",
    "PIPX_VENV",
    "CURRENT_VENV_LINK",
    "PREV_VENV_LINK",
    "LOCAL_BIN",
    "CAR_WRAPPER_PATH",
)


def _clear_pipx_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PIPX_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _make_pipx_install(root: Path, local_bin: Path) -> Path:
    venv = root / "venvs" / "codex-autorunner"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    car = bin_dir / "car"
    car.write_text("#!/bin/sh\n", encoding="utf-8")
    car.chmod(0o755)
    local_bin.mkdir(parents=True)
    (local_bin / "car").symlink_to(car)
    return venv


def test_resolve_pipx_layout_detects_share_pipx_from_car_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_pipx_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(update_engine.sys, "executable", str(tmp_path / "python"))
    pipx_root = tmp_path / ".local" / "share" / "pipx"
    venv = _make_pipx_install(pipx_root, tmp_path / ".local" / "bin")

    layout = resolve_pipx_layout()

    assert layout.pipx_root == pipx_root
    assert layout.active_venv == venv
    assert layout.current_link == pipx_root / "venvs" / "codex-autorunner.current"
    assert layout.prev_link == pipx_root / "venvs" / "codex-autorunner.prev"


def test_resolve_pipx_layout_env_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_pipx_env(monkeypatch)
    env_root = tmp_path / "env-pipx"
    env_venv = tmp_path / "explicit" / "codex-autorunner"
    current = tmp_path / "links" / "current"
    prev = tmp_path / "links" / "prev"
    wrapper = tmp_path / "bin" / "car"
    monkeypatch.setenv("PIPX_ROOT", str(env_root))
    monkeypatch.setenv("PIPX_VENV", str(env_venv))
    monkeypatch.setenv("CURRENT_VENV_LINK", str(current))
    monkeypatch.setenv("PREV_VENV_LINK", str(prev))
    monkeypatch.setenv("CAR_WRAPPER_PATH", str(wrapper))

    layout = resolve_pipx_layout()

    assert layout.pipx_root == env_root
    assert layout.active_venv == env_venv
    assert layout.current_link == current
    assert layout.prev_link == prev


def test_resolve_pipx_layout_pipx_root_only_beats_conflicting_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_pipx_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(update_engine.sys, "executable", str(tmp_path / "python"))
    wrapper_root = tmp_path / ".local" / "share" / "pipx"
    _make_pipx_install(wrapper_root, tmp_path / ".local" / "bin")
    env_root = tmp_path / "custom-pipx"
    monkeypatch.setenv("PIPX_ROOT", str(env_root))

    layout = resolve_pipx_layout()

    assert layout.pipx_root == env_root
    assert layout.active_venv == env_root / "venvs" / "codex-autorunner"
    assert layout.current_link == env_root / "venvs" / "codex-autorunner.current"
    assert layout.prev_link == env_root / "venvs" / "codex-autorunner.prev"


def test_cutover_missing_default_target_fails_without_broken_current_link(
    tmp_path: Path,
) -> None:
    pipx_root = tmp_path / "pipx"
    current = pipx_root / "venvs" / "codex-autorunner.current"
    prev = pipx_root / "venvs" / "codex-autorunner.prev"
    missing = pipx_root / "venvs" / "codex-autorunner"
    manager = CutoverManager(
        current_venv_link=current,
        prev_venv_link=prev,
        pipx_root=pipx_root,
    )

    with pytest.raises(CutoverError, match="not a usable venv") as exc:
        manager.initialize_current_link(
            missing,
            detected_candidates=(missing, tmp_path / "other"),
        )

    message = str(exc.value)
    assert str(missing) in message
    assert str(tmp_path / "other") in message
    assert not current.exists()
    assert not current.is_symlink()


def test_resolve_pipx_layout_ignores_unusable_existing_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_pipx_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(update_engine.sys, "executable", str(tmp_path / "python"))
    unusable_root = tmp_path / ".local" / "share" / "pipx"
    unusable = unusable_root / "venvs" / "codex-autorunner"
    unusable.mkdir(parents=True)

    layout = resolve_pipx_layout()

    assert layout.pipx_root == tmp_path / ".local" / "pipx"
    assert (
        layout.active_venv
        == tmp_path / ".local" / "pipx" / "venvs" / "codex-autorunner"
    )
    assert unusable in layout.candidates


def test_resolve_pipx_layout_detects_existing_legacy_pipx_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_pipx_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(update_engine.sys, "executable", str(tmp_path / "python"))
    pipx_root = tmp_path / ".local" / "pipx"
    venv = _make_pipx_install(pipx_root, tmp_path / ".local" / "bin")

    layout = resolve_pipx_layout()

    assert layout.pipx_root == pipx_root
    assert layout.active_venv == venv


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
    _make_pipx_install(pipx_root, tmp_path / ".local" / "bin")
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
