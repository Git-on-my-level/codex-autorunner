from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from ....core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE
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
from ..app_state import HubAppContext


class ServiceScopeLinkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: str
    id: Optional[str] = None
    path: Optional[str] = None


class RegisterStaticServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str
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


class RegisterManagedServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    argv: list[str] = Field(default_factory=list)
    cwd: str
    env: dict[str, str] = Field(default_factory=dict)
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
    restart_policy: Optional[dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("restart_policy", "restartPolicy"),
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
    router = APIRouter(tags=["hub-services"])
    manager = context.preview_service_manager

    @router.get("/hub/read-models/services")
    async def get_services_read_model(scope: Optional[str] = None) -> dict[str, Any]:
        records = await asyncio.to_thread(manager.registry.list)
        filtered = _filter_records(records, scope=scope)
        return build_services_read_model(filtered)

    @router.get("/hub/services")
    async def list_services(scope: Optional[str] = None) -> dict[str, Any]:
        records = await asyncio.to_thread(manager.registry.list)
        filtered = _filter_records(records, scope=scope)
        return {
            "services": [record.to_dict() for record in filtered],
            "read_model": build_services_read_model(filtered),
        }

    @router.get("/hub/services/{service_id}")
    async def get_service(service_id: str) -> dict[str, Any]:
        record = await _require_record(manager, service_id)
        return _service_response(record)

    @router.post("/hub/services/static")
    async def register_static(
        payload: RegisterStaticServiceRequest,
    ) -> dict[str, Any]:
        try:
            record = await asyncio.to_thread(
                manager.register_static,
                Path(payload.path),
                name=payload.name,
                kind=payload.kind,
                scope_links=_scope_payloads(payload.scope_links),
                created_by=payload.created_by,
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
                port_policy=payload.port_policy,
                health_check=payload.health_check,
                scope_links=_scope_payloads(payload.scope_links),
                created_by=payload.created_by,
                auto_start_on_hub_start=payload.auto_start_on_hub_start,
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
        if payload.restart_policy is not None:
            changes["restart_policy"] = payload.restart_policy
        if payload.metadata is not None:
            changes["metadata"] = payload.metadata
        try:
            record = await asyncio.to_thread(
                manager.registry.update, service_id, changes
            )
        except PreviewServiceNotFoundError as exc:
            raise _not_found(exc) from exc
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
        return {**_service_response(record), "health": result.to_dict()}

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
        return {"service": record.to_dict(), "deleted": True}

    @router.post("/hub/services/{service_id}/unlink")
    async def unlink_service(
        service_id: str,
        payload: OptionalDestructiveBody,
    ) -> dict[str, Any]:
        record = await _unlink(manager, service_id, payload)
        return {"service": record.to_dict(), "deleted": True}

    @router.delete("/hub/services/{service_id}")
    async def delete_service(
        service_id: str,
        payload: OptionalDestructiveBody,
    ) -> dict[str, Any]:
        record = await _unlink(manager, service_id, payload)
        return {"service": record.to_dict(), "deleted": True}

    @router.get("/hub/services/{service_id}/logs")
    async def service_logs(
        service_id: str,
        tail: int = Query(default=200, ge=0, le=5000),
    ) -> dict[str, Any]:
        try:
            text = await asyncio.to_thread(manager.logs, service_id, tail=tail)
        except PreviewServiceNotFoundError as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise _bad_request(exc) from exc
        return {"service_id": service_id, "tail": tail, "text": text}

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
    record = await _require_record(manager, service_id)
    if _is_running_managed(record):
        if payload.force:
            record = await asyncio.to_thread(
                manager.kill,
                service_id,
                force=True,
                force_attestation=_force_attestation(
                    payload,
                    target_scope=f"hub.preview_services.teardown:{service_id}",
                ),
            )
        else:
            record = await asyncio.to_thread(manager.stop, service_id)
    await asyncio.to_thread(manager.registry.delete, service_id)
    return record


async def _unlink(
    manager: PreviewServiceSupervisor,
    service_id: str,
    payload: DestructiveServiceRequest,
) -> PreviewServiceRecord:
    record = await _require_record(manager, service_id)
    if _is_running_managed(record) and not payload.force:
        raise HTTPException(
            status_code=400,
            detail="Cannot unlink a running managed preview service without force; use teardown to stop it first.",
        )
    if _is_running_managed(record):
        _force_attestation(
            payload,
            target_scope=f"hub.preview_services.unlink_running:{service_id}",
        )
    deleted = await asyncio.to_thread(manager.registry.delete, service_id)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"Preview service not found: {service_id}"
        )
    return record


def _service_response(record: PreviewServiceRecord) -> dict[str, Any]:
    return {"service": record.to_dict(), "read_model": service_read_model(record)}


def _scope_payloads(scope_links: list[ServiceScopeLinkPayload]) -> list[dict[str, Any]]:
    return [link.model_dump(mode="json", exclude_none=True) for link in scope_links]


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
