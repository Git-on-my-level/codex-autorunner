"""Hub and chat-service health checks with base-path awareness."""

from __future__ import annotations

import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config_parsers import normalize_base_path
from ..utils import resolve_executable

BoolTriState = Literal["auto", "true", "false"]
DEFAULT_HUB_PRE_CHAT_HEALTH_TIMEOUT_SECONDS = 120.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 2.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
_SERVICE_STATUS_CHECK_TIMEOUT_SECONDS = 2.0


@dataclass
class HealthWaitResult:
    ok: bool
    timed_out: bool = False
    message: str = ""


class HealthChecker:
    """Poll hub HTTP health and optional chat supervisor state."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 4173,
        base_path: str = "",
        timeout: float = 30.0,
        interval: float = 0.5,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        health_path: str = "",
        static_path: str = "",
        check_static: BoolTriState = "auto",
        logger: logging.Logger | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.base_path = normalize_base_path(base_path)
        self.timeout = float(timeout)
        self.interval = float(interval)
        self.connect_timeout = float(connect_timeout)
        self.request_timeout = float(request_timeout)
        self.health_path = health_path or self._default_health_path()
        self.static_path = static_path or self._default_static_path()
        self.check_static = _normalize_bool_tri_state(check_static)
        self.logger = logger or logging.getLogger(__name__)

    def hub_health_url(self) -> str:
        path = self.health_path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"http://{self.host}:{self.port}{path}"

    def static_asset_url(self) -> str:
        path = self.static_path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"http://{self.host}:{self.port}{path}"

    def wait_for_hub_health(
        self,
        *,
        include_static: bool = True,
        timeout: float | None = None,
    ) -> HealthWaitResult:
        """Poll hub ``/health`` (and optional static asset) until success or timeout."""
        deadline = time.time() + (self.timeout if timeout is None else float(timeout))
        while True:
            if self.check_hub_health_once(include_static=include_static):
                return HealthWaitResult(ok=True)
            if time.time() >= deadline:
                return HealthWaitResult(
                    ok=False,
                    timed_out=True,
                    message="Hub health wait timed out.",
                )
            time.sleep(self.interval)

    def check_hub_health_once(self, *, include_static: bool = True) -> bool:
        if not self._http_ok(self.hub_health_url()):
            return False
        if include_static and self.should_check_static():
            return self.check_static_asset()
        return True

    def check_static_asset(self) -> bool:
        if not self.static_path:
            return True
        return self._http_ok(self.static_asset_url())

    def should_check_static(self) -> bool:
        if self.check_static == "false":
            return False
        if self.check_static == "true":
            return True
        return bool(self.static_path)

    def wait_hub_before_chat(
        self,
        *,
        timeout: float = DEFAULT_HUB_PRE_CHAT_HEALTH_TIMEOUT_SECONDS,
    ) -> HealthWaitResult:
        """Warmup gate: wait for hub health before restarting chat services."""
        self.logger.info(
            "Waiting for hub health before chat reload (timeout=%ss)...", timeout
        )
        result = self.wait_for_hub_health(include_static=False, timeout=timeout)
        if result.ok:
            self.logger.info("Hub health OK before chat service reload.")
        else:
            self.logger.error("Hub health check failed before chat reload.")
        return result

    def systemd_service_active(
        self,
        service_name: str,
        *,
        scope: Literal["user", "system"] = "user",
    ) -> bool:
        if not service_name or resolve_executable("systemctl") is None:
            return False
        cmd = ["systemctl"]
        if scope == "user":
            cmd.append("--user")
        cmd += ["is-active", "--quiet", service_name]
        try:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=_SERVICE_STATUS_CHECK_TIMEOUT_SECONDS,
            )
        except (subprocess.SubprocessError, OSError):
            return False
        return result.returncode == 0

    def telegram_cli_healthy(
        self,
        *,
        python_bin: Path | str,
        hub_root: Path | str,
    ) -> bool:
        """Deep telegram health: open/migrate state DB, then ping Telegram API.

        Mirrors the mac refresh script's ``telegram state-check`` +
        ``telegram health`` gate, which is stronger than "launchd label running".
        """
        console = Path(python_bin).parent / "codex-autorunner"
        if not console.exists() or not os.access(console, os.X_OK):
            return False
        for args in (
            [str(console), "telegram", "state-check", "--path", str(hub_root)],
            [
                str(console),
                "telegram",
                "health",
                "--path",
                str(hub_root),
                "--timeout",
                str(int(self.request_timeout)),
            ],
        ):
            try:
                result = subprocess.run(
                    args,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
            except (subprocess.SubprocessError, OSError):
                return False
            if result.returncode != 0:
                return False
        return True

    def wait_telegram_cli_healthy(
        self,
        *,
        python_bin: Path | str,
        hub_root: Path | str,
        timeout: float | None = None,
    ) -> HealthWaitResult:
        deadline = time.time() + (self.timeout if timeout is None else float(timeout))
        while True:
            if self.telegram_cli_healthy(python_bin=python_bin, hub_root=hub_root):
                return HealthWaitResult(ok=True)
            if time.time() >= deadline:
                return HealthWaitResult(
                    ok=False,
                    timed_out=True,
                    message="Telegram CLI health did not pass.",
                )
            time.sleep(self.interval)

    def launchd_label_running(self, label: str) -> bool:
        """Return True when a launchd label is running or has a live PID."""
        if not label or resolve_executable("launchctl") is None:
            return False
        uid = os.getuid() if hasattr(os, "getuid") else 0
        domain = f"gui/{uid}/{label}"
        try:
            result = subprocess.run(
                ["launchctl", "print", domain],
                check=False,
                capture_output=True,
                text=True,
                timeout=_SERVICE_STATUS_CHECK_TIMEOUT_SECONDS,
            )
        except (subprocess.SubprocessError, OSError):
            return False
        if result.returncode != 0:
            return False
        text = result.stdout or ""
        if "state = running" in text:
            return True
        for line in text.splitlines():
            if "pid =" not in line:
                continue
            try:
                pid = int(line.split("=", 1)[1].strip())
            except (ValueError, IndexError):
                continue
            if pid > 0:
                return True
        return False

    def launchd_kickstart(self, label: str) -> bool:
        """Kickstart a loaded LaunchAgent; returns False on failure."""
        if not label or resolve_executable("launchctl") is None:
            return False
        uid = os.getuid() if hasattr(os, "getuid") else 0
        domain = f"gui/{uid}/{label}"
        try:
            result = subprocess.run(
                ["launchctl", "kickstart", "-k", domain],
                check=False,
                capture_output=True,
                text=True,
                timeout=_SERVICE_STATUS_CHECK_TIMEOUT_SECONDS,
            )
        except (subprocess.SubprocessError, OSError):
            return False
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            self.logger.error('Could not kickstart service "%s": %s', label, detail)
            return False
        return True

    def wait_systemd_service_active(
        self,
        service_name: str,
        *,
        scope: Literal["user", "system"] = "user",
        timeout: float | None = None,
    ) -> HealthWaitResult:
        deadline = time.time() + (self.timeout if timeout is None else float(timeout))
        while True:
            if self.systemd_service_active(service_name, scope=scope):
                return HealthWaitResult(ok=True)
            if time.time() >= deadline:
                return HealthWaitResult(
                    ok=False,
                    timed_out=True,
                    message=f"Service {service_name} did not become active.",
                )
            time.sleep(self.interval)

    def wait_launchd_label_running(
        self,
        label: str,
        *,
        timeout: float | None = None,
    ) -> HealthWaitResult:
        deadline = time.time() + (self.timeout if timeout is None else float(timeout))
        while True:
            if self.launchd_label_running(label):
                return HealthWaitResult(ok=True)
            if time.time() >= deadline:
                return HealthWaitResult(
                    ok=False,
                    timed_out=True,
                    message=f"Launchd label {label} did not become running.",
                )
            time.sleep(self.interval)

    def _default_health_path(self) -> str:
        health_path, _ = health_paths_for_base_path(self.base_path)
        return health_path

    def _default_static_path(self) -> str:
        _, static_path = health_paths_for_base_path(self.base_path)
        return static_path

    def _http_ok(self, url: str) -> bool:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.request_timeout,
            ) as response:
                return 200 <= int(response.status) < 300
        except urllib.error.HTTPError as exc:
            return 200 <= int(exc.code) < 300
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return False


def wait_hub_before_chat(
    checker: HealthChecker,
    *,
    enabled: bool = True,
    timeout: float = DEFAULT_HUB_PRE_CHAT_HEALTH_TIMEOUT_SECONDS,
) -> HealthWaitResult:
    """Gate chat reload on hub HTTP health when enabled."""
    if not enabled:
        return HealthWaitResult(ok=True, message="Hub pre-chat health gate disabled.")
    return checker.wait_hub_before_chat(timeout=timeout)


def health_paths_for_base_path(base_path: str) -> tuple[str, str]:
    """Return ``(health_path, static_path)`` for a normalized base path."""
    normalized = normalize_base_path(base_path)
    if normalized:
        return f"{normalized}/health", f"{normalized}/_app/version.json"
    return "/health", "/_app/version.json"


def _normalize_bool_tri_state(raw: str | BoolTriState) -> BoolTriState:
    value = str(raw or "auto").strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return "true"
    if value in {"0", "false", "no", "n", "off"}:
        return "false"
    return "auto"
