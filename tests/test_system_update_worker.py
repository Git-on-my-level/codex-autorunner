from __future__ import annotations

import json
import logging
import time
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
        ("chat", "chat"),
        ("chat-apps", "chat"),
        ("telegram", "telegram"),
        ("tg", "telegram"),
        ("discord", "discord"),
        ("dc", "discord"),
    ],
)
def test_normalize_update_target(raw: str | None, expected: str) -> None:
    assert system._normalize_update_target(raw) == expected


def test_available_update_target_options_web_only_when_no_chat_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        system.update_core,
        "_chat_target_active",
        lambda **_kwargs: False,
    )
    options = system._available_update_target_options(
        raw_config={
            "telegram_bot": {"enabled": False},
            "discord_bot": {"enabled": False},
        },
        update_backend="systemd-user",
        linux_service_names={"hub": "car-hub"},
    )
    assert options == (("web", "Web only"),)
    assert (
        system._default_update_target(
            raw_config={
                "telegram_bot": {"enabled": False},
                "discord_bot": {"enabled": False},
            },
            update_backend="systemd-user",
            linux_service_names={"hub": "car-hub"},
        )
        == "web"
    )


def test_available_update_target_options_include_telegram_when_enableable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        system.update_core,
        "_chat_target_active",
        lambda **_kwargs: False,
    )
    monkeypatch.setenv("CAR_TELEGRAM_BOT_TOKEN", "token")
    options = system._available_update_target_options(
        raw_config={
            "telegram_bot": {
                "enabled": True,
                "bot_token_env": "CAR_TELEGRAM_BOT_TOKEN",
            },
            "discord_bot": {"enabled": False},
        },
        update_backend="systemd-user",
        linux_service_names={"hub": "car-hub"},
    )
    assert options == (
        ("both", "Web + Chat Apps"),
        ("web", "Web only"),
        ("telegram", "Telegram only"),
    )


def test_available_update_target_options_include_discord_when_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        system.update_core,
        "_chat_target_active",
        lambda **kwargs: kwargs.get("target") == "discord",
    )
    options = system._available_update_target_options(
        raw_config={
            "telegram_bot": {"enabled": False},
            "discord_bot": {"enabled": False},
        },
        update_backend="systemd-user",
        linux_service_names={"hub": "car-hub", "discord": "car-discord"},
    )
    assert options == (
        ("both", "Web + Chat Apps"),
        ("web", "Web only"),
        ("discord", "Discord only"),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "auto"),
        ("", "auto"),
        ("AUTO", "auto"),
        ("launchd", "launchd"),
        ("systemd-user", "systemd-user"),
    ],
)
def test_normalize_update_backend(raw: str | None, expected: str) -> None:
    assert system._normalize_update_backend(raw) == expected


def test_resolve_update_backend_auto_linux(monkeypatch) -> None:
    monkeypatch.setattr(system.sys, "platform", "linux", raising=False)
    assert system._resolve_update_backend("auto") == "systemd-user"


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("launchd", ("git", "bash", "curl", "launchctl")),
        ("systemd-user", ("git", "bash", "curl", "systemctl")),
    ],
)
def test_required_update_commands(backend: str, expected: tuple[str, ...]) -> None:
    assert system._required_update_commands(backend) == expected


def test_update_lock_active_clears_stale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    lock_path = system._update_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 999999}), encoding="utf-8")

    monkeypatch.setattr(system, "_pid_is_running", lambda _pid: False)
    assert system._update_lock_active() is None
    assert not lock_path.exists()


def test_read_update_status_allows_recent_running_without_lock(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    status_path = system._update_status_path()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "status": "running",
                "message": "Update spawned.",
                "at": time.time(),
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(system.update_core, "_update_lock_active", lambda: None)

    payload = system._read_update_status()
    assert isinstance(payload, dict)
    assert payload["status"] == "running"


def test_read_update_status_marks_stale_running_without_lock(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    status_path = system._update_status_path()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "status": "running",
                "message": "Update started.",
                "at": time.time() - 60,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(system.update_core, "_update_lock_active", lambda: None)

    payload = system._read_update_status()
    assert isinstance(payload, dict)
    assert payload["status"] == "error"
    assert payload["previous_status"] == "running"


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
        update_backend="systemd-user",
        linux_hub_service_name="car-hub",
        linux_telegram_service_name="car-telegram",
        linux_discord_service_name="car-discord",
        notify_platform="discord",
        notify_context={"chat_id": "channel-1"},
    )

    status_path = system._update_status_path()
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "running"
    assert payload["notify_platform"] == "discord"
    assert payload["notify_context"] == {"chat_id": "channel-1"}
    assert "log_path" in payload
    cmd = calls["cmd"]
    assert "--repo-url" in cmd
    assert str(update_dir) in cmd
    assert "--backend" in cmd
    assert "systemd-user" in cmd
    assert "--hub-service-name" in cmd
    assert "car-hub" in cmd
    assert "--discord-service-name" in cmd
    assert "car-discord" in cmd


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


def test_system_update_worker_rejects_invalid_backend(
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
        update_target="web",
        update_backend="invalid-backend",
    )

    payload = json.loads(system._update_status_path().read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert "Unsupported update backend" in str(payload["message"])


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


@pytest.mark.parametrize(
    ("backend", "missing_cmd"),
    [
        ("launchd", "launchctl"),
        ("systemd-user", "systemctl"),
    ],
)
def test_system_update_worker_backend_specific_missing_command(
    tmp_path: Path, monkeypatch, backend: str, missing_cmd: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_which(cmd: str) -> str | None:
        if cmd == missing_cmd:
            return None
        return f"/usr/bin/{cmd}"

    monkeypatch.setattr(system.shutil, "which", fake_which)
    logger = logging.getLogger("test")
    update_dir = tmp_path / "update"

    system._system_update_worker(
        repo_url="https://example.com/repo.git",
        repo_ref="main",
        update_dir=update_dir,
        logger=logger,
        update_target="web",
        update_backend=backend,
    )

    payload = json.loads(system._update_status_path().read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert missing_cmd in str(payload["message"])


def test_system_update_worker_sets_helper_python_for_refresh_script(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    update_dir = tmp_path / "update"
    (update_dir / ".git").mkdir(parents=True)
    refresh_script = update_dir / "scripts" / "safe-refresh-local-linux-hub.sh"
    refresh_script.parent.mkdir(parents=True, exist_ok=True)
    refresh_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

    monkeypatch.setattr(system.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(system.update_core, "_is_valid_git_repo", lambda _path: True)
    monkeypatch.setattr(system.update_core, "_run_cmd", lambda *_args, **_kwargs: None)

    captured_env: dict[str, str] = {}

    class _Proc:
        def __init__(self) -> None:
            self.stdout: list[str] = []
            self.returncode = 0

        def wait(self) -> int:
            return 0

    def fake_popen(cmd, cwd, env, stdout, stderr, text):  # type: ignore[no-untyped-def]
        captured_env.update(env)
        return _Proc()

    monkeypatch.setattr(system.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(system.sys, "executable", "/opt/car/bin/python3", raising=False)
    logger = logging.getLogger("test")

    system._system_update_worker(
        repo_url="https://example.com/repo.git",
        repo_ref="main",
        update_dir=update_dir,
        logger=logger,
        update_target="web",
        update_backend="systemd-user",
        skip_checks=True,
        linux_discord_service_name="car-discord",
    )

    assert captured_env["HELPER_PYTHON"] == "/opt/car/bin/python3"
    assert captured_env["UPDATE_DISCORD_SERVICE_NAME"] == "car-discord"
