from __future__ import annotations

import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..config import HubConfig
from .types import DoctorCheck


@dataclass(frozen=True)
class _SystemdUnitState:
    name: str
    load_state: str
    active_state: str
    sub_state: str


def linux_systemd_doctor_checks(hub_config: HubConfig) -> list[DoctorCheck]:
    """Collect Linux/systemd supervision checks for always-on hub installs."""
    if platform.system().lower() != "linux":
        return []

    backend = str(getattr(hub_config, "update_backend", "auto") or "auto")
    if backend not in {"auto", "systemd-user", "systemd-system"}:
        return []

    service_names = _service_names(hub_config)
    checks: list[DoctorCheck] = []
    checks.extend(_chat_surface_unit_checks(hub_config, service_names, backend))
    checks.extend(_hub_port_owner_checks(hub_config, service_names))
    return checks


def _service_names(hub_config: HubConfig) -> dict[str, str]:
    raw_names = getattr(hub_config, "update_linux_service_names", {}) or {}
    names: dict[str, str] = {}
    if isinstance(raw_names, Mapping):
        for key, value in raw_names.items():
            text = str(value or "").strip()
            if text:
                names[str(key)] = text
    return names


def _chat_surface_unit_checks(
    hub_config: HubConfig, service_names: Mapping[str, str], backend: str
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    raw = hub_config.raw if isinstance(hub_config.raw, dict) else {}
    enabled_surfaces = {
        "telegram": _surface_enabled(raw, "telegram_bot"),
        "discord": _surface_enabled(raw, "discord_bot"),
    }

    for surface, enabled in enabled_surfaces.items():
        if not enabled:
            continue
        service_name = service_names.get(surface)
        if not service_name:
            continue
        state = _systemctl_show(service_name, backend=backend)
        if state is None:
            checks.append(
                DoctorCheck(
                    name=f"{surface.title()} systemd unit",
                    passed=True,
                    message=(
                        f"Could not inspect {service_name}; skipping systemd "
                        "chat-unit state check."
                    ),
                    check_id=f"systemd.{surface}.unit",
                    severity="warning",
                )
            )
            continue
        if state.load_state == "not-found":
            checks.append(
                DoctorCheck(
                    name=f"{surface.title()} systemd unit",
                    passed=False,
                    message=f"{service_name} is configured but not installed.",
                    check_id=f"systemd.{surface}.unit",
                    severity="warning",
                    fix=(
                        f"Install and enable the {service_name} systemd unit, or "
                        f"remove update.linux_service_names.{surface}."
                    ),
                )
            )
            continue
        if state.active_state == "active":
            checks.append(
                DoctorCheck(
                    name=f"{surface.title()} systemd unit",
                    passed=True,
                    message=f"{service_name} is active ({state.sub_state}).",
                    check_id=f"systemd.{surface}.unit",
                    severity="info",
                )
            )
            continue
        if state.active_state == "inactive":
            checks.append(
                DoctorCheck(
                    name=f"{surface.title()} systemd unit",
                    passed=False,
                    message=(
                        f"{service_name} is inactive ({state.sub_state}) even "
                        f"though {surface} is enabled."
                    ),
                    check_id=f"systemd.{surface}.unit",
                    severity="warning",
                    fix=(
                        f"Start {service_name} and use Restart=always with a "
                        "restart delay for always-on chat surfaces."
                    ),
                )
            )
            continue
        checks.append(
            DoctorCheck(
                name=f"{surface.title()} systemd unit",
                passed=False,
                message=f"{service_name} is {state.active_state} ({state.sub_state}).",
                check_id=f"systemd.{surface}.unit",
                severity="error" if state.active_state == "failed" else "warning",
                fix=f"Inspect with `systemctl status {service_name}`.",
            )
        )

    return checks


def _hub_port_owner_checks(
    hub_config: HubConfig, service_names: Mapping[str, str]
) -> list[DoctorCheck]:
    hub_service = service_names.get("hub")
    if not hub_service:
        return []
    port = getattr(hub_config, "server_port", None)
    if not isinstance(port, int) or port <= 0:
        return []

    owner = _listening_port_owner(port)
    if owner is None:
        return []
    pid = owner.get("pid")
    if not isinstance(pid, int):
        return []

    cgroups = _process_cgroups(pid)
    if not cgroups:
        return [
            DoctorCheck(
                name="Hub systemd ownership",
                passed=True,
                message=(
                    f"Port {port} is owned by pid {pid}, but cgroup ownership "
                    "could not be inspected."
                ),
                check_id="systemd.hub.port_owner",
                severity="warning",
            )
        ]

    if _cgroups_include_service(cgroups, hub_service):
        return [
            DoctorCheck(
                name="Hub systemd ownership",
                passed=True,
                message=f"Port {port} is owned by {hub_service} (pid {pid}).",
                check_id="systemd.hub.port_owner",
                severity="info",
            )
        ]

    cgroup_desc = "; ".join(cgroups)
    return [
        DoctorCheck(
            name="Hub systemd ownership",
            passed=False,
            message=(
                f"Port {port} is owned by pid {pid} outside expected service "
                f"{hub_service}: {cgroup_desc}"
            ),
            check_id="systemd.hub.port_owner",
            severity="warning",
            fix=(
                f"Stop the incidental hub process, then start {hub_service} so "
                "systemd owns the canonical hub."
            ),
        )
    ]


def _surface_enabled(raw: Mapping[str, object], section: str) -> bool:
    value = raw.get(section)
    return isinstance(value, Mapping) and value.get("enabled") is True


def _systemctl_show(service_name: str, *, backend: str) -> _SystemdUnitState | None:
    cmd = ["systemctl"]
    if backend in {"auto", "systemd-user"}:
        cmd.append("--user")
    cmd.extend(
        [
            "show",
            service_name,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--no-pager",
        ]
    )
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 and not proc.stdout:
        return None
    values: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value.strip()
    return _SystemdUnitState(
        name=service_name,
        load_state=values.get("LoadState", ""),
        active_state=values.get("ActiveState", ""),
        sub_state=values.get("SubState", ""),
    )


def _listening_port_owner(port: int) -> dict[str, object] | None:
    return _listening_port_owner_from_ss(port) or _listening_port_owner_from_lsof(port)


def _listening_port_owner_from_ss(port: int) -> dict[str, object] | None:
    try:
        proc = subprocess.run(
            ["ss", "-H", "-ltnp", f"sport = :{port}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"pid=(\d+)", proc.stdout or "")
    if not match:
        return None
    return {"pid": int(match.group(1)), "source": "ss"}


def _listening_port_owner_from_lsof(port: int) -> dict[str, object] | None:
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("p") and line[1:].isdigit():
            return {"pid": int(line[1:]), "source": "lsof"}
    return None


def _process_cgroups(pid: int) -> tuple[str, ...]:
    path = Path("/proc") / str(pid) / "cgroup"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    return tuple(line for line in lines if line.strip())


def _cgroups_include_service(cgroups: tuple[str, ...], service_name: str) -> bool:
    expected = (
        service_name if service_name.endswith(".service") else f"{service_name}.service"
    )
    return any(expected in line for line in cgroups)
