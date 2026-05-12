from __future__ import annotations

import asyncio

from fastapi import APIRouter

from ..app_state import HubAppContext
from ..schemas import HubStateUpdateRequest


def build_hub_state_routes(context: HubAppContext) -> APIRouter:
    router = APIRouter(prefix="/hub/state", tags=["hub-state"])

    @router.get("")
    async def get_hub_state() -> dict[str, str]:
        title = await asyncio.to_thread(context.supervisor.get_hub_title)
        return {"title": title}

    @router.put("")
    async def update_hub_state(payload: HubStateUpdateRequest) -> dict[str, str]:
        title = await asyncio.to_thread(context.supervisor.set_hub_title, payload.title)
        return {"title": title}

    return router
