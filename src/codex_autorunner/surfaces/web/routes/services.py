from __future__ import annotations

import asyncio
import inspect
import logging
import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Iterable, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
import websockets
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, WebSocket
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.exceptions import ConnectionClosed, WebSocketException

from ....core.config_validation import is_loopback_host
from ....core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE
from ....core.path_utils import ConfigPathError, resolve_config_path
from ....core.preview_services import (
    PreviewPortAllocationError,
    PreviewServiceKind,
    PreviewServiceNotFoundError,
    PreviewServiceRecord,
    PreviewServiceStatus,
    PreviewServiceSupervisor,
    PreviewServiceSupervisorError,
    service_read_model,
)
from ....core.preview_services import (
    services_read_model as build_services_read_model,
)
from ....core.state_roots import resolve_hub_state_root
from ..app_state import HubAppContext
from ..services.preview_capabilities import (
    DEFAULT_PREVIEW_CAPABILITY_TTL_SECONDS,
    PreviewCapabilityStore,
)

logger = logging.getLogger("codex_autorunner.preview_services.routes")

DEFAULT_PROXY_MAX_BODY_BYTES = 10 * 1024 * 1024
DEFAULT_PROXY_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_PROXY_READ_TIMEOUT_SECONDS = 60.0
DEFAULT_PROXY_WRITE_TIMEOUT_SECONDS = 60.0
DEFAULT_PROXY_POOL_TIMEOUT_SECONDS = 5.0
DEFAULT_PROXY_MAX_GLOBAL_STREAMS = 128
DEFAULT_PROXY_MAX_SERVICE_STREAMS = 16
PROXY_STREAM_ACQUIRE_TIMEOUT_SECONDS = 0.1

_GLOBAL_PROXY_SEMAPHORE: asyncio.Semaphore | None = None
_GLOBAL_PROXY_SEMAPHORE_LIMIT: int | None = None
_SERVICE_PROXY_SEMAPHORES: dict[str, tuple[int, asyncio.Semaphore]] = {}
_ROOT_RELATIVE_HTML_URL_RE = re.compile(
    r"(?P<quote>[\"'])/(?!(?:/|[a-zA-Z][a-zA-Z0-9+.-]*:))"
    r"(?P<path>[^\"'<> \t\r\n]*)"
    r"(?P=quote)"
)


class ServiceScopeLinkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: str
    id: Optional[str] = None
    path: Optional[str] = None


class StaticSourcePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: str
    workspace_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("workspace_id", "workspaceId"),
    )
    path: str


class RegisterStaticServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str
    source: Optional[StaticSourcePayload] = None
    name: Optional[str] = None
    kind: Optional[str] = None
    scope_links: list[ServiceScopeLinkPayload] = Field(
        default_factory=list,
        validation_alias=AliasChoices("scope_links", "scopeLinks"),
    )
    created_by: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("created_by", "createdBy"),
    )
    service_class: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("service_class", "serviceClass"),
    )
    trust_level: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("trust_level", "trustLevel"),
    )
    ownership: Optional[str] = None
    network_policy: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("network_policy", "networkPolicy"),
    )


class RegisterLoopbackServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str
    name: Optional[str] = None
    health_path: Optional[str] = Field(
        default="/",
        validation_alias=AliasChoices("health_path", "healthPath"),
    )
    scope_links: list[ServiceScopeLinkPayload] = Field(
        default_factory=list,
        validation_alias=AliasChoices("scope_links", "scopeLinks"),
    )
    created_by: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("created_by", "createdBy"),
    )
    service_class: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("service_class", "serviceClass"),
    )
    trust_level: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("trust_level", "trustLevel"),
    )
    ownership: Optional[str] = None
    network_policy: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("network_policy", "networkPolicy"),
    )


class RegisterManagedServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    argv: list[str] = Field(default_factory=list)
    cwd: str
    env: dict[str, str] = Field(default_factory=dict)
    env_policy: str = Field(
        default="minimal",
        validation_alias=AliasChoices("env_policy", "envPolicy"),
    )
    port_policy: Optional[dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("port_policy", "portPolicy"),
    )
    health_check: Optional[dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("health_check", "healthCheck"),
    )
    scope_links: list[ServiceScopeLinkPayload] = Field(
        default_factory=list,
        validation_alias=AliasChoices("scope_links", "scopeLinks"),
    )
    created_by: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("created_by", "createdBy"),
    )
    auto_start_on_hub_start: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "auto_start_on_hub_start",
            "autoStartOnHubStart",
        ),
    )
    service_class: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("service_class", "serviceClass"),
    )
    trust_level: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("trust_level", "trustLevel"),
    )
    ownership: Optional[str] = None
    network_policy: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("network_policy", "networkPolicy"),
    )
    start: bool = False


class UpdateServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: Optional[str] = None
    scope_links: Optional[list[ServiceScopeLinkPayload]] = Field(
        default=None,
        validation_alias=AliasChoices("scope_links", "scopeLinks"),
    )
    health_check: Optional[dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("health_check", "healthCheck"),
    )
    command: Optional[dict[str, Any]] = None
    port_policy: Optional[dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("port_policy", "portPolicy"),
    )
    restart_policy: Optional[dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("restart_policy", "restartPolicy"),
    )
    service_class: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("service_class", "serviceClass"),
    )
    trust_level: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("trust_level", "trustLevel"),
    )
    ownership: Optional[str] = None
    network_policy: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("network_policy", "networkPolicy"),
    )
    metadata: Optional[dict[str, Any]] = None


class DestructiveServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    force: bool = False
    force_attestation: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("force_attestation", "forceAttestation"),
    )


OptionalDestructiveBody = Annotated[
    DestructiveServiceRequest,
    Body(default_factory=DestructiveServiceRequest),
]


def build_services_routes(context: HubAppContext) -> APIRouter:
    def _require_preview_services_enabled() -> None:
        if not _preview_services_enabled(context):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Preview services are disabled. "
                    "Set preview_services.enabled=true in the hub config to enable."
                ),
            )

    router = APIRouter(
        tags=["hub-services"],
        dependencies=[Depends(_require_preview_services_enabled)],
    )
    manager = context.preview_service_manager
    capability_store = PreviewCapabilityStore(
        context.config.root,
        durable=bool(getattr(context.config, "durable_writes", False)),
    )

    @router.get("/hub/read-models/services")
    async def get_services_read_model(scope: Optional[str] = None) -> dict[str, Any]:
        records = await asyncio.to_thread(manager.registry.list)
        filtered = _filter_records(records, scope=scope)
        return _services_read_model(filtered)

    @router.get("/hub/services")
    async def list_services(scope: Optional[str] = None) -> dict[str, Any]:
        records = await asyncio.to_thread(manager.registry.list)
        filtered = _filter_records(records, scope=scope)
        return {
            "services": [record.to_dict() for record in filtered],
            "read_model": _services_read_model(filtered),
        }

    @router.get("/hub/services/{service_id}")
    async def get_service(service_id: str) -> dict[str, Any]:
        record = await _require_record(manager, service_id)
        return _service_response(
            record,
            events=await asyncio.to_thread(manager.events, service_id),
        )

    @router.post("/hub/services/static")
    async def register_static(
        payload: RegisterStaticServiceRequest,
    ) -> dict[str, Any]:
        try:
            record = await asyncio.to_thread(
                manager.register_static,
                _static_registration_path(context, payload),
                name=payload.name,
                kind=payload.kind,
                scope_links=_scope_payloads(payload.scope_links),
                created_by=payload.created_by,
                service_class=payload.service_class,
                trust_level=payload.trust_level,
                ownership=payload.ownership,
                network_policy=payload.network_policy,
                metadata=_static_source_metadata(payload),
            )
        except (OSError, ValueError) as exc:
            raise _bad_request(exc) from exc
        return _service_response(record)

    @router.post("/hub/services/loopback-url")
    async def register_loopback_url(
        payload: RegisterLoopbackServiceRequest,
    ) -> dict[str, Any]:
        try:
            record = await asyncio.to_thread(
                manager.register_loopback_url,
                payload.url,
                name=payload.name,
                health_path=payload.health_path,
                scope_links=_scope_payloads(payload.scope_links),
                created_by=payload.created_by,
                service_class=payload.service_class,
                trust_level=payload.trust_level,
                ownership=payload.ownership,
                network_policy=payload.network_policy,
            )
        except ValueError as exc:
            raise _bad_request(exc) from exc
        return _service_response(record)

    @router.post("/hub/services/managed")
    async def register_managed(
        payload: RegisterManagedServiceRequest,
    ) -> dict[str, Any]:
        action = (
            manager.start_managed_command
            if payload.start
            else manager.register_managed_command
        )
        try:
            record = await asyncio.to_thread(
                action,
                name=payload.name,
                argv=payload.argv,
                cwd=Path(payload.cwd),
                env=payload.env,
                env_policy=payload.env_policy,
                port_policy=payload.port_policy,
                health_check=payload.health_check,
                scope_links=_scope_payloads(payload.scope_links),
                created_by=payload.created_by,
                auto_start_on_hub_start=payload.auto_start_on_hub_start,
                service_class=payload.service_class,
                trust_level=payload.trust_level,
                ownership=payload.ownership,
                network_policy=payload.network_policy,
            )
        except PreviewPortAllocationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PreviewServiceSupervisorError as exc:
            raise _supervisor_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise _bad_request(exc) from exc
        return _service_response(record)

    @router.patch("/hub/services/{service_id}")
    async def update_service(
        service_id: str,
        payload: UpdateServiceRequest,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if payload.name is not None:
            changes["name"] = payload.name
        if payload.scope_links is not None:
            changes["scope_links"] = _scope_payloads(payload.scope_links)
        if payload.health_check is not None:
            changes["health_check"] = payload.health_check
        if payload.command is not None:
            changes["command"] = payload.command
        if payload.port_policy is not None:
            changes["port_policy"] = payload.port_policy
        if payload.restart_policy is not None:
            changes["restart_policy"] = payload.restart_policy
        if payload.service_class is not None:
            changes["service_class"] = payload.service_class
        if payload.trust_level is not None:
            changes["trust_level"] = payload.trust_level
        if payload.ownership is not None:
            changes["ownership"] = payload.ownership
        if payload.network_policy is not None:
            changes["network_policy"] = payload.network_policy
        if payload.metadata is not None:
            changes["metadata"] = payload.metadata
        try:
            record = await asyncio.to_thread(
                manager.update_service, service_id, changes
            )
        except PreviewServiceNotFoundError as exc:
            raise _not_found(exc) from exc
        except PreviewServiceSupervisorError as exc:
            raise _supervisor_error(exc) from exc
        except ValueError as exc:
            raise _bad_request(exc) from exc
        return _service_response(record)

    @router.post("/hub/services/{service_id}/health")
    async def check_health(service_id: str) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(manager.check_health, service_id)
            record = await asyncio.to_thread(manager.registry.require, service_id)
        except PreviewServiceNotFoundError as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise _bad_request(exc) from exc
        return {
            **_service_response(record),
            "health": result.to_dict(),
        }

    @router.post("/hub/services/{service_id}/start")
    async def start_service(service_id: str) -> dict[str, Any]:
        return _service_response(await _lifecycle(manager, service_id, "start"))

    @router.post("/hub/services/{service_id}/stop")
    async def stop_service(service_id: str) -> dict[str, Any]:
        return _service_response(await _lifecycle(manager, service_id, "stop"))

    @router.post("/hub/services/{service_id}/restart")
    async def restart_service(service_id: str) -> dict[str, Any]:
        return _service_response(await _lifecycle(manager, service_id, "restart"))

    @router.post("/hub/services/{service_id}/kill")
    async def kill_service(
        service_id: str,
        payload: DestructiveServiceRequest,
    ) -> dict[str, Any]:
        try:
            record = await asyncio.to_thread(
                manager.kill,
                service_id,
                force=payload.force,
                force_attestation=_force_attestation(
                    payload,
                    target_scope=f"hub.preview_services.kill:{service_id}",
                ),
            )
        except PreviewServiceNotFoundError as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise _bad_request(exc) from exc
        return _service_response(record)

    @router.post("/hub/services/{service_id}/teardown")
    async def teardown_service(
        service_id: str,
        payload: OptionalDestructiveBody,
    ) -> dict[str, Any]:
        record = await _teardown(manager, service_id, payload)
        await asyncio.to_thread(capability_store.revoke_service, service_id)
        return {"service": record.to_dict(), "deleted": True}

    @router.post("/hub/services/{service_id}/unlink")
    async def unlink_service(
        service_id: str,
        payload: OptionalDestructiveBody,
    ) -> dict[str, Any]:
        record = await _unlink(manager, service_id, payload)
        await asyncio.to_thread(capability_store.revoke_service, service_id)
        return {"service": record.to_dict(), "deleted": True}

    @router.delete("/hub/services/{service_id}")
    async def delete_service(
        service_id: str,
        payload: OptionalDestructiveBody,
    ) -> dict[str, Any]:
        record = await _unlink(manager, service_id, payload)
        await asyncio.to_thread(capability_store.revoke_service, service_id)
        return {"service": record.to_dict(), "deleted": True}

    @router.post("/hub/services/{service_id}/preview-token")
    async def issue_preview_token(
        service_id: str,
        ttl_seconds: int = Query(
            default=DEFAULT_PREVIEW_CAPABILITY_TTL_SECONDS,
            ge=1,
            le=60 * 60 * 24 * 30,
            alias="ttl",
        ),
    ) -> dict[str, Any]:
        record = await _require_record(manager, service_id)
        capability = await asyncio.to_thread(
            capability_store.issue,
            service_id,
            ttl_seconds=ttl_seconds,
        )
        return {
            "service_id": record.service_id,
            "preview_url": capability.url,
            "expires_at": capability.expires_at,
        }

    @router.post("/hub/services/{service_id}/preview-token/revoke")
    async def revoke_preview_tokens(service_id: str) -> dict[str, Any]:
        await _require_record(manager, service_id)
        revoked = await asyncio.to_thread(capability_store.revoke_service, service_id)
        return {"service_id": service_id, "revoked": revoked}

    @router.get("/hub/services/{service_id}/logs")
    async def service_logs(
        service_id: str,
        tail: int = Query(default=200, ge=0, le=5000),
        stderr: bool = Query(default=False),
        since: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            text = await asyncio.to_thread(manager.logs, service_id, tail=tail)
            record = await asyncio.to_thread(manager.registry.require, service_id)
            events = await asyncio.to_thread(manager.events, service_id)
        except PreviewServiceNotFoundError as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise _bad_request(exc) from exc
        process = (
            record.process.model_dump(mode="json", exclude_none=True)
            if record.process
            else {}
        )
        return {
            "service_id": service_id,
            "tail": tail,
            "stderr": stderr,
            "since": since,
            "text": text,
            "exit_code": process.get("exit_code"),
            "started_at": process.get("started_at"),
            "exited_at": process.get("exited_at"),
            "last_exit_reason": process.get("last_exit_reason"),
            "events": events[-20:],
        }

    @router.api_route(
        "/preview/services/{service_id}/",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @router.api_route(
        "/preview/services/{service_id}/{preview_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def preview_service(
        service_id: str,
        request: Request,
        preview_path: str = "",
    ) -> Response:
        record = await _require_record(manager, service_id)
        if _prefer_capability_urls(context):
            capability = await asyncio.to_thread(
                capability_store.issue,
                service_id,
            )
            return RedirectResponse(
                _request_url_for_path(
                    request,
                    _join_url_path(capability.url, preview_path),
                ),
                status_code=307,
            )
        return await _preview_response(context, record, request, preview_path)

    @router.api_route(
        "/preview/p/{token}/",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @router.api_route(
        "/preview/p/{token}/{preview_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def preview_capability(
        token: str,
        request: Request,
        preview_path: str = "",
    ) -> Response:
        capability = await asyncio.to_thread(capability_store.validate, token)
        if capability is None:
            raise HTTPException(
                status_code=403,
                detail="Invalid preview capability",
            )
        record = await _require_record(manager, capability.service_id)
        combined_path = _join_capability_path(capability.path_prefix, preview_path)
        response = await _preview_response(context, record, request, combined_path)
        _apply_capability_preview_response_headers(
            response,
            preview_prefix=_preview_request_prefix(request, preview_path),
        )
        return response

    @router.websocket("/preview/services/{service_id}/")
    @router.websocket("/preview/services/{service_id}/{preview_path:path}")
    async def preview_service_websocket(
        websocket: WebSocket,
        service_id: str,
        preview_path: str = "",
    ) -> None:
        try:
            record = await _require_record(manager, service_id)
            await _proxy_websocket(context, record, websocket, preview_path)
        except HTTPException as exc:
            await _close_websocket_for_http_error(websocket, exc)

    @router.websocket("/preview/p/{token}/")
    @router.websocket("/preview/p/{token}/{preview_path:path}")
    async def preview_capability_websocket(
        websocket: WebSocket,
        token: str,
        preview_path: str = "",
    ) -> None:
        try:
            capability = await asyncio.to_thread(capability_store.validate, token)
            if capability is None:
                raise HTTPException(
                    status_code=403,
                    detail="Invalid preview capability",
                )
            record = await _require_record(manager, capability.service_id)
            combined_path = _join_capability_path(capability.path_prefix, preview_path)
            await _proxy_websocket(context, record, websocket, combined_path)
        except HTTPException as exc:
            await _close_websocket_for_http_error(websocket, exc)

    return router


async def _require_record(
    manager: PreviewServiceSupervisor,
    service_id: str,
) -> PreviewServiceRecord:
    try:
        return await asyncio.to_thread(manager.registry.require, service_id)
    except PreviewServiceNotFoundError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


async def _lifecycle(
    manager: PreviewServiceSupervisor,
    service_id: str,
    action: str,
) -> PreviewServiceRecord:
    try:
        method = getattr(manager, action)
        return await asyncio.to_thread(method, service_id)
    except PreviewServiceNotFoundError as exc:
        raise _not_found(exc) from exc
    except PreviewPortAllocationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PreviewServiceSupervisorError as exc:
        raise _supervisor_error(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


async def _teardown(
    manager: PreviewServiceSupervisor,
    service_id: str,
    payload: DestructiveServiceRequest,
) -> PreviewServiceRecord:
    try:
        return await asyncio.to_thread(
            manager.teardown,
            service_id,
            force=payload.force,
            force_attestation=_force_attestation(
                payload,
                target_scope=f"hub.preview_services.teardown:{service_id}",
            ),
        )
    except PreviewServiceNotFoundError as exc:
        raise _not_found(exc) from exc
    except PreviewServiceSupervisorError as exc:
        raise _supervisor_error(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


async def _unlink(
    manager: PreviewServiceSupervisor,
    service_id: str,
    payload: DestructiveServiceRequest,
) -> PreviewServiceRecord:
    try:
        return await asyncio.to_thread(
            manager.unlink,
            service_id,
            force=payload.force,
            force_attestation=_force_attestation(
                payload,
                target_scope=f"hub.preview_services.unlink_running:{service_id}",
            ),
        )
    except PreviewServiceNotFoundError as exc:
        raise _not_found(exc) from exc
    except PreviewServiceSupervisorError as exc:
        raise _supervisor_error(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


def _service_response(
    record: PreviewServiceRecord,
    *,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "service": record.to_dict(),
        "read_model": _service_read_model(record),
    }
    if events is not None:
        response["events"] = events[-20:]
    return response


def _services_read_model(
    records: list[PreviewServiceRecord],
) -> dict[str, Any]:
    read_model = build_services_read_model(records)
    read_model["services"] = [
        _with_preview_capability_status(item)
        for item in read_model.get("services", [])
        if isinstance(item, dict)
    ]
    return read_model


def _service_read_model(record: PreviewServiceRecord) -> dict[str, Any]:
    return _with_preview_capability_status(service_read_model(record))


def _with_preview_capability_status(item: dict[str, Any]) -> dict[str, Any]:
    if not item.get("proxy_enabled", True):
        return item
    service_id = str(item.get("service_id") or "")
    if not service_id:
        return item
    return {
        **item,
        "preview_url": None,
        "preview_url_status": "not_issued",
        "preview_url_expires_at": None,
    }


def _scope_payloads(scope_links: list[ServiceScopeLinkPayload]) -> list[dict[str, Any]]:
    return [link.model_dump(mode="json", exclude_none=True) for link in scope_links]


def _static_registration_path(
    context: HubAppContext,
    payload: RegisterStaticServiceRequest,
) -> Path:
    if payload.source is None:
        raw_path = _static_path_from_text(
            context,
            payload.path,
            follow_final_symlink=False,
        )
        _reject_static_registration_symlink_target(raw_path)
        resolved_path = raw_path.resolve()
        _require_path_under_allowed_static_roots(context, resolved_path)
        return raw_path
    source = payload.source
    if source.type != "workspace":
        raise ValueError(f"Unsupported static source type: {source.type}")
    if not source.workspace_id:
        raise ValueError("workspace static source requires workspace_id")
    relative = _workspace_source_relative_path(source.path)
    root = _workspace_static_root(context, source.workspace_id)
    return root / relative


def _reject_static_registration_symlink_target(path: Path) -> None:
    try:
        if path.is_symlink():
            raise ValueError("static path must not be a symlink")
    except OSError as exc:
        raise ValueError("static path cannot be inspected") from exc


def _static_path_from_text(
    context: HubAppContext,
    value: str,
    *,
    follow_final_symlink: bool,
) -> Path:
    raw_value = value.strip()
    if not raw_value:
        raise ValueError("static path must be non-empty")
    if not _looks_absolute_or_home_path(raw_value):
        raise ValueError(
            "static path must be absolute unless source.type=workspace is used"
        )
    if ".." in raw_value.replace("\\", "/").split("/"):
        raise ValueError("static path must not contain parent traversal")
    try:
        return resolve_config_path(
            raw_value,
            context.config.root,
            allow_absolute=True,
            allow_home=True,
            follow_final_symlink=follow_final_symlink,
            scope="preview_services.static.path",
        )
    except ConfigPathError as exc:
        raise ValueError(str(exc)) from exc


def _looks_absolute_or_home_path(value: str) -> bool:
    if value.startswith(("/", "~")):
        return True
    return len(value) >= 3 and value[1] == ":" and value[2] in {"/", "\\"}


def _require_path_under_allowed_static_roots(
    context: HubAppContext,
    path: Path,
) -> None:
    roots = _allowed_static_roots(context, None)
    for root in roots:
        try:
            path.relative_to(root)
            return
        except ValueError:
            continue
    raise ValueError("static preview target is outside allowed roots")


def _static_source_metadata(payload: RegisterStaticServiceRequest) -> dict[str, Any]:
    if payload.source is None:
        return {}
    return {
        "source": payload.source.model_dump(
            mode="json",
            by_alias=False,
            exclude_none=True,
        )
    }


def _workspace_source_relative_path(path: str) -> Path:
    value = path.strip()
    if not value:
        raise ValueError("workspace static source path must be non-empty")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("workspace static source path must be relative")
    return Path(*pure.parts)


def _workspace_static_root(context: HubAppContext, workspace_id: str) -> Path:
    clean_id = workspace_id.strip()
    if not clean_id or any(part in clean_id for part in ("/", "\\", "..")):
        raise ValueError("workspace_id must be a path-safe identifier")
    return resolve_hub_state_root(context.config.root) / "workspaces" / clean_id


def _force_attestation(
    payload: DestructiveServiceRequest,
    *,
    target_scope: str,
) -> dict[str, str] | None:
    if payload.force_attestation is None:
        return None
    return {
        "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
        "user_request": payload.force_attestation,
        "target_scope": target_scope,
    }


def _filter_records(
    records: list[PreviewServiceRecord],
    *,
    scope: str | None,
) -> list[PreviewServiceRecord]:
    if not scope:
        return sorted(records, key=lambda record: record.service_id)
    return [
        record
        for record in sorted(records, key=lambda item: item.service_id)
        if _record_matches_scope(record, scope)
    ]


def _record_matches_scope(record: PreviewServiceRecord, scope: str) -> bool:
    for link in record.scope_links:
        if link.id and f"{link.kind}:{link.id}" == scope:
            return True
        if link.path and f"{link.kind}:{link.path}" == scope:
            return True
        if not link.id and not link.path and str(link.kind) == scope:
            return True
    return False


def _is_running_managed(record: PreviewServiceRecord) -> bool:
    return bool(
        record.kind == PreviewServiceKind.MANAGED_COMMAND.value
        and record.status
        in {
            PreviewServiceStatus.STARTING.value,
            PreviewServiceStatus.RUNNING.value,
            PreviewServiceStatus.HEALTHY.value,
            PreviewServiceStatus.UNHEALTHY.value,
        }
    )


async def _preview_response(
    context: HubAppContext,
    record: PreviewServiceRecord,
    request: Request,
    preview_path: str,
) -> Response:
    if not record.exposure.proxy_enabled:
        raise HTTPException(status_code=404, detail="Preview proxy is disabled")
    if record.kind == PreviewServiceKind.STATIC_FILE.value:
        return _static_file_response(context, record, preview_path, request.method)
    if record.kind == PreviewServiceKind.STATIC_DIR.value:
        return _static_dir_response(context, record, preview_path, request.method)
    if record.kind in {
        PreviewServiceKind.LOOPBACK_URL.value,
        PreviewServiceKind.MANAGED_COMMAND.value,
    }:
        return await _proxy_http_request(context, record, request, preview_path)
    raise HTTPException(
        status_code=400, detail=f"Unsupported service kind: {record.kind}"
    )


def _join_capability_path(path_prefix: str, preview_path: str) -> str:
    prefix = _validate_preview_path(path_prefix)
    suffix = _validate_preview_path(preview_path)
    if prefix and suffix:
        return f"{prefix}/{suffix}"
    return prefix or suffix


def _request_url_for_path(request: Request, path: str) -> str:
    base = str(request.base_url).rstrip("/")
    root_path = request.scope.get("root_path") or ""
    if root_path and path.startswith(f"{root_path}/"):
        return f"{base}{path[len(root_path) :]}"
    return f"{base}{path}"


def _static_file_response(
    context: HubAppContext,
    record: PreviewServiceRecord,
    preview_path: str,
    method: str,
) -> Response:
    target = _require_static_target_path(context, record)
    safe_path = _validate_preview_path(preview_path)
    if safe_path not in {"", target.name}:
        raise HTTPException(status_code=404, detail="Static preview file not found")
    root = _require_allowed_static_root(context, record, target)
    _reject_sensitive_static_path(root, target)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Static preview file not found")
    if method == "HEAD":
        return Response(headers={"content-length": str(target.stat().st_size)})
    return FileResponse(target)


def _static_dir_response(
    context: HubAppContext,
    record: PreviewServiceRecord,
    preview_path: str,
    method: str,
) -> Response:
    root = _require_static_target_path(context, record)
    if not root.is_dir():
        raise HTTPException(
            status_code=404, detail="Static preview directory not found"
        )
    relative = _validate_preview_path(preview_path)
    if not relative:
        relative = "index.html"
    relative_parts = PurePosixPath(relative).parts
    _reject_sensitive_requested_parts(relative_parts)
    target = _resolve_static_child(root, relative_parts)
    if target.is_dir():
        target = _resolve_static_child(root, (*relative_parts, "index.html"))
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Static preview file not found")
    if method == "HEAD":
        return Response(headers={"content-length": str(target.stat().st_size)})
    return FileResponse(target)


def _require_static_target_path(
    context: HubAppContext,
    record: PreviewServiceRecord,
) -> Path:
    target_path = record.target.path if record.target is not None else None
    if not target_path:
        raise HTTPException(status_code=400, detail="Static service has no target path")
    raw_path = Path(target_path).expanduser()
    try:
        if raw_path.is_symlink():
            raise HTTPException(
                status_code=403,
                detail="Static preview target path contains a symlink",
            )
    except OSError as exc:
        raise HTTPException(
            status_code=403, detail="Static preview target path cannot be inspected"
        ) from exc
    path = raw_path.resolve()
    root = _require_allowed_static_root(context, record, path)
    _reject_sensitive_static_path(root, path)
    return path


def _require_allowed_static_root(
    context: HubAppContext,
    record: PreviewServiceRecord,
    path: Path,
) -> Path:
    roots = _allowed_static_roots(context, record)
    matches: list[Path] = []
    for root in roots:
        try:
            path.relative_to(root)
            matches.append(root)
        except ValueError:
            continue
    if not matches:
        raise HTTPException(
            status_code=403,
            detail="Static preview target is outside allowed roots",
        )
    return max(matches, key=lambda item: len(item.parts))


def _allowed_static_roots(
    context: HubAppContext,
    record: PreviewServiceRecord | None,
) -> list[Path]:
    roots: list[Path] = [context.config.root]
    for snapshot in context.supervisor.list_repos(use_cache=True):
        for attr in ("absolute_path", "path"):
            value = getattr(snapshot, attr, None)
            if value:
                roots.append(Path(value))
    preview_config = _preview_config(context)
    configured = preview_config.get("static_allowed_roots")
    if isinstance(configured, list):
        for item in configured:
            raw_item = str(item).strip()
            if not raw_item:
                continue
            try:
                roots.append(
                    resolve_config_path(
                        raw_item,
                        context.config.root,
                        allow_absolute=True,
                        allow_home=True,
                    )
                )
            except ConfigPathError:
                continue
    metadata = getattr(record, "metadata", None)
    source = (metadata or {}).get("source") if isinstance(metadata, dict) else None
    if isinstance(source, dict) and source.get("type") == "workspace":
        workspace_id = str(source.get("workspace_id") or "").strip()
        if workspace_id:
            try:
                roots.append(_workspace_static_root(context, workspace_id))
            except ValueError:
                pass
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.expanduser().resolve())
        except OSError:
            continue
    return resolved


def _validate_preview_path(preview_path: str) -> str:
    clean = preview_path.strip("/")
    if not clean:
        return ""
    pure = PurePosixPath(clean)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise HTTPException(status_code=403, detail="Invalid preview path")
    return pure.as_posix()


def _reject_sensitive_requested_parts(relative_parts: tuple[str, ...]) -> None:
    for part in relative_parts:
        _reject_sensitive_static_part(part)


def _reject_sensitive_static_path(root: Path, target: Path) -> None:
    try:
        relative_parts = target.relative_to(root).parts
    except ValueError as exc:
        raise HTTPException(
            status_code=403, detail="Static preview path escapes root"
        ) from exc
    for index, part in enumerate(relative_parts):
        _reject_sensitive_static_part(part)
        raw_component = root.joinpath(*relative_parts[: index + 1])
        try:
            if raw_component.is_symlink():
                raise HTTPException(
                    status_code=403,
                    detail="Static preview path contains a symlink",
                )
        except OSError as exc:
            raise HTTPException(
                status_code=403, detail="Static preview path cannot be inspected"
            ) from exc


def _reject_sensitive_static_part(part: str) -> None:
    sensitive_names = {
        ".git",
        ".codex-autorunner",
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
    lowered = part.lower()
    if part.startswith(".") or lowered in sensitive_names:
        raise HTTPException(
            status_code=403, detail="Static preview file is hidden or sensitive"
        )
    if lowered.endswith((".pem", ".key", ".p12", ".pfx")):
        raise HTTPException(
            status_code=403, detail="Static preview file is hidden or sensitive"
        )
    if any(token in lowered for token in ("secret", "private_key", "api_key")):
        raise HTTPException(
            status_code=403, detail="Static preview file is hidden or sensitive"
        )


def _resolve_static_child(root: Path, relative_parts: tuple[str, ...]) -> Path:
    _reject_sensitive_requested_parts(relative_parts)
    cursor = root
    for part in relative_parts:
        cursor = cursor / part
        try:
            if cursor.is_symlink():
                raise HTTPException(
                    status_code=403,
                    detail="Static preview path contains a symlink",
                )
        except OSError as exc:
            raise HTTPException(
                status_code=403, detail="Static preview path cannot be inspected"
            ) from exc
    target = cursor.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(
            status_code=403, detail="Static preview path escapes root"
        ) from exc
    _reject_sensitive_static_path(root, target)
    return target


async def _proxy_http_request(
    context: HubAppContext,
    record: PreviewServiceRecord,
    request: Request,
    preview_path: str,
) -> Response:
    target_url = _proxy_target_url(context, record, preview_path, request.url.query)
    preview_prefix = _preview_request_prefix(request, preview_path)
    headers = _proxy_request_headers(request, preview_prefix=preview_prefix)
    body = await _read_limited_body(
        request,
        max_bytes=_proxy_int_config(
            context,
            "proxy_max_body_bytes",
            DEFAULT_PROXY_MAX_BODY_BYTES,
        ),
    )
    timeout = _proxy_timeout(context)
    global_semaphore, service_semaphore = _proxy_stream_semaphores(context, record)
    await _acquire_proxy_stream_slots(global_semaphore, service_semaphore)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    upstream: httpx.Response | None = None
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                target_url,
                headers=headers,
                content=body,
            ),
            stream=True,
        )
        try:
            response_headers = _proxy_response_headers(
                upstream.headers.items(),
                request=request,
                record=record,
                preview_prefix=preview_prefix,
            )
            if _should_rewrite_proxy_html(upstream):
                content = await upstream.aread()
                rewritten = _rewrite_proxy_html_body(
                    content,
                    content_type=upstream.headers.get("content-type", ""),
                    preview_prefix=preview_prefix,
                )
                await _close_proxy_response(
                    upstream,
                    client,
                    global_semaphore,
                    service_semaphore,
                )
                _set_header(response_headers, "Content-Length", str(len(rewritten)))
                return Response(
                    content=rewritten,
                    status_code=upstream.status_code,
                    headers=response_headers,
                )
            return StreamingResponse(
                upstream.aiter_bytes(),
                status_code=upstream.status_code,
                headers=response_headers,
                background=BackgroundTask(
                    _close_proxy_response,
                    upstream,
                    client,
                    global_semaphore,
                    service_semaphore,
                ),
            )
        except Exception:
            await _close_proxy_response(
                upstream,
                client,
                global_semaphore,
                service_semaphore,
            )
            raise
    except httpx.TimeoutException as exc:
        await client.aclose()
        service_semaphore.release()
        global_semaphore.release()
        raise HTTPException(
            status_code=504, detail=f"Preview proxy timed out: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        await client.aclose()
        service_semaphore.release()
        global_semaphore.release()
        raise HTTPException(
            status_code=502, detail=f"Preview proxy failed: {exc}"
        ) from exc


async def _proxy_websocket(
    context: HubAppContext,
    record: PreviewServiceRecord,
    websocket: WebSocket,
    preview_path: str,
) -> None:
    if not record.exposure.proxy_enabled:
        raise HTTPException(status_code=404, detail="Preview proxy is disabled")
    if record.kind not in {
        PreviewServiceKind.LOOPBACK_URL.value,
        PreviewServiceKind.MANAGED_COMMAND.value,
    }:
        raise HTTPException(
            status_code=400,
            detail=f"Preview websocket proxy does not support service kind: {record.kind}",
        )
    target_url = _proxy_target_websocket_url(
        context,
        record,
        preview_path,
        websocket.url.query,
    )
    preview_prefix = _preview_websocket_prefix(websocket, preview_path)
    headers = _proxy_websocket_request_headers(
        websocket.headers.items(),
        websocket=websocket,
        preview_prefix=preview_prefix,
    )
    connect_kwargs = _websocket_connect_kwargs(headers)
    global_semaphore, service_semaphore = _proxy_stream_semaphores(context, record)
    await _acquire_proxy_stream_slots(global_semaphore, service_semaphore)
    try:
        async with websockets.connect(target_url, **connect_kwargs) as upstream:
            await websocket.accept()
            await _relay_websocket_messages(websocket, upstream)
    except (OSError, WebSocketException):
        reason = "Preview websocket proxy failed"
        if websocket.application_state != WebSocketState.DISCONNECTED:
            await websocket.close(code=1011, reason=reason)
        return
    finally:
        service_semaphore.release()
        global_semaphore.release()


def _websocket_connect_kwargs(headers: dict[str, str]) -> dict[str, Any]:
    signature = inspect.signature(websockets.connect)
    parameters = signature.parameters
    connect_kwargs: dict[str, Any] = {}
    if "additional_headers" in parameters:
        connect_kwargs["additional_headers"] = headers or None
    elif "extra_headers" in parameters:
        connect_kwargs["extra_headers"] = headers or None
    if "proxy" in parameters:
        connect_kwargs["proxy"] = None
    return connect_kwargs


async def _relay_websocket_messages(websocket: WebSocket, upstream: Any) -> None:
    async def client_to_upstream() -> None:
        try:
            while True:
                message = await websocket.receive()
                message_type = message.get("type")
                if message_type == "websocket.disconnect":
                    await upstream.close()
                    return
                if "text" in message and message["text"] is not None:
                    await upstream.send(message["text"])
                elif "bytes" in message and message["bytes"] is not None:
                    await upstream.send(message["bytes"])
        except (WebSocketDisconnect, ConnectionClosed):
            await upstream.close()

    async def upstream_to_client() -> None:
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(str(message))
        except (WebSocketDisconnect, ConnectionClosed):
            return

    tasks = {
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


async def _close_websocket_for_http_error(
    websocket: WebSocket,
    exc: HTTPException,
) -> None:
    code = 1008 if exc.status_code in {400, 403, 404} else 1011
    reason = str(exc.detail)
    await websocket.close(code=code, reason=reason[:120])


async def _close_proxy_response(
    upstream: httpx.Response,
    client: httpx.AsyncClient,
    global_semaphore: asyncio.Semaphore,
    service_semaphore: asyncio.Semaphore,
) -> None:
    try:
        await upstream.aclose()
        await client.aclose()
    finally:
        service_semaphore.release()
        global_semaphore.release()


async def _acquire_proxy_stream_slots(
    global_semaphore: asyncio.Semaphore,
    service_semaphore: asyncio.Semaphore,
) -> None:
    global_acquired = False
    try:
        await asyncio.wait_for(
            global_semaphore.acquire(),
            timeout=PROXY_STREAM_ACQUIRE_TIMEOUT_SECONDS,
        )
        global_acquired = True
        await asyncio.wait_for(
            service_semaphore.acquire(),
            timeout=PROXY_STREAM_ACQUIRE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        if global_acquired:
            global_semaphore.release()
        raise HTTPException(
            status_code=429,
            detail="Preview proxy stream limit reached",
        ) from exc


def _proxy_target_url(
    context: HubAppContext,
    record: PreviewServiceRecord,
    preview_path: str,
    query_string: str,
) -> str:
    target = record.target
    direct_url = target.direct_url if target is not None else None
    if not direct_url:
        raise HTTPException(status_code=400, detail="Preview service has no target URL")
    split = urlsplit(direct_url)
    if split.scheme not in {"http", "https"}:
        raise HTTPException(
            status_code=400, detail="Preview target must be http or https"
        )
    if not _proxy_host_allowed(context, split.hostname or ""):
        raise HTTPException(
            status_code=403, detail="Preview target host is not allowed"
        )
    clean_path = _validate_preview_path(preview_path)
    base_path = split.path or "/"
    if not base_path.endswith("/"):
        base_path = f"{base_path}/"
    full_path = base_path
    if clean_path:
        full_path = f"{base_path}{quote(clean_path, safe='/@')}"
    base_query = parse_qsl(split.query, keep_blank_values=True)
    request_query = _proxy_request_query_params(query_string)
    query = urlencode(base_query + request_query, doseq=True)
    return urlunsplit((split.scheme, split.netloc, full_path, query, ""))


def _proxy_target_websocket_url(
    context: HubAppContext,
    record: PreviewServiceRecord,
    preview_path: str,
    query_string: str,
) -> str:
    http_url = _proxy_target_url(context, record, preview_path, query_string)
    split = urlsplit(http_url)
    scheme = "wss" if split.scheme == "https" else "ws"
    return urlunsplit((scheme, split.netloc, split.path, split.query, ""))


def _proxy_host_allowed(context: HubAppContext, host: str) -> bool:
    if is_loopback_host(host):
        return True
    preview_config = _preview_config(context)
    allowed = preview_config.get("proxy_allowed_hosts")
    if not isinstance(allowed, list):
        return False
    return host in {str(item).strip() for item in allowed if str(item).strip()}


def _preview_config(context: HubAppContext) -> dict[str, Any]:
    raw_config = getattr(context.config, "raw", {})
    if not isinstance(raw_config, dict):
        return {}
    preview_config = raw_config.get("preview_services")
    return preview_config if isinstance(preview_config, dict) else {}


def _preview_services_enabled(context: HubAppContext) -> bool:
    enabled = _preview_config(context).get("enabled")
    return True if enabled is None else bool(enabled)


def _prefer_capability_urls(context: HubAppContext) -> bool:
    raw_config = getattr(context.config, "raw", {})
    if not isinstance(raw_config, dict):
        return False
    auth_config = raw_config.get("auth")
    if not isinstance(auth_config, dict):
        return False
    return auth_config.get("mode") == "hosted_bearer"


_HOP_BY_HOP_HEADERS = {
    "accept-encoding",
    "connection",
    "content-length",
    "content-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

_SENSITIVE_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "host",
    "referer",
}

_SENSITIVE_RESPONSE_HEADERS = {
    "service-worker-allowed",
    "set-cookie",
}

_SENSITIVE_QUERY_PARAMS = {
    "car_token",
    "car_auth_token",
    "car_preview_token",
}


def _proxy_request_query_params(query_string: str) -> list[tuple[str, str]]:
    return [
        (key, value)
        for key, value in parse_qsl(query_string, keep_blank_values=True)
        if key.lower() not in _SENSITIVE_QUERY_PARAMS
    ]


async def _read_limited_body(request: Request, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Preview proxy request body exceeds {max_bytes} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _proxy_request_headers(
    request: Request,
    *,
    preview_prefix: str,
) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for key, value in request.headers.items():
        lowered = key.lower()
        if lowered in _HOP_BY_HOP_HEADERS or lowered in _SENSITIVE_REQUEST_HEADERS:
            continue
        forwarded[key] = value
    forwarded["Accept-Encoding"] = "identity"
    forwarded.update(_forwarded_headers(request, preview_prefix=preview_prefix))
    return forwarded


def _proxy_websocket_request_headers(
    headers: Iterable[tuple[str, str]],
    *,
    websocket: WebSocket,
    preview_prefix: str,
) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for key, value in headers:
        lowered = key.lower()
        if lowered in _HOP_BY_HOP_HEADERS or lowered in _SENSITIVE_REQUEST_HEADERS:
            continue
        if lowered.startswith("sec-websocket-"):
            continue
        forwarded[key] = value
    forwarded.update(_forwarded_headers(websocket, preview_prefix=preview_prefix))
    return forwarded


def _proxy_response_headers(
    headers: Iterable[tuple[str, str]],
    *,
    request: Request,
    record: PreviewServiceRecord,
    preview_prefix: str,
) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for key, value in headers:
        lowered = key.lower()
        if lowered in _HOP_BY_HOP_HEADERS or lowered in _SENSITIVE_RESPONSE_HEADERS:
            continue
        if lowered == "location":
            forwarded[key] = _rewrite_location_header(
                value,
                request=request,
                record=record,
                preview_prefix=preview_prefix,
            )
            continue
        forwarded[key] = value
    _apply_capability_preview_header_values(forwarded, preview_prefix=preview_prefix)
    return forwarded


def _set_header(headers: dict[str, str], key: str, value: str) -> None:
    for existing in list(headers):
        if existing.lower() == key.lower():
            del headers[existing]
    headers[key] = value


def _should_rewrite_proxy_html(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return "text/html" in content_type.lower()


def _rewrite_proxy_html_body(
    content: bytes,
    *,
    content_type: str,
    preview_prefix: str,
) -> bytes:
    charset = _content_type_charset(content_type)
    try:
        text = content.decode(charset, errors="replace")
    except LookupError:
        charset = "utf-8"
        text = content.decode(charset, errors="replace")
    prefix = "/" + preview_prefix.strip("/")

    def replace(match: re.Match[str]) -> str:
        quote = match.group("quote")
        path = match.group("path")
        return f"{quote}{_prepend_url_path(prefix, path)}{quote}"

    return _ROOT_RELATIVE_HTML_URL_RE.sub(replace, text).encode(charset)


def _prepend_url_path(prefix: str, suffix: str) -> str:
    clean_prefix = "/" + prefix.strip("/")
    return f"{clean_prefix}/{suffix}"


def _content_type_charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        key, separator, value = part.strip().partition("=")
        if separator and key.lower() == "charset" and value.strip():
            return value.strip().strip("\"'")
    return "utf-8"


def _is_capability_preview_prefix(preview_prefix: str) -> bool:
    parts = [part for part in preview_prefix.split("/") if part]
    for index, part in enumerate(parts):
        if part == "preview" and index + 1 < len(parts) and parts[index + 1] == "p":
            return True
    return False


def _redact_capability_preview_prefix(preview_prefix: str) -> str:
    if not _is_capability_preview_prefix(preview_prefix):
        return preview_prefix
    parts = preview_prefix.split("/")
    for index, part in enumerate(parts):
        if part == "preview" and index + 2 < len(parts) and parts[index + 1] == "p":
            parts[index + 2] = "<redacted>"
            return "/".join(parts)
    return preview_prefix


def _apply_capability_preview_header_values(
    headers: dict[str, str],
    *,
    preview_prefix: str,
) -> None:
    if not _is_capability_preview_prefix(preview_prefix):
        return
    existing = {key.lower() for key in headers}
    if "referrer-policy" not in existing:
        headers["Referrer-Policy"] = "no-referrer"
    if "cache-control" not in existing:
        headers["Cache-Control"] = "no-store, private"


def _apply_capability_preview_response_headers(
    response: Response,
    *,
    preview_prefix: str,
) -> None:
    if not _is_capability_preview_prefix(preview_prefix):
        return
    if "referrer-policy" not in response.headers:
        response.headers["Referrer-Policy"] = "no-referrer"
    if "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store, private"


def _forwarded_headers(
    request: Request | WebSocket,
    *,
    preview_prefix: str,
) -> dict[str, str]:
    host = request.headers.get("host") or request.url.netloc
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    client_host = request.client.host if request.client is not None else ""
    x_forwarded_for = client_host
    existing = request.headers.get("x-forwarded-for")
    if existing and client_host:
        x_forwarded_for = f"{existing}, {client_host}"
    safe_preview_prefix = _redact_capability_preview_prefix(preview_prefix)
    return {
        "X-Forwarded-Host": host,
        "X-Forwarded-Proto": request.url.scheme,
        "X-Forwarded-Port": str(port),
        "X-Forwarded-For": x_forwarded_for,
        "X-Forwarded-Prefix": safe_preview_prefix,
        "X-Real-IP": client_host,
    }


def _rewrite_location_header(
    value: str,
    *,
    request: Request,
    record: PreviewServiceRecord,
    preview_prefix: str,
) -> str:
    split = urlsplit(value)
    if not split.scheme and not split.netloc and value.startswith("/"):
        return _absolute_preview_url(request, _join_url_path(preview_prefix, value))
    target = record.target
    direct_url = target.direct_url if target is not None else None
    if not direct_url:
        return value
    target_split = urlsplit(direct_url)
    if (
        split.scheme in {"http", "https"}
        and split.scheme == target_split.scheme
        and split.netloc == target_split.netloc
    ):
        base_path = target_split.path or "/"
        location_path = split.path or "/"
        if base_path != "/" and location_path.startswith(base_path.rstrip("/") + "/"):
            location_path = location_path[len(base_path.rstrip("/")) :]
        rewritten_path = _join_url_path(preview_prefix, location_path)
        return _absolute_preview_url(
            request,
            urlunsplit(("", "", rewritten_path, split.query, split.fragment)),
        )
    return value


def _absolute_preview_url(request: Request, path: str) -> str:
    return _request_url_for_path(request, path)


def _join_url_path(prefix: str, suffix: str) -> str:
    clean_prefix = "/" + prefix.strip("/")
    clean_suffix = "/" + suffix.strip("/")
    if clean_suffix == "/":
        return clean_prefix + "/"
    return clean_prefix + clean_suffix


def _preview_request_prefix(request: Request, preview_path: str) -> str:
    return _preview_prefix_from_path(request.url.path, preview_path)


def _preview_websocket_prefix(websocket: WebSocket, preview_path: str) -> str:
    return _preview_prefix_from_path(websocket.url.path, preview_path)


def _preview_prefix_from_path(path: str, preview_path: str) -> str:
    clean_preview = _validate_preview_path(preview_path)
    clean_path = "/" + path.strip("/")
    if clean_preview:
        suffix = "/" + clean_preview
        if clean_path.endswith(suffix):
            clean_path = clean_path[: -len(suffix)]
    return clean_path.rstrip("/") or "/"


def _proxy_timeout(context: HubAppContext) -> httpx.Timeout:
    return httpx.Timeout(
        connect=_proxy_float_config(
            context,
            "proxy_connect_timeout_seconds",
            DEFAULT_PROXY_CONNECT_TIMEOUT_SECONDS,
        ),
        read=_proxy_float_config(
            context,
            "proxy_read_timeout_seconds",
            DEFAULT_PROXY_READ_TIMEOUT_SECONDS,
        ),
        write=_proxy_float_config(
            context,
            "proxy_write_timeout_seconds",
            DEFAULT_PROXY_WRITE_TIMEOUT_SECONDS,
        ),
        pool=_proxy_float_config(
            context,
            "proxy_pool_timeout_seconds",
            DEFAULT_PROXY_POOL_TIMEOUT_SECONDS,
        ),
    )


def _proxy_stream_semaphores(
    context: HubAppContext,
    record: PreviewServiceRecord,
) -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
    global _GLOBAL_PROXY_SEMAPHORE, _GLOBAL_PROXY_SEMAPHORE_LIMIT
    global_limit = _proxy_int_config(
        context,
        "proxy_max_global_streams",
        DEFAULT_PROXY_MAX_GLOBAL_STREAMS,
    )
    service_limit = _proxy_int_config(
        context,
        "proxy_max_service_streams",
        DEFAULT_PROXY_MAX_SERVICE_STREAMS,
    )
    if _GLOBAL_PROXY_SEMAPHORE is None or _GLOBAL_PROXY_SEMAPHORE_LIMIT != global_limit:
        _GLOBAL_PROXY_SEMAPHORE = asyncio.Semaphore(global_limit)
        _GLOBAL_PROXY_SEMAPHORE_LIMIT = global_limit
    service_entry = _SERVICE_PROXY_SEMAPHORES.get(record.service_id)
    if service_entry is None or service_entry[0] != service_limit:
        service_entry = (service_limit, asyncio.Semaphore(service_limit))
        _SERVICE_PROXY_SEMAPHORES[record.service_id] = service_entry
    return _GLOBAL_PROXY_SEMAPHORE, service_entry[1]


def _proxy_int_config(context: HubAppContext, key: str, default: int) -> int:
    value = _preview_config(context).get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _proxy_float_config(context: HubAppContext, key: str, default: float) -> float:
    value = _preview_config(context).get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return default


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _supervisor_error(exc: PreviewServiceSupervisorError) -> HTTPException:
    text = str(exc)
    lower = text.lower()
    if "preview port" in lower or "available preview ports" in lower:
        return HTTPException(status_code=409, detail=text)
    return HTTPException(status_code=400, detail=text)
