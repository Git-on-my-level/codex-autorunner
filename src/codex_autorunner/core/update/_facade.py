import importlib.metadata
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import unquote, urlparse

from ..git_utils import GitError, run_git
from ..locks import process_matches_identity
from ..update_paths import resolve_update_paths
from ..update_targets import (
    UpdateTargetDefinition,
    available_update_target_definitions,
    get_update_target_definition,
    normalize_update_target,
)
from ..utils import resolve_executable


class UpdateInProgressError(RuntimeError):
    """Raised when an update is already running."""


_UPDATE_LOCK_STARTUP_GRACE_SECONDS = 10.0
_UPDATE_LOCK_CMD_HINTS = (
    "codex_autorunner.core.update_runner",
    "codex_autorunner.core.update.runner",
)
_UPDATE_BUILD_ARTIFACT_DIRS = ("build", "dist", ".eggs")
_UPDATE_BUILD_ARTIFACT_GLOBS = ("*.egg-info", "src/*.egg-info")
_UPDATE_CACHE_RECOVERY_HINTS = (
    "unresolved deltas left after unpacking",
    "unpack-objects failed",
    "pack has bad object",
    "bad object",
    "index file corrupt",
    "object file is empty",
    "unable to read sha1 file",
)
_UPDATE_CMD_TIMEOUT_SECONDS = 300
_SERVICE_STATUS_CHECK_TIMEOUT_SECONDS = 2
_GIT_FETCH_UPDATE_TIMEOUT_SECONDS = 60


def _run_cmd(cmd: list[str], cwd: Path) -> None:
    """Run a subprocess command, raising on failure."""
    try:
        subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=_UPDATE_CMD_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as e:
        # Include stdout/stderr in the error message for debugging
        detail = (
            f"Command failed: {' '.join(cmd)}\nStdout: {e.stdout}\nStderr: {e.stderr}"
        )
        raise RuntimeError(detail) from e


def _remove_update_artifact(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def _cleanup_update_build_artifacts(
    update_dir: Path, logger: logging.Logger
) -> list[str]:
    removed: list[str] = []
    for rel_path in _UPDATE_BUILD_ARTIFACT_DIRS:
        path = update_dir / rel_path
        if _remove_update_artifact(path):
            removed.append(rel_path)
    for pattern in _UPDATE_BUILD_ARTIFACT_GLOBS:
        for path in sorted(update_dir.glob(pattern)):
            if _remove_update_artifact(path):
                removed.append(str(path.relative_to(update_dir)))
    if removed:
        logger.info(
            "Removed cached update build artifacts: %s",
            ", ".join(removed),
        )
    return removed


def _refresh_failure_is_retryable(output_lines: list[str]) -> bool:
    if not output_lines:
        return False
    haystack = "\n".join(output_lines).lower()
    if "codex-autorunner" not in haystack:
        return False
    if "no such file or directory" not in haystack:
        return False
    if "build/" not in haystack and "build\\" not in haystack:
        return False
    return (
        "failed building wheel for codex-autorunner" in haystack
        or "building wheel for codex-autorunner" in haystack
    )


def _reset_update_cache_for_retry(
    update_dir: Path,
    *,
    logger: logging.Logger,
) -> bool:
    if not update_dir.exists() or not _is_valid_git_repo(update_dir):
        logger.warning(
            "Skipping update refresh retry; cache at %s is not a valid git repo.",
            update_dir,
        )
        return False
    try:
        logger.warning(
            "Refresh failed with a retryable stale-build-artifact error; resetting tracked files and cleaning build artifacts in %s before retrying once.",
            update_dir,
        )
        _run_cmd(["git", "reset", "--hard", "FETCH_HEAD"], cwd=update_dir)
        _cleanup_update_build_artifacts(update_dir, logger)
    except (RuntimeError, OSError) as exc:
        logger.warning(
            "Aggressive update cache cleanup failed; refresh retry skipped. %s",
            exc,
        )
        return False
    return True


def _update_cache_refresh_failure_is_retryable(
    error: Union[BaseException, str],
) -> bool:
    message = str(error).lower()
    return any(hint in message for hint in _UPDATE_CACHE_RECOVERY_HINTS)


def _run_refresh_script(
    *,
    refresh_script: Path,
    update_dir: Path,
    env: dict[str, str],
    logger: logging.Logger,
) -> tuple[int, list[str]]:
    output_tail: deque[str] = deque(maxlen=400)
    proc = subprocess.Popen(
        [str(refresh_script)],
        cwd=update_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.stdout:
        for line in proc.stdout:
            rendered = line.rstrip("\n")
            logger.info("[Updater] %s", rendered)
            output_tail.append(rendered)
    proc.wait()
    return proc.returncode, list(output_tail)


def _normalize_update_target(raw: Optional[str]) -> str:
    return normalize_update_target(raw)


def _get_update_target_definition(raw: Optional[str]) -> UpdateTargetDefinition:
    return get_update_target_definition(raw)


def _normalize_update_backend(raw: Optional[str]) -> str:
    if raw is None:
        return "auto"
    value = str(raw).strip().lower()
    if value in ("", "auto", "launchd", "systemd-user", "systemd-system"):
        return value or "auto"
    raise ValueError(
        "Unsupported update backend "
        "(use auto, launchd, systemd-user, or systemd-system)."
    )


def _resolve_update_backend(raw: Optional[str]) -> str:
    backend = _normalize_update_backend(raw)
    if backend != "auto":
        return backend
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd-user"
    if resolve_executable("systemctl") is not None:
        return "systemd-user"
    return "launchd"


def _is_systemd_backend(backend: str) -> bool:
    return backend in ("systemd-user", "systemd-system")


def _systemd_scope_for_backend(backend: str) -> str:
    """Map a systemd backend to its systemctl scope ('user' or 'system')."""
    return "user" if backend == "systemd-user" else "system"


def _required_update_commands(backend: str) -> tuple[str, ...]:
    base = ("git", "bash", "curl")
    if backend == "launchd":
        return (*base, "launchctl")
    if _is_systemd_backend(backend):
        return (*base, "systemctl")
    raise ValueError(f"Unsupported update backend: {backend}")


def _is_systemd_service_active(service_name: str, *, scope: str = "user") -> bool:
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


def _is_systemd_user_service_active(service_name: str) -> bool:
    return _is_systemd_service_active(service_name, scope="user")


def _launchd_domain() -> str:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return f"gui/{uid}"


def _is_launchd_label_active(label: str) -> bool:
    if not label or resolve_executable("launchctl") is None:
        return False
    domain = _launchd_domain()
    try:
        result = subprocess.run(
            ["launchctl", "print", f"{domain}/{label}"],
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


def _chat_target_enableable(
    *,
    raw_config: Optional[dict[str, Any]],
    target: str,
) -> bool:
    raw = raw_config if isinstance(raw_config, dict) else {}
    if target == "telegram":
        cfg = raw.get("telegram_bot")
        if not isinstance(cfg, dict):
            return False
        if not bool(cfg.get("enabled", False)):
            return False
        env_name = str(cfg.get("bot_token_env", "CAR_TELEGRAM_BOT_TOKEN")).strip()
        return bool(env_name and os.environ.get(env_name))
    if target == "discord":
        cfg = raw.get("discord_bot")
        if not isinstance(cfg, dict):
            return False
        if not bool(cfg.get("enabled", False)):
            return False
        bot_env = str(cfg.get("bot_token_env", "CAR_DISCORD_BOT_TOKEN")).strip()
        app_env = str(cfg.get("app_id_env", "CAR_DISCORD_APP_ID")).strip()
        return bool(
            bot_env and app_env and os.environ.get(bot_env) and os.environ.get(app_env)
        )
    return False


def _chat_target_active(
    *,
    target: str,
    update_backend: str,
    linux_service_names: Optional[dict[str, str]],
) -> bool:
    try:
        backend = _resolve_update_backend(update_backend)
    except ValueError:
        backend = "launchd" if platform.system().lower() == "darwin" else "systemd-user"
    if _is_systemd_backend(backend):
        services = {
            "telegram": "car-telegram",
            "discord": "car-discord",
        }
        if isinstance(linux_service_names, dict):
            for key in ("telegram", "discord"):
                value = linux_service_names.get(key)
                if isinstance(value, str) and value.strip():
                    services[key] = value.strip()
        service_name = services.get(target, "")
        return _is_systemd_service_active(
            service_name, scope=_systemd_scope_for_backend(backend)
        )
    base_label = str(os.environ.get("LABEL", "com.codex.autorunner")).strip()
    telegram_label = str(
        os.environ.get("TELEGRAM_LABEL", f"{base_label}.telegram")
    ).strip()
    discord_label = str(
        os.environ.get("DISCORD_LABEL", f"{base_label}.discord")
    ).strip()
    if target == "telegram":
        return _is_launchd_label_active(telegram_label)
    if target == "discord":
        return _is_launchd_label_active(discord_label)
    return False


def _available_update_target_definitions(
    *,
    raw_config: Optional[dict[str, Any]] = None,
    update_backend: str = "auto",
    linux_service_names: Optional[dict[str, str]] = None,
    include_runtime_probes: bool = True,
) -> tuple[UpdateTargetDefinition, ...]:
    telegram_available = _chat_target_enableable(
        raw_config=raw_config, target="telegram"
    ) or (
        include_runtime_probes
        and _chat_target_active(
            target="telegram",
            update_backend=update_backend,
            linux_service_names=linux_service_names,
        )
    )
    discord_available = _chat_target_enableable(
        raw_config=raw_config, target="discord"
    ) or (
        include_runtime_probes
        and _chat_target_active(
            target="discord",
            update_backend=update_backend,
            linux_service_names=linux_service_names,
        )
    )

    return available_update_target_definitions(
        telegram_available=telegram_available,
        discord_available=discord_available,
    )


def _available_update_target_options(
    *,
    raw_config: Optional[dict[str, Any]] = None,
    update_backend: str = "auto",
    linux_service_names: Optional[dict[str, str]] = None,
    include_runtime_probes: bool = True,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (definition.value, definition.label)
        for definition in _available_update_target_definitions(
            raw_config=raw_config,
            update_backend=update_backend,
            linux_service_names=linux_service_names,
            include_runtime_probes=include_runtime_probes,
        )
    )


def _default_update_target(
    *,
    raw_config: Optional[dict[str, Any]] = None,
    update_backend: str = "auto",
    linux_service_names: Optional[dict[str, str]] = None,
    include_runtime_probes: bool = True,
) -> str:
    values = {
        definition.value
        for definition in _available_update_target_definitions(
            raw_config=raw_config,
            update_backend=update_backend,
            linux_service_names=linux_service_names,
            include_runtime_probes=include_runtime_probes,
        )
    }
    if "all" in values:
        return "all"
    return "web"


def _refresh_script(backend: str, update_dir: Path) -> Optional[Path]:
    if backend == "launchd":
        return update_dir / "scripts" / "safe-refresh-local-mac-hub.sh"
    if _is_systemd_backend(backend):
        return update_dir / "scripts" / "safe-refresh-local-linux-hub.sh"
    return None


def _backend_refresh_label(backend: str) -> str:
    if backend == "systemd-user":
        return "systemd user service"
    if backend == "systemd-system":
        return "systemd system service"
    return "launchd service"


def _normalize_update_ref(raw: Optional[str]) -> str:
    value = str(raw or "").strip()
    return value or "main"


def _format_update_confirmation_warning(
    *,
    active_count: int,
    singular_label: str = "session",
    plural_label: Optional[str] = None,
) -> Optional[str]:
    try:
        count = int(active_count)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return None
    singular = str(singular_label or "session").strip() or "session"
    plural = str(plural_label or f"{singular}s").strip() or f"{singular}s"
    label = singular if count == 1 else plural
    verb = "is" if count == 1 else "are"
    return (
        f"{count} active {label} {verb} still running. "
        "Updating will restart the service. Continue?"
    )


def _update_target_restarts_surface(
    raw_target: Optional[str],
    *,
    surface: str,
) -> bool:
    definition = _get_update_target_definition(raw_target)
    normalized_surface = str(surface or "").strip().lower()
    if normalized_surface == "web":
        return bool(definition.includes_web)
    if normalized_surface == "telegram":
        return definition.value in {"all", "chat", "telegram"}
    if normalized_surface == "discord":
        return definition.value in {"all", "chat", "discord"}
    raise ValueError(f"Unsupported update surface: {surface}")


def _update_status_path() -> Path:
    return resolve_update_paths().status_path


def _write_update_status(status: str, message: str, **extra) -> None:
    payload = {"status": status, "message": message, "at": time.time(), **extra}
    path = _update_status_path()
    existing = None
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
    if isinstance(existing, dict):
        for key in (
            "notify_chat_id",
            "notify_thread_id",
            "notify_reply_to",
            "notify_platform",
            "notify_context",
            "notify_sent_at",
            "phase_timings",
            "last_phase_timing",
        ):
            if key not in payload and key in existing:
                payload[key] = existing[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _refresh_script_committed_ok_status(status: object) -> bool:
    return isinstance(status, dict) and status.get("status") == "ok"


_REFRESH_OUTPUT_SUMMARY_MAX_CHARS = 300


def _summarize_refresh_output(output_lines: list[str]) -> str:
    """Return the last meaningful refresh-script line for surfacing in status.

    The refresh script can die under ``set -euo pipefail`` without writing its
    own status (e.g. a failing command substitution), leaving only a generic
    worker message. Surfacing the last output line makes such failures
    observable instead of opaque "check hub logs" errors.
    """
    for line in reversed(output_lines or []):
        text = str(line).strip()
        if text:
            if len(text) > _REFRESH_OUTPUT_SUMMARY_MAX_CHARS:
                text = text[: _REFRESH_OUTPUT_SUMMARY_MAX_CHARS - 1].rstrip() + "…"
            return text
    return ""


def _is_valid_git_repo(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def _has_valid_head(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _read_update_status() -> Optional[dict[str, object]]:
    path = _update_status_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if status in ("running", "spawned") and _update_lock_active() is None:
        started_at = payload.get("at")
        if (
            isinstance(started_at, (int, float))
            and (time.time() - float(started_at)) < _UPDATE_LOCK_STARTUP_GRACE_SECONDS
        ):
            return payload
        _write_update_status(
            "error",
            "Update not running; last update may have crashed.",
            previous_status=status,
        )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
    return payload


def _update_lock_path() -> Path:
    return resolve_update_paths().lock_path


def _read_update_lock() -> Optional[dict[str, object]]:
    path = _update_lock_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _update_lock_active() -> Optional[dict]:
    lock = _read_update_lock()
    if not lock:
        try:
            _update_lock_path().unlink()
        except OSError:
            pass
        return None
    pid = lock.get("pid")
    if isinstance(pid, int):
        pid_matches = process_matches_identity(
            pid,
            expected_cmd_substrings=_UPDATE_LOCK_CMD_HINTS,
        )
        if pid_matches:
            return lock
    try:
        _update_lock_path().unlink()
    except OSError:
        pass
    return None


def _acquire_update_lock(
    *, repo_url: str, repo_ref: str, update_target: str, logger: logging.Logger
) -> bool:
    lock_path = _update_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "started_at": time.time(),
        "repo_url": repo_url,
        "repo_ref": repo_ref,
        "update_target": update_target,
    }
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        existing = _update_lock_active()
        if existing:
            msg = f"Update already running (pid {existing.get('pid')})."
            logger.info(msg)
            raise UpdateInProgressError(msg) from exc
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            msg = "Update already running."
            logger.info(msg)
            raise UpdateInProgressError(msg) from exc
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(payload))
    return True


def _release_update_lock() -> None:
    lock = _read_update_lock()
    if not lock or lock.get("pid") != os.getpid():
        return
    try:
        _update_lock_path().unlink()
    except OSError:
        pass


def _find_git_root(start: Path) -> Optional[Path]:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _find_git_root_from_install_metadata() -> Optional[Path]:
    """
    Best-effort: when installed from a local directory, pip may record a PEP 610
    direct URL which can point back to a working tree that has a .git directory.
    """
    try:
        dist = importlib.metadata.distribution("codex-autorunner")
    except importlib.metadata.PackageNotFoundError:
        return None

    direct_url = dist.read_text("direct_url.json")
    if not direct_url:
        return None

    try:
        payload = json.loads(direct_url)
    except json.JSONDecodeError:
        return None

    raw_url = payload.get("url")
    if not isinstance(raw_url, str) or not raw_url:
        return None

    parsed = urlparse(raw_url)
    if parsed.scheme != "file":
        return None

    candidate = Path(unquote(parsed.path)).expanduser()
    if not candidate.exists():
        return None

    return _find_git_root(candidate)


def _resolve_local_repo_root(
    *, module_dir: Path, update_cache_dir: Path
) -> Optional[Path]:
    repo_root = _find_git_root(module_dir)
    if repo_root is not None:
        return repo_root

    if (update_cache_dir / ".git").exists() and _has_valid_head(update_cache_dir):
        return update_cache_dir

    return _find_git_root_from_install_metadata()


def _system_update_check(
    *,
    repo_url: str,
    repo_ref: str,
    module_dir: Optional[Path] = None,
    update_cache_dir: Optional[Path] = None,
) -> dict:
    module_dir = module_dir or Path(__file__).resolve().parent
    update_cache_dir = update_cache_dir or resolve_update_paths().cache_dir
    repo_ref = _normalize_update_ref(repo_ref)

    repo_root = _resolve_local_repo_root(
        module_dir=module_dir, update_cache_dir=update_cache_dir
    )
    if repo_root is None:
        return {
            "status": "ok",
            "update_available": True,
            "message": "No local git state found; update may be available.",
        }

    try:
        local_sha = run_git(["rev-parse", "HEAD"], repo_root, check=True).stdout.strip()
    except GitError as exc:
        return {
            "status": "ok",
            "update_available": True,
            "message": f"Unable to read local git state ({exc}); update may be available.",
        }

    try:
        run_git(
            ["fetch", "--quiet", repo_url, repo_ref],
            repo_root,
            timeout_seconds=_GIT_FETCH_UPDATE_TIMEOUT_SECONDS,
            check=True,
        )
        remote_sha = run_git(
            ["rev-parse", "FETCH_HEAD"], repo_root, check=True
        ).stdout.strip()
    except GitError as exc:
        return {
            "status": "ok",
            "update_available": True,
            "message": f"Unable to check remote updates ({exc}); you can try updating anyway.",
            "local_commit": local_sha,
        }

    if not remote_sha or not local_sha:
        return {
            "status": "ok",
            "update_available": True,
            "message": "Unable to determine update status; you can try updating anyway.",
        }

    if remote_sha == local_sha:
        return {
            "status": "ok",
            "update_available": False,
            "message": "No update available (already up to date).",
            "local_commit": local_sha,
            "remote_commit": remote_sha,
        }

    local_is_ancestor = (
        run_git(
            ["merge-base", "--is-ancestor", local_sha, remote_sha], repo_root
        ).returncode
        == 0
    )
    remote_is_ancestor = (
        run_git(
            ["merge-base", "--is-ancestor", remote_sha, local_sha], repo_root
        ).returncode
        == 0
    )

    if local_is_ancestor:
        message = "Update available."
        update_available = True
    elif remote_is_ancestor:
        message = "No update available (local version is ahead of remote)."
        update_available = False
    else:
        message = "Update available (local version diverged from remote)."
        update_available = True

    return {
        "status": "ok",
        "update_available": update_available,
        "message": message,
        "local_commit": local_sha,
        "remote_commit": remote_sha,
    }


def _capture_update_identity_hint() -> dict[str, Any]:
    from .detect import detect_supervisor_identity

    pipx_root = Path(
        os.environ.get("PIPX_ROOT", str(Path("~/.local/pipx").expanduser()))
    ).expanduser()
    current_link = Path(
        os.environ.get(
            "CURRENT_VENV_LINK",
            str(pipx_root / "venvs" / "codex-autorunner.current"),
        )
    ).expanduser()
    local_bin = Path(
        os.environ.get("LOCAL_BIN", str(Path("~/.local/bin").expanduser()))
    ).expanduser()
    car_wrapper = Path(
        os.environ.get("CAR_WRAPPER_PATH", str(local_bin / "car"))
    ).expanduser()
    identity = detect_supervisor_identity(
        current_venv_link=current_link,
        car_wrapper_path=car_wrapper,
        hub_pid=os.getpid(),
    )
    hint: dict[str, Any] = {
        "backend": identity.backend,
        "scope": identity.scope,
        "unit_name": identity.unit_name,
        "label": identity.label,
        "hub_pid": identity.hub_pid,
        "exec_start_or_program": identity.exec_start_or_program,
    }
    if identity.hub_root is not None:
        hint["hub_root"] = str(identity.hub_root)
    return hint


def _system_update_worker(
    *,
    repo_url: str,
    repo_ref: str,
    update_dir: Path,
    logger: logging.Logger,
    update_target: str = "all",
    update_backend: str = "auto",
    skip_checks: bool = True,
    linux_hub_service_name: Optional[str] = None,
    linux_telegram_service_name: Optional[str] = None,
    linux_discord_service_name: Optional[str] = None,
    restart_command: Optional[Union[str, list[str]]] = None,
    systemctl_sudo: str = "auto",
    allow_in_place: bool = False,
    identity_hint: Optional[dict[str, Any]] = None,
    server_host: str = "127.0.0.1",
    server_port: int = 4173,
    server_base_path: str = "",
) -> None:
    from .engine import UpdateEngine, UpdateEngineConfig

    linux_names: dict[str, str] = {}
    if linux_hub_service_name:
        linux_names["hub"] = linux_hub_service_name
    if linux_telegram_service_name:
        linux_names["telegram"] = linux_telegram_service_name
    if linux_discord_service_name:
        linux_names["discord"] = linux_discord_service_name

    config = UpdateEngineConfig(
        repo_url=repo_url,
        repo_ref=repo_ref,
        update_dir=update_dir,
        update_target=update_target,
        update_backend=update_backend,
        skip_checks=skip_checks,
        linux_service_names=linux_names,
        restart_command=restart_command,
        systemctl_sudo=systemctl_sudo,
        allow_in_place=allow_in_place,
        identity_hint=identity_hint,
        helper_python=sys.executable or None,
        server_host=server_host,
        server_port=server_port,
        server_base_path=server_base_path,
    )
    UpdateEngine(config, logger=logger).run()


def _spawn_update_process(
    *,
    repo_url: str,
    repo_ref: str,
    update_dir: Path,
    logger: logging.Logger,
    update_target: str = "all",
    update_backend: str = "auto",
    skip_checks: bool = True,
    notify_chat_id: Optional[int] = None,
    notify_thread_id: Optional[int] = None,
    notify_reply_to: Optional[int] = None,
    notify_platform: Optional[str] = None,
    notify_context: Optional[dict[str, Any]] = None,
    linux_hub_service_name: Optional[str] = None,
    linux_telegram_service_name: Optional[str] = None,
    linux_discord_service_name: Optional[str] = None,
    restart_command: Optional[Union[str, list[str]]] = None,
    systemctl_sudo: str = "auto",
    allow_in_place: bool = False,
    server_host: str = "127.0.0.1",
    server_port: int = 4173,
    server_base_path: str = "",
) -> None:
    active = _update_lock_active()
    if active:
        raise UpdateInProgressError(
            f"Update already running (pid {active.get('pid')})."
        )
    status_path = _update_status_path()
    log_path = status_path.parent / "update-standalone.log"
    _write_update_status(
        "running",
        "Update spawned.",
        phase="spawned",
        repo_url=repo_url,
        update_dir=str(update_dir),
        repo_ref=repo_ref,
        update_target=update_target,
        update_backend=update_backend,
        linux_hub_service_name=linux_hub_service_name,
        linux_telegram_service_name=linux_telegram_service_name,
        linux_discord_service_name=linux_discord_service_name,
        log_path=str(log_path),
        notify_chat_id=notify_chat_id,
        notify_thread_id=notify_thread_id,
        notify_reply_to=notify_reply_to,
        notify_platform=notify_platform,
        notify_context=notify_context if isinstance(notify_context, dict) else None,
        notify_sent_at=None,
    )
    identity_hint = _capture_update_identity_hint()
    cmd = [
        sys.executable,
        "-m",
        "codex_autorunner.core.update.runner",
        "--repo-url",
        repo_url,
        "--repo-ref",
        repo_ref,
        "--update-dir",
        str(update_dir),
        "--target",
        update_target,
        "--log-path",
        str(log_path),
        "--identity-hint",
        json.dumps(identity_hint),
        "--server-host",
        server_host,
        "--server-port",
        str(server_port),
        "--server-base-path",
        server_base_path,
    ]
    cmd.extend(["--backend", update_backend])
    cmd.extend(["--systemctl-sudo", systemctl_sudo])
    if allow_in_place:
        cmd.append("--allow-in-place")
    if restart_command is not None:
        if isinstance(restart_command, list):
            cmd.extend(["--restart-command", json.dumps(restart_command)])
        else:
            cmd.extend(["--restart-command", restart_command])
    if linux_hub_service_name:
        cmd.extend(["--hub-service-name", linux_hub_service_name])
    if linux_telegram_service_name:
        cmd.extend(["--telegram-service-name", linux_telegram_service_name])
    if linux_discord_service_name:
        cmd.extend(["--discord-service-name", linux_discord_service_name])
    if skip_checks:
        cmd.append("--skip-checks")
    else:
        cmd.append("--no-skip-checks")
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            subprocess.Popen(
                cmd,
                cwd=str(update_dir.parent),
                start_new_session=True,
                stdout=log_file,
                stderr=log_file,
            )
    except Exception:  # intentional: top-level error handler
        logger.exception("Failed to spawn update worker")
        _write_update_status(
            "error",
            "Failed to spawn update worker; see hub logs for details.",
        )
