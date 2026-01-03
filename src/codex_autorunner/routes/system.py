import importlib.metadata
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, Request

from ..config import HubConfig
from ..git_utils import GitError, run_git


class UpdateInProgressError(RuntimeError):
    """Raised when an update is already running."""


def _run_cmd(cmd: list[str], cwd: Path) -> None:
    """Run a subprocess command, raising on failure."""
    try:
        subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,  # 5 mins should be enough for clone/install
        )
    except subprocess.CalledProcessError as e:
        # Include stdout/stderr in the error message for debugging
        detail = f"Command failed: {' '.join(cmd)}\nStdout: {e.stdout}\nStderr: {e.stderr}"
        raise RuntimeError(detail) from e


def _normalize_update_target(raw: Optional[str]) -> str:
    if raw is None:
        return "both"
    value = str(raw).strip().lower()
    if value in ("", "both", "all"):
        return "both"
    if value in ("web", "hub", "server", "ui"):
        return "web"
    if value in ("telegram", "tg", "bot"):
        return "telegram"
    raise ValueError("Unsupported update target (use both, web, or telegram).")


def _normalize_update_ref(raw: Optional[str]) -> str:
    value = str(raw or "").strip()
    return value or "main"


def _update_status_path() -> Path:
    return Path.home() / ".codex-autorunner" / "update_status.json"


def _write_update_status(status: str, message: str, **extra) -> None:
    payload = {"status": status, "message": message, "at": time.time(), **extra}
    path = _update_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_update_status() -> Optional[dict]:
    path = _update_status_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _update_lock_path() -> Path:
    return Path.home() / ".codex-autorunner" / "update.lock"


def _read_update_lock() -> Optional[dict]:
    path = _update_lock_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _update_lock_active() -> Optional[dict]:
    lock = _read_update_lock()
    if not lock:
        try:
            _update_lock_path().unlink()
        except OSError:
            pass
        return None
    pid = lock.get("pid")
    if isinstance(pid, int) and _pid_is_running(pid):
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
    except FileExistsError:
        existing = _update_lock_active()
        if existing:
            msg = f"Update already running (pid {existing.get('pid')})."
            logger.info(msg)
            raise UpdateInProgressError(msg)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            msg = "Update already running."
            logger.info(msg)
            raise UpdateInProgressError(msg)
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
        import json

        payload = json.loads(direct_url)
    except Exception:
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


def _resolve_local_repo_root(*, module_dir: Path, update_cache_dir: Path) -> Optional[Path]:
    repo_root = _find_git_root(module_dir)
    if repo_root is not None:
        return repo_root

    if (update_cache_dir / ".git").exists():
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
    update_cache_dir = update_cache_dir or (Path.home() / ".codex-autorunner" / "update_cache")
    repo_ref = _normalize_update_ref(repo_ref)

    repo_root = _resolve_local_repo_root(module_dir=module_dir, update_cache_dir=update_cache_dir)
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
            timeout_seconds=60,
            check=True,
        )
        remote_sha = run_git(["rev-parse", "FETCH_HEAD"], repo_root, check=True).stdout.strip()
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
        run_git(["merge-base", "--is-ancestor", local_sha, remote_sha], repo_root).returncode
        == 0
    )
    remote_is_ancestor = (
        run_git(["merge-base", "--is-ancestor", remote_sha, local_sha], repo_root).returncode
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


def _system_update_worker(
    *,
    repo_url: str,
    repo_ref: str,
    update_dir: Path,
    logger: logging.Logger,
    update_target: str = "both",
) -> None:
    status_path = _update_status_path()
    lock_acquired = False
    try:
        try:
            update_target = _normalize_update_target(update_target)
        except ValueError as exc:
            msg = str(exc)
            logger.error(msg)
            _write_update_status("error", msg)
            return
        repo_ref = _normalize_update_ref(repo_ref)
        try:
            lock_acquired = _acquire_update_lock(
                repo_url=repo_url,
                repo_ref=repo_ref,
                update_target=update_target,
                logger=logger,
            )
        except UpdateInProgressError:
            return

        _write_update_status(
            "running",
            "Update started.",
            repo_url=repo_url,
            update_dir=str(update_dir),
            repo_ref=repo_ref,
            update_target=update_target,
        )

        missing = []
        for cmd in ("git", "bash", "launchctl", "curl"):
            if shutil.which(cmd) is None:
                missing.append(cmd)
        if missing:
            msg = f"Missing required commands: {', '.join(missing)}"
            logger.error(msg)
            _write_update_status("error", msg)
            return

        update_dir.parent.mkdir(parents=True, exist_ok=True)

        if update_dir.exists() and (update_dir / ".git").exists():
            logger.info(
                "Updating source in %s from %s (%s)", update_dir, repo_url, repo_ref
            )
            try:
                _run_cmd(["git", "remote", "set-url", "origin", repo_url], cwd=update_dir)
            except Exception:
                _run_cmd(["git", "remote", "add", "origin", repo_url], cwd=update_dir)
            _run_cmd(["git", "fetch", "origin", repo_ref], cwd=update_dir)
            _run_cmd(["git", "reset", "--hard", "FETCH_HEAD"], cwd=update_dir)
        else:
            if update_dir.exists():
                shutil.rmtree(update_dir)
            logger.info("Cloning %s into %s", repo_url, update_dir)
            _run_cmd(["git", "clone", repo_url, str(update_dir)], cwd=update_dir.parent)
            _run_cmd(["git", "fetch", "origin", repo_ref], cwd=update_dir)
            _run_cmd(["git", "reset", "--hard", "FETCH_HEAD"], cwd=update_dir)

        if os.environ.get("CODEX_AUTORUNNER_SKIP_UPDATE_CHECKS") == "1":
            logger.info("Skipping update checks (CODEX_AUTORUNNER_SKIP_UPDATE_CHECKS=1).")
        else:
            logger.info("Running checks...")
            try:
                _run_cmd(["./scripts/check.sh"], cwd=update_dir)
            except Exception as exc:
                logger.warning("Checks failed; continuing with refresh. %s", exc)

        logger.info("Refreshing launchd service...")
        refresh_script = update_dir / "scripts" / "safe-refresh-local-mac-hub.sh"
        if not refresh_script.exists():
            refresh_script = update_dir / "scripts" / "refresh-local-mac-hub.sh"

        env = os.environ.copy()
        env["PACKAGE_SRC"] = str(update_dir)
        env["UPDATE_STATUS_PATH"] = str(status_path)
        env["UPDATE_TARGET"] = update_target

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
                logger.info("[Updater] %s", line.rstrip("\n"))
        proc.wait()
        if proc.returncode != 0:
            existing = _read_update_status()
            if not existing or existing.get("status") not in ("rollback", "error"):
                _write_update_status(
                    "rollback",
                    "Update failed; rollback attempted. Check hub logs for details.",
                    exit_code=proc.returncode,
                )
            return

        existing = _read_update_status()
        if not existing or existing.get("status") not in ("rollback", "error"):
            _write_update_status(
                "ok", "Update completed successfully.", update_target=update_target
            )
    except Exception:
        logger.exception("System update failed")
        _write_update_status(
            "error",
            "Update crashed; see hub logs for details.",
        )
    finally:
        if lock_acquired:
            _release_update_lock()


def _spawn_update_process(
    *,
    repo_url: str,
    repo_ref: str,
    update_dir: Path,
    logger: logging.Logger,
    update_target: str = "both",
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
        repo_url=repo_url,
        update_dir=str(update_dir),
        repo_ref=repo_ref,
        update_target=update_target,
        log_path=str(log_path),
    )
    cmd = [
        sys.executable,
        "-m",
        "codex_autorunner.update_runner",
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
    ]
    try:
        subprocess.Popen(
            cmd,
            cwd=str(update_dir.parent),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        logger.exception("Failed to spawn update worker")
        _write_update_status(
            "error",
            "Failed to spawn update worker; see hub logs for details.",
        )


def build_system_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def system_health(request: Request):
        try:
            config = request.app.state.config
        except AttributeError:
            config = None
        mode = "hub" if isinstance(config, HubConfig) else "repo"
        base_path = getattr(request.app.state, "base_path", "")
        asset_version = getattr(request.app.state, "asset_version", None)
        return {
            "status": "ok",
            "mode": mode,
            "base_path": base_path,
            "asset_version": asset_version,
        }

    @router.get("/system/update/check")
    async def system_update_check(request: Request):
        """
        Check if an update is available by comparing local git state vs remote.
        If local git state is unavailable, report that an update may be available.
        """
        try:
            config = request.app.state.config
        except AttributeError:
            config = None

        repo_url = "https://github.com/Git-on-my-level/codex-autorunner.git"
        repo_ref = "main"
        if config and isinstance(config, HubConfig):
            configured_url = getattr(config, "update_repo_url", None)
            if configured_url:
                repo_url = configured_url
            configured_ref = getattr(config, "update_repo_ref", None)
            if configured_ref:
                repo_ref = configured_ref

        try:
            return _system_update_check(repo_url=repo_url, repo_ref=repo_ref)
        except Exception as e:
            logger = getattr(getattr(request.app, "state", None), "logger", None)
            if logger:
                logger.error("Update check error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/system/update")
    async def system_update(request: Request):
        """
        Pull latest code and refresh the running service.
        This will restart the server if successful.
        """
        try:
            config = request.app.state.config
        except AttributeError:
            config = None

        # Determine URL
        repo_url = "https://github.com/Git-on-my-level/codex-autorunner.git"
        repo_ref = "main"
        if config and isinstance(config, HubConfig):
            configured_url = getattr(config, "update_repo_url", None)
            if configured_url:
                repo_url = configured_url
            configured_ref = getattr(config, "update_repo_ref", None)
            if configured_ref:
                repo_ref = configured_ref

        home_dot_car = Path.home() / ".codex-autorunner"
        update_dir = home_dot_car / "update_cache"

        try:
            try:
                payload = await request.json()
            except Exception:
                payload = None
            target_raw = None
            if isinstance(payload, dict):
                target_raw = payload.get("target")
            if target_raw is None:
                target_raw = request.query_params.get("target")
            update_target = _normalize_update_target(target_raw)
            logger = getattr(getattr(request.app, "state", None), "logger", None)
            if logger is None:
                logger = logging.getLogger("codex_autorunner.system_update")
            _spawn_update_process(
                repo_url=repo_url,
                repo_ref=_normalize_update_ref(repo_ref),
                update_dir=update_dir,
                logger=logger,
                update_target=update_target,
            )
            return {
                "status": "ok",
                "message": f"Update started ({update_target}). Service will restart shortly.",
                "target": update_target,
            }
        except UpdateInProgressError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as e:
            logger = getattr(getattr(request.app, "state", None), "logger", None)
            if logger:
                logger.error("Update error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/system/update/status")
    async def system_update_status():
        status = _read_update_status()
        if status is None:
            return {"status": "unknown", "message": "No update status recorded."}
        return status

    return router
