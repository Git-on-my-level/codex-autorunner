from __future__ import annotations

import logging
import plistlib
import subprocess
from pathlib import Path

import pytest

from codex_autorunner.core.update.launchd import (
    LaunchdPlistManager,
    build_chat_plist_dict,
    discord_missing_env_names,
    discord_state,
    inject_opencode_path,
    normalize_chat_plist,
    normalize_hub_plist_text,
    normalize_process_limits,
    telegram_state,
)
from codex_autorunner.core.update.supervisors import (
    LaunchdManagedAdapter,
    UpdateServices,
)

LOGGER = logging.getLogger("test")
DESIRED = "/pipx/venvs/codex-autorunner.current/bin/codex-autorunner"


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_normalize_hub_plist_text_rewrites_serve_command() -> None:
    text = "<string>PATH=/x:$PATH; codex-autorunner hub serve --path /h</string>"
    out = normalize_hub_plist_text(text, DESIRED)
    assert out is not None
    assert f"{DESIRED} hub serve" in out
    assert "; codex-autorunner hub serve" not in out


def test_normalize_hub_plist_text_noop_when_already_current() -> None:
    text = f"<string>{DESIRED} hub serve --path /h</string>"
    assert normalize_hub_plist_text(text, DESIRED) is None


def test_normalize_hub_plist_text_raises_when_command_absent() -> None:
    with pytest.raises(ValueError):
        normalize_hub_plist_text("<string>something else</string>", DESIRED)


def test_inject_opencode_path_prepends_to_existing_path() -> None:
    plist = {"ProgramArguments": ["/bin/sh", "-lc", "PATH=/a:$PATH; run"]}
    assert inject_opencode_path(plist, "/opencode/bin") is True
    assert plist["ProgramArguments"][2] == "PATH=/opencode/bin:/a:$PATH; run"
    # Idempotent.
    assert inject_opencode_path(plist, "/opencode/bin") is False


def test_inject_opencode_path_when_no_path_prefix() -> None:
    plist = {"ProgramArguments": ["/bin/sh", "-lc", "run cmd"]}
    assert inject_opencode_path(plist, "/opencode/bin") is True
    assert plist["ProgramArguments"][2] == "PATH=/opencode/bin:$PATH; run cmd"


def test_normalize_process_limits_drops_number_of_processes() -> None:
    plist = {
        "SoftResourceLimits": {"NumberOfProcesses": 512, "NumberOfFiles": 4096},
        "HardResourceLimits": {"NumberOfProcesses": 512},
    }
    assert normalize_process_limits(plist) is True
    assert "NumberOfProcesses" not in plist["SoftResourceLimits"]
    assert plist["SoftResourceLimits"] == {"NumberOfFiles": 4096}
    # Empty section removed entirely.
    assert "HardResourceLimits" not in plist
    assert normalize_process_limits(plist) is False


def test_normalize_chat_plist_rewrites_telegram() -> None:
    plist = {
        "ProgramArguments": [
            "/bin/sh",
            "-lc",
            "PATH=/x:$PATH; codex-autorunner telegram start --path /h",
        ]
    }
    assert normalize_chat_plist(plist, DESIRED, "telegram") is True
    assert plist["ProgramArguments"][2] == (
        f"PATH=/x:$PATH; {DESIRED} telegram start --path /h"
    )


def test_normalize_chat_plist_raises_when_command_absent() -> None:
    plist = {"ProgramArguments": ["/bin/sh", "-lc", "echo nope"]}
    with pytest.raises(ValueError):
        normalize_chat_plist(plist, DESIRED, "discord")


def test_build_chat_plist_dict_shape() -> None:
    plist = build_chat_plist_dict(
        label="com.codex.autorunner.telegram",
        kind="telegram",
        hub_root=Path("/hub"),
        current_venv_link=Path("/pipx/venvs/codex-autorunner.current"),
        path_dirs=["/opencode/bin", "/local/bin"],
        log_path=Path("/hub/.codex-autorunner/codex-autorunner-telegram.log"),
    )
    assert plist["Label"] == "com.codex.autorunner.telegram"
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    cmd = plist["ProgramArguments"][2]
    assert "PATH=/opencode/bin:/local/bin:$PATH;" in cmd
    assert "telegram start --path /hub" in cmd


# --------------------------------------------------------------------------- #
# Chat-state detection
# --------------------------------------------------------------------------- #
def _write_config(hub_root: Path, body: str) -> None:
    (hub_root / ".codex-autorunner").mkdir(parents=True, exist_ok=True)
    (hub_root / ".codex-autorunner" / "config.yml").write_text(body, encoding="utf-8")


def test_telegram_state_from_config(tmp_path: Path) -> None:
    _write_config(tmp_path, "telegram_bot:\n  enabled: true\n")
    assert telegram_state(tmp_path, environ={}) == "enabled"
    _write_config(tmp_path, "telegram_bot:\n  enabled: false\n")
    assert telegram_state(tmp_path, environ={}) == "disabled"


def test_telegram_state_env_override(tmp_path: Path) -> None:
    _write_config(tmp_path, "telegram_bot:\n  enabled: true\n")
    assert telegram_state(tmp_path, environ={"ENABLE_TELEGRAM_BOT": "0"}) == "disabled"
    assert telegram_state(None, environ={"ENABLE_TELEGRAM_BOT": "1"}) == "enabled"


def test_discord_state_missing_env(tmp_path: Path) -> None:
    _write_config(tmp_path, "discord_bot:\n  enabled: true\n")
    assert discord_state(tmp_path, environ={}) == "missing_env"
    assert discord_missing_env_names(tmp_path, environ={}) == [
        "CAR_DISCORD_BOT_TOKEN",
        "CAR_DISCORD_APP_ID",
    ]
    env = {"CAR_DISCORD_BOT_TOKEN": "t", "CAR_DISCORD_APP_ID": "a"}
    assert discord_state(tmp_path, environ=env) == "enabled"


def test_discord_state_disabled(tmp_path: Path) -> None:
    _write_config(tmp_path, "discord_bot:\n  enabled: false\n")
    assert discord_state(tmp_path, environ={}) == "disabled"


# --------------------------------------------------------------------------- #
# Orchestration (LaunchdPlistManager) with a fake command runner
# --------------------------------------------------------------------------- #
class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.print_pid: int | None = None

    def __call__(self, cmd, *, timeout):  # noqa: ANN001
        self.calls.append(list(cmd))
        if cmd[:2] == ["launchctl", "print"]:
            stdout = f"\tpid = {self.print_pid}\n" if self.print_pid else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if cmd[:1] == ["pgrep"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == list(prefix) for call in self.calls)


def _hub_plist(tmp_path: Path) -> Path:
    path = tmp_path / "com.codex.autorunner.plist"
    with path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.codex.autorunner",
                "ProgramArguments": [
                    "/bin/sh",
                    "-lc",
                    "PATH=/x:$PATH; codex-autorunner hub serve --path /hub",
                ],
                "SoftResourceLimits": {"NumberOfProcesses": 256},
            },
            handle,
        )
    return path


def _manager(tmp_path: Path, runner: FakeRunner, **kwargs) -> LaunchdPlistManager:
    defaults = dict(
        label="com.codex.autorunner",
        hub_plist_path=_hub_plist(tmp_path),
        current_venv_link=Path("/pipx/venvs/codex-autorunner.current"),
        hub_root=tmp_path,
        uid=501,
        opencode_bin="/opencode/bin",
        path_dirs=("/opencode/bin", "/local/bin"),
        telegram_label="com.codex.autorunner.telegram",
        telegram_plist_path=tmp_path / "telegram.plist",
        discord_label="com.codex.autorunner.discord",
        discord_plist_path=tmp_path / "discord.plist",
        environ={},
        logger=LOGGER,
        command_runner=runner,
        sleep=lambda _s: None,
    )
    defaults.update(kwargs)
    return LaunchdPlistManager(**defaults)


def test_reload_hub_normalizes_plist_and_restarts(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = _manager(tmp_path, runner)
    result = manager.reload_hub()
    assert result.success
    text = manager.hub_plist_path.read_text(encoding="utf-8")
    assert f"{manager.desired_bin} hub serve" in text
    with manager.hub_plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    # OpenCode path injected; process-limit normalized away.
    assert "/opencode/bin" in plist["ProgramArguments"][2]
    assert "SoftResourceLimits" not in plist
    assert runner.ran("launchctl", "unload", "-w")
    assert runner.ran("launchctl", "load", "-w")
    assert runner.ran("launchctl", "kickstart", "-k")


def test_reload_telegram_enabled_creates_plist(tmp_path: Path) -> None:
    _write_config(tmp_path, "telegram_bot:\n  enabled: true\n")
    runner = FakeRunner()
    manager = _manager(tmp_path, runner)
    assert not manager.telegram_plist_path.exists()
    result = manager.reload_telegram()
    assert result.success
    assert manager.telegram_plist_path.exists()
    assert runner.ran("launchctl", "load", "-w")
    assert runner.ran("launchctl", "kickstart", "-k")


def test_reload_telegram_disabled_unloads_existing(tmp_path: Path) -> None:
    _write_config(tmp_path, "telegram_bot:\n  enabled: false\n")
    runner = FakeRunner()
    manager = _manager(tmp_path, runner)
    # Pre-create plist so the disable path unloads it.
    manager.telegram_plist_path.write_text("x", encoding="utf-8")
    result = manager.reload_telegram()
    assert result.success
    assert runner.ran("launchctl", "unload", "-w")
    assert not runner.ran("launchctl", "kickstart", "-k")


def test_reload_telegram_disabled_no_plist_is_noop(tmp_path: Path) -> None:
    _write_config(tmp_path, "telegram_bot:\n  enabled: false\n")
    runner = FakeRunner()
    manager = _manager(tmp_path, runner)
    result = manager.reload_telegram()
    assert result.success
    assert runner.calls == []


def test_kickstart_failure_surfaces_error(tmp_path: Path) -> None:
    class FailingKickstart(FakeRunner):
        def __call__(self, cmd, *, timeout):  # noqa: ANN001
            self.calls.append(list(cmd))
            if cmd[:2] == ["launchctl", "kickstart"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
            if cmd[:2] == ["launchctl", "print"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    runner = FailingKickstart()
    manager = _manager(tmp_path, runner)
    result = manager.reload_hub()
    assert not result.success
    assert "kickstart" in (result.error or "")


def test_engine_launchd_supervisor_uses_managed_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_autorunner.core.update.detect import SupervisorIdentity
    from codex_autorunner.core.update.engine import UpdateEngine, UpdateEngineConfig

    monkeypatch.setenv("HOME", str(tmp_path))
    identity = SupervisorIdentity(
        backend="launchd",
        scope=None,
        unit_name="com.codex.autorunner",
        label="com.codex.autorunner",
        hub_pid=42,
        hub_root=tmp_path,
        exec_start_or_program=f"{DESIRED} hub serve",
        routes_through_car_wrapper=False,
        routes_through_current_venv=True,
        is_container=False,
    )
    config = UpdateEngineConfig(
        repo_url="https://example/repo.git",
        repo_ref="main",
        update_dir=tmp_path / "cache",
    )
    engine = UpdateEngine(config, logger=LOGGER)
    supervisor = engine._build_supervisor(identity, {"hub": "com.codex.autorunner"})
    assert isinstance(supervisor.adapters[0], LaunchdManagedAdapter)


def test_telegram_cli_health_invokes_state_check_and_health(
    tmp_path: Path,
) -> None:
    from codex_autorunner.core.update.health import HealthChecker

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    console = bin_dir / "codex-autorunner"
    console.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    console.chmod(0o755)
    checker = HealthChecker(logger=LOGGER, timeout=5.0)
    assert checker.telegram_cli_healthy(
        python_bin=bin_dir / "python", hub_root=tmp_path
    )


def test_managed_adapter_aggregates_services(tmp_path: Path) -> None:
    _write_config(tmp_path, "telegram_bot:\n  enabled: false\n")
    runner = FakeRunner()
    manager = _manager(tmp_path, runner, discord_label=None, discord_plist_path=None)
    adapter = LaunchdManagedAdapter(manager)
    result = adapter.restart(
        UpdateServices(restart_hub=True, restart_telegram=True, restart_discord=False)
    )
    assert result.success
    assert result.method_used == "launchd"
