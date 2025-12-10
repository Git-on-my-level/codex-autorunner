import asyncio
import json
import threading
import uuid
from importlib import resources
from pathlib import Path
from typing import Optional
from asyncio.subprocess import PIPE, STDOUT, create_subprocess_exec

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ConfigError, HubConfig, load_config
from .engine import Engine, LockError, doctor
from .logging_utils import setup_rotating_logger
from .doc_chat import (
    DocChatBusyError,
    DocChatError,
    DocChatService,
    DocChatValidationError,
    _normalize_kind,
)
from .hub import HubSupervisor
from .pty_session import PTYSession
from .state import load_state, save_state, RunnerState, now_iso
from .utils import atomic_write, find_repo_root
from .spec_ingest import (
    SpecIngestError,
    generate_docs_from_spec,
    write_ingested_docs,
    clear_work_docs,
)


class RunnerManager:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.thread: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, once: bool = False) -> None:
        with self._lock:
            if self.running:
                return
            self.stop_flag.clear()
            target_runs = 1 if once else None
            self.thread = threading.Thread(
                target=self.engine.run_loop,
                kwargs={
                    "stop_after_runs": target_runs,
                    "external_stop_flag": self.stop_flag,
                },
                daemon=True,
            )
            self.thread.start()

    def stop(self) -> None:
        with self._lock:
            if self.stop_flag:
                self.stop_flag.set()

    def kill(self) -> None:
        with self._lock:
            self.stop_flag.set()
            # Best-effort join to allow loop to exit.
            if self.thread:
                self.thread.join(timeout=1.0)


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


async def _state_stream(engine: Engine, manager: RunnerManager):
    last_payload = None
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
            pass
        await asyncio.sleep(1.0)


def _static_dir() -> Path:
    return Path(resources.files("codex_autorunner")) / "static"


def create_app(repo_root: Optional[Path] = None) -> FastAPI:
    config = load_config(repo_root or Path.cwd())
    if isinstance(config, HubConfig):
        raise ConfigError("create_app requires repo mode configuration")
    engine = Engine(config.root)
    manager = RunnerManager(engine)
    doc_chat = DocChatService(engine)
    terminal_sessions: dict[str, PTYSession] = {}
    terminal_max_idle_seconds = 3600
    terminal_lock = asyncio.Lock()

    app = FastAPI()
    app.state.logger = setup_rotating_logger(
        f"repo[{engine.repo_root}]", engine.config.log
    )
    app.state.logger.info("Repo server ready at %s", engine.repo_root)
    static_dir = _static_dir()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        index_path = static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=500, detail="Static UI assets missing; reinstall package"
            )
        return FileResponse(index_path)

    @app.get("/api/docs")
    def get_docs():
        return {
            "todo": engine.docs.read_doc("todo"),
            "progress": engine.docs.read_doc("progress"),
            "opinions": engine.docs.read_doc("opinions"),
            "spec": engine.docs.read_doc("spec"),
        }

    @app.put("/api/docs/{kind}")
    def put_doc(kind: str, payload: dict):
        key = kind.lower()
        if key not in ("todo", "progress", "opinions", "spec"):
            raise HTTPException(status_code=400, detail="invalid doc kind")
        content = payload.get("content", "")
        atomic_write(engine.config.doc_path(key), content)
        return {"kind": key, "content": content}

    @app.post("/api/docs/{kind}/chat")
    async def chat_doc(kind: str, payload: Optional[dict] = None):
        try:
            request = doc_chat.parse_request(kind, payload)
        except DocChatValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)

        if doc_chat.doc_busy(request.kind):
            raise HTTPException(
                status_code=409,
                detail=f"Doc chat already running for {request.kind}",
            )

        if request.stream:
            return StreamingResponse(
                doc_chat.stream(request), media_type="text/event-stream"
            )

        try:
            async with doc_chat.doc_lock(request.kind):
                result = await doc_chat.execute(request)
        except DocChatBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        if result.get("status") != "ok":
            detail = result.get("detail") or "Doc chat failed"
            raise HTTPException(status_code=500, detail=detail)
        return result

    @app.post("/api/docs/{kind}/chat/apply")
    async def apply_chat_patch(kind: str):
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
            "agent_message": doc_chat.last_agent_message or f"Updated {key.upper()} via doc chat.",
        }

    @app.post("/api/docs/{kind}/chat/discard")
    async def discard_chat_patch(kind: str):
        key = _normalize_kind(kind)
        try:
            async with doc_chat.doc_lock(key):
                content = doc_chat.discard_patch(key)
        except DocChatError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return {"status": "ok", "kind": key, "content": content}

    @app.get("/api/docs/{kind}/chat/pending")
    async def pending_chat_patch(kind: str):
        key = _normalize_kind(kind)
        pending = doc_chat.pending_patch(key)
        if not pending:
            raise HTTPException(status_code=404, detail="No pending patch")
        return pending

    @app.post("/api/ingest-spec")
    def ingest_spec(payload: Optional[dict] = None):
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

    @app.post("/api/docs/clear")
    def clear_docs():
        try:
            docs = clear_work_docs(engine)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return docs

    @app.get("/api/state")
    def get_state():
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

    @app.get("/api/state/stream")
    async def stream_state():
        return StreamingResponse(
            _state_stream(engine, manager), media_type="text/event-stream"
        )

    @app.post("/api/run/start")
    def start_run(payload: Optional[dict] = None):
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        try:
            app.state.logger.info("run/start once=%s", once)
        except Exception:
            pass
        manager.start(once=once)
        return {"running": manager.running, "once": once}

    @app.post("/api/run/stop")
    def stop_run():
        try:
            app.state.logger.info("run/stop requested")
        except Exception:
            pass
        manager.stop()
        return {"running": manager.running}

    @app.post("/api/run/kill")
    def kill_run():
        try:
            app.state.logger.info("run/kill requested")
        except Exception:
            pass
        manager.kill()
        # mark state as idle/error after kill
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

    @app.post("/api/run/resume")
    def resume_run(payload: Optional[dict] = None):
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        # clear stale lock if needed
        try:
            app.state.logger.info("run/resume once=%s", once)
        except Exception:
            pass
        from .engine import clear_stale_lock

        clear_stale_lock(engine.lock_path)
        manager.stop_flag.clear()
        manager.start(once=once)
        return {"running": manager.running, "once": once}

    @app.post("/api/run/reset")
    def reset_runner():
        """Reset runner metadata: clear state, logs, and locks."""
        if manager.running:
            raise HTTPException(
                status_code=409, detail="Cannot reset while runner is active"
            )
        try:
            app.state.logger.info("run/reset requested")
        except Exception:
            pass
        # Clear stale lock
        engine.lock_path.unlink(missing_ok=True)
        # Reset state to initial values
        initial_state = RunnerState(
            last_run_id=None,
            status="idle",
            last_exit_code=None,
            last_run_started_at=None,
            last_run_finished_at=None,
            runner_pid=None,
        )
        save_state(engine.state_path, initial_state)
        # Clear logs
        if engine.log_path.exists():
            engine.log_path.unlink()
        return {"status": "ok", "message": "Runner reset complete"}

    @app.get("/api/logs")
    def get_logs(run_id: Optional[int] = None, tail: Optional[int] = None):
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

    @app.get("/api/logs/stream")
    async def stream_logs():
        return StreamingResponse(
            _log_stream(engine.log_path), media_type="text/event-stream"
        )

    @app.websocket("/api/terminal")
    async def terminal(ws: WebSocket):
        await ws.accept()
        session_id = str(uuid.uuid4())
        resume_mode = ws.query_params.get("mode") == "resume"
        if resume_mode:
            cmd = [
                engine.config.codex_binary,
                "--yolo",
                "resume",
                *engine.config.codex_terminal_args,
            ]
        else:
            cmd = [engine.config.codex_binary, *engine.config.codex_terminal_args]
        try:
            session = PTYSession(cmd, cwd=str(engine.repo_root))
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

        async with terminal_lock:
            terminal_sessions[session_id] = session

        async def pty_to_ws():
            # Stream PTY output as binary frames.
            try:
                while session.isalive():
                    if session.is_stale(terminal_max_idle_seconds):
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "exit",
                                    "code": None,
                                    "reason": "idle_timeout",
                                    "session_id": session_id,
                                }
                            )
                        )
                        break
                    data = session.read()
                    if data:
                        await ws.send_bytes(data)
                    else:
                        await asyncio.sleep(0.02)
                exit_code = session.exit_code()
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "exit",
                            "code": exit_code,
                            "session_id": session_id,
                        }
                    )
                )
            except Exception:
                pass

        async def ws_to_pty():
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("bytes") is not None:
                        session.write(msg["bytes"])
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
                            session.resize(cols, rows)
                    elif payload.get("type") == "ping":
                        await ws.send_text(json.dumps({"type": "pong"}))
            except WebSocketDisconnect:
                pass
            except Exception:
                pass

        forward_task = asyncio.create_task(pty_to_ws())
        input_task = asyncio.create_task(ws_to_pty())
        await asyncio.wait([forward_task, input_task], return_when=asyncio.FIRST_COMPLETED)
        session.terminate()
        async with terminal_lock:
            terminal_sessions.pop(session_id, None)
        try:
            await ws.close()
        except Exception:
            pass

    @app.on_event("shutdown")
    async def shutdown_terminal_sessions():
        async with terminal_lock:
            for session in terminal_sessions.values():
                session.terminate()
            terminal_sessions.clear()

    return app


def create_hub_app(hub_root: Optional[Path] = None) -> FastAPI:
    config = load_config(hub_root or Path.cwd())
    if not isinstance(config, HubConfig):
        raise ConfigError("Hub app requires hub mode configuration")
    supervisor = HubSupervisor(config)
    app = FastAPI()
    app.state.logger = setup_rotating_logger(f"hub[{config.root}]", config.log)
    try:
        app.state.logger.info("Hub app ready at %s", config.root)
    except Exception:
        pass
    static_dir = _static_dir()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    mounted_repos: set[str] = set()
    mount_errors: dict[str, str] = {}

    def _mount_repo(prefix: str, repo_path: Path) -> bool:
        if prefix in mounted_repos:
            return True
        if prefix in mount_errors:
            return False
        try:
            sub_app = create_app(repo_path)
        except ConfigError as exc:
            mount_errors[prefix] = str(exc)
            try:
                app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception:
                pass
            return False
        except Exception as exc:
            mount_errors[prefix] = str(exc)
            try:
                app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception:
                pass
            return False
        app.mount(f"/repos/{prefix}", sub_app)
        mounted_repos.add(prefix)
        mount_errors.pop(prefix, None)
        return True

    def _refresh_mounts(snapshots):
        for snap in snapshots:
            if snap.initialized and snap.exists_on_disk:
                _mount_repo(snap.id, snap.path)

    def _add_mount_info(repo_dict: dict) -> dict:
        """Add mount_status to repo dict for UI to know if navigation is possible."""
        repo_id = repo_dict.get("id")
        if repo_id in mounted_repos:
            repo_dict["mounted"] = True
        elif repo_id in mount_errors:
            repo_dict["mounted"] = False
            repo_dict["mount_error"] = mount_errors[repo_id]
        else:
            repo_dict["mounted"] = False
        return repo_dict

    initial_snapshots = supervisor.scan()
    _refresh_mounts(initial_snapshots)

    @app.get("/hub/repos")
    def list_repos():
        try:
            app.state.logger.info("Hub list_repos")
        except Exception:
            pass
        snapshots = supervisor.list_repos()
        _refresh_mounts(snapshots)
        return {
            "last_scan_at": supervisor.state.last_scan_at,
            "repos": [_add_mount_info(repo.to_dict(config.root)) for repo in snapshots],
        }

    @app.post("/hub/repos/scan")
    def scan_repos():
        try:
            app.state.logger.info("Hub scan_repos")
        except Exception:
            pass
        snapshots = supervisor.scan()
        _refresh_mounts(snapshots)
        return {
            "last_scan_at": supervisor.state.last_scan_at,
            "repos": [_add_mount_info(repo.to_dict(config.root)) for repo in snapshots],
        }

    @app.post("/hub/repos")
    def create_repo(payload: Optional[dict] = None):
        if not payload or not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")
        repo_id = payload.get("id") or payload.get("repo_id")
        if not repo_id:
            raise HTTPException(status_code=400, detail="Missing repo id")
        repo_path_val = payload.get("path")
        repo_path = Path(repo_path_val) if repo_path_val else None
        git_init = bool(payload.get("git_init", True))
        force = bool(payload.get("force", False))
        try:
            app.state.logger.info(
                "Hub create repo id=%s path=%s git_init=%s force=%s",
                repo_id,
                repo_path_val,
                git_init,
                force,
            )
        except Exception:
            pass
        try:
            snapshot = supervisor.create_repo(
                str(repo_id), repo_path=repo_path, git_init=git_init, force=force
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/run")
    def run_repo(repo_id: str, payload: Optional[dict] = None):
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        try:
            app.state.logger.info("Hub run %s once=%s", repo_id, once)
        except Exception:
            pass
        try:
            snapshot = supervisor.run_repo(repo_id, once=once)
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/stop")
    def stop_repo(repo_id: str):
        try:
            app.state.logger.info("Hub stop %s", repo_id)
        except Exception:
            pass
        try:
            snapshot = supervisor.stop_repo(repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/resume")
    def resume_repo(repo_id: str, payload: Optional[dict] = None):
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        try:
            app.state.logger.info("Hub resume %s once=%s", repo_id, once)
        except Exception:
            pass
        try:
            snapshot = supervisor.resume_repo(repo_id, once=once)
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/kill")
    def kill_repo(repo_id: str):
        try:
            app.state.logger.info("Hub kill %s", repo_id)
        except Exception:
            pass
        try:
            snapshot = supervisor.kill_repo(repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.post("/hub/repos/{repo_id}/init")
    def init_repo(repo_id: str):
        try:
            app.state.logger.info("Hub init %s", repo_id)
        except Exception:
            pass
        try:
            snapshot = supervisor.init_repo(repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _refresh_mounts([snapshot])
        return _add_mount_info(snapshot.to_dict(config.root))

    @app.get("/", include_in_schema=False)
    def hub_index():
        index_path = static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=500, detail="Static UI assets missing; reinstall package"
            )
        return FileResponse(index_path)

    return app


def doctor_server(repo_root: Path) -> None:
    root = find_repo_root(repo_root)
    doctor(root)
    load_config(root)
