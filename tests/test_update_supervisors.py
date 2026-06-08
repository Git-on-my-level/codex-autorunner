from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.update.detect import SupervisorIdentity
from codex_autorunner.core.update.supervisors import (
    CommandAdapter,
    LaunchdAdapter,
    LayeredSupervisor,
    SignalAdapter,
    SystemdAdapter,
    UpdateServices,
    build_layered_supervisor,
    resolve_systemctl_sudo_prefix,
)


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_resolve_systemctl_sudo_prefix_auto_for_system_scope() -> None:
    assert resolve_systemctl_sudo_prefix(
        scope="system", configured="auto", uid=1000
    ) == [
        "sudo",
        "-n",
    ]
    assert (
        resolve_systemctl_sudo_prefix(scope="user", configured="auto", uid=1000) is None
    )
    assert (
        resolve_systemctl_sudo_prefix(scope="system", configured="false", uid=1000)
        is None
    )


def test_systemd_adapter_wraps_restart_with_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout: float) -> _FakeResult:
        calls.append(list(cmd))
        return _FakeResult()

    adapter = SystemdAdapter(
        scope="system",
        hub_service="car-hub.service",
        subprocess_runner=fake_run,
    )
    result = adapter.restart(UpdateServices(restart_hub=True))
    assert result.success is True
    assert calls[0][:3] == ["sudo", "-n", "systemctl"]
    assert calls[1][-1] == "car-hub.service"


def test_command_adapter_runs_configured_command() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout: float) -> _FakeResult:
        calls.append(list(cmd))
        return _FakeResult()

    adapter = CommandAdapter(restart_command=["/bin/true"], subprocess_runner=fake_run)
    result = adapter.restart(UpdateServices())
    assert result.success is True
    assert calls == [["/bin/true"]]


def test_signal_adapter_refuses_without_restart_policy() -> None:
    identity = SupervisorIdentity(
        backend="systemd-user",
        scope="user",
        unit_name="car-hub.service",
        label=None,
        hub_pid=1234,
        hub_root=None,
        exec_start_or_program=None,
        routes_through_car_wrapper=False,
        routes_through_current_venv=False,
        is_container=False,
    )

    def fake_systemctl(scope: str, unit_name: str, args) -> str:
        return "no"

    adapter = SignalAdapter(
        identity=identity,
        systemctl_reader=fake_systemctl,
    )
    result = adapter.restart(UpdateServices(restart_hub=True))
    assert result.success is False
    assert "Restart=" in (result.error or "")


def test_layered_supervisor_falls_through_to_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = SupervisorIdentity(
        backend="systemd-user",
        scope="user",
        unit_name="car-hub.service",
        label=None,
        hub_pid=999_999,
        hub_root=None,
        exec_start_or_program=None,
        routes_through_car_wrapper=False,
        routes_through_current_venv=False,
        is_container=False,
    )

    def failing_run(cmd, *, timeout: float) -> _FakeResult:
        return _FakeResult(returncode=1, stderr="boom")

    def fake_systemctl(scope: str, unit_name: str, args) -> str:
        return "always"

    monkeypatch.setattr(
        "codex_autorunner.core.update.supervisors.os.kill",
        lambda pid, sig: None,
    )

    layered = LayeredSupervisor(
        adapters=[
            SystemdAdapter(
                scope="user",
                hub_service="car-hub.service",
                subprocess_runner=failing_run,
            ),
            SignalAdapter(
                identity=identity,
                systemctl_reader=fake_systemctl,
            ),
        ]
    )
    result = layered.restart(UpdateServices(restart_hub=True))
    assert result.success is True
    assert result.method_used == "signal"


def test_launchd_adapter_kickstarts_selected_services() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout: float) -> _FakeResult:
        calls.append(list(cmd))
        return _FakeResult()

    adapter = LaunchdAdapter(
        label="com.codex.autorunner",
        domain="gui/501",
        telegram_label="com.codex.autorunner.telegram",
        subprocess_runner=fake_run,
    )
    result = adapter.restart(
        UpdateServices(restart_hub=True, restart_telegram=True, restart_discord=False)
    )
    assert result.success is True
    assert len(calls) == 2
    assert calls[0][-1] == "gui/501/com.codex.autorunner"


def test_build_layered_supervisor_orders_native_command_signal() -> None:
    identity = SupervisorIdentity(
        backend="systemd-user",
        scope="user",
        unit_name="car-hub.service",
        label=None,
        hub_pid=1,
        hub_root=Path("/tmp"),
        exec_start_or_program=None,
        routes_through_car_wrapper=False,
        routes_through_current_venv=False,
        is_container=False,
    )
    layered = build_layered_supervisor(
        identity=identity,
        restart_command=["/bin/true"],
    )
    assert len(layered.adapters) == 3
    assert isinstance(layered.adapters[0], SystemdAdapter)
    assert isinstance(layered.adapters[1], CommandAdapter)
    assert isinstance(layered.adapters[2], SignalAdapter)
