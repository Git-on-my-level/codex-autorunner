from __future__ import annotations

import os
import shlex
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, Sequence

from .detect import (
    LaunchctlReader,
    SupervisorIdentity,
    SystemctlReader,
    launchctl_has_restart_policy,
    systemctl_restart_policy,
)

if TYPE_CHECKING:
    from .launchd import LaunchdPlistManager

_RESTART_CMD_TIMEOUT_SECONDS = 120
_SERVICE_STATUS_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class UpdateServices:
    restart_hub: bool = True
    restart_telegram: bool = False
    restart_discord: bool = False


@dataclass(frozen=True)
class RestartResult:
    success: bool
    method_used: str
    error: str | None = None


class SupervisorAdapter(Protocol):
    def restart(self, services: UpdateServices) -> RestartResult: ...


SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


def _default_subprocess_runner(
    cmd: Sequence[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _command_failure_message(
    cmd: Sequence[str],
    result: subprocess.CompletedProcess[str],
) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    rendered = " ".join(shlex.quote(part) for part in cmd)
    if detail:
        return f"Command failed ({rendered}): {detail}"
    return f"Command failed ({rendered}) with exit code {result.returncode}"


def _systemctl_prefix(scope: str, sudo_prefix: Sequence[str] | None) -> list[str]:
    prefix: list[str] = []
    if sudo_prefix:
        prefix.extend(sudo_prefix)
    prefix.append("systemctl")
    if scope == "user":
        prefix.append("--user")
    return prefix


def resolve_systemctl_sudo_prefix(
    *,
    scope: str,
    configured: str | bool | Sequence[str] | None,
    uid: int | None = None,
) -> list[str] | None:
    effective_uid = uid if uid is not None else os.getuid()
    if scope != "system" or effective_uid == 0:
        return None
    if configured is False or configured == "false":
        return None
    if configured is True or configured == "true":
        return ["sudo", "-n"]
    if isinstance(configured, str):
        normalized = configured.strip().lower()
        if normalized in ("", "auto"):
            return ["sudo", "-n"]
        return shlex.split(configured)
    if isinstance(configured, Sequence) and not isinstance(configured, str):
        return list(configured)
    return ["sudo", "-n"]


def _restart_policy_allows_signal(
    identity: SupervisorIdentity,
    *,
    systemctl_reader: SystemctlReader | None = None,
    launchctl_reader: LaunchctlReader | None = None,
) -> tuple[bool, str]:
    if identity.backend.startswith("systemd"):
        scope = identity.scope or (
            "user" if identity.backend == "systemd-user" else "system"
        )
        unit = identity.unit_name
        if not unit:
            return False, "Missing systemd unit name for restart-policy check."
        policy = systemctl_restart_policy(
            scope=scope,
            unit_name=unit,
            systemctl_reader=systemctl_reader,
        )
        if policy and policy.lower() in {
            "always",
            "on-failure",
            "on-abnormal",
            "on-abort",
            "on-watchdog",
        }:
            return True, ""
        return (
            False,
            f"Unit {unit} has Restart={policy or 'unset'}; signal-PID restart requires "
            "Restart=always or an equivalent relaunch policy.",
        )
    if identity.backend == "launchd":
        pid = identity.hub_pid or os.getpid()
        if launchctl_has_restart_policy(pid=pid, launchctl_reader=launchctl_reader):
            return True, ""
        return (
            False,
            "LaunchAgent lacks a persistent restart policy (KeepAlive/state=running).",
        )
    return False, "Signal-PID restart requires a supervised hub process."


@dataclass(frozen=True)
class LaunchdAdapter:
    label: str
    domain: str
    hub_plist_path: Path | None = None
    telegram_label: str | None = None
    telegram_plist_path: Path | None = None
    discord_label: str | None = None
    discord_plist_path: Path | None = None
    subprocess_runner: SubprocessRunner | None = None

    def restart(self, services: UpdateServices) -> RestartResult:
        run = self.subprocess_runner or _default_subprocess_runner
        targets: list[tuple[str, str]] = []
        if services.restart_hub:
            targets.append((self.domain, self.label))
        if services.restart_telegram and self.telegram_label:
            targets.append((self.domain, self.telegram_label))
        if services.restart_discord and self.discord_label:
            targets.append((self.domain, self.discord_label))
        if not targets:
            return RestartResult(success=True, method_used="launchd")

        errors: list[str] = []
        for domain, label in targets:
            cmd = ["launchctl", "kickstart", "-k", f"{domain}/{label}"]
            result = run(cmd, timeout=_RESTART_CMD_TIMEOUT_SECONDS)
            if result.returncode != 0:
                errors.append(_command_failure_message(cmd, result))
        if errors:
            return RestartResult(
                success=False,
                method_used="launchd",
                error="; ".join(errors),
            )
        return RestartResult(success=True, method_used="launchd")


@dataclass(frozen=True)
class LaunchdManagedAdapter:
    """Restart hub/chat launchd agents via a full plist-managing controller.

    Unlike :class:`LaunchdAdapter` (which only ``kickstart``s already-loaded
    labels), this adapter normalizes plists, creates/enables/disables chat
    agents, and performs graceful stop + load + kickstart, matching the mac
    refresh script behavior.
    """

    manager: "LaunchdPlistManager"

    def restart(self, services: UpdateServices) -> RestartResult:
        errors: list[str] = []
        if services.restart_hub:
            result = self.manager.reload_hub()
            if not result.success and result.error:
                errors.append(result.error)
        if services.restart_telegram:
            result = self.manager.reload_telegram()
            if not result.success and result.error:
                errors.append(result.error)
        if services.restart_discord:
            result = self.manager.reload_discord()
            if not result.success and result.error:
                errors.append(result.error)
        if errors:
            return RestartResult(
                success=False,
                method_used="launchd",
                error="; ".join(errors),
            )
        return RestartResult(success=True, method_used="launchd")


@dataclass(frozen=True)
class SystemdAdapter:
    scope: str
    hub_service: str
    telegram_service: str | None = None
    discord_service: str | None = None
    sudo_prefix: Sequence[str] | None = None
    systemctl_sudo: str | bool | Sequence[str] | None = "auto"
    subprocess_runner: SubprocessRunner | None = None

    def restart(self, services: UpdateServices) -> RestartResult:
        run = self.subprocess_runner or _default_subprocess_runner
        sudo_prefix = self.sudo_prefix
        if sudo_prefix is None:
            sudo_prefix = resolve_systemctl_sudo_prefix(
                scope=self.scope,
                configured=self.systemctl_sudo,
            )
        prefix = _systemctl_prefix(self.scope, sudo_prefix)

        reload = run([*prefix, "daemon-reload"], timeout=_RESTART_CMD_TIMEOUT_SECONDS)
        if reload.returncode != 0:
            return RestartResult(
                success=False,
                method_used="systemd",
                error=_command_failure_message([*prefix, "daemon-reload"], reload),
            )

        targets: list[str] = []
        if services.restart_hub:
            targets.append(self.hub_service)
        if services.restart_telegram and self.telegram_service:
            targets.append(self.telegram_service)
        if services.restart_discord and self.discord_service:
            targets.append(self.discord_service)
        if not targets:
            return RestartResult(success=True, method_used="systemd")

        errors: list[str] = []
        for service_name in targets:
            cmd = [*prefix, "restart", service_name]
            result = run(cmd, timeout=_RESTART_CMD_TIMEOUT_SECONDS)
            if result.returncode != 0:
                errors.append(_command_failure_message(cmd, result))
        if errors:
            return RestartResult(
                success=False,
                method_used="systemd",
                error="; ".join(errors),
            )
        return RestartResult(success=True, method_used="systemd")


@dataclass(frozen=True)
class CommandAdapter:
    restart_command: Sequence[str] | str | None
    subprocess_runner: SubprocessRunner | None = None
    cwd: Path | None = None

    def restart(self, services: UpdateServices) -> RestartResult:
        if not self.restart_command:
            return RestartResult(
                success=False,
                method_used="command",
                error="No restart_command configured.",
            )
        run = self.subprocess_runner or _default_subprocess_runner
        cmd = (
            shlex.split(self.restart_command)
            if isinstance(self.restart_command, str)
            else list(self.restart_command)
        )
        if not cmd:
            return RestartResult(
                success=False,
                method_used="command",
                error="restart_command is empty.",
            )
        try:
            result = run(cmd, timeout=_RESTART_CMD_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            return RestartResult(
                success=False,
                method_used="command",
                error=str(exc),
            )
        if result.returncode != 0:
            return RestartResult(
                success=False,
                method_used="command",
                error=_command_failure_message(cmd, result),
            )
        return RestartResult(success=True, method_used="command")


@dataclass(frozen=True)
class SignalAdapter:
    identity: SupervisorIdentity
    verify_restart_policy: bool = True
    systemctl_reader: SystemctlReader | None = None
    launchctl_reader: LaunchctlReader | None = None
    subprocess_runner: SubprocessRunner | None = None

    def restart(self, services: UpdateServices) -> RestartResult:
        if not services.restart_hub:
            return RestartResult(success=True, method_used="signal")
        pid = self.identity.hub_pid
        if pid is None or pid <= 0:
            return RestartResult(
                success=False,
                method_used="signal",
                error="Hub PID is unavailable for signal restart.",
            )
        if self.verify_restart_policy:
            allowed, reason = _restart_policy_allows_signal(
                self.identity,
                systemctl_reader=self.systemctl_reader,
                launchctl_reader=self.launchctl_reader,
            )
            if not allowed:
                return RestartResult(
                    success=False,
                    method_used="signal",
                    error=reason,
                )
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            return RestartResult(
                success=False,
                method_used="signal",
                error=f"Failed to signal hub PID {pid}: {exc}",
            )
        return RestartResult(success=True, method_used="signal")


@dataclass(frozen=True)
class LayeredSupervisor:
    adapters: Sequence[SupervisorAdapter]

    def restart(self, services: UpdateServices) -> RestartResult:
        errors: list[str] = []
        for adapter in self.adapters:
            result = adapter.restart(services)
            if result.success:
                return result
            if result.error:
                errors.append(f"{result.method_used}: {result.error}")
        if errors:
            return RestartResult(
                success=False,
                method_used="layered",
                error="; ".join(errors),
            )
        return RestartResult(
            success=False,
            method_used="layered",
            error="No restart adapters configured.",
        )


def build_layered_supervisor(
    *,
    identity: SupervisorIdentity,
    restart_command: Sequence[str] | str | None = None,
    hub_service: str | None = None,
    telegram_service: str | None = None,
    discord_service: str | None = None,
    launchd_label: str | None = None,
    launchd_domain: str | None = None,
    systemctl_sudo: str | bool | Sequence[str] | None = "auto",
    verify_signal_restart_policy: bool = True,
    subprocess_runner: SubprocessRunner | None = None,
) -> LayeredSupervisor:
    adapters: list[SupervisorAdapter] = []
    if identity.backend == "launchd":
        label = launchd_label or identity.label or identity.unit_name
        domain = launchd_domain or f"gui/{os.getuid()}"
        if label:
            adapters.append(
                LaunchdAdapter(
                    label=label,
                    domain=domain,
                    telegram_label=telegram_service,
                    discord_label=discord_service,
                    subprocess_runner=subprocess_runner,
                )
            )
    elif identity.backend.startswith("systemd"):
        scope = identity.scope or (
            "user" if identity.backend == "systemd-user" else "system"
        )
        service = hub_service or identity.unit_name
        if service:
            adapters.append(
                SystemdAdapter(
                    scope=scope,
                    hub_service=service,
                    telegram_service=telegram_service,
                    discord_service=discord_service,
                    systemctl_sudo=systemctl_sudo,
                    subprocess_runner=subprocess_runner,
                )
            )
    if restart_command:
        adapters.append(
            CommandAdapter(
                restart_command=restart_command,
                subprocess_runner=subprocess_runner,
            )
        )
    adapters.append(
        SignalAdapter(
            identity=identity,
            verify_restart_policy=verify_signal_restart_policy,
            subprocess_runner=subprocess_runner,
        )
    )
    return LayeredSupervisor(adapters=adapters)
