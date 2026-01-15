"""
App-server support routes (thread registry).
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ..core.app_server_threads import normalize_feature_key
from ..core.utils import is_within
from ..web.schemas import (
    AppServerThreadResetAllResponse,
    AppServerThreadResetRequest,
    AppServerThreadResetResponse,
    AppServerThreadsResponse,
)


def build_app_server_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/app-server/turns/{turn_id}/events")
    async def stream_app_server_turn_events(
        turn_id: str, request: Request, thread_id: str
    ):
        events = getattr(request.app.state, "app_server_events", None)
        if events is None:
            raise HTTPException(status_code=404, detail="App-server events unavailable")
        if not thread_id:
            raise HTTPException(status_code=400, detail="thread_id is required")
        return StreamingResponse(
            events.stream(thread_id, turn_id),
            media_type="text/event-stream",
        )

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

    @router.post(
        "/api/app-server/threads/reset-all",
        response_model=AppServerThreadResetAllResponse,
    )
    def reset_app_server_threads(request: Request):
        registry = request.app.state.app_server_threads
        registry.reset_all()
        return {"status": "ok", "cleared": True}

    @router.get("/api/app-server/threads/backup")
    def download_app_server_threads_backup(request: Request):
        registry = request.app.state.app_server_threads
        notice = registry.corruption_notice() or {}
        backup_path = notice.get("backup_path")
        if not isinstance(backup_path, str) or not backup_path:
            raise HTTPException(status_code=404, detail="No backup available")
        path = Path(backup_path)
        engine = request.app.state.engine
        if not is_within(engine.repo_root, path):
            raise HTTPException(status_code=400, detail="Invalid backup path")
        if not path.exists():
            raise HTTPException(status_code=404, detail="Backup not found")
        return FileResponse(path, filename=path.name)

    return router
