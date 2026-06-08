from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

Backend = Literal["launchd", "systemd-user", "systemd-system", "none"]
Scope = Literal["user", "system"]

_CGROUP_UNIT_RE = re.compile(r"([^/]+\.service)(?:/|$)")
_SYSTEM_SLICE_RE = re.compile(r"/system\.slice/([^/]+\.service)")
_USER_SLICE_RE = re.compile(
    r"/user@(\d+)\.service(?:/[^/]+/([^/]+\.service)|/([^/]+\.service))"
)
_CONTAINER_MARKERS = ("docker", "containerd", "kubepods", "lxc", "podman")
_LAUNCHCTL_TIMEOUT_SECONDS = 5
_SYSTEMCTL_TIMEOUT_SECONDS = 5

CgroupReader = Callable[[], str]
LaunchctlReader = Callable[[int], Mapping[str, Any]]
SystemctlReader = Callable[[str, str, Sequence[str]], str]


@dataclass(frozen=True)
class SupervisorIdentity:
    backend: Backend
    scope: Scope | None
    unit_name: str | None
    label: str | None
    hub_pid: int | None
    hub_root: Path | None
    exec_start_or_program: str | None
    routes_through_car_wrapper: bool
    routes_through_current_venv: bool
    is_container: bool
    remediation: str | None = None


def default_cgroup_reader() -> str:
    path = Path("/proc/self/cgroup")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def default_launchctl_reader(pid: int) -> Mapping[str, Any]:
    import subprocess

    if pid <= 0 or sys.platform != "darwin":
        return {}
    try:
        result = subprocess.run(
            ["launchctl", "print", f"pid/{pid}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    return _parse_launchctl_print(result.stdout or "")


def default_systemctl_reader(scope: str, unit_name: str, args: Sequence[str]) -> str:
    import subprocess

    if not unit_name or sys.platform.startswith("linux") is False:
        return ""
    cmd = ["systemctl"]
    if scope == "user":
        cmd.append("--user")
    cmd.extend(args)
    cmd.append(unit_name)
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def detect_container(
    *,
    cgroup_text: str | None = None,
    dockerenv_exists: Callable[[], bool] | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    cgroup = cgroup_text if cgroup_text is not None else default_cgroup_reader()
    haystack = cgroup.lower()
    if any(marker in haystack for marker in _CONTAINER_MARKERS):
        return True
    dockerenv = dockerenv_exists or (lambda: Path("/.dockerenv").exists())
    if dockerenv():
        return True
    env = environ if environ is not None else os.environ
    container_env = str(env.get("CONTAINER", "")).strip().lower()
    if container_env in {"1", "true", "yes"}:
        return True
    return False


def parse_cgroup_identity(cgroup_text: str) -> tuple[Backend, Scope | None, str | None]:
    if not cgroup_text.strip():
        return "none", None, None

    for line in cgroup_text.splitlines():
        _, _, path = _split_cgroup_line(line)
        if not path or path == "/":
            continue

        system_match = _SYSTEM_SLICE_RE.search(path)
        if system_match:
            return "systemd-system", "system", system_match.group(1)

        user_match = _USER_SLICE_RE.search(path)
        if user_match:
            unit = user_match.group(2) or user_match.group(3)
            if unit:
                return "systemd-user", "user", unit

        if ".service" in path:
            unit_match = _CGROUP_UNIT_RE.search(path)
            if unit_match:
                unit = unit_match.group(1)
                if "/user.slice/" in path or "user@" in path:
                    return "systemd-user", "user", unit
                if "/system.slice/" in path:
                    return "systemd-system", "system", unit

    return "none", None, None


def detect_supervisor_identity(
    *,
    hint: dict[str, Any] | None = None,
    current_venv_link: Path,
    car_wrapper_path: Path,
    hub_pid: int | None = None,
    cgroup_reader: CgroupReader | None = None,
    launchctl_reader: LaunchctlReader | None = None,
    systemctl_reader: SystemctlReader | None = None,
    environ: Mapping[str, str] | None = None,
) -> SupervisorIdentity:
    read_cgroup = cgroup_reader or default_cgroup_reader
    read_launchctl = launchctl_reader or default_launchctl_reader
    read_systemctl = systemctl_reader or default_systemctl_reader
    env = environ if environ is not None else os.environ
    pid = hub_pid if hub_pid is not None else os.getpid()

    cgroup_text = read_cgroup()
    is_container = detect_container(cgroup_text=cgroup_text, environ=env)

    backend: Backend = "none"
    scope: Scope | None = None
    unit_name: str | None = None
    label: str | None = None
    hub_root: Path | None = None
    exec_start: str | None = None

    if isinstance(hint, dict):
        hinted_backend = _normalize_backend_hint(hint.get("backend"))
        if hinted_backend:
            backend = hinted_backend
        hinted_scope = _normalize_scope_hint(hint.get("scope"))
        if hinted_scope:
            scope = hinted_scope
        unit_name = _optional_str(hint.get("unit_name")) or unit_name
        label = _optional_str(hint.get("label")) or label
        hinted_pid = hint.get("hub_pid")
        if isinstance(hinted_pid, int) and hinted_pid > 0:
            pid = hinted_pid
        hinted_root = hint.get("hub_root")
        if isinstance(hinted_root, str) and hinted_root.strip():
            hub_root = Path(hinted_root)
        exec_start = _optional_str(hint.get("exec_start_or_program")) or exec_start

    if sys.platform.startswith("linux"):
        parsed_backend, parsed_scope, parsed_unit = parse_cgroup_identity(cgroup_text)
        if backend == "none":
            backend = parsed_backend
        if scope is None:
            scope = parsed_scope
        if unit_name is None:
            unit_name = parsed_unit
        if backend.startswith("systemd") and unit_name:
            if exec_start is None:
                exec_start = read_systemctl(
                    scope or ("user" if backend == "systemd-user" else "system"),
                    unit_name,
                    ["show", "-p", "ExecStart", "--value"],
                )
            if hub_root is None:
                working_dir = read_systemctl(
                    scope or ("user" if backend == "systemd-user" else "system"),
                    unit_name,
                    ["show", "-p", "WorkingDirectory", "--value"],
                )
                if working_dir:
                    hub_root = Path(working_dir)

    if sys.platform == "darwin":
        launchctl_info = read_launchctl(pid)
        if backend == "none" and launchctl_info.get("label"):
            backend = "launchd"
        if label is None:
            label = _optional_str(launchctl_info.get("label"))
        if unit_name is None:
            unit_name = label
        if exec_start is None:
            exec_start = _optional_str(launchctl_info.get("program"))
            program_args = launchctl_info.get("program_args")
            if not exec_start and isinstance(program_args, Sequence):
                exec_start = " ".join(str(part) for part in program_args)
        if hub_root is None:
            cwd = _optional_str(launchctl_info.get("working_directory"))
            if cwd:
                hub_root = Path(cwd)

    routes_car, routes_venv = _resolve_routing_flags(
        exec_start_or_program=exec_start,
        current_venv_link=current_venv_link,
        car_wrapper_path=car_wrapper_path,
    )

    remediation = _build_identity_remediation(
        backend=backend,
        is_container=is_container,
        routes_through_car_wrapper=routes_car,
        routes_through_current_venv=routes_venv,
        unit_name=unit_name or label,
    )

    return SupervisorIdentity(
        backend=backend,
        scope=scope,
        unit_name=unit_name,
        label=label,
        hub_pid=pid,
        hub_root=hub_root,
        exec_start_or_program=exec_start,
        routes_through_car_wrapper=routes_car,
        routes_through_current_venv=routes_venv,
        is_container=is_container,
        remediation=remediation,
    )


def resolve_backend(config_backend: str, identity: SupervisorIdentity) -> Backend:
    normalized = _normalize_backend_hint(config_backend) or "auto"
    if normalized != "auto":
        return normalized
    if identity.backend != "none":
        return identity.backend
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd-user"
    return "none"


def guard_self_update(identity: SupervisorIdentity) -> None:
    if identity.is_container and identity.backend == "none":
        raise RuntimeError(
            "Refusing self-update inside an ephemeral container with no supervisor. "
            "Run updates from the host or configure a persistent supervisor."
        )
    if identity.backend == "none" and not identity.is_container:
        raise RuntimeError(
            "Refusing self-update because no launchd or systemd supervisor was detected. "
            "Configure update.restart_command or run under a managed service."
        )


def verify_cutover_routing(identity: SupervisorIdentity) -> tuple[bool, str]:
    if identity.routes_through_car_wrapper or identity.routes_through_current_venv:
        return True, ""
    unit = identity.unit_name or identity.label or "service"
    remediation = (
        f"Service {unit} does not route through the car wrapper or CURRENT_VENV_LINK. "
        "Point ExecStart/Program at the car wrapper or the current venv symlink before "
        "using staged cutover, or set update.allow_in_place=true for emergency in-place install."
    )
    if identity.remediation:
        remediation = f"{remediation} {identity.remediation}"
    return False, remediation


def systemctl_restart_policy(
    *,
    scope: str,
    unit_name: str,
    systemctl_reader: SystemctlReader | None = None,
) -> str | None:
    read_systemctl = systemctl_reader or default_systemctl_reader
    if not unit_name:
        return None
    raw = read_systemctl(scope, unit_name, ["show", "-p", "Restart", "--value"])
    return raw or None


def launchctl_has_restart_policy(
    *,
    pid: int,
    launchctl_reader: LaunchctlReader | None = None,
) -> bool:
    read_launchctl = launchctl_reader or default_launchctl_reader
    info = read_launchctl(pid)
    keep_alive = info.get("keep_alive")
    if isinstance(keep_alive, bool):
        return keep_alive
    if isinstance(keep_alive, str):
        return keep_alive.strip().lower() in {"true", "yes", "1"}
    state = str(info.get("state", "")).strip().lower()
    return state == "running"


def _split_cgroup_line(line: str) -> tuple[str, str, str]:
    parts = line.strip().split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return "", "", line.strip()


def _parse_launchctl_print(text: str) -> dict[str, Any]:
    info: dict[str, Any] = {}
    program_args: list[str] = []
    in_program_args = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("program arguments = {"):
            in_program_args = True
            continue
        if in_program_args:
            if line == "}":
                in_program_args = False
                continue
            program_args.append(line.rstrip(",").strip().strip('"'))
            continue
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        key = key.strip().lower().replace(" ", "_")
        value = value.strip().strip('"')
        info[key] = value
    if program_args:
        info["program_args"] = program_args
    if "path" in info and "label" not in info:
        path = str(info["path"])
        if "/" in path:
            info["label"] = path.rsplit("/", 1)[-1]
    return info


def _normalize_backend_hint(raw: Any) -> Backend | None:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in ("", "auto"):
        return None
    if value in ("launchd", "systemd-user", "systemd-system", "none"):
        return value  # type: ignore[return-value]
    return None


def _normalize_scope_hint(raw: Any) -> Scope | None:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in ("user", "system"):
        return value  # type: ignore[return-value]
    return None


def _optional_str(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _resolve_routing_flags(
    *,
    exec_start_or_program: str | None,
    current_venv_link: Path,
    car_wrapper_path: Path,
) -> tuple[bool, bool]:
    if not exec_start_or_program:
        return False, False
    haystack = exec_start_or_program
    routes_car = str(car_wrapper_path) in haystack or "/car " in f"{haystack} "
    routes_venv = str(current_venv_link) in haystack
    if not routes_venv and current_venv_link.is_symlink():
        try:
            resolved = current_venv_link.resolve()
            routes_venv = str(resolved) in haystack
        except OSError:
            pass
    if not routes_car:
        wrapper_name = car_wrapper_path.name
        routes_car = bool(wrapper_name and wrapper_name in haystack)
    return routes_car, routes_venv


def _build_identity_remediation(
    *,
    backend: Backend,
    is_container: bool,
    routes_through_car_wrapper: bool,
    routes_through_current_venv: bool,
    unit_name: str | None,
) -> str | None:
    if routes_through_car_wrapper or routes_through_current_venv:
        return None
    parts: list[str] = []
    if is_container:
        parts.append("Running inside a container.")
    if backend == "none":
        parts.append("No supervisor detected.")
    elif unit_name:
        parts.append(f"Supervisor target {unit_name} lacks car/current-venv routing.")
    return " ".join(parts) if parts else None


def enrich_identity(
    identity: SupervisorIdentity,
    **changes: Any,
) -> SupervisorIdentity:
    return replace(identity, **changes)
