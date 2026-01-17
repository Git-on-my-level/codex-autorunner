"""
Terminal session registry routes.
"""

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..core.state import persist_session_registry
from ..web.schemas import (
    SessionsResponse,
    SessionStopRequest,
    SessionStopResponse,
)


def _relative_repo_path(repo_path: str, repo_root: Path) -> str:
    path = Path(repo_path)
    if not path.is_absolute():
        return repo_path
    try:
        rel = path.resolve().relative_to(repo_root)
        return rel.as_posix() or "."
    except Exception:
        return path.name


def _relative_repo_key(repo_key: str, repo_root: Path) -> str:
    """
    Format repo_to_session keys for display in API responses.

    Keys are either:
    - `<repo_path>` for the default codex agent (backwards compatible)
    - `<repo_path>:<agent>` for non-default agents (e.g. opencode)
    """
    if ":" not in repo_key:
        return _relative_repo_path(repo_key, repo_root)
    repo_path, agent = repo_key.split(":", 1)
    rel = _relative_repo_path(repo_path, repo_root)
    agent = agent.strip().lower()
    if not agent or agent == "codex":
        return rel
    return f"{rel}:{agent}"


def _allow_abs_paths(request: Request, include_abs_paths: bool) -> bool:
    if not include_abs_paths:
        return False
    return bool(getattr(request.app.state, "auth_token", None))


def _session_payload(
    session_id: str,
    record,
    terminal_sessions: dict,
    repo_root: Path,
    include_abs_paths: bool,
) -> dict:
    active = terminal_sessions.get(session_id)
    alive = bool(active and active.pty.isalive())
    payload = {
        "session_id": session_id,
        "repo_path": _relative_repo_path(record.repo_path, repo_root),
        "created_at": record.created_at,
        "last_seen_at": record.last_seen_at,
        "status": record.status,
        "alive": alive,
    }
    if include_abs_paths:
        payload["abs_repo_path"] = record.repo_path
    return payload


def build_sessions_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/sessions", response_model=SessionsResponse)
    def list_sessions(request: Request, include_abs_paths: bool = False):
        terminal_sessions = request.app.state.terminal_sessions
        session_registry = request.app.state.session_registry
        repo_to_session = request.app.state.repo_to_session
        repo_root = Path(request.app.state.engine.repo_root)
        allow_abs = _allow_abs_paths(request, include_abs_paths)
        sessions = [
            _session_payload(
                session_id, record, terminal_sessions, repo_root, allow_abs
            )
            for session_id, record in session_registry.items()
        ]
        repo_to_session_payload = {
            _relative_repo_key(repo_key, repo_root): session_id
            for repo_key, session_id in repo_to_session.items()
        }
        payload = {
            "sessions": sessions,
            "repo_to_session": repo_to_session_payload,
        }
        if allow_abs:
            payload["abs_repo_to_session"] = dict(repo_to_session)
        return {
            **payload,
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
            repo_root = Path(request.app.state.engine.repo_root)
            normalized_repo_path = repo_path.strip()
            if normalized_repo_path:
                candidate = Path(normalized_repo_path)
                if not candidate.is_absolute():
                    candidate = (repo_root / candidate).resolve()
                try:
                    candidate.relative_to(repo_root)
                except ValueError:
                    normalized_repo_path = ""
                else:
                    normalized_repo_path = str(candidate)
            candidates: list[str] = []
            if normalized_repo_path:
                candidates.extend(
                    [normalized_repo_path, f"{normalized_repo_path}:opencode"]
                )
            candidates.extend([repo_path, f"{repo_path}:opencode"])
            for key in candidates:
                mapped = repo_to_session.get(key)
                if mapped:
                    session_id = mapped
                    break
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
