"""
App-server support routes (thread registry).
"""

from fastapi import APIRouter, HTTPException, Request

from ..core.app_server_threads import normalize_feature_key
from ..web.schemas import (
    AppServerThreadResetRequest,
    AppServerThreadResetResponse,
    AppServerThreadsResponse,
)


def build_app_server_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/app-server/threads", response_model=AppServerThreadsResponse)
    def app_server_threads(request: Request):
        registry = request.app.state.app_server_threads
        return registry.feature_map()

    @router.post(
        "/api/app-server/threads/reset", response_model=AppServerThreadResetResponse
    )
    def reset_app_server_thread(request: Request, payload: AppServerThreadResetRequest):
        registry = request.app.state.app_server_threads
        try:
            key = normalize_feature_key(payload.key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        cleared = registry.reset_thread(key)
        return {"status": "ok", "key": key, "cleared": cleared}

    return router
