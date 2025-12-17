import logging
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..config import HubConfig


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
