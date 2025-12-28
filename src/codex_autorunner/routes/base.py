"""
Base routes: Index, state streaming, WebSocket terminal, and logs.
"""

import asyncio
import logging
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from ..codex_cli import extract_flag_value
from ..pty_session import ActiveSession, PTYSession, REPLAY_END
from ..state import SessionRecord, load_state, now_iso, persist_session_registry
from ..static_assets import index_response_headers, render_index_html
from ..logging_utils import safe_log
from .shared import build_codex_terminal_cmd, log_stream, state_stream

ALT_SCREEN_ENTER = b"\x1b[?1049h"


def build_base_routes(static_dir: Path) -> APIRouter:
    """Build routes for index, state, logs, and terminal WebSocket."""
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    def index(request: Request):
        index_path = static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=500, detail="Static UI assets missing; reinstall package"
            )
        html = render_index_html(static_dir, request.app.state.asset_version)
        return HTMLResponse(html, headers=index_response_headers())

    @router.get("/api/state")
    def get_state(request: Request):
        engine = request.app.state.engine
        manager = request.app.state.manager
        config = request.app.state.config
        state = load_state(engine.state_path)
        outstanding, done = engine.docs.todos()
        codex_model = config.codex_model or extract_flag_value(
            config.codex_args, "--model"
        )
        return {
            "last_run_id": state.last_run_id,
            "status": state.status,
            "last_exit_code": state.last_exit_code,
            "last_run_started_at": state.last_run_started_at,
            "last_run_finished_at": state.last_run_finished_at,
            "outstanding_count": len(outstanding),
            "done_count": len(done),
            "running": manager.running,
            "runner_pid": state.runner_pid,
            "terminal_idle_timeout_seconds": config.terminal_idle_timeout_seconds,
            "codex_model": codex_model or "auto",
        }

    @router.get("/api/version")
    def get_version(request: Request):
        return {"asset_version": request.app.state.asset_version}

    @router.get("/api/state/stream")
    async def stream_state_endpoint(request: Request):
        engine = request.app.state.engine
        manager = request.app.state.manager
        return StreamingResponse(
            state_stream(engine, manager, logger=request.app.state.logger),
            media_type="text/event-stream",
        )

    @router.get("/api/logs")
    def get_logs(
        request: Request, run_id: Optional[int] = None, tail: Optional[int] = None
    ):
        engine = request.app.state.engine
        if run_id is not None:
            block = engine.read_run_block(run_id)
            if not block:
                raise HTTPException(status_code=404, detail="run not found")
            return JSONResponse({"run_id": run_id, "log": block})
        if tail is not None:
            return JSONResponse({"tail": tail, "log": engine.tail_log(tail)})
        state = load_state(engine.state_path)
        if state.last_run_id is None:
            return JSONResponse({"log": ""})
        block = engine.read_run_block(state.last_run_id) or ""
        return JSONResponse({"run_id": state.last_run_id, "log": block})

    @router.get("/api/logs/stream")
    async def stream_logs_endpoint(request: Request):
        engine = request.app.state.engine
        return StreamingResponse(
            log_stream(engine.log_path), media_type="text/event-stream"
        )

    @router.websocket("/api/terminal")
    async def terminal(ws: WebSocket):
        await ws.accept()
        app = ws.scope.get("app")
        logger = app.state.logger
        engine = app.state.engine
        terminal_sessions: dict[str, ActiveSession] = app.state.terminal_sessions
        terminal_lock: asyncio.Lock = app.state.terminal_lock
        session_registry: dict[str, SessionRecord] = app.state.session_registry
        repo_to_session: dict[str, str] = app.state.repo_to_session
        repo_path = str(engine.repo_root)
        state_path = engine.state_path

        client_session_id = ws.query_params.get("session_id")
        close_session_id = ws.query_params.get("close_session_id")
        mode = (ws.query_params.get("mode") or "").strip().lower()
        attach_only = mode == "attach"
        session_id = None
        active_session: Optional[ActiveSession] = None
        seen_update_interval = 5.0

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
                        repo: sid
                        for repo, sid in repo_to_session.items()
                        if sid != client_session_id
                    }
                    app.state.repo_to_session = repo_to_session
                    active_session = None
                    _mark_dirty()
                else:
                    session_id = client_session_id

            if not active_session:
                mapped_session_id = repo_to_session.get(repo_path)
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
                        repo_to_session.pop(repo_path, None)
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
                            repo: sid
                            for repo, sid in repo_to_session.items()
                            if sid != close_session_id
                        }
                        app.state.repo_to_session = repo_to_session
                        _mark_dirty()
                session_id = str(uuid.uuid4())
                resume_mode = mode == "resume"
                cmd = build_codex_terminal_cmd(engine, resume_mode=resume_mode)
                try:
                    pty = PTYSession(cmd, cwd=str(engine.repo_root))
                    active_session = ActiveSession(
                        session_id, pty, asyncio.get_running_loop()
                    )
                    terminal_sessions[session_id] = active_session
                    session_registry[session_id] = SessionRecord(
                        repo_path=repo_path,
                        created_at=now_iso(),
                        last_seen_at=now_iso(),
                        status="active",
                    )
                    repo_to_session[repo_path] = session_id
                    _mark_dirty()
                except FileNotFoundError:
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": f"Codex binary not found: {engine.config.codex_binary}",
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
                    )
                    _mark_dirty()
                if session_id and repo_to_session.get(repo_path) != session_id:
                    repo_to_session[repo_path] = session_id
                    _mark_dirty()
                _maybe_persist_sessions(force=True)

        await ws.send_text(json.dumps({"type": "hello", "session_id": session_id}))
        await ws.send_bytes(ALT_SCREEN_ENTER)
        queue = active_session.add_subscriber()

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
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("bytes") is not None:
                        active_session.pty.write(msg["bytes"])
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
                        if active_session.mark_input_id_seen(input_id):
                            active_session.pty.write(encoded)
                        await ws.send_text(
                            json.dumps({"type": "ack", "id": input_id, "ok": True})
                        )
                        if session_id:
                            _touch_session(session_id)
                    elif payload.get("type") == "ping":
                        await ws.send_text(json.dumps({"type": "pong"}))
                        if session_id:
                            _touch_session(session_id)
            except WebSocketDisconnect:
                pass
            except Exception:
                safe_log(logger, logging.WARNING, "Terminal WS to PTY bridge failed")

        forward_task = asyncio.create_task(pty_to_ws())
        input_task = asyncio.create_task(ws_to_pty())
        done, pending = await asyncio.wait(
            [forward_task, input_task], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            try:
                task.result()
            except Exception:
                safe_log(logger, logging.WARNING, "Terminal websocket task failed")

        if active_session:
            active_session.remove_subscriber(queue)
            if not active_session.pty.isalive():
                async with terminal_lock:
                    terminal_sessions.pop(session_id, None)
                    if session_id:
                        session_registry.pop(session_id, None)
                        repo_to_session = {
                            repo: sid
                            for repo, sid in repo_to_session.items()
                            if sid != session_id
                        }
                        app.state.repo_to_session = repo_to_session
                        _mark_dirty()
            if session_id:
                _touch_session(session_id)
            _maybe_persist_sessions(force=True)

        forward_task.cancel()
        input_task.cancel()
        try:
            await ws.close()
        except Exception:
            safe_log(logger, logging.WARNING, "Terminal websocket close failed")

    return router
