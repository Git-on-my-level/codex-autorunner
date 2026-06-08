"""macOS launchd plist management for the staged update engine.

Ports the launchd-specific behavior from ``scripts/safe-refresh-local-mac-hub.sh``
into testable Python: hub plist normalization (route ExecStart through the
``CURRENT_VENV_LINK``), OpenCode ``PATH`` injection, process-limit
normalization, chat-agent plist creation, chat enable/disable/missing-env
detection, graceful stop (pid tree + SIGKILL fallback), and load/kickstart.

The pure helpers operate on parsed plist dicts / raw text so they can be unit
tested without a real launchd. The orchestration methods shell out through an
injectable command runner.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Mapping, Optional, Sequence

ChatKind = Literal["telegram", "discord"]
ChatState = Literal["enabled", "disabled", "missing_env", "unknown"]

DEFAULT_STOP_WAIT_SECONDS = 10.0
_KICKSTART_TIMEOUT_SECONDS = 120
_LAUNCHCTL_TIMEOUT_SECONDS = 30

CommandRunner = Callable[..., "subprocess.CompletedProcess[str]"]

_DISCORD_DEFAULT_BOT_ENV = "CAR_DISCORD_BOT_TOKEN"
_DISCORD_DEFAULT_APP_ENV = "CAR_DISCORD_APP_ID"
_TELEGRAM_DEFAULT_BOT_ENV = "CAR_TELEGRAM_BOT_TOKEN"

_HUB_COMMAND_REPLACEMENTS = (
    "; codex-autorunner hub serve",
    " codex-autorunner hub serve",
    ">codex-autorunner hub serve",
    "codex-autorunner hub serve",
)


# --------------------------------------------------------------------------- #
# Pure plist helpers
# --------------------------------------------------------------------------- #
def normalize_hub_plist_text(text: str, desired_bin: str) -> Optional[str]:
    """Rewrite the hub plist text to launch ``desired_bin hub serve``.

    Returns the new text, or ``None`` when the plist already routes through
    ``desired_bin``. Raises ``ValueError`` when no ``codex-autorunner hub serve``
    command can be found (mirrors the script's hard failure).
    """
    if desired_bin in text:
        return None
    for needle in _HUB_COMMAND_REPLACEMENTS:
        if needle in text:
            replacement = needle.replace("codex-autorunner", desired_bin)
            return text.replace(needle, replacement, 1)
    raise ValueError(
        "Unable to update plist automatically; expected to find a "
        "'codex-autorunner hub serve' command."
    )


def inject_opencode_path(plist: dict, opencode_bin: str) -> bool:
    """Prepend ``opencode_bin`` to the ``PATH=`` prefix in ProgramArguments[2].

    Returns True when the plist was modified.
    """
    if not opencode_bin:
        return False
    program_args = plist.get("ProgramArguments")
    if not isinstance(program_args, list) or len(program_args) < 3:
        raise ValueError("LaunchAgent plist missing ProgramArguments list.")
    cmd = program_args[2]
    if not isinstance(cmd, str):
        raise ValueError("LaunchAgent plist ProgramArguments[2] is not a string.")
    if opencode_bin in cmd:
        return False
    if "PATH=" in cmd:
        cmd = cmd.replace("PATH=", f"PATH={opencode_bin}:", 1)
    else:
        cmd = f"PATH={opencode_bin}:$PATH; {cmd}"
    program_args[2] = cmd
    plist["ProgramArguments"] = program_args
    return True


def normalize_process_limits(plist: dict) -> bool:
    """Drop ``NumberOfProcesses`` from Soft/Hard resource limits.

    Returns True when the plist was modified.
    """
    updated = False
    for key in ("SoftResourceLimits", "HardResourceLimits"):
        section = plist.get(key)
        if not isinstance(section, dict):
            continue
        if "NumberOfProcesses" in section:
            section.pop("NumberOfProcesses", None)
            updated = True
        if not section:
            plist.pop(key, None)
            updated = True
        else:
            plist[key] = section
    return updated


def normalize_chat_plist(plist: dict, desired_bin: str, kind: ChatKind) -> bool:
    """Rewrite a chat plist's ``codex-autorunner <kind> start`` to ``desired_bin``.

    Returns True when modified. Raises ``ValueError`` if the expected command is
    absent (mirrors the script's hard failure).
    """
    program_args = plist.get("ProgramArguments")
    if not isinstance(program_args, list):
        raise ValueError(f"{kind.title()} plist missing ProgramArguments list.")
    needle = f"{kind} start"
    pattern = re.compile(r"(^|[\s;])[^\s;]*codex-autorunner(?= " + kind + r" start\b)")
    legacy = f"codex-autorunner {kind} start"
    for idx, arg in enumerate(program_args):
        if not isinstance(arg, str):
            continue
        if needle not in arg or "codex-autorunner" not in arg:
            continue
        new_arg, count = pattern.subn(
            lambda m: f"{m.group(1)}{desired_bin}", arg, count=1
        )
        if count == 0 and legacy in arg:
            new_arg = arg.replace(legacy, f"{desired_bin} {kind} start", 1)
            count = 1
        if count:
            program_args[idx] = new_arg
            plist["ProgramArguments"] = program_args
            return True
        break
    raise ValueError(
        f"Unable to update {kind} plist automatically; expected to find a "
        f"'codex-autorunner {kind} start' command."
    )


def build_chat_plist_dict(
    *,
    label: str,
    kind: ChatKind,
    hub_root: Path,
    current_venv_link: Path,
    path_dirs: Sequence[str],
    log_path: Path,
) -> dict:
    """Build a LaunchAgent plist dict for a chat service (telegram/discord)."""
    path_prefix = ":".join([d for d in path_dirs if d])
    command = (
        f"PATH={path_prefix}:$PATH; "
        f"{current_venv_link}/bin/codex-autorunner {kind} start --path {hub_root}"
    )
    return {
        "Label": label,
        "ProgramArguments": ["/bin/sh", "-lc", command],
        "WorkingDirectory": str(hub_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


# --------------------------------------------------------------------------- #
# Chat enable/disable detection (config.yml + env overrides)
# --------------------------------------------------------------------------- #
def _load_hub_config(hub_root: Optional[Path]) -> Optional[dict]:
    if hub_root is None:
        return None
    config_path = Path(hub_root) / ".codex-autorunner" / "config.yml"
    if not config_path.exists():
        return None
    try:
        import yaml
    except ImportError:
        return None
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError:
        return None
    return data if isinstance(data, dict) else None


def _env_var_is_set(
    hub_root: Optional[Path],
    name: str,
    environ: Mapping[str, str],
) -> bool:
    if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return False
    if environ.get(name):
        return True
    if hub_root is None:
        return False
    for candidate in (
        Path(hub_root) / ".env",
        Path(hub_root) / ".codex-autorunner" / ".env",
    ):
        if not candidate.exists():
            continue
        try:
            from dotenv import dotenv_values

            values = dotenv_values(candidate)
            if isinstance(values, dict) and values.get(name):
                return True
            continue
        except ImportError:
            pass
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("export "):
                    stripped = stripped[len("export ") :].strip()
                if "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                if key.strip() != name:
                    continue
                value = value.strip()
                if value and value[0] in {"'", '"'} and value[-1] == value[0]:
                    value = value[1:-1]
                if value:
                    return True
        except OSError:
            continue
    return False


def telegram_state(
    hub_root: Optional[Path],
    *,
    environ: Mapping[str, str],
) -> ChatState:
    """Resolve telegram enable state from ``ENABLE_TELEGRAM_BOT`` then config.yml."""
    override = str(environ.get("ENABLE_TELEGRAM_BOT", "auto")).strip().lower()
    if override in ("1", "true"):
        return "enabled"
    if override in ("0", "false"):
        return "disabled"
    config = _load_hub_config(hub_root)
    if config is None:
        return "unknown"
    cfg = config.get("telegram_bot")
    if isinstance(cfg, dict) and bool(cfg.get("enabled", False)):
        return "enabled"
    return "disabled"


def discord_state(
    hub_root: Optional[Path],
    *,
    environ: Mapping[str, str],
) -> ChatState:
    """Resolve discord enable state, including missing-env detection."""
    bot_env, app_env, cfg_state = _discord_config(hub_root)
    override = str(environ.get("ENABLE_DISCORD_BOT", "auto")).strip().lower()
    if override in ("0", "false"):
        return "disabled"
    if override in ("1", "true"):
        cfg_state = "enabled"
        bot_env = bot_env or _DISCORD_DEFAULT_BOT_ENV
        app_env = app_env or _DISCORD_DEFAULT_APP_ENV
    if cfg_state != "enabled":
        return cfg_state
    if not _env_var_is_set(hub_root, bot_env, environ) or not _env_var_is_set(
        hub_root, app_env, environ
    ):
        return "missing_env"
    return "enabled"


def discord_missing_env_names(
    hub_root: Optional[Path],
    *,
    environ: Mapping[str, str],
) -> list[str]:
    bot_env, app_env, _ = _discord_config(hub_root)
    bot_env = bot_env or _DISCORD_DEFAULT_BOT_ENV
    app_env = app_env or _DISCORD_DEFAULT_APP_ENV
    missing: list[str] = []
    if not _env_var_is_set(hub_root, bot_env, environ):
        missing.append(bot_env)
    if not _env_var_is_set(hub_root, app_env, environ):
        missing.append(app_env)
    return missing


def _discord_config(hub_root: Optional[Path]) -> tuple[str, str, ChatState]:
    config = _load_hub_config(hub_root)
    if config is None:
        return _DISCORD_DEFAULT_BOT_ENV, _DISCORD_DEFAULT_APP_ENV, "unknown"
    cfg = config.get("discord_bot")
    if not isinstance(cfg, dict):
        return _DISCORD_DEFAULT_BOT_ENV, _DISCORD_DEFAULT_APP_ENV, "disabled"
    enabled = bool(cfg.get("enabled", False))
    bot_env = (
        str(cfg.get("bot_token_env", _DISCORD_DEFAULT_BOT_ENV)).strip()
        or _DISCORD_DEFAULT_BOT_ENV
    )
    app_env = (
        str(cfg.get("app_id_env", _DISCORD_DEFAULT_APP_ENV)).strip()
        or _DISCORD_DEFAULT_APP_ENV
    )
    return bot_env, app_env, ("enabled" if enabled else "disabled")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@dataclass
class LaunchdServiceResult:
    success: bool
    error: Optional[str] = None


@dataclass
class LaunchdPlistManager:
    """Normalize launchd plists and reload hub/chat agents on macOS."""

    label: str
    hub_plist_path: Path
    current_venv_link: Path
    hub_root: Optional[Path]
    uid: int
    opencode_bin: str = ""
    path_dirs: Sequence[str] = field(default_factory=tuple)
    telegram_label: Optional[str] = None
    telegram_plist_path: Optional[Path] = None
    discord_label: Optional[str] = None
    discord_plist_path: Optional[Path] = None
    stop_wait_seconds: float = DEFAULT_STOP_WAIT_SECONDS
    environ: Mapping[str, str] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))
    command_runner: Optional[CommandRunner] = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic

    @property
    def desired_bin(self) -> str:
        return f"{self.current_venv_link}/bin/codex-autorunner"

    def domain(self, label: str) -> str:
        return f"gui/{self.uid}/{label}"

    # -- subprocess plumbing ------------------------------------------------- #
    def _run(
        self, cmd: Sequence[str], *, timeout: float
    ) -> "subprocess.CompletedProcess[str]":
        runner = self.command_runner or _default_command_runner
        return runner(list(cmd), timeout=timeout)

    # -- hub ----------------------------------------------------------------- #
    def normalize_hub_plist(self) -> None:
        path = self.hub_plist_path
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        new_text = normalize_hub_plist_text(text, self.desired_bin)
        if new_text is not None:
            self.logger.info("Updating plist to use %s...", self.desired_bin)
            path.write_text(new_text, encoding="utf-8")
        self._normalize_plist_file(path)

    def _normalize_plist_file(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            with path.open("rb") as handle:
                plist = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            return
        updated = False
        try:
            updated = inject_opencode_path(plist, self.opencode_bin) or updated
        except ValueError:
            pass
        updated = normalize_process_limits(plist) or updated
        if updated:
            with path.open("wb") as handle:
                plistlib.dump(plist, handle)

    def reload_hub(self) -> LaunchdServiceResult:
        self.normalize_hub_plist()
        self._stop(self.hub_plist_path, self.label)
        load_err = self._load_and_kickstart(self.hub_plist_path, self.label)
        if load_err:
            return LaunchdServiceResult(False, load_err)
        return LaunchdServiceResult(True)

    def stop_hub(self) -> None:
        self._stop(self.hub_plist_path, self.label)

    # -- chat ---------------------------------------------------------------- #
    def reload_telegram(self) -> LaunchdServiceResult:
        if not self.telegram_label or self.telegram_plist_path is None:
            return LaunchdServiceResult(True)
        state = telegram_state(self.hub_root, environ=self.environ)
        return self._reload_chat(
            kind="telegram",
            label=self.telegram_label,
            plist_path=self.telegram_plist_path,
            state=state,
        )

    def reload_discord(self) -> LaunchdServiceResult:
        if not self.discord_label or self.discord_plist_path is None:
            return LaunchdServiceResult(True)
        state = discord_state(self.hub_root, environ=self.environ)
        return self._reload_chat(
            kind="discord",
            label=self.discord_label,
            plist_path=self.discord_plist_path,
            state=state,
        )

    def _reload_chat(
        self,
        *,
        kind: ChatKind,
        label: str,
        plist_path: Path,
        state: ChatState,
    ) -> LaunchdServiceResult:
        if state == "enabled":
            if self.hub_root is None:
                self.logger.warning(
                    "%s enabled but unable to derive hub root; skipping LaunchAgent.",
                    kind.title(),
                )
                return LaunchdServiceResult(True)
            if not plist_path.exists():
                self._write_chat_plist(kind=kind, label=label, plist_path=plist_path)
            self._ensure_chat_plist_uses_current_venv(kind, plist_path)
            self._normalize_plist_file(plist_path)
            self._stop(plist_path, label)
            err = self._load_and_kickstart(plist_path, label)
            if err:
                return LaunchdServiceResult(False, err)
            return LaunchdServiceResult(True)

        if state in ("disabled", "missing_env"):
            if plist_path.exists():
                self.logger.info(
                    "%s %s; unloading launchd service %s...",
                    kind.title(),
                    state,
                    label,
                )
                self._stop(plist_path, label)
            return LaunchdServiceResult(True)

        # unknown: keep existing service running, just reload if present
        if not plist_path.exists():
            return LaunchdServiceResult(True)
        self._normalize_plist_file(plist_path)
        self._stop(plist_path, label)
        err = self._load_and_kickstart(plist_path, label)
        if err:
            return LaunchdServiceResult(False, err)
        return LaunchdServiceResult(True)

    def _ensure_chat_plist_uses_current_venv(
        self, kind: ChatKind, plist_path: Path
    ) -> None:
        if not plist_path.exists():
            return
        text = plist_path.read_text(encoding="utf-8")
        if self.desired_bin in text:
            return
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        if normalize_chat_plist(plist, self.desired_bin, kind):
            self.logger.info("Updating %s plist to use %s...", kind, self.desired_bin)
            with plist_path.open("wb") as handle:
                plistlib.dump(plist, handle)

    def _write_chat_plist(
        self, *, kind: ChatKind, label: str, plist_path: Path
    ) -> None:
        assert self.hub_root is not None
        log_path = self.hub_root / ".codex-autorunner" / f"codex-autorunner-{kind}.log"
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        (self.hub_root / ".codex-autorunner").mkdir(parents=True, exist_ok=True)
        self.logger.info("Writing launchd plist to %s...", plist_path)
        plist = build_chat_plist_dict(
            label=label,
            kind=kind,
            hub_root=self.hub_root,
            current_venv_link=self.current_venv_link,
            path_dirs=self.path_dirs,
            log_path=log_path,
        )
        with plist_path.open("wb") as handle:
            plistlib.dump(plist, handle)

    # -- launchctl primitives ------------------------------------------------ #
    def _service_pid(self, label: str) -> Optional[int]:
        result = self._run(
            ["launchctl", "print", self.domain(label)],
            timeout=_LAUNCHCTL_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return None
        for line in (result.stdout or "").splitlines():
            if "pid =" in line:
                try:
                    pid = int(line.split("=", 1)[1].strip())
                except (ValueError, IndexError):
                    continue
                if pid > 0:
                    return pid
        return None

    def _collect_pid_tree(self, pid: int) -> list[int]:
        if pid <= 0:
            return []
        pids = [pid]
        result = self._run(
            ["pgrep", "-P", str(pid)], timeout=_LAUNCHCTL_TIMEOUT_SECONDS
        )
        if result.returncode == 0:
            for line in (result.stdout or "").split():
                try:
                    child = int(line.strip())
                except ValueError:
                    continue
                pids.extend(self._collect_pid_tree(child))
        return pids

    def _wait_pid_exit(self, pids: Sequence[int]) -> bool:
        deadline = self.now() + self.stop_wait_seconds
        while True:
            alive = [pid for pid in pids if _pid_alive(pid)]
            if not alive:
                return True
            if self.now() >= deadline:
                return False
            self.sleep(0.1)

    def _stop(self, plist_path: Path, label: str) -> None:
        pids: list[int] = []
        root_pid = self._service_pid(label)
        if root_pid:
            pids = self._collect_pid_tree(root_pid)
        self._run(
            ["launchctl", "unload", "-w", str(plist_path)],
            timeout=_LAUNCHCTL_TIMEOUT_SECONDS,
        )
        if pids and not self._wait_pid_exit(pids):
            for pid in pids:
                _kill(pid)
            self._wait_pid_exit(pids)

    def _load_and_kickstart(self, plist_path: Path, label: str) -> Optional[str]:
        load = self._run(
            ["launchctl", "load", "-w", str(plist_path)],
            timeout=_LAUNCHCTL_TIMEOUT_SECONDS,
        )
        if load.returncode != 0:
            detail = (load.stderr or load.stdout or "").strip()
            return f"launchctl load failed for {label}: {detail}"
        kick = self._run(
            ["launchctl", "kickstart", "-k", self.domain(label)],
            timeout=_KICKSTART_TIMEOUT_SECONDS,
        )
        if kick.returncode != 0:
            detail = (kick.stderr or kick.stdout or "").strip()
            return f'Could not kickstart service "{label}": {detail}'
        return None


def _default_command_runner(
    cmd: Sequence[str], *, timeout: float
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(cmd),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kill(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except OSError:
        pass


__all__ = (
    "ChatKind",
    "ChatState",
    "LaunchdPlistManager",
    "LaunchdServiceResult",
    "build_chat_plist_dict",
    "discord_missing_env_names",
    "discord_state",
    "inject_opencode_path",
    "normalize_chat_plist",
    "normalize_hub_plist_text",
    "normalize_process_limits",
    "telegram_state",
)
