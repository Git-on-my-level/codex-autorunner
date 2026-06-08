from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from codex_autorunner.core.update.cutover import CutoverManager
from codex_autorunner.core.update.health import (
    HealthChecker,
    health_paths_for_base_path,
    wait_hub_before_chat,
)
from codex_autorunner.core.update.install import (
    StagedInstallError,
    _validate_wheel_contents,
    select_pip_extras,
)


def _make_venv(path: Path) -> None:
    bin_dir = path / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)


def test_select_pip_extras_defaults_to_browser() -> None:
    assert select_pip_extras() == "[browser]"


def test_select_pip_extras_voice_local(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    config_dir = hub_root / ".codex-autorunner"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yml").write_text(
        "repo_defaults:\n  voice:\n    provider: local_whisper\n",
        encoding="utf-8",
    )
    assert select_pip_extras(hub_root=hub_root) == "[browser,voice-local]"


def test_select_pip_extras_voice_mlx_from_env(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    env_dir = hub_root / ".codex-autorunner"
    env_dir.mkdir(parents=True)
    (env_dir / ".env").write_text(
        "CODEX_AUTORUNNER_VOICE_PROVIDER=mlx_whisper\n",
        encoding="utf-8",
    )
    assert select_pip_extras(hub_root=hub_root) == "[browser,voice-mlx]"


def test_validate_wheel_contents_requires_packages(tmp_path: Path) -> None:
    wheel_path = tmp_path / "empty.whl"
    import zipfile

    with zipfile.ZipFile(wheel_path, "w") as zf:
        zf.writestr("codex_autorunner/__init__.py", "")
    with pytest.raises(StagedInstallError, match="missing required packages"):
        _validate_wheel_contents(wheel_path)


def test_validate_wheel_contents_requires_update_runner(tmp_path: Path) -> None:
    wheel_path = tmp_path / "missing-runner.whl"
    import zipfile

    with zipfile.ZipFile(wheel_path, "w") as zf:
        for name in (
            "codex_autorunner/workspace/__init__.py",
            "codex_autorunner/tickets/__init__.py",
            "codex_autorunner/adapters/docker/__init__.py",
            "codex_autorunner/web_static/index.html",
            "codex_autorunner/core/update/__init__.py",
            "codex_autorunner/core/update_runner.py",
        ):
            zf.writestr(name, "")

    with pytest.raises(StagedInstallError, match="core/update/runner.py"):
        _validate_wheel_contents(wheel_path)


def test_cutover_manager_flip_and_rollback(tmp_path: Path) -> None:
    pipx_root = tmp_path / "pipx"
    venvs = pipx_root / "venvs"
    current = venvs / "codex-autorunner.current"
    prev = venvs / "codex-autorunner.prev"
    old = venvs / "codex-autorunner.old"
    candidate = venvs / "codex-autorunner.next-20260101-120000"
    _make_venv(old)
    _make_venv(candidate)

    manager = CutoverManager(
        current_venv_link=current,
        prev_venv_link=prev,
        pipx_root=pipx_root,
        keep_old_venvs=1,
        logger=logging.getLogger("test.cutover"),
    )
    resolved = manager.initialize_current_link(old)
    assert resolved == old.resolve()
    assert current.resolve() == old.resolve()

    manager.prepare(old)
    assert prev.resolve() == old.resolve()

    manager.flip_to(candidate)
    assert current.resolve() == candidate.resolve()

    manager.rollback_to(old)
    assert current.resolve() == old.resolve()


def test_cutover_manager_prune_keeps_recent_and_active(tmp_path: Path) -> None:
    pipx_root = tmp_path / "pipx"
    venvs = pipx_root / "venvs"
    current = venvs / "codex-autorunner.current"
    prev = venvs / "codex-autorunner.prev"
    live = venvs / "codex-autorunner.live"
    newest = venvs / "codex-autorunner.next-newest"
    newer = venvs / "codex-autorunner.next-newer"
    older = venvs / "codex-autorunner.next-older"
    oldest = venvs / "codex-autorunner.next-oldest"
    _make_venv(live)
    os.symlink(live, current)
    os.symlink(live, prev)
    for index, path in enumerate((oldest, older, newer, newest)):
        _make_venv(path)
        os.utime(path, (index + 1, index + 1))

    manager = CutoverManager(
        current_venv_link=current,
        prev_venv_link=prev,
        pipx_root=pipx_root,
        keep_old_venvs=2,
    )

    manager.prune_old_venvs()
    assert newest.exists()
    assert newer.exists()
    assert not oldest.exists()
    assert not older.exists()


def test_cutover_sync_car_wrapper_writes_dispatch_path(tmp_path: Path) -> None:
    pipx_root = tmp_path / "pipx"
    venvs = pipx_root / "venvs"
    current = venvs / "codex-autorunner.current"
    prev = venvs / "codex-autorunner.prev"
    target = venvs / "codex-autorunner.live"
    _make_venv(target)
    os.symlink(target, current)

    local_bin = tmp_path / "bin"
    manager = CutoverManager(
        current_venv_link=current,
        prev_venv_link=prev,
        pipx_root=pipx_root,
    )
    wrapper = manager.sync_car_wrapper(
        package_src=tmp_path,
        local_bin=local_bin,
    )
    text = wrapper.read_text(encoding="utf-8")
    assert str(current) in text
    assert "codex_autorunner.cli" in text
    assert os.access(wrapper, os.X_OK)


def test_health_checker_base_path_urls() -> None:
    checker = HealthChecker(port=8080, base_path="/car")
    assert checker.hub_health_url() == "http://127.0.0.1:8080/car/health"
    assert checker.static_asset_url() == "http://127.0.0.1:8080/car/_app/version.json"


def test_health_paths_for_base_path() -> None:
    assert health_paths_for_base_path("") == ("/health", "/_app/version.json")
    assert health_paths_for_base_path("/car") == (
        "/car/health",
        "/car/_app/version.json",
    )


def test_wait_hub_before_chat_disabled() -> None:
    checker = HealthChecker(port=4173)
    result = wait_hub_before_chat(checker, enabled=False)
    assert result.ok is True
    assert "disabled" in result.message.lower()


def test_health_checker_http_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float = 0) -> FakeResponse:
        calls.append(getattr(request, "full_url", str(request)))
        return FakeResponse()

    monkeypatch.setattr(
        "codex_autorunner.core.update.health.urllib.request.urlopen",
        fake_urlopen,
    )
    checker = HealthChecker(port=9000, base_path="/car", interval=0.01, timeout=1.0)
    result = checker.wait_for_hub_health(include_static=False)
    assert result.ok is True
    assert calls == ["http://127.0.0.1:9000/car/health"]
