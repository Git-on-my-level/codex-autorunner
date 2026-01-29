import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..core import update as update_core
from ..core.config import HubConfig
from ..core.update import (
    UpdateInProgressError,
    _normalize_update_ref,
    _normalize_update_target,
    _read_update_status,
    _spawn_update_process,
    _system_update_check,
)
from ..core.update_paths import resolve_update_paths
from ..web.schemas import (
    SystemHealthResponse,
    SystemUpdateCheckResponse,
    SystemUpdateRequest,
    SystemUpdateResponse,
    SystemUpdateStatusResponse,
)
from ..web.static_assets import missing_static_assets
from ..web.static_refresh import refresh_static_assets

_pid_is_running = update_core._pid_is_running
_system_update_worker = update_core._system_update_worker
_update_lock_active = update_core._update_lock_active
_update_lock_path = update_core._update_lock_path
_update_status_path = update_core._update_status_path
shutil = update_core.shutil
subprocess = update_core.subprocess


def build_system_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/health", response_model=SystemHealthResponse)
    async def system_health(request: Request):
        try:
            config = request.app.state.config
        except AttributeError:
            config = None
        mode = "hub" if isinstance(config, HubConfig) else "repo"
        base_path = getattr(request.app.state, "base_path", "")
        asset_version = getattr(request.app.state, "asset_version", None)
        static_dir = getattr(getattr(request.app, "state", None), "static_dir", None)
        if not isinstance(static_dir, Path):
            return JSONResponse(
                {
                    "status": "error",
                    "detail": "Static UI assets missing; reinstall package",
                    "mode": mode,
                    "base_path": base_path,
                },
                status_code=500,
            )
        missing = await asyncio.to_thread(missing_static_assets, static_dir)
        if missing:
            if refresh_static_assets(request.app):
                static_dir = getattr(
                    getattr(request.app, "state", None), "static_dir", None
                )
                if isinstance(static_dir, Path):
                    missing = await asyncio.to_thread(missing_static_assets, static_dir)
                else:
                    missing = ["index.html"]
            if not missing:
                return {
                    "status": "ok",
                    "mode": mode,
                    "base_path": base_path,
                    "asset_version": asset_version,
                }
            return JSONResponse(
                {
                    "status": "error",
                    "detail": "Static UI assets missing; reinstall package",
                    "missing": missing,
                    "mode": mode,
                    "base_path": base_path,
                },
                status_code=500,
            )
        return {
            "status": "ok",
            "mode": mode,
            "base_path": base_path,
            "asset_version": asset_version,
        }

    @router.get("/system/update/check", response_model=SystemUpdateCheckResponse)
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
            return await asyncio.to_thread(
                _system_update_check, repo_url=repo_url, repo_ref=repo_ref
            )
        except Exception as e:
            logger = getattr(getattr(request.app, "state", None), "logger", None)
            if logger:
                logger.error("Update check error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/system/update", response_model=SystemUpdateResponse)
    async def system_update(
        request: Request, payload: Optional[SystemUpdateRequest] = None
    ):
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
        skip_checks = False
        if config and isinstance(config, HubConfig):
            configured_url = getattr(config, "update_repo_url", None)
            if configured_url:
                repo_url = configured_url
            configured_ref = getattr(config, "update_repo_ref", None)
            if configured_ref:
                repo_ref = configured_ref
            skip_checks = bool(getattr(config, "update_skip_checks", False))
        elif config is not None:
            skip_checks = bool(getattr(config, "update_skip_checks", False))

        update_dir = resolve_update_paths(config=config).cache_dir

        try:
            target_raw = payload.target if payload else None
            if target_raw is None:
                target_raw = request.query_params.get("target")
            update_target = _normalize_update_target(target_raw)
            logger = getattr(getattr(request.app, "state", None), "logger", None)
            if logger is None:
                logger = logging.getLogger("codex_autorunner.system_update")
            await asyncio.to_thread(
                _spawn_update_process,
                repo_url=repo_url,
                repo_ref=_normalize_update_ref(repo_ref),
                update_dir=update_dir,
                logger=logger,
                update_target=update_target,
                skip_checks=skip_checks,
            )
            return {
                "status": "ok",
                "message": f"Update started ({update_target}). Service will restart shortly.",
                "target": update_target,
            }
        except UpdateInProgressError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as e:
            logger = getattr(getattr(request.app, "state", None), "logger", None)
            if logger:
                logger.error("Update error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/system/update/status", response_model=SystemUpdateStatusResponse)
    async def system_update_status():
        status = await asyncio.to_thread(_read_update_status)
        if status is None:
            return {"status": "unknown", "message": "No update status recorded."}
        return status

    return router
