"""
App-server support routes (thread registry).
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ...core.app_server_threads import normalize_feature_key
from ...core.utils import is_within
from ...integrations.app_server.client import CodexAppServerError
from .schemas import (
    AppServerThreadArchiveRequest,
    AppServerThreadArchiveResponse,
    AppServerThreadResetAllResponse,
    AppServerThreadResetRequest,
    AppServerThreadResetResponse,
    AppServerThreadsResponse,
)
from .shared import SSE_HEADERS


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
            headers=SSE_HEADERS,
        )

    @router.get("/api/app-server/threads", response_model=AppServerThreadsResponse)
    def app_server_threads(request: Request):
        registry = request.app.state.app_server_threads
        return registry.feature_map()

    @router.get("/api/app-server/models")
    async def app_server_models(request: Request):
        engine = request.app.state.engine
        supervisor = request.app.state.app_server_supervisor
        try:
            client = await supervisor.get_client(engine.repo_root)
            return await client.model_list()
        except CodexAppServerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

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
        "/api/app-server/threads/archive",
        response_model=AppServerThreadArchiveResponse,
    )
    async def archive_app_server_thread(
        request: Request, payload: AppServerThreadArchiveRequest
    ):
        thread_id = payload.thread_id.strip()
        if not thread_id:
            raise HTTPException(status_code=400, detail="thread_id is required")
        engine = request.app.state.engine
        supervisor = request.app.state.app_server_supervisor
        try:
            client = await supervisor.get_client(engine.repo_root)
            await client.thread_archive(thread_id)
        except CodexAppServerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"status": "ok", "thread_id": thread_id, "archived": True}

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

    @router.get("/api/app-server/account")
    async def app_server_account(request: Request):
        engine = request.app.state.engine
        supervisor = request.app.state.app_server_supervisor
        try:
            client = await supervisor.get_client(engine.repo_root)
            return await client.account_read()
        except CodexAppServerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/api/app-server/rate-limits")
    async def app_server_rate_limits(request: Request):
        engine = request.app.state.engine
        supervisor = request.app.state.app_server_supervisor
        try:
            client = await supervisor.get_client(engine.repo_root)
            return await client.rate_limits_read()
        except CodexAppServerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return router
