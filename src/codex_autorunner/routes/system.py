import logging
import os
import shutil
import subprocess
import importlib.metadata
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..config import HubConfig
from ..git_utils import GitError, run_git


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
    module_dir: Optional[Path] = None,
    update_cache_dir: Optional[Path] = None,
) -> dict:
    module_dir = module_dir or Path(__file__).resolve().parent
    update_cache_dir = update_cache_dir or (Path.home() / ".codex-autorunner" / "update_cache")

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
            ["fetch", "--quiet", repo_url, "main"],
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


def _system_update_worker(*, repo_url: str, update_dir: Path, logger: logging.Logger) -> None:
    try:
        update_dir.parent.mkdir(parents=True, exist_ok=True)

        if update_dir.exists() and (update_dir / ".git").exists():
            logger.info("Updating source in %s from %s", update_dir, repo_url)
            _run_cmd(["git", "fetch", "origin"], cwd=update_dir)
            _run_cmd(["git", "reset", "--hard", "origin/main"], cwd=update_dir)
        else:
            if update_dir.exists():
                shutil.rmtree(update_dir)
            logger.info("Cloning %s into %s", repo_url, update_dir)
            _run_cmd(["git", "clone", repo_url, str(update_dir)], cwd=update_dir.parent)

        logger.info("Running checks...")
        _run_cmd(["./scripts/check.sh"], cwd=update_dir)

        logger.info("Refreshing launchd service...")
        refresh_script = update_dir / "scripts" / "safe-refresh-local-mac-hub.sh"
        if not refresh_script.exists():
            refresh_script = update_dir / "scripts" / "refresh-local-mac-hub.sh"

        env = os.environ.copy()
        env["PACKAGE_SRC"] = str(update_dir)

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
    except Exception:
        logger.exception("System update failed")


def build_system_routes() -> APIRouter:
    router = APIRouter()

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
        if config and isinstance(config, HubConfig):
            configured_url = getattr(config, "update_repo_url", None)
            if configured_url:
                repo_url = configured_url

        try:
            return _system_update_check(repo_url=repo_url)
        except Exception as e:
            logger = getattr(getattr(request.app, "state", None), "logger", None)
            if logger:
                logger.error("Update check error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/system/update")
    async def system_update(request: Request, background_tasks: BackgroundTasks):
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
        if config and isinstance(config, HubConfig):
            configured_url = getattr(config, "update_repo_url", None)
            if configured_url:
                repo_url = configured_url

        home_dot_car = Path.home() / ".codex-autorunner"
        update_dir = home_dot_car / "update_cache"

        try:
            logger = getattr(getattr(request.app, "state", None), "logger", None)
            if logger is None:
                logger = logging.getLogger("codex_autorunner.system_update")

            background_tasks.add_task(
                _system_update_worker,
                repo_url=repo_url,
                update_dir=update_dir,
                logger=logger,
            )
            return {"status": "ok", "message": "Update started. Service will restart shortly."}
        except Exception as e:
            logger = getattr(getattr(request.app, "state", None), "logger", None)
            if logger:
                logger.error("Update error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    return router
