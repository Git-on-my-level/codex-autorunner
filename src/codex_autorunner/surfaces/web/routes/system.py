import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ....core import update as update_core
from ....core.config import HubConfig
from ....core.constants import DEFAULT_UPDATE_REPO_REF, DEFAULT_UPDATE_REPO_URL
from ....core.orchestration.execution_history_maintenance import (
    collect_execution_history_database_health,
)
from ....core.orchestration.sqlite import (
    collect_orchestration_control_plane_status,
    evaluate_current_orchestration_compatibility,
    refresh_orchestration_process_heartbeat,
)
from ....core.self_describe import collect_describe_data
from ....core.update import (
    UpdateInProgressError,
    _format_update_confirmation_warning,
    _normalize_update_ref,
    _normalize_update_target,
    _read_update_status,
    _spawn_update_process,
    _system_update_check,
    _update_target_restarts_surface,
)
from ....core.update_paths import resolve_update_paths
from ....core.utils import find_repo_root
from ..schemas import (
    SystemHealthResponse,
    SystemUpdateCheckResponse,
    SystemUpdateRequest,
    SystemUpdateResponse,
    SystemUpdateStatusResponse,
    SystemUpdateTargetOption,
    SystemUpdateTargetsResponse,
)
from ..serializers import build_orchestration_health

_system_update_worker = update_core._system_update_worker
_update_lock_active = update_core._update_lock_active
_update_lock_path = update_core._update_lock_path
_update_status_path = update_core._update_status_path
_normalize_update_backend = update_core._normalize_update_backend
_resolve_update_backend = update_core._resolve_update_backend
_required_update_commands = update_core._required_update_commands
_available_update_target_options = update_core._available_update_target_options
_available_update_target_definitions = update_core._available_update_target_definitions
_default_update_target = update_core._default_update_target
_get_update_target_definition = update_core._get_update_target_definition
shutil = update_core.shutil
subprocess = update_core.subprocess
sys = update_core.sys


def _count_active_terminal_sessions(request: Request) -> int:
    terminal_sessions = getattr(
        getattr(request.app, "state", None), "terminal_sessions", {}
    )
    if not isinstance(terminal_sessions, dict):
        return 0
    count = 0
    for session in terminal_sessions.values():
        pty = getattr(session, "pty", None)
        isalive = getattr(pty, "isalive", None)
        if callable(isalive) and isalive():
            count += 1
    return count


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
        orchestration_health = None
        compatibility_payload = None
        compatibility_ok = True
        if isinstance(config, HubConfig):
            compatibility = await asyncio.to_thread(
                evaluate_current_orchestration_compatibility,
                config.root,
                process_role="hub",
                durable=bool(getattr(config, "durable_writes", False)),
            )
            compatibility_ok = compatibility.compatible
            compatibility_payload = compatibility.to_dict()
            control_plane_status = await asyncio.to_thread(
                collect_orchestration_control_plane_status,
                config.root,
                process_role="hub",
                durable=bool(getattr(config, "durable_writes", False)),
            )
            if compatibility_ok:
                await asyncio.to_thread(
                    refresh_orchestration_process_heartbeat,
                    config.root,
                    process_role="hub",
                    observed_schema_generation=compatibility.observed_schema,
                )
            database_health = (
                await asyncio.to_thread(
                    collect_execution_history_database_health,
                    config.root,
                )
                if compatibility.compatible
                else None
            )
            last_housekeeping = getattr(
                getattr(request.app, "state", None),
                "orchestration_housekeeping",
                None,
            )
            orchestration_health = build_orchestration_health(
                database_health,
                last_housekeeping=(
                    last_housekeeping if isinstance(last_housekeeping, dict) else None
                ),
            )
            orchestration_health["control_plane"] = control_plane_status
        response: dict = {
            "status": "ok" if compatibility_ok else "restart_required",
            "mode": mode,
            "base_path": base_path,
            "asset_version": asset_version,
            "orchestration": orchestration_health,
        }
        if compatibility_payload is not None:
            response["compatibility"] = compatibility_payload
        if mode == "hub":
            supervisor = getattr(request.app.state, "hub_supervisor", None)
            if supervisor is not None:
                response["hub_startup_phase"] = getattr(
                    supervisor, "startup_phase", None
                )
            deferred_done = getattr(
                request.app.state, "hub_deferred_startup_complete", None
            )
            if deferred_done is not None:
                response["hub_deferred_startup_complete"] = deferred_done
        if isinstance(config, HubConfig) and not compatibility_ok:
            return JSONResponse(status_code=503, content=response)
        return response

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

        repo_url = DEFAULT_UPDATE_REPO_URL
        repo_ref = DEFAULT_UPDATE_REPO_REF
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
        except (
            Exception
        ) as e:  # intentional: git check runs in thread, unknown failure surface
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
        repo_url = DEFAULT_UPDATE_REPO_URL
        repo_ref = DEFAULT_UPDATE_REPO_REF
        skip_checks = True
        update_backend = "auto"
        update_services: Optional[dict[str, str]] = None
        linux_hub_service_name = None
        linux_telegram_service_name = None
        linux_discord_service_name = None
        restart_command = None
        systemctl_sudo = "auto"
        allow_in_place = False
        server_host = "127.0.0.1"
        server_port = 4173
        server_base_path = ""
        if config and isinstance(config, HubConfig):
            configured_url = getattr(config, "update_repo_url", None)
            if configured_url:
                repo_url = configured_url
            configured_ref = getattr(config, "update_repo_ref", None)
            if configured_ref:
                repo_ref = configured_ref
            skip_checks = bool(getattr(config, "update_skip_checks", True))
            update_backend = getattr(config, "update_backend", update_backend)
            update_services = getattr(config, "update_linux_service_names", None)
            if isinstance(update_services, dict):
                linux_hub_service_name = update_services.get("hub")
                linux_telegram_service_name = update_services.get("telegram")
                linux_discord_service_name = update_services.get("discord")
            restart_command = getattr(config, "update_restart_command", None)
            systemctl_sudo = str(
                getattr(config, "update_systemctl_sudo", systemctl_sudo)
            )
            allow_in_place = bool(getattr(config, "update_allow_in_place", False))
            server_host = str(getattr(config, "server_host", server_host))
            server_port = int(getattr(config, "server_port", server_port))
            server_base_path = str(
                getattr(config, "server_base_path", server_base_path)
            )
        elif config is not None:
            skip_checks = bool(getattr(config, "update_skip_checks", True))
            update_backend = getattr(config, "update_backend", update_backend)
            update_services = getattr(config, "update_linux_service_names", None)
            if isinstance(update_services, dict):
                linux_hub_service_name = update_services.get("hub")
                linux_telegram_service_name = update_services.get("telegram")
                linux_discord_service_name = update_services.get("discord")

        update_dir = resolve_update_paths(config=config).cache_dir

        try:
            target_raw = payload.target if payload else None
            force_update = bool(payload.force) if payload else False
            if target_raw is None:
                target_raw = request.query_params.get("target")
            if not force_update:
                force_update = request.query_params.get("force") in {
                    "1",
                    "true",
                    "yes",
                }
            if target_raw is None:
                target_raw = _default_update_target(
                    raw_config=(config.raw if hasattr(config, "raw") else None),
                    update_backend=str(update_backend),
                    linux_service_names=(
                        update_services if isinstance(update_services, dict) else None
                    ),
                )
            update_target = _normalize_update_target(target_raw)
            if not force_update and _update_target_restarts_surface(
                update_target, surface="web"
            ):
                warning = _format_update_confirmation_warning(
                    active_count=_count_active_terminal_sessions(request),
                    singular_label="terminal session",
                )
                if warning:
                    return {
                        "status": "warning",
                        "message": warning,
                        "target": update_target,
                        "requires_confirmation": True,
                    }
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
                update_backend=update_backend,
                linux_hub_service_name=(
                    linux_hub_service_name
                    if isinstance(linux_hub_service_name, str)
                    else None
                ),
                linux_telegram_service_name=(
                    linux_telegram_service_name
                    if isinstance(linux_telegram_service_name, str)
                    else None
                ),
                linux_discord_service_name=(
                    linux_discord_service_name
                    if isinstance(linux_discord_service_name, str)
                    else None
                ),
                restart_command=restart_command,
                systemctl_sudo=systemctl_sudo,
                allow_in_place=allow_in_place,
                server_host=server_host,
                server_port=server_port,
                server_base_path=server_base_path,
            )
            target_info = _get_update_target_definition(update_target)
            return {
                "status": "ok",
                "message": f"Update started ({target_info.label}). Service will restart shortly.",
                "target": update_target,
                "requires_confirmation": False,
            }
        except UpdateInProgressError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as e:  # intentional: last-resort handler for update endpoint
            logger = getattr(getattr(request.app, "state", None), "logger", None)
            if logger:
                logger.error("Update error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/system/update/targets", response_model=SystemUpdateTargetsResponse)
    async def system_update_targets(request: Request):
        try:
            config = request.app.state.config
        except AttributeError:
            config = None

        update_backend = "auto"
        update_services: Optional[dict[str, str]] = None
        raw_config = getattr(config, "raw", None)
        if config is not None:
            update_backend = str(getattr(config, "update_backend", update_backend))
            raw_services = getattr(config, "update_linux_service_names", None)
            if isinstance(raw_services, dict):
                update_services = raw_services

        options = _available_update_target_definitions(
            raw_config=raw_config if isinstance(raw_config, dict) else None,
            update_backend=update_backend,
            linux_service_names=update_services,
        )
        default_target = _default_update_target(
            raw_config=raw_config if isinstance(raw_config, dict) else None,
            update_backend=update_backend,
            linux_service_names=update_services,
        )
        return {
            "targets": [
                SystemUpdateTargetOption(
                    value=definition.value,
                    label=definition.label,
                    description=definition.description,
                    includes_web=definition.includes_web,
                    restart_notice=definition.restart_notice,
                )
                for definition in options
            ],
            "default_target": default_target,
        }

    @router.get("/system/update/status", response_model=SystemUpdateStatusResponse)
    async def system_update_status():
        status = await asyncio.to_thread(_read_update_status)
        if status is None:
            return {"status": "unknown", "message": "No update status recorded."}
        return status

    @router.get("/system/describe")
    async def system_describe(request: Request):
        try:
            repo_root = find_repo_root(request.app.state.engine.repo_root)
            return await asyncio.to_thread(collect_describe_data, repo_root)
        except (
            Exception
        ) as exc:  # intentional: diagnostic endpoint, surface any failure as 500
            logger = getattr(getattr(request.app, "state", None), "logger", None)
            if logger:
                logger.error("Describe endpoint error: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return router
