from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

import codex_autorunner.core.locks as locks_module
import codex_autorunner.surfaces.web.routes.system as system
from codex_autorunner.core.update_targets import (
    update_target_command_choices,
    update_target_label_pairs,
    update_target_values,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "all"),
        ("", "all"),
        ("ALL", "all"),
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


def test_normalize_update_target_accepts_legacy_both_alias() -> None:
    assert system._normalize_update_target("both") == "all"


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
    assert options == (("web", "web"),)
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
        ("all", "all"),
        ("web", "web"),
        ("telegram", "telegram"),
    )
    definitions = system._available_update_target_definitions(
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
    assert definitions[0].description == "Web + Telegram"
    assert definitions[0].restart_notice == "The web UI and Telegram will restart."


def test_available_update_target_options_include_discord_when_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.core.update._facade._chat_target_active",
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
        ("all", "all"),
        ("web", "web"),
        ("discord", "discord"),
    )
    definitions = system._available_update_target_definitions(
        raw_config={
            "telegram_bot": {"enabled": False},
            "discord_bot": {"enabled": False},
        },
        update_backend="systemd-user",
        linux_service_names={"hub": "car-hub", "discord": "car-discord"},
    )
    assert definitions[0].description == "Web + Discord"
    assert definitions[0].restart_notice == "The web UI and Discord will restart."


def test_update_target_helpers_share_the_same_core_definitions() -> None:
    assert update_target_values(include_status=True) == (
        "all",
        "web",
        "chat",
        "telegram",
        "discord",
        "status",
    )
    assert update_target_label_pairs() == (
        ("all", "all"),
        ("web", "web"),
        ("chat", "chat"),
        ("telegram", "telegram"),
        ("discord", "discord"),
    )
    assert update_target_command_choices(include_status=True) == (
        {"name": "all", "value": "all"},
        {"name": "web", "value": "web"},
        {"name": "chat", "value": "chat"},
        {"name": "telegram", "value": "telegram"},
        {"name": "discord", "value": "discord"},
        {"name": "status", "value": "status"},
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "auto"),
        ("", "auto"),
        ("AUTO", "auto"),
        ("launchd", "launchd"),
        ("systemd-user", "systemd-user"),
        ("systemd-system", "systemd-system"),
        ("SYSTEMD-SYSTEM", "systemd-system"),
    ],
)
def test_normalize_update_backend(raw: str | None, expected: str) -> None:
    assert system._normalize_update_backend(raw) == expected


def test_normalize_update_backend_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="systemd-system"):
        system._normalize_update_backend("nope")


def test_resolve_update_backend_auto_linux(monkeypatch) -> None:
    monkeypatch.setattr(system.sys, "platform", "linux", raising=False)
    assert system._resolve_update_backend("auto") == "systemd-user"


def test_resolve_update_backend_explicit_systemd_system(monkeypatch) -> None:
    monkeypatch.setattr(system.sys, "platform", "linux", raising=False)
    assert system._resolve_update_backend("systemd-system") == "systemd-system"


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("launchd", ("git", "bash", "curl", "launchctl")),
        ("systemd-user", ("git", "bash", "curl", "systemctl")),
        ("systemd-system", ("git", "bash", "curl", "systemctl")),
    ],
)
def test_required_update_commands(backend: str, expected: tuple[str, ...]) -> None:
    assert system._required_update_commands(backend) == expected


def test_update_lock_active_clears_stale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    lock_path = system._update_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 999999}), encoding="utf-8")

    monkeypatch.setattr(
        locks_module, "process_matches_identity", lambda *_a, **_k: False
    )
    assert system._update_lock_active() is None
    assert not lock_path.exists()


def test_update_lock_active_clears_pid_reuse_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    lock_path = system._update_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 1234}), encoding="utf-8")

    monkeypatch.setattr(
        locks_module, "process_matches_identity", lambda *_a, **_k: False
    )
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


def test_cleanup_update_build_artifacts_removes_packaging_outputs(
    tmp_path: Path,
) -> None:
    update_dir = tmp_path / "update"
    (update_dir / "build").mkdir(parents=True)
    (update_dir / "dist").mkdir()
    (update_dir / ".eggs").mkdir()
    (update_dir / "package.egg-info").mkdir()
    (update_dir / "src" / "codex_autorunner.egg-info").mkdir(parents=True)

    removed = system.update_core._cleanup_update_build_artifacts(
        update_dir, logging.getLogger("test")
    )

    assert removed == [
        "build",
        "dist",
        ".eggs",
        "package.egg-info",
        "src/codex_autorunner.egg-info",
    ]
    assert not (update_dir / "build").exists()
    assert not (update_dir / "dist").exists()
    assert not (update_dir / ".eggs").exists()
    assert not (update_dir / "package.egg-info").exists()
    assert not (update_dir / "src" / "codex_autorunner.egg-info").exists()


def test_refresh_failure_is_retryable_only_for_packaging_style_errors() -> None:
    assert system.update_core._refresh_failure_is_retryable(
        [
            "error: subprocess-exited-with-error",
            "Failed building wheel for codex-autorunner",
            "error: [Errno 2] No such file or directory: 'build/bdist...'",
        ]
    )
    assert not system.update_core._refresh_failure_is_retryable(
        ["Hub health check failed."]
    )


def test_update_cache_refresh_failure_is_retryable_detects_git_corruption() -> None:
    assert system.update_core._update_cache_refresh_failure_is_retryable(
        "fatal: unresolved deltas left after unpacking\nfatal: unpack-objects failed"
    )
    assert not system.update_core._update_cache_refresh_failure_is_retryable(
        "fatal: unable to access 'https://example.com/repo.git/': Could not resolve host"
    )


def test_spawn_update_process_writes_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    calls: dict[str, object] = {}

    class _FakeProc:
        def kill(self) -> None:
            return None

        def poll(self) -> int:
            return 0

    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["cmd"] = args[0] if args else kwargs.get("cmd")
        calls["cwd"] = kwargs.get("cwd") or (args[1] if len(args) > 1 else None)
        return _FakeProc()

    monkeypatch.setattr(
        "codex_autorunner.core.update._facade.subprocess.Popen",
        fake_popen,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.update._facade._capture_update_identity_hint",
        lambda: {"backend": "systemd-user", "hub_pid": 1},
    )

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
    assert payload["phase"] == "spawned"
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
    assert "codex_autorunner.core.update.runner" in cmd
    assert "--identity-hint" in cmd


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
    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.resolve_executable", lambda _cmd: None
    )
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

    def fake_resolve_executable(cmd: str) -> str | None:
        if cmd == missing_cmd:
            return None
        return f"/usr/bin/{cmd}"

    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.resolve_executable",
        fake_resolve_executable,
    )
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


def test_system_update_worker_delegates_to_update_engine(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    captured: dict[str, object] = {}

    class _FakeEngine:
        def __init__(self, config, *, logger, status_path=None, lock_path=None) -> None:
            captured["config"] = config
            captured["logger"] = logger

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.UpdateEngine",
        _FakeEngine,
    )
    logger = logging.getLogger("test")
    update_dir = tmp_path / "update"

    system._system_update_worker(
        repo_url="https://example.com/repo.git",
        repo_ref="main",
        update_dir=update_dir,
        logger=logger,
        update_target="web",
        update_backend="systemd-system",
        skip_checks=True,
        linux_hub_service_name="car-hub",
        linux_discord_service_name="car-discord",
        systemctl_sudo="true",
        allow_in_place=True,
    )

    assert captured.get("ran") is True
    config = captured["config"]
    assert config.linux_service_names["hub"] == "car-hub"
    assert config.linux_service_names["discord"] == "car-discord"
    assert config.systemctl_sudo == "true"
    assert config.allow_in_place is True


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ([], ""),
        (["", "  "], ""),
        (["first\n", "last line\n"], "last line"),
        (["only", ""], "only"),
    ],
)
def test_summarize_refresh_output(lines: list[str], expected: str) -> None:
    assert system.update_core._summarize_refresh_output(lines) == expected


def test_summarize_refresh_output_truncates_long_lines() -> None:
    summary = system.update_core._summarize_refresh_output(["x" * 1000])
    assert len(summary) <= system.update_core._REFRESH_OUTPUT_SUMMARY_MAX_CHARS
    assert summary.endswith("…")


def test_system_update_worker_reclones_when_cached_repo_fetch_is_corrupt(
    tmp_path: Path, monkeypatch
) -> None:
    from codex_autorunner.core.update import detect as update_detect
    from codex_autorunner.core.update import source as update_source

    monkeypatch.setenv("HOME", str(tmp_path))
    update_dir = tmp_path / "update"
    (update_dir / ".git").mkdir(parents=True)
    (update_dir / "stale.txt").write_text("stale", encoding="utf-8")

    monkeypatch.setattr(update_source, "is_valid_git_repo", lambda _path: True)
    monkeypatch.setattr(
        update_detect,
        "detect_supervisor_identity",
        lambda **_kwargs: update_detect.SupervisorIdentity(
            backend="systemd-user",
            scope="user",
            unit_name="car-hub",
            label=None,
            hub_pid=1,
            hub_root=tmp_path / "hub",
            exec_start_or_program="/home/user/.local/bin/car serve",
            routes_through_car_wrapper=True,
            routes_through_current_venv=False,
            is_container=False,
        ),
    )
    monkeypatch.setattr(
        update_detect,
        "guard_self_update",
        lambda _identity: None,
    )

    run_cmd_calls: list[tuple[list[str], Path]] = []
    failed_fetch = False

    def fake_run_git_cmd(cmd: list[str], cwd: Path) -> None:
        nonlocal failed_fetch
        run_cmd_calls.append((list(cmd), cwd))
        if (
            cmd == ["git", "fetch", "origin", "main"]
            and cwd == update_dir
            and not failed_fetch
        ):
            failed_fetch = True
            raise RuntimeError(
                "fatal: unresolved deltas left after unpacking\n"
                "fatal: unpack-objects failed"
            )

    monkeypatch.setattr(update_source, "run_git_cmd", fake_run_git_cmd)

    class _FakeEngine:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self) -> None:
            update_source.prepare_update_source(
                update_dir,
                "https://example.com/repo.git",
                "main",
                logging.getLogger("test"),
            )

    monkeypatch.setattr(
        "codex_autorunner.core.update.engine.UpdateEngine",
        _FakeEngine,
    )

    system._system_update_worker(
        repo_url="https://example.com/repo.git",
        repo_ref="main",
        update_dir=update_dir,
        logger=logging.getLogger("test"),
        update_target="web",
        update_backend="systemd-user",
        skip_checks=True,
        identity_hint={"hub_root": str(tmp_path / "hub")},
        allow_in_place=True,
    )

    assert failed_fetch is True
    assert not update_dir.exists()
    assert (
        ["git", "clone", "https://example.com/repo.git", str(update_dir)],
        update_dir.parent,
    ) in run_cmd_calls
