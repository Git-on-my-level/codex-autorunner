import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Optional

import subprocess
from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .doc_chat import (
    DocChatBusyError,
    DocChatError,
    DocChatValidationError,
    _normalize_kind,
)
from .about_car import ABOUT_CAR_REL_PATH, ensure_about_car_file
from .pty_session import ActiveSession, PTYSession
from .spec_ingest import (
    SpecIngestError,
    clear_work_docs,
    generate_docs_from_spec,
    write_ingested_docs,
)
from .state import RunnerState, load_state, now_iso, save_state
from .usage import (
    UsageError,
    default_codex_home,
    parse_iso_datetime,
    summarize_repo_usage,
)
from .utils import atomic_write
from .snapshot import (
    SnapshotError,
    generate_snapshot,
    load_snapshot,
    load_snapshot_state,
)
from .voice import VoiceService, VoiceServiceError
from .engine import LockError
from .github import GitHubError, GitHubService


def _codex_supports_input_flag(binary: str) -> bool:
    """
    Best-effort capability check for `codex --input <file>` style context ingestion.

    Some Codex CLI versions support `--input` (often on `codex exec`); others don't.
    We probe `--help` so we can prefer `--input` when available without breaking older installs.
    """
    try:
        # Keep this fast and non-fatal; avoid hanging the server.
        result = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        return "--input" in text
    except Exception:
        return False


def build_codex_terminal_cmd(engine, *, resume_mode: bool) -> list[str]:
    """
    Build the subprocess argv for launching the Codex interactive CLI inside a PTY.

    For "new" sessions we seed a small CAR briefing as the initial prompt so Codex
    knows where canonical work docs live (even if `.codex-autorunner/` is gitignored).
    """
    if resume_mode:
        return [
            engine.config.codex_binary,
            "--yolo",
            "resume",
            *engine.config.codex_terminal_args,
        ]

    cmd = [
        engine.config.codex_binary,
        "--yolo",
        *engine.config.codex_terminal_args,
    ]
    try:
        about_path = engine.repo_root / ABOUT_CAR_REL_PATH
        if not about_path.exists():
            ensure_about_car_file(engine.config)
        if _codex_supports_input_flag(engine.config.codex_binary):
            cmd.extend(["--input", str(about_path)])
        else:
            # Back-compat fallback: older Codex CLIs don't support --input.
            # In that case we still seed the session by passing the ABOUT text
            # as the initial prompt argument.
            about_text = about_path.read_text(encoding="utf-8")
            if about_text.strip():
                cmd.append(about_text)
    except Exception:
        # Best-effort: never block terminal launch due to context helper.
        pass
    return cmd


async def _log_stream(log_path: Path):
    if not log_path.exists():
        yield "data: log file not found\n\n"
        return
    with log_path.open("r", encoding="utf-8") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                yield f"data: {line.rstrip()}\n\n"
            else:
                await asyncio.sleep(0.5)


async def _state_stream(engine, manager, logger=None):
    last_payload = None
    last_error_log_at = 0.0
    while True:
        try:
            state = await asyncio.to_thread(load_state, engine.state_path)
            outstanding, done = await asyncio.to_thread(engine.docs.todos)
            payload = {
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
            if payload != last_payload:
                yield f"data: {json.dumps(payload)}\n\n"
                last_payload = payload
        except Exception:
            # Don't spam logs, but don't swallow silently either.
            now = time.time()
            if logger is not None and (now - last_error_log_at) > 60:
                last_error_log_at = now
                try:
                    logger.warning("state stream error", exc_info=True)
                except Exception:
                    pass
        await asyncio.sleep(1.0)


def build_repo_router(static_dir: Path) -> APIRouter:
    router = APIRouter()

    def _github(request: Request) -> GitHubService:
        engine = request.app.state.engine
        return GitHubService(engine.repo_root, raw_config=engine.config.raw)

    @router.get("/", include_in_schema=False)
    def index(request: Request):
        index_path = static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=500, detail="Static UI assets missing; reinstall package"
            )
        return FileResponse(index_path)

    @router.get("/api/docs")
    def get_docs(request: Request):
        engine = request.app.state.engine
        return {
            "todo": engine.docs.read_doc("todo"),
            "progress": engine.docs.read_doc("progress"),
            "opinions": engine.docs.read_doc("opinions"),
            "spec": engine.docs.read_doc("spec"),
            "summary": engine.docs.read_doc("summary"),
        }

    @router.put("/api/docs/{kind}")
    def put_doc(kind: str, payload: dict, request: Request):
        engine = request.app.state.engine
        key = kind.lower()
        if key not in ("todo", "progress", "opinions", "spec", "summary"):
            raise HTTPException(status_code=400, detail="invalid doc kind")
        content = payload.get("content", "")
        atomic_write(engine.config.doc_path(key), content)
        return {"kind": key, "content": content}

    @router.get("/api/snapshot")
    def get_snapshot(request: Request):
        engine = request.app.state.engine
        content = load_snapshot(engine)
        state = load_snapshot_state(engine)
        return {"exists": bool(content), "content": content or "", "state": state or {}}

    @router.post("/api/snapshot")
    async def post_snapshot(request: Request, payload: Optional[dict] = None):
        # Snapshot generation has a single default behavior now; we accept an
        # optional JSON object for backwards compatibility, but ignore any fields.
        if payload is not None and not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )

        engine = request.app.state.engine
        try:
            result = await asyncio.to_thread(
                generate_snapshot,
                engine,
            )
        except SnapshotError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        return {
            "content": result.content,
            "truncated": result.truncated,
            "state": result.state,
        }

    @router.post("/api/docs/{kind}/chat")
    async def chat_doc(kind: str, request: Request, payload: Optional[dict] = None):
        doc_chat = request.app.state.doc_chat
        try:
            doc_req = doc_chat.parse_request(kind, payload)
        except DocChatValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)

        if doc_chat.doc_busy(doc_req.kind):
            raise HTTPException(
                status_code=409,
                detail=f"Doc chat already running for {doc_req.kind}",
            )

        if doc_req.stream:
            return StreamingResponse(
                doc_chat.stream(doc_req), media_type="text/event-stream"
            )

        try:
            async with doc_chat.doc_lock(doc_req.kind):
                result = await doc_chat.execute(doc_req)
        except DocChatBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        if result.get("status") != "ok":
            detail = result.get("detail") or "Doc chat failed"
            raise HTTPException(status_code=500, detail=detail)
        return result

    @router.post("/api/docs/{kind}/chat/apply")
    async def apply_chat_patch(kind: str, request: Request):
        doc_chat = request.app.state.doc_chat
        key = _normalize_kind(kind)
        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)

        try:
            async with doc_chat.doc_lock(key):
                content = doc_chat.apply_saved_patch(key)
        except DocChatBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except DocChatError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return {
            "status": "ok",
            "kind": key,
            "content": content,
            "agent_message": doc_chat.last_agent_message
            or f"Updated {key.upper()} via doc chat.",
        }

    @router.post("/api/docs/{kind}/chat/discard")
    async def discard_chat_patch(kind: str, request: Request):
        doc_chat = request.app.state.doc_chat
        key = _normalize_kind(kind)
        try:
            async with doc_chat.doc_lock(key):
                content = doc_chat.discard_patch(key)
        except DocChatError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return {"status": "ok", "kind": key, "content": content}

    @router.get("/api/docs/{kind}/chat/pending")
    async def pending_chat_patch(kind: str, request: Request):
        doc_chat = request.app.state.doc_chat
        key = _normalize_kind(kind)
        pending = doc_chat.pending_patch(key)
        if not pending:
            raise HTTPException(status_code=404, detail="No pending patch")
        return pending

    @router.get("/api/voice/config")
    def get_voice_config(request: Request):
        voice_service: Optional[VoiceService] = request.app.state.voice_service
        voice_config = request.app.state.voice_config
        if voice_service is None:
            # Degrade gracefully: still return config to the UI even if service init failed.
            try:
                return VoiceService(
                    voice_config, logger=request.app.state.logger
                ).config_payload()
            except Exception:
                return {
                    "enabled": False,
                    "provider": voice_config.provider,
                    "latency_mode": voice_config.latency_mode,
                    "chunk_ms": voice_config.chunk_ms,
                    "sample_rate": voice_config.sample_rate,
                    "warn_on_remote_api": voice_config.warn_on_remote_api,
                    "has_api_key": False,
                    "api_key_env": (
                        voice_config.providers.get(
                            voice_config.provider or "openai_whisper", {}
                        )
                        or {}
                    ).get("api_key_env", "OPENAI_API_KEY"),
                    "push_to_talk": {
                        "max_ms": voice_config.push_to_talk.max_ms,
                        "silence_auto_stop_ms": voice_config.push_to_talk.silence_auto_stop_ms,
                        "min_hold_ms": voice_config.push_to_talk.min_hold_ms,
                    },
                }
        return voice_service.config_payload()

    @router.post("/api/voice/transcribe")
    async def transcribe_voice(
        request: Request,
        file: Optional[UploadFile] = File(None),
        language: Optional[str] = None,
    ):
        voice_service: Optional[VoiceService] = request.app.state.voice_service
        voice_config = request.app.state.voice_config
        if not voice_service or not voice_config.enabled:
            raise HTTPException(status_code=400, detail="Voice is disabled")

        filename: Optional[str] = None
        content_type: Optional[str] = None
        if file is not None:
            filename = file.filename
            content_type = file.content_type
            try:
                audio_bytes = await file.read()
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail="Unable to read audio upload"
                ) from exc
        else:
            audio_bytes = await request.body()
        try:
            result = await asyncio.to_thread(
                voice_service.transcribe,
                audio_bytes,
                client="web",
                user_agent=request.headers.get("user-agent"),
                language=language,
                filename=filename,
                content_type=content_type,
            )
        except VoiceServiceError as exc:
            if exc.reason == "unauthorized":
                status = 401
            elif exc.reason == "forbidden":
                status = 403
            elif exc.reason == "audio_too_large":
                status = 413
            elif exc.reason == "rate_limited":
                status = 429
            else:
                status = (
                    400
                    if exc.reason in ("disabled", "empty_audio", "invalid_audio")
                    else 502
                )
            raise HTTPException(status_code=status, detail=exc.detail)
        return {"status": "ok", **result}

    @router.post("/api/ingest-spec")
    def ingest_spec(request: Request, payload: Optional[dict] = None):
        engine = request.app.state.engine
        force = False
        spec_override: Optional[Path] = None
        if payload and isinstance(payload, dict):
            force = bool(payload.get("force", False))
            override = payload.get("spec_path")
            if override:
                spec_override = Path(str(override))
        try:
            docs = generate_docs_from_spec(engine, spec_path=spec_override)
            write_ingested_docs(engine, docs, force=force)
        except SpecIngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return docs

    @router.post("/api/docs/clear")
    def clear_docs(request: Request):
        engine = request.app.state.engine
        try:
            docs = clear_work_docs(engine)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return docs

    @router.get("/api/usage")
    def get_usage(
        request: Request, since: Optional[str] = None, until: Optional[str] = None
    ):
        engine = request.app.state.engine
        try:
            since_dt = parse_iso_datetime(since)
            until_dt = parse_iso_datetime(until)
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        summary = summarize_repo_usage(
            engine.repo_root,
            default_codex_home(),
            since=since_dt,
            until=until_dt,
        )
        return {
            "mode": "repo",
            "repo": str(engine.repo_root),
            "codex_home": str(default_codex_home()),
            "since": since,
            "until": until,
            **summary.to_dict(),
        }

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

    # ──────────────────────────────────────────────────────────────────────────
    # GitHub integration
    # ──────────────────────────────────────────────────────────────────────────

    @router.get("/api/github/status")
    async def github_status(request: Request):
        try:
            return await asyncio.to_thread(_github(request).status_payload)
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/api/github/pr")
    async def github_pr(request: Request):
        svc = _github(request)
        try:
            status = await asyncio.to_thread(svc.status_payload)
            return {
                "status": "ok",
                "git": status.get("git"),
                "pr": status.get("pr"),
                "links": status.get("pr_links"),
                "link": status.get("link") or {},
            }
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/api/github/link-issue")
    async def github_link_issue(request: Request, payload: Optional[dict] = None):
        if not payload or not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
        issue = payload.get("issue")
        if not issue:
            raise HTTPException(status_code=400, detail="Missing issue")
        try:
            state = await asyncio.to_thread(_github(request).link_issue, str(issue))
            return {"status": "ok", "link": state}
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/api/github/spec/from-issue")
    async def github_spec_from_issue(request: Request, payload: Optional[dict] = None):
        if not payload or not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
        issue = payload.get("issue")
        if not issue:
            raise HTTPException(status_code=400, detail="Missing issue")

        doc_chat = request.app.state.doc_chat
        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)
        if doc_chat.doc_busy("spec"):
            raise HTTPException(
                status_code=409, detail="Doc chat already running for spec"
            )

        svc = _github(request)
        try:
            prompt, link_state = await asyncio.to_thread(
                svc.build_spec_prompt_from_issue, str(issue)
            )
            doc_req = doc_chat.parse_request(
                "spec", {"message": prompt, "stream": False}
            )
            async with doc_chat.doc_lock("spec"):
                result = await doc_chat.execute(doc_req)
            if result.get("status") != "ok":
                detail = result.get("detail") or "SPEC generation failed"
                raise HTTPException(status_code=500, detail=detail)
            result["github"] = {"issue": link_state.get("issue")}
            return result
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/api/github/pr/sync")
    async def github_pr_sync(request: Request, payload: Optional[dict] = None):
        payload = payload or {}
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
        if payload.get("mode") is not None:
            raise HTTPException(
                status_code=400,
                detail="Repo mode does not support worktrees; create a hub worktree repo instead.",
            )
        draft = bool(payload.get("draft", True))
        title = payload.get("title")
        body = payload.get("body")
        try:
            return await asyncio.to_thread(
                _github(request).sync_pr,
                draft=draft,
                title=str(title) if title else None,
                body=str(body) if body else None,
            )
        except GitHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/api/state/stream")
    async def stream_state(request: Request):
        engine = request.app.state.engine
        manager = request.app.state.manager
        return StreamingResponse(
            _state_stream(engine, manager, logger=request.app.state.logger),
            media_type="text/event-stream",
        )

    @router.post("/api/run/start")
    def start_run(request: Request, payload: Optional[dict] = None):
        manager = request.app.state.manager
        logger = request.app.state.logger
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        try:
            logger.info("run/start once=%s", once)
        except Exception:
            pass
        try:
            manager.start(once=once)
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"running": manager.running, "once": once}

    @router.post("/api/run/stop")
    def stop_run(request: Request):
        manager = request.app.state.manager
        logger = request.app.state.logger
        try:
            logger.info("run/stop requested")
        except Exception:
            pass
        manager.stop()
        return {"running": manager.running}

    @router.post("/api/run/kill")
    def kill_run(request: Request):
        engine = request.app.state.engine
        manager = request.app.state.manager
        logger = request.app.state.logger
        try:
            logger.info("run/kill requested")
        except Exception:
            pass
        manager.kill()
        state = load_state(engine.state_path)
        new_state = RunnerState(
            last_run_id=state.last_run_id,
            status="error",
            last_exit_code=137,
            last_run_started_at=state.last_run_started_at,
            last_run_finished_at=now_iso(),
            runner_pid=None,
        )
        save_state(engine.state_path, new_state)
        engine.release_lock()
        return {"running": manager.running}

    @router.post("/api/run/resume")
    def resume_run(request: Request, payload: Optional[dict] = None):
        engine = request.app.state.engine
        manager = request.app.state.manager
        logger = request.app.state.logger
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        try:
            logger.info("run/resume once=%s", once)
        except Exception:
            pass
        from .engine import clear_stale_lock

        clear_stale_lock(engine.lock_path)
        manager.stop_flag.clear()
        manager.start(once=once)
        return {"running": manager.running, "once": once}

    @router.post("/api/run/reset")
    def reset_runner(request: Request):
        engine = request.app.state.engine
        manager = request.app.state.manager
        logger = request.app.state.logger
        if manager.running:
            raise HTTPException(
                status_code=409, detail="Cannot reset while runner is active"
            )
        try:
            logger.info("run/reset requested")
        except Exception:
            pass
        engine.lock_path.unlink(missing_ok=True)
        initial_state = RunnerState(
            last_run_id=None,
            status="idle",
            last_exit_code=None,
            last_run_started_at=None,
            last_run_finished_at=None,
            runner_pid=None,
        )
        save_state(engine.state_path, initial_state)
        if engine.log_path.exists():
            engine.log_path.unlink()
        return {"status": "ok", "message": "Runner reset complete"}

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
    async def stream_logs(request: Request):
        engine = request.app.state.engine
        return StreamingResponse(
            _log_stream(engine.log_path), media_type="text/event-stream"
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
