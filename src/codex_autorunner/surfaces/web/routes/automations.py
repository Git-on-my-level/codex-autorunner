from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ....core.automation import AutomationStore
from ....core.automation.product import (
    AutomationPresetRequest,
    AutomationUpdateRequest,
    automation_detail,
    automation_overview,
    create_preset_automation,
    run_automation_now,
    update_automation,
)
from ....core.automation.product import (
    set_automation_enabled as set_automation_enabled_core,
)
from ....core.time_utils import now_iso
from ..app_state import HubAppContext

AUTOMATION_WORKSPACE_ROUTE = "/hub/read-models/automations/workspace"
AUTOMATION_WORKSPACE_INDEX_ROUTE = "/hub/read-models/automations/workspace-index"


class AutomationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str
    execution_mode: Optional[str] = None
    name: Optional[str] = None
    repo_id: Optional[str] = None
    timezone: Optional[str] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    weekday: Optional[int] = None
    prompt: Optional[str] = None
    ticket_body: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    profile: Optional[str] = None
    worker_child_policy: Optional[dict[str, Any]] = None
    enabled: bool = False


class AutomationEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AutomationUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: Optional[str] = None
    enabled: Optional[bool] = None
    execution_mode: Optional[str] = None
    timezone: Optional[str] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    weekday: Optional[int] = None
    prompt: Optional[str] = None
    ticket_body: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    profile: Optional[str] = None
    worker_child_policy: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None


def build_automation_routes(context: HubAppContext) -> APIRouter:
    router = APIRouter(tags=["hub-automations"])

    def store() -> AutomationStore:
        return AutomationStore(context.config.root)

    @router.get(AUTOMATION_WORKSPACE_ROUTE)
    async def automation_workspace(limit: int = 100) -> dict[str, Any]:
        overview = automation_overview(store(), limit=limit)
        return {
            **overview,
            "target_options": _automation_target_options(context),
            "agent_defaults": _automation_agent_defaults(context),
            "generated_at": now_iso(),
        }

    @router.get(AUTOMATION_WORKSPACE_INDEX_ROUTE)
    async def automation_workspace_index(
        limit: int = 100,
        recent_job_limit: int = 1,
        include_target_options: bool = False,
    ) -> dict[str, Any]:
        overview = automation_overview(
            store(),
            limit=limit,
            recent_job_limit=recent_job_limit,
            include_job_history=False,
            include_raw=False,
        )
        payload = {
            **overview,
            "agent_defaults": _automation_agent_defaults(context),
            "generated_at": now_iso(),
        }
        if include_target_options:
            payload["target_options"] = _automation_target_options(context)
        return payload

    @router.get("/hub/read-models/automations/target-options")
    async def automation_target_options() -> dict[str, Any]:
        return {"target_options": _automation_target_options(context)}

    @router.get("/hub/automations")
    async def list_automations(limit: int = 100) -> dict[str, Any]:
        return automation_overview(store(), limit=limit)

    @router.get("/hub/automations/{rule_id}")
    async def get_automation(rule_id: str) -> dict[str, Any]:
        try:
            return {"automation": automation_detail(store(), rule_id)}
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Automation not found: {rule_id}"
            ) from exc

    @router.post("/hub/automations")
    async def create_automation(payload: AutomationCreateRequest) -> dict[str, Any]:
        try:
            created = create_preset_automation(
                store(),
                AutomationPresetRequest(
                    preset=payload.preset,
                    execution_mode=payload.execution_mode,
                    name=payload.name,
                    repo_id=payload.repo_id,
                    timezone=payload.timezone,
                    hour=payload.hour,
                    minute=payload.minute,
                    weekday=payload.weekday,
                    prompt=payload.prompt,
                    ticket_body=payload.ticket_body,
                    agent=payload.agent,
                    model=payload.model,
                    reasoning=payload.reasoning,
                    profile=payload.profile,
                    worker_child_policy=payload.worker_child_policy,
                    enabled=payload.enabled,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"automation": created}

    @router.patch("/hub/automations/{rule_id}")
    async def update_automation_route(
        rule_id: str, payload: AutomationUpdatePayload
    ) -> dict[str, Any]:
        raw_payload = payload.model_extra or {}
        blocked = sorted(raw_payload)
        if blocked:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "AUTOMATION_PRODUCT_UNSUPPORTED_RAW_FIELDS",
                    "message": (
                        "Product automation updates only accept typed edit fields; "
                        "use /hub/api/control-plane/automations/rules for raw rule edits."
                    ),
                    "fields": blocked,
                },
            )
        try:
            updated = update_automation(
                store(),
                rule_id,
                AutomationUpdateRequest(**payload.model_dump(exclude_unset=True)),
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Automation not found: {rule_id}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"automation": updated}

    @router.post("/hub/automations/{rule_id}/run")
    async def run_automation(rule_id: str) -> dict[str, Any]:
        try:
            return run_automation_now(
                store(), rule_id, source="web", supervisor=context.supervisor
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Automation not found: {rule_id}"
            ) from exc

    @router.delete("/hub/automations/{rule_id}")
    async def delete_automation(rule_id: str) -> dict[str, Any]:
        rule_store = store()
        existing = rule_store.get_rule(rule_id)
        if existing is None:
            raise HTTPException(
                status_code=404, detail=f"Automation not found: {rule_id}"
            )
        if existing.enabled:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "AUTOMATION_DELETE_REQUIRES_PAUSED",
                    "message": "Pause the automation before deleting it.",
                },
            )
        rule_store.delete_rule(rule_id)
        return {"deleted": rule_id}

    @router.post("/hub/automations/{rule_id}/enabled")
    async def set_automation_enabled(
        rule_id: str, payload: AutomationEnabledRequest
    ) -> dict[str, Any]:
        try:
            return {
                "automation": set_automation_enabled_core(
                    store(), rule_id, payload.enabled
                )
            }
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Automation not found: {rule_id}"
            ) from exc

    return router


def _automation_target_options(context: HubAppContext) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for snapshot in context.supervisor.list_repos(use_cache=True):
        repo_id = str(getattr(snapshot, "id", "") or "").strip()
        if not repo_id:
            continue
        kind = "worktree" if getattr(snapshot, "kind", None) == "worktree" else "repo"
        label = str(
            getattr(snapshot, "display_name", None)
            or getattr(snapshot, "branch", None)
            or repo_id
        )
        disabled = not bool(getattr(snapshot, "exists_on_disk", True)) or not bool(
            getattr(snapshot, "initialized", True)
        )
        option: dict[str, Any] = {"id": repo_id, "label": label, "kind": kind}
        if disabled:
            option["disabled"] = True
        options.append(option)
    return sorted(options, key=lambda item: (item["kind"] == "worktree", item["label"]))


def _automation_agent_defaults(context: HubAppContext) -> dict[str, Any]:
    raw_config = getattr(context.config, "raw", {})
    pma_config = raw_config.get("pma", {}) if isinstance(raw_config, dict) else {}
    defaults = pma_config if isinstance(pma_config, dict) else {}
    return {
        "default_agent": str(defaults.get("default_agent") or "codex"),
        "default_profile": defaults.get("profile") or None,
        "default_model": defaults.get("model") or None,
        "default_reasoning": defaults.get("reasoning") or None,
    }


__all__ = [
    "AUTOMATION_WORKSPACE_INDEX_ROUTE",
    "AUTOMATION_WORKSPACE_ROUTE",
    "build_automation_routes",
]
