"""
Base routes: Index, WebSocket terminal.
"""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
)

from ...core.config import HubConfig
from ...core.logging_utils import safe_log
from ...core.state import SessionRecord, now_iso, persist_session_registry
from .pty_session import REPLAY_END, ActiveSession, PTYSession
from .schemas import VersionResponse
from .shared import (
    build_codex_terminal_cmd,
    build_opencode_terminal_cmd,
)
from .static_assets import index_response_headers, render_index_html
from .static_refresh import refresh_static_assets

ALT_SCREEN_ENTER = b"\x1b[?1049h"


def _serve_index(request: Request, static_dir: Path):
    active_static = getattr(request.app.state, "static_dir", static_dir)
    index_path = active_static / "index.html"
    if not index_path.exists():
        if refresh_static_assets(request.app):
            active_static = request.app.state.static_dir
            index_path = active_static / "index.html"
    if not index_path.exists():
        raise HTTPException(
            status_code=500, detail="Static UI assets missing; reinstall package"
        )
    html = render_index_html(active_static, request.app.state.asset_version)
    return HTMLResponse(html, headers=index_response_headers())


def build_base_routes(static_dir: Path) -> APIRouter:
    """Build routes for index, state, logs, and terminal WebSocket."""
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    def index(request: Request):
        return _serve_index(request, static_dir)

    @router.get("/api/version", response_model=VersionResponse)
    def get_version(request: Request):
        return {"asset_version": request.app.state.asset_version}

    @router.get("/api/repo/health")
    def repo_health(request: Request):
        config = getattr(request.app.state, "config", None)
        if isinstance(config, HubConfig):
            raise HTTPException(
                status_code=404, detail="Repo health not available in hub mode"
            )

        engine = getattr(request.app.state, "engine", None)
        repo_root = getattr(engine, "repo_root", None)
        if repo_root is None:
            return JSONResponse(
                {"status": "error", "detail": "Repo context unavailable"},
                status_code=503,
            )

        flows_db = repo_root / ".codex-autorunner" / "flows.db"

        docs_dir = repo_root / ".codex-autorunner"
        docs_status = "ok" if docs_dir.exists() else "missing"

        tickets_dir = repo_root / ".codex-autorunner" / "tickets"
        tickets_status = "ok" if tickets_dir.exists() else "missing"

        flows_status = "ok" if tickets_dir.exists() else "missing"
        flows_detail = None

        overall_status = (
            "ok" if docs_status == "ok" and tickets_status == "ok" else "degraded"
        )

        return {
            "status": overall_status,
            "mode": "repo",
            "repo_root": str(repo_root),
            "flows": {
                "status": flows_status,
                "path": str(flows_db),
                "detail": flows_detail,
            },
            "docs": {"status": docs_status, "path": str(docs_dir)},
            "tickets": {"status": tickets_status, "path": str(tickets_dir)},
        }

    @router.websocket("/api/terminal")
    async def terminal(ws: WebSocket):
        selected_protocol = None
        protocol_header = ws.headers.get("sec-websocket-protocol")
        if protocol_header:
            for entry in protocol_header.split(","):
                candidate = entry.strip()
                if not candidate:
                    continue
                if candidate == "car-token":
                    selected_protocol = candidate
                    break
                if candidate.startswith("car-token-b64."):
                    selected_protocol = candidate
                    break
                if candidate.startswith("car-token."):
                    selected_protocol = candidate
                    break
        if selected_protocol:
            await ws.accept(subprotocol=selected_protocol)
        else:
            await ws.accept()
        app = ws.scope.get("app")
        if app is None:
            await ws.close()
            return
        # Track websocket for graceful shutdown during reload
        active_websockets = getattr(app.state, "active_websockets", None)
        if active_websockets is not None:
            active_websockets.add(ws)
        logger = app.state.logger
        engine = app.state.engine
        terminal_sessions: dict[str, ActiveSession] = app.state.terminal_sessions
        terminal_lock: asyncio.Lock = app.state.terminal_lock
        session_registry: dict[str, SessionRecord] = app.state.session_registry
        repo_to_session: dict[str, str] = app.state.repo_to_session
        session_env = getattr(app.state, "env", None)
        repo_path = str(engine.repo_root)
        state_path = engine.state_path
        agent = (ws.query_params.get("agent") or "codex").strip().lower()
        model = (ws.query_params.get("model") or "").strip() or None
        reasoning = (ws.query_params.get("reasoning") or "").strip() or None
        session_id = None
        active_session: Optional[ActiveSession] = None
        seen_update_interval = 5.0

        ws_input_bytes_total = 0
        ws_input_message_count = 0
        ws_rate_limit_window_start = time.monotonic()
        MAX_BYTES_PER_CONNECTION = 10 * 1024 * 1024
        MAX_MESSAGES_PER_WINDOW = 1000
        RATE_LIMIT_WINDOW_SECONDS = 60.0

        def _session_key(repo: str, agent: str) -> str:
            normalized = (agent or "").strip().lower()
            # Backwards-compatible keying:
            # - Codex sessions continue to use the bare repo path key so existing
            #   CLI/API callers that only know `repo_path` keep working.
            # - Other agents (e.g. OpenCode) use a scoped `repo:agent` key.
            if not normalized or normalized == "codex":
                return repo
            return f"{repo}:{normalized}"

        client_session_id = ws.query_params.get("session_id")
        close_session_id = ws.query_params.get("close_session_id")
        mode = (ws.query_params.get("mode") or "").strip().lower()
        attach_only = mode == "attach"
        terminal_debug_param = (ws.query_params.get("terminal_debug") or "").strip()
        terminal_debug = terminal_debug_param.lower() in {"1", "true", "yes", "on"}

        def _mark_dirty() -> None:
            app.state.session_state_dirty = True

        def _maybe_persist_sessions(force: bool = False) -> None:
            now = time.time()
            last_write = app.state.session_state_last_write
            if not force and not app.state.session_state_dirty:
                return
            if not force and now - last_write < seen_update_interval:
                return
            persist_session_registry(state_path, session_registry, repo_to_session)
            app.state.session_state_last_write = now
            app.state.session_state_dirty = False

        def _touch_session(session_id: str) -> None:
            record = session_registry.get(session_id)
            if not record:
                return
            record.last_seen_at = now_iso()
            if record.status != "active":
                record.status = "active"
            _mark_dirty()
            _maybe_persist_sessions()

        async with terminal_lock:
            if client_session_id and client_session_id in terminal_sessions:
                active_session = terminal_sessions[client_session_id]
                if not active_session.pty.isalive():
                    active_session.close()
                    terminal_sessions.pop(client_session_id, None)
                    session_registry.pop(client_session_id, None)
                    repo_to_session = {
                        _session_key(repo, ag): sid
                        for repo, ag, sid in [
                            (
                                k.split(":", 1)[0],
                                (
                                    (k.split(":", 1)[1] or "codex")
                                    if ":" in k
                                    else "codex"
                                ),
                                v,
                            )
                            for k, v in repo_to_session.items()
                        ]
                        if sid != client_session_id
                    }
                    app.state.repo_to_session = repo_to_session
                    active_session = None
                    _mark_dirty()
                else:
                    session_id = client_session_id

            if not active_session:
                mapped_session_id = repo_to_session.get(_session_key(repo_path, agent))
                if mapped_session_id:
                    mapped_session = terminal_sessions.get(mapped_session_id)
                    if mapped_session and mapped_session.pty.isalive():
                        active_session = mapped_session
                        session_id = mapped_session_id
                    else:
                        if mapped_session:
                            mapped_session.close()
                        terminal_sessions.pop(mapped_session_id, None)
                        session_registry.pop(mapped_session_id, None)
                        repo_to_session.pop(_session_key(repo_path, agent), None)
                        _mark_dirty()
                if attach_only:
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": "Session not found",
                                "session_id": client_session_id,
                            }
                        )
                    )
                    await ws.close()
                    return
                if (
                    close_session_id
                    and close_session_id in terminal_sessions
                    and close_session_id != client_session_id
                ):
                    try:
                        session_to_close = terminal_sessions[close_session_id]
                        session_to_close.close()
                        await session_to_close.wait_closed()
                    finally:
                        terminal_sessions.pop(close_session_id, None)
                        session_registry.pop(close_session_id, None)
                        repo_to_session = {
                            _session_key(repo, ag): sid
                            for repo, ag, sid in [
                                (
                                    k.split(":", 1)[0],
                                    (
                                        (k.split(":", 1)[1] or "codex")
                                        if ":" in k
                                        else "codex"
                                    ),
                                    v,
                                )
                                for k, v in repo_to_session.items()
                            ]
                            if sid != close_session_id
                        }
                        app.state.repo_to_session = repo_to_session
                        _mark_dirty()
                session_id = str(uuid.uuid4())
                resume_mode = mode == "resume"
                if agent == "opencode":
                    cmd = build_opencode_terminal_cmd(
                        engine.config.agent_binary("opencode"),
                        model,
                    )
                else:
                    cmd = build_codex_terminal_cmd(
                        engine,
                        resume_mode=resume_mode,
                        model=model,
                        reasoning=reasoning,
                    )
                try:
                    pty = PTYSession(cmd, cwd=str(engine.repo_root), env=session_env)
                    active_session = ActiveSession(
                        session_id, pty, asyncio.get_running_loop()
                    )
                    terminal_sessions[session_id] = active_session
                    session_registry[session_id] = SessionRecord(
                        repo_path=repo_path,
                        created_at=now_iso(),
                        last_seen_at=now_iso(),
                        status="active",
                        agent=agent,
                    )
                    repo_to_session[_session_key(repo_path, agent)] = session_id
                    _mark_dirty()
                except FileNotFoundError:
                    binary = cmd[0] if cmd else "codex"
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": f"Agent binary not found: {binary}",
                            }
                        )
                    )
                    await ws.close()
                    return
            if active_session:
                if session_id and session_id not in session_registry:
                    session_registry[session_id] = SessionRecord(
                        repo_path=repo_path,
                        created_at=now_iso(),
                        last_seen_at=now_iso(),
                        status="active",
                        agent=agent,
                    )
                    _mark_dirty()
                if (
                    session_id
                    and repo_to_session.get(_session_key(repo_path, agent))
                    != session_id
                ):
                    repo_to_session[_session_key(repo_path, agent)] = session_id
                    _mark_dirty()
                _maybe_persist_sessions(force=True)

        if attach_only and active_session:
            active_session.refresh_alt_screen_state()
        await ws.send_text(json.dumps({"type": "hello", "session_id": session_id}))
        if attach_only and active_session and active_session.alt_screen_active:
            await ws.send_bytes(ALT_SCREEN_ENTER)
        if terminal_debug and active_session:
            buffer_bytes, buffer_chunks = active_session.get_buffer_stats()
            safe_log(
                logger,
                logging.INFO,
                (
                    "Terminal connect debug: mode="
                    f"{mode} session={session_id} attach={attach_only} "
                    f"alt_screen={active_session.alt_screen_active} "
                    f"buffer_bytes={buffer_bytes} buffer_chunks={buffer_chunks}"
                ),
            )
        include_replay_end = attach_only or mode == "resume" or bool(client_session_id)
        if active_session is None:
            await ws.close()
            return
        queue = active_session.add_subscriber(include_replay_end=include_replay_end)

        async def pty_to_ws():
            try:
                while True:
                    data = await queue.get()
                    if data is REPLAY_END:
                        await ws.send_text(json.dumps({"type": "replay_end"}))
                        continue
                    if data is None:
                        if active_session:
                            exit_code = active_session.pty.exit_code()
                            if session_id:
                                async with terminal_lock:
                                    record = session_registry.get(session_id)
                                    if record:
                                        record.status = "closed"
                                        record.last_seen_at = now_iso()
                                _mark_dirty()
                            notifier = getattr(engine, "notifier", None)
                            if notifier:
                                asyncio.create_task(
                                    notifier.notify_tui_session_finished_async(
                                        session_id=session_id,
                                        exit_code=exit_code,
                                        repo_path=repo_path,
                                    )
                                )
                            await ws.send_text(
                                json.dumps(
                                    {
                                        "type": "exit",
                                        "code": exit_code,
                                        "session_id": session_id,
                                    }
                                )
                            )
                        break
                    await ws.send_bytes(data)
                    if session_id:
                        _touch_session(session_id)
            except Exception:
                safe_log(logger, logging.WARNING, "Terminal PTY to WS bridge failed")

        async def ws_to_pty():
            nonlocal ws_input_bytes_total, ws_input_message_count, ws_rate_limit_window_start
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("bytes") is not None:
                        ws_input_message_count += 1
                        ws_input_bytes_total += len(msg["bytes"])
                        if (
                            ws_input_bytes_total > MAX_BYTES_PER_CONNECTION
                            or ws_input_message_count > MAX_MESSAGES_PER_WINDOW
                        ):
                            await ws.close(code=1008, reason="Rate limit exceeded")
                            return
                        now = time.monotonic()
                        if now - ws_rate_limit_window_start > RATE_LIMIT_WINDOW_SECONDS:
                            ws_input_bytes_total = 0
                            ws_input_message_count = 0
                            ws_rate_limit_window_start = now
                        # Queue input so PTY writes never block the event loop.
                        active_session.write_input(msg["bytes"])
                        active_session.mark_input_activity()
                        if session_id:
                            _touch_session(session_id)
                        continue
                    text = msg.get("text")
                    if not text:
                        continue
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") == "resize":
                        cols = int(payload.get("cols", 0))
                        rows = int(payload.get("rows", 0))
                        if cols > 0 and rows > 0:
                            active_session.pty.resize(cols, rows)
                    elif payload.get("type") == "input":
                        input_id = payload.get("id")
                        data = payload.get("data")
                        if not input_id or not isinstance(input_id, str):
                            await ws.send_text(
                                json.dumps(
                                    {
                                        "type": "error",
                                        "message": "invalid input id",
                                    }
                                )
                            )
                            continue
                        if data is None or not isinstance(data, str):
                            await ws.send_text(
                                json.dumps(
                                    {
                                        "type": "ack",
                                        "id": input_id,
                                        "ok": False,
                                        "message": "invalid input data",
                                    }
                                )
                            )
                            continue
                        encoded = data.encode("utf-8", errors="replace")
                        if len(encoded) > 1024 * 1024:
                            await ws.send_text(
                                json.dumps(
                                    {
                                        "type": "ack",
                                        "id": input_id,
                                        "ok": False,
                                        "message": "input too large",
                                    }
                                )
                            )
                            continue
                        ws_input_message_count += 1
                        ws_input_bytes_total += len(encoded)
                        if (
                            ws_input_bytes_total > MAX_BYTES_PER_CONNECTION
                            or ws_input_message_count > MAX_MESSAGES_PER_WINDOW
                        ):
                            await ws.close(code=1008, reason="Rate limit exceeded")
                            return
                        now = time.monotonic()
                        if now - ws_rate_limit_window_start > RATE_LIMIT_WINDOW_SECONDS:
                            ws_input_bytes_total = 0
                            ws_input_message_count = 0
                            ws_rate_limit_window_start = now
                        if active_session.mark_input_id_seen(input_id):
                            active_session.write_input(encoded)
                            active_session.mark_input_activity()
                        await ws.send_text(
                            json.dumps({"type": "ack", "id": input_id, "ok": True})
                        )
                        if session_id:
                            _touch_session(session_id)
                    elif payload.get("type") == "ping":
                        ws_input_message_count += 1
                        if ws_input_message_count > MAX_MESSAGES_PER_WINDOW:
                            await ws.close(code=1008, reason="Rate limit exceeded")
                            return
                        now = time.monotonic()
                        if now - ws_rate_limit_window_start > RATE_LIMIT_WINDOW_SECONDS:
                            ws_input_message_count = 0
                            ws_rate_limit_window_start = now
                        await ws.send_text(json.dumps({"type": "pong"}))
                        if session_id:
                            _touch_session(session_id)
            except WebSocketDisconnect:
                pass
            except Exception:
                safe_log(logger, logging.WARNING, "Terminal WS to PTY bridge failed")

        forward_task = asyncio.create_task(pty_to_ws())
        input_task = asyncio.create_task(ws_to_pty())
        try:
            done, pending = await asyncio.wait(
                [forward_task, input_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    task.result()
                except Exception:
                    safe_log(logger, logging.WARNING, "Terminal websocket task failed")
        finally:
            forward_task.cancel()
            input_task.cancel()

        if active_session:
            active_session.remove_subscriber(queue)
            if not active_session.pty.isalive():
                async with terminal_lock:
                    if session_id:
                        terminal_sessions.pop(session_id, None)
                        session_registry.pop(session_id, None)
                        repo_to_session = {
                            _session_key(repo, ag): sid
                            for repo, ag, sid in [
                                (
                                    k.split(":", 1)[0],
                                    (
                                        (k.split(":", 1)[1] or "codex")
                                        if ":" in k
                                        else "codex"
                                    ),
                                    v,
                                )
                                for k, v in repo_to_session.items()
                            ]
                            if sid != session_id
                        }
                        app.state.repo_to_session = repo_to_session
                        _mark_dirty()
            if session_id:
                _touch_session(session_id)
            _maybe_persist_sessions(force=True)

        try:
            await ws.close()
        except Exception:
            safe_log(logger, logging.WARNING, "Terminal websocket close failed")
        finally:
            # Unregister websocket from active set
            if active_websockets is not None:
                active_websockets.discard(ws)

    return router


def build_frontend_routes(static_dir: Path) -> APIRouter:
    """Build catch-all routes for frontend tabs."""
    router = APIRouter()

    @router.get("/{tab}", include_in_schema=False)
    def tab_route(tab: str, request: Request):
        if tab in {
            "workspace",
            "tickets",
            "messages",
            "analytics",
            "terminal",
            "settings",
        }:
            return _serve_index(request, static_dir)
        raise HTTPException(status_code=404, detail="Not Found")

    return router
