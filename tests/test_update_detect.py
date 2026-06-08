from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.update.detect import (
    detect_container,
    detect_supervisor_identity,
    guard_self_update,
    parse_cgroup_identity,
    resolve_backend,
    verify_cutover_routing,
)


@pytest.mark.parametrize(
    ("cgroup_text", "backend", "scope", "unit"),
    [
        (
            "0::/system.slice/car-hub.service",
            "systemd-system",
            "system",
            "car-hub.service",
        ),
        (
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/car-hub.service",
            "systemd-user",
            "user",
            "car-hub.service",
        ),
        (
            "12:memory:/docker/abc123",
            "none",
            None,
            None,
        ),
    ],
)
def test_parse_cgroup_identity(
    cgroup_text: str,
    backend: str,
    scope: str | None,
    unit: str | None,
) -> None:
    assert parse_cgroup_identity(cgroup_text) == (backend, scope, unit)


def test_detect_container_from_dockerenv_and_env() -> None:
    assert detect_container(
        cgroup_text="0::/",
        dockerenv_exists=lambda: True,
        environ={},
    )
    assert detect_container(
        cgroup_text="0::/",
        dockerenv_exists=lambda: False,
        environ={"CONTAINER": "true"},
    )


def test_detect_supervisor_identity_uses_hint_and_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    current = tmp_path / "codex-autorunner.current"
    current.symlink_to(tmp_path / "venv")
    wrapper = tmp_path / "car"

    identity = detect_supervisor_identity(
        hint={
            "backend": "systemd-user",
            "scope": "user",
            "unit_name": "car-hub.service",
            "exec_start_or_program": f"{wrapper} serve",
            "hub_root": str(tmp_path),
        },
        current_venv_link=current,
        car_wrapper_path=wrapper,
        cgroup_reader=lambda: "0::/user.slice/user-1000.slice/user@1000.service/app.slice/car-hub.service",
    )
    assert identity.backend == "systemd-user"
    assert identity.unit_name == "car-hub.service"
    assert identity.routes_through_car_wrapper is True
    assert identity.hub_root == tmp_path


def test_resolve_backend_auto_prefers_identity() -> None:
    from codex_autorunner.core.update.detect import SupervisorIdentity

    identity = SupervisorIdentity(
        backend="systemd-system",
        scope="system",
        unit_name="car-hub.service",
        label=None,
        hub_pid=1,
        hub_root=None,
        exec_start_or_program=None,
        routes_through_car_wrapper=False,
        routes_through_current_venv=False,
        is_container=False,
    )
    assert resolve_backend("auto", identity) == "systemd-system"
    assert resolve_backend("launchd", identity) == "launchd"


def test_guard_self_update_refuses_ephemeral_container() -> None:
    from codex_autorunner.core.update.detect import SupervisorIdentity

    identity = SupervisorIdentity(
        backend="none",
        scope=None,
        unit_name=None,
        label=None,
        hub_pid=1,
        hub_root=None,
        exec_start_or_program=None,
        routes_through_car_wrapper=False,
        routes_through_current_venv=False,
        is_container=True,
    )
    with pytest.raises(RuntimeError, match="ephemeral container"):
        guard_self_update(identity)


def test_verify_cutover_routing_requires_wrapper_or_venv() -> None:
    from codex_autorunner.core.update.detect import SupervisorIdentity

    ok_identity = SupervisorIdentity(
        backend="systemd-user",
        scope="user",
        unit_name="car-hub.service",
        label=None,
        hub_pid=1,
        hub_root=None,
        exec_start_or_program="/usr/local/bin/car serve",
        routes_through_car_wrapper=True,
        routes_through_current_venv=False,
        is_container=False,
    )
    assert verify_cutover_routing(ok_identity) == (True, "")

    bad_identity = SupervisorIdentity(
        backend="systemd-user",
        scope="user",
        unit_name="car-hub.service",
        label=None,
        hub_pid=1,
        hub_root=None,
        exec_start_or_program="/usr/bin/python -m codex_autorunner",
        routes_through_car_wrapper=False,
        routes_through_current_venv=False,
        is_container=False,
    )
    ok, remediation = verify_cutover_routing(bad_identity)
    assert ok is False
    assert "car wrapper" in remediation
