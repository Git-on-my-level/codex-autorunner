from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from fastapi import APIRouter, HTTPException

from ...schemas import (
    HubCreateAgentWorkspaceRequest,
    HubDeleteAgentWorkspaceRequest,
    HubDestinationSetRequest,
    HubJobResponse,
    HubRemoveAgentWorkspaceRequest,
    HubUpdateAgentWorkspaceRequest,
)

if TYPE_CHECKING:
    from ...app_state import HubAppContext


class HubAgentWorkspaceService:
    def __init__(self, context: HubAppContext) -> None:
        self._context = context

    def _load_workspace(self, workspace_id: str) -> tuple[Any, Any]:
        from .....manifest import load_manifest

        manifest = load_manifest(
            self._context.config.manifest_path, self._context.config.root
        )
        workspace = manifest.get_agent_workspace(workspace_id)
        if workspace is None:
            raise HTTPException(
                status_code=404,
                detail=f"Agent workspace not found: {workspace_id}",
            )
        return manifest, workspace

    def _workspace_payload(self, workspace_id: str) -> dict[str, Any]:
        from .....core.destinations import (
            resolve_effective_agent_workspace_destination,
        )

        manifest, workspace = self._load_workspace(workspace_id)
        snapshot = self._context.supervisor.get_agent_workspace_snapshot(workspace_id)
        payload = snapshot.to_dict(self._context.config.root)
        resolution = resolve_effective_agent_workspace_destination(workspace)
        payload["configured_destination"] = workspace.destination
        payload["source"] = "configured" if workspace.destination else "default"
        payload["issues"] = [
            *manifest.issues_for_repo(workspace.id),
            *list(resolution.issues or ()),
        ]
        return payload

    def _normalize_destination_payload(
        self, payload: HubDestinationSetRequest
    ) -> dict[str, Any]:
        from .....core.destinations import validate_destination_write_payload

        normalized_kind = payload.kind.strip().lower()
        destination: dict[str, Any]
        if normalized_kind == "local":
            destination = {"kind": "local"}
        elif normalized_kind == "docker":
            destination = {"kind": "docker", "image": (payload.image or "").strip()}
            container_name = (payload.container_name or "").strip()
            if container_name:
                destination["container_name"] = container_name
            profile = (payload.profile or "").strip()
            if profile:
                destination["profile"] = profile
            workdir = (payload.workdir or "").strip()
            if workdir:
                destination["workdir"] = workdir
            env_passthrough = [
                str(item).strip()
                for item in (payload.env_passthrough or [])
                if str(item).strip()
            ]
            if env_passthrough:
                destination["env_passthrough"] = env_passthrough
            if payload.env:
                destination["env"] = dict(payload.env)
            mounts: list[dict[str, Any]] = []
            for item in payload.mounts or []:
                source = str((item or {}).get("source") or "")
                target = str((item or {}).get("target") or "")
                mount_payload: dict[str, Any] = {"source": source, "target": target}
                read_only = (item or {}).get("read_only")
                if read_only is None and "readOnly" in (item or {}):
                    read_only = (item or {}).get("readOnly")
                if read_only is None and "readonly" in (item or {}):
                    read_only = (item or {}).get("readonly")
                if read_only is not None:
                    mount_payload["read_only"] = read_only
                mounts.append(mount_payload)
            if mounts:
                destination["mounts"] = mounts
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported destination kind: {payload.kind!r}. "
                    "Use 'local' or 'docker'."
                ),
            )

        validated = validate_destination_write_payload(
            destination, context="destination"
        )
        if not validated.valid or validated.normalized_destination is None:
            detail = "; ".join(validated.errors) or "Invalid destination payload"
            raise HTTPException(status_code=400, detail=detail)
        return validated.normalized_destination

    async def list_agent_workspaces(self) -> dict[str, Any]:
        workspaces = await asyncio.to_thread(
            self._context.supervisor.list_agent_workspaces, use_cache=False
        )
        return {
            "agent_workspaces": [
                workspace.to_dict(self._context.config.root) for workspace in workspaces
            ]
        }

    async def create_agent_workspace(
        self, payload: HubCreateAgentWorkspaceRequest
    ) -> dict[str, Any]:
        from .....core.logging_utils import safe_log

        workspace_id = (payload.workspace_id or "").strip()
        runtime = (payload.runtime or "").strip()
        display_name = (payload.display_name or "").strip() or None
        if not workspace_id:
            raise HTTPException(status_code=400, detail="Missing workspace id")
        if not runtime:
            raise HTTPException(status_code=400, detail="Missing runtime")
        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub create agent workspace id=%s runtime=%s" % (workspace_id, runtime),
        )
        try:
            snapshot = await asyncio.to_thread(
                self._context.supervisor.create_agent_workspace,
                workspace_id=workspace_id,
                runtime=runtime,
                display_name=display_name,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return snapshot.to_dict(self._context.config.root)

    async def create_agent_workspace_job(
        self, payload: HubCreateAgentWorkspaceRequest
    ) -> dict[str, Any]:
        from .....core.request_context import get_request_id

        workspace_id = (payload.workspace_id or "").strip()
        runtime = (payload.runtime or "").strip()
        display_name = (payload.display_name or "").strip() or None

        async def _run_create_agent_workspace():
            if not workspace_id:
                raise ValueError("Missing workspace id")
            if not runtime:
                raise ValueError("Missing runtime")
            snapshot = await asyncio.to_thread(
                self._context.supervisor.create_agent_workspace,
                workspace_id=workspace_id,
                runtime=runtime,
                display_name=display_name,
            )
            return snapshot.to_dict(self._context.config.root)

        job = await self._context.job_manager.submit(
            "hub.create_agent_workspace",
            _run_create_agent_workspace,
            request_id=get_request_id(),
        )
        return job.to_dict()

    async def get_agent_workspace(self, workspace_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._workspace_payload, workspace_id)

    async def update_agent_workspace(
        self, workspace_id: str, payload: HubUpdateAgentWorkspaceRequest
    ) -> dict[str, Any]:
        fields_set = set(payload.model_fields_set or set())
        if not ({"enabled", "display_name"} & fields_set):
            raise HTTPException(
                status_code=400,
                detail="No agent workspace fields to update",
            )
        display_name: Optional[str] = None
        if "display_name" in fields_set:
            display_name = (payload.display_name or "").strip()
            if not display_name:
                raise HTTPException(
                    status_code=400,
                    detail="display_name must be non-empty when provided",
                )
        enabled = payload.enabled if "enabled" in fields_set else None
        try:
            await asyncio.to_thread(
                self._context.supervisor.update_agent_workspace,
                workspace_id,
                enabled=enabled,
                display_name=display_name,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await asyncio.to_thread(self._workspace_payload, workspace_id)

    async def get_agent_workspace_destination(
        self, workspace_id: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._workspace_payload, workspace_id)

    async def set_agent_workspace_destination(
        self, workspace_id: str, payload: HubDestinationSetRequest
    ) -> dict[str, Any]:
        normalized_destination = self._normalize_destination_payload(payload)
        try:
            await asyncio.to_thread(
                self._context.supervisor.set_agent_workspace_destination,
                workspace_id,
                normalized_destination,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await asyncio.to_thread(self._workspace_payload, workspace_id)

    async def remove_agent_workspace(
        self,
        workspace_id: str,
        payload: Optional[HubRemoveAgentWorkspaceRequest] = None,
    ) -> dict[str, Any]:
        if payload and payload.delete_dir:
            raise HTTPException(
                status_code=400,
                detail="Use delete for destructive agent workspace removal",
            )
        try:
            await asyncio.to_thread(
                self._context.supervisor.remove_agent_workspace,
                workspace_id,
                delete_dir=False,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "workspace_id": workspace_id, "delete_dir": False}

    async def remove_agent_workspace_job(
        self,
        workspace_id: str,
        payload: Optional[HubRemoveAgentWorkspaceRequest] = None,
    ) -> dict[str, Any]:
        from .....core.request_context import get_request_id

        if payload and payload.delete_dir:
            raise HTTPException(
                status_code=400,
                detail="Use delete for destructive agent workspace removal",
            )

        async def _run_remove_agent_workspace():
            await asyncio.to_thread(
                self._context.supervisor.remove_agent_workspace,
                workspace_id,
                delete_dir=False,
            )
            return {"status": "ok", "workspace_id": workspace_id, "delete_dir": False}

        job = await self._context.job_manager.submit(
            "hub.remove_agent_workspace",
            _run_remove_agent_workspace,
            request_id=get_request_id(),
        )
        return job.to_dict()

    async def delete_agent_workspace(
        self,
        workspace_id: str,
        payload: Optional[HubDeleteAgentWorkspaceRequest] = None,
    ) -> dict[str, Any]:
        if payload is not None and payload.delete_dir is False:
            raise HTTPException(
                status_code=400,
                detail="delete_dir must be true for destructive delete",
            )
        try:
            await asyncio.to_thread(
                self._context.supervisor.remove_agent_workspace,
                workspace_id,
                delete_dir=True,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "workspace_id": workspace_id, "delete_dir": True}

    async def delete_agent_workspace_job(
        self,
        workspace_id: str,
        payload: Optional[HubDeleteAgentWorkspaceRequest] = None,
    ) -> dict[str, Any]:
        from .....core.request_context import get_request_id

        if payload is not None and payload.delete_dir is False:
            raise HTTPException(
                status_code=400,
                detail="delete_dir must be true for destructive delete",
            )

        async def _run_delete_agent_workspace():
            await asyncio.to_thread(
                self._context.supervisor.remove_agent_workspace,
                workspace_id,
                delete_dir=True,
            )
            return {"status": "ok", "workspace_id": workspace_id, "delete_dir": True}

        job = await self._context.job_manager.submit(
            "hub.delete_agent_workspace",
            _run_delete_agent_workspace,
            request_id=get_request_id(),
        )
        return job.to_dict()


def build_hub_agent_workspace_router(context: HubAppContext) -> APIRouter:
    router = APIRouter()
    service = HubAgentWorkspaceService(context)

    @router.get("/hub/agent-workspaces")
    async def list_agent_workspaces():
        return await service.list_agent_workspaces()

    @router.post("/hub/agent-workspaces")
    async def create_agent_workspace(payload: HubCreateAgentWorkspaceRequest):
        return await service.create_agent_workspace(payload)

    @router.post("/hub/jobs/agent-workspaces", response_model=HubJobResponse)
    async def create_agent_workspace_job(payload: HubCreateAgentWorkspaceRequest):
        return await service.create_agent_workspace_job(payload)

    @router.get("/hub/agent-workspaces/{workspace_id}")
    async def get_agent_workspace(workspace_id: str):
        return await service.get_agent_workspace(workspace_id)

    @router.patch("/hub/agent-workspaces/{workspace_id}")
    async def update_agent_workspace(
        workspace_id: str, payload: HubUpdateAgentWorkspaceRequest
    ):
        return await service.update_agent_workspace(workspace_id, payload)

    @router.get("/hub/agent-workspaces/{workspace_id}/destination")
    async def get_agent_workspace_destination(workspace_id: str):
        return await service.get_agent_workspace_destination(workspace_id)

    @router.post("/hub/agent-workspaces/{workspace_id}/destination")
    async def set_agent_workspace_destination(
        workspace_id: str, payload: HubDestinationSetRequest
    ):
        return await service.set_agent_workspace_destination(workspace_id, payload)

    @router.post("/hub/agent-workspaces/{workspace_id}/remove")
    async def remove_agent_workspace(
        workspace_id: str, payload: Optional[HubRemoveAgentWorkspaceRequest] = None
    ):
        return await service.remove_agent_workspace(workspace_id, payload)

    @router.post(
        "/hub/jobs/agent-workspaces/{workspace_id}/remove",
        response_model=HubJobResponse,
    )
    async def remove_agent_workspace_job(
        workspace_id: str, payload: Optional[HubRemoveAgentWorkspaceRequest] = None
    ):
        return await service.remove_agent_workspace_job(workspace_id, payload)

    @router.post("/hub/agent-workspaces/{workspace_id}/delete")
    async def delete_agent_workspace(
        workspace_id: str, payload: Optional[HubDeleteAgentWorkspaceRequest] = None
    ):
        return await service.delete_agent_workspace(workspace_id, payload)

    @router.post(
        "/hub/jobs/agent-workspaces/{workspace_id}/delete",
        response_model=HubJobResponse,
    )
    async def delete_agent_workspace_job(
        workspace_id: str, payload: Optional[HubDeleteAgentWorkspaceRequest] = None
    ):
        return await service.delete_agent_workspace_job(workspace_id, payload)

    return router
