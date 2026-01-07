"""
Terminal session registry routes.
"""

import time

from fastapi import APIRouter, HTTPException, Request

from ..core.state import persist_session_registry
from ..web.schemas import (
    SessionsResponse,
    SessionStopRequest,
    SessionStopResponse,
)


def _session_payload(session_id: str, record, terminal_sessions: dict) -> dict:
    active = terminal_sessions.get(session_id)
    alive = bool(active and active.pty.isalive())
    return {
        "session_id": session_id,
        "repo_path": record.repo_path,
        "created_at": record.created_at,
        "last_seen_at": record.last_seen_at,
        "status": record.status,
        "alive": alive,
    }


def build_sessions_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/sessions", response_model=SessionsResponse)
    def list_sessions(request: Request):
        terminal_sessions = request.app.state.terminal_sessions
        session_registry = request.app.state.session_registry
        repo_to_session = request.app.state.repo_to_session
        sessions = [
            _session_payload(session_id, record, terminal_sessions)
            for session_id, record in session_registry.items()
        ]
        return {
            "sessions": sessions,
            "repo_to_session": dict(repo_to_session),
        }

    @router.post("/api/sessions/stop", response_model=SessionStopResponse)
    async def stop_session(request: Request, payload: SessionStopRequest):
        session_id = payload.session_id
        repo_path = payload.repo_path
        if not session_id and not repo_path:
            raise HTTPException(
                status_code=400, detail="Provide session_id or repo_path"
            )

        terminal_sessions = request.app.state.terminal_sessions
        session_registry = request.app.state.session_registry
        repo_to_session = request.app.state.repo_to_session
        terminal_lock = request.app.state.terminal_lock
        engine = request.app.state.engine

        if repo_path and isinstance(repo_path, str):
            session_id = repo_to_session.get(repo_path)
        if not isinstance(session_id, str) or not session_id:
            raise HTTPException(status_code=404, detail="Session not found")
        if session_id not in session_registry and session_id not in terminal_sessions:
            raise HTTPException(status_code=404, detail="Session not found")

        async with terminal_lock:
            session = terminal_sessions.get(session_id)
            if session:
                session.close()
                await session.wait_closed()
                terminal_sessions.pop(session_id, None)
            session_registry.pop(session_id, None)
            repo_to_session = {
                repo: sid for repo, sid in repo_to_session.items() if sid != session_id
            }
            request.app.state.repo_to_session = repo_to_session
            persist_session_registry(
                engine.state_path, session_registry, repo_to_session
            )
            request.app.state.session_state_last_write = time.time()
            request.app.state.session_state_dirty = False

        return {"status": "stopped", "session_id": session_id}

    return router
