"""
Base routes: Index, state streaming, WebSocket terminal, and logs.
"""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ..pty_session import ActiveSession, PTYSession
from ..state import load_state
from .shared import build_codex_terminal_cmd, log_stream, state_stream


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
        return FileResponse(index_path)

    @router.get("/api/state")
    def get_state(request: Request):
        engine = request.app.state.engine
        manager = request.app.state.manager
        state = load_state(engine.state_path)
        outstanding, done = engine.docs.todos()
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
        }

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
        engine = app.state.engine
        terminal_sessions: dict[str, ActiveSession] = app.state.terminal_sessions
        terminal_lock: asyncio.Lock = app.state.terminal_lock

        client_session_id = ws.query_params.get("session_id")
        close_session_id = ws.query_params.get("close_session_id")
        mode = (ws.query_params.get("mode") or "").strip().lower()
        attach_only = mode == "attach"
        session_id = None
        active_session: Optional[ActiveSession] = None

        async with terminal_lock:
            if client_session_id and client_session_id in terminal_sessions:
                active_session = terminal_sessions[client_session_id]
                if not active_session.pty.isalive():
                    active_session.close()
                    terminal_sessions.pop(client_session_id, None)
                    active_session = None
                else:
                    session_id = client_session_id

            if not active_session:
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
                session_id = str(uuid.uuid4())
                resume_mode = mode == "resume"
                cmd = build_codex_terminal_cmd(engine, resume_mode=resume_mode)
                try:
                    pty = PTYSession(cmd, cwd=str(engine.repo_root))
                    active_session = ActiveSession(
                        session_id, pty, asyncio.get_running_loop()
                    )
                    terminal_sessions[session_id] = active_session
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

        await ws.send_text(json.dumps({"type": "hello", "session_id": session_id}))
        queue = active_session.add_subscriber()

        async def pty_to_ws():
            try:
                while True:
                    data = await queue.get()
                    if data is None:
                        if active_session:
                            exit_code = active_session.pty.exit_code()
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
            except Exception:
                pass

        async def ws_to_pty():
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("bytes") is not None:
                        active_session.pty.write(msg["bytes"])
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
                    elif payload.get("type") == "ping":
                        await ws.send_text(json.dumps({"type": "pong"}))
            except WebSocketDisconnect:
                pass
            except Exception:
                pass

        forward_task = asyncio.create_task(pty_to_ws())
        input_task = asyncio.create_task(ws_to_pty())
        done, pending = await asyncio.wait(
            [forward_task, input_task], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            try:
                task.result()
            except Exception:
                pass

        if active_session:
            active_session.remove_subscriber(queue)
            if not active_session.pty.isalive():
                async with terminal_lock:
                    terminal_sessions.pop(session_id, None)

        forward_task.cancel()
        input_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass

    return router
