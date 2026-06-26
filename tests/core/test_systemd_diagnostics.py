from __future__ import annotations

import subprocess
from types import SimpleNamespace

from codex_autorunner.core.diagnostics import systemd


def _hub_config(
    *,
    raw: dict[str, object] | None = None,
    backend: str = "systemd-user",
    port: int = 4517,
):
    return SimpleNamespace(
        raw=raw or {},
        update_backend=backend,
        update_linux_service_names={
            "hub": "car-hub",
            "telegram": "car-telegram",
            "discord": "car-discord",
        },
        server_port=port,
    )


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["cmd"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def test_systemd_doctor_warns_when_enabled_chat_unit_is_inactive(monkeypatch) -> None:
    monkeypatch.setattr(systemd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(systemd, "_listening_port_owner", lambda _port: None)

    def fake_run(cmd, **_kwargs):
        assert cmd[:2] == ["systemctl", "--user"]
        assert "car-discord" in cmd
        return _completed("LoadState=loaded\nActiveState=inactive\nSubState=dead\n")

    monkeypatch.setattr(systemd.subprocess, "run", fake_run)

    checks = systemd.linux_systemd_doctor_checks(
        _hub_config(raw={"discord_bot": {"enabled": True}})
    )
    by_id = {check.check_id: check for check in checks}

    assert by_id["systemd.discord.unit"].passed is False
    assert by_id["systemd.discord.unit"].severity == "warning"
    assert "inactive" in by_id["systemd.discord.unit"].message
    assert "Restart=always" in (by_id["systemd.discord.unit"].fix or "")


def test_systemd_doctor_accepts_active_chat_unit(monkeypatch) -> None:
    monkeypatch.setattr(systemd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(systemd, "_listening_port_owner", lambda _port: None)
    monkeypatch.setattr(
        systemd.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(
            "LoadState=loaded\nActiveState=active\nSubState=running\n"
        ),
    )

    checks = systemd.linux_systemd_doctor_checks(
        _hub_config(raw={"telegram_bot": {"enabled": True}})
    )
    by_id = {check.check_id: check for check in checks}

    assert by_id["systemd.telegram.unit"].passed is True
    assert by_id["systemd.telegram.unit"].severity == "info"


def test_systemd_doctor_warns_when_hub_port_owned_outside_cgroup(
    monkeypatch,
) -> None:
    monkeypatch.setattr(systemd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(systemd, "_listening_port_owner", lambda _port: {"pid": 1234})
    monkeypatch.setattr(
        systemd,
        "_process_cgroups",
        lambda _pid: ("0::/user.slice/user-1000.slice/session-2.scope",),
    )

    checks = systemd.linux_systemd_doctor_checks(_hub_config())
    by_id = {check.check_id: check for check in checks}

    assert by_id["systemd.hub.port_owner"].passed is False
    assert by_id["systemd.hub.port_owner"].severity == "warning"
    assert "outside expected service car-hub" in by_id["systemd.hub.port_owner"].message


def test_systemd_doctor_accepts_hub_port_owned_by_expected_cgroup(monkeypatch) -> None:
    monkeypatch.setattr(systemd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(systemd, "_listening_port_owner", lambda _port: {"pid": 1234})
    monkeypatch.setattr(
        systemd,
        "_process_cgroups",
        lambda _pid: ("0::/system.slice/car-hub.service",),
    )

    checks = systemd.linux_systemd_doctor_checks(_hub_config())
    by_id = {check.check_id: check for check in checks}

    assert by_id["systemd.hub.port_owner"].passed is True
    assert "car-hub" in by_id["systemd.hub.port_owner"].message


def test_systemd_doctor_skips_non_linux(monkeypatch) -> None:
    monkeypatch.setattr(systemd.platform, "system", lambda: "Darwin")

    assert systemd.linux_systemd_doctor_checks(_hub_config()) == []
