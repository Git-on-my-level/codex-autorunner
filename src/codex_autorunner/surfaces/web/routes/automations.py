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
from ..app_state import HubAppContext


class AutomationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str
    name: Optional[str] = None
    repo_id: Optional[str] = None
    timezone: str = "UTC"
    hour: int = 9
    minute: int = 0
    weekday: int = 0
    prompt: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    profile: Optional[str] = None
    enabled: bool = False


class AutomationEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AutomationUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    enabled: Optional[bool] = None
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
    trigger_kind: Optional[str] = None
    trigger: Optional[dict[str, Any]] = None
    filters: Optional[dict[str, Any]] = None
    target_policy: Optional[str] = None
    target: Optional[dict[str, Any]] = None
    executor_kind: Optional[str] = None
    executor: Optional[dict[str, Any]] = None
    policy: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None


def build_automation_routes(context: HubAppContext) -> APIRouter:
    router = APIRouter(prefix="/hub/automations", tags=["hub-automations"])

    def store() -> AutomationStore:
        return AutomationStore(context.config.root)

    @router.get("")
    async def list_automations(limit: int = 100) -> dict[str, Any]:
        return automation_overview(store(), limit=limit)

    @router.get("/{rule_id}")
    async def get_automation(rule_id: str) -> dict[str, Any]:
        try:
            return {"automation": automation_detail(store(), rule_id)}
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Automation not found: {rule_id}"
            ) from exc

    @router.post("")
    async def create_automation(payload: AutomationCreateRequest) -> dict[str, Any]:
        try:
            created = create_preset_automation(
                store(),
                AutomationPresetRequest(
                    preset=payload.preset,
                    name=payload.name,
                    repo_id=payload.repo_id,
                    timezone=payload.timezone,
                    hour=payload.hour,
                    minute=payload.minute,
                    weekday=payload.weekday,
                    prompt=payload.prompt,
                    agent=payload.agent,
                    model=payload.model,
                    reasoning=payload.reasoning,
                    profile=payload.profile,
                    enabled=payload.enabled,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"automation": created}

    @router.patch("/{rule_id}")
    async def update_automation_route(
        rule_id: str, payload: AutomationUpdatePayload
    ) -> dict[str, Any]:
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

    @router.post("/{rule_id}/run")
    async def run_automation(rule_id: str) -> dict[str, Any]:
        try:
            return run_automation_now(
                store(), rule_id, source="web", supervisor=context.supervisor
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Automation not found: {rule_id}"
            ) from exc

    @router.post("/{rule_id}/enabled")
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


__all__ = ["build_automation_routes"]
