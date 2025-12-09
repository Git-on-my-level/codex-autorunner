import asyncio
import threading
from importlib import resources
from pathlib import Path
from typing import Optional
from asyncio.subprocess import PIPE, STDOUT, create_subprocess_exec

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ConfigError, load_config
from .engine import Engine, doctor
from .prompt import build_chat_prompt
from .state import load_state, save_state, RunnerState, now_iso
from .utils import atomic_write, find_repo_root


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


def _auth_dependency(token: Optional[str]):
    def dependency(request: Request):
        if token is None:
            return
        header = request.headers.get("Authorization")
        expected = f"Bearer {token}"
        if header != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

    return dependency


def _static_dir() -> Path:
    return Path(resources.files("codex_autorunner")) / "static"


def create_app(repo_root: Path) -> FastAPI:
    repo_root = find_repo_root(repo_root)
    engine = Engine(repo_root)
    manager = RunnerManager(engine)

    app = FastAPI()
    static_dir = _static_dir()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    auth_dep = _auth_dependency(engine.config.server_auth_token)

    @app.get("/", include_in_schema=False)
    def index():
        index_path = static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(
                status_code=500, detail="Static UI assets missing; reinstall package"
            )
        return FileResponse(index_path)

    @app.get("/api/docs", dependencies=[Depends(auth_dep)])
    def get_docs():
        return {
            "todo": engine.docs.read_doc("todo"),
            "progress": engine.docs.read_doc("progress"),
            "opinions": engine.docs.read_doc("opinions"),
        }

    @app.put("/api/docs/{kind}", dependencies=[Depends(auth_dep)])
    def put_doc(kind: str, payload: dict):
        key = kind.lower()
        if key not in ("todo", "progress", "opinions"):
            raise HTTPException(status_code=400, detail="invalid doc kind")
        content = payload.get("content", "")
        atomic_write(engine.config.doc_path(key), content)
        return {"kind": key, "content": content}

    @app.get("/api/state", dependencies=[Depends(auth_dep)])
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

    @app.post("/api/run/start", dependencies=[Depends(auth_dep)])
    def start_run(payload: Optional[dict] = None):
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        manager.start(once=once)
        return {"running": manager.running, "once": once}

    @app.post("/api/run/stop", dependencies=[Depends(auth_dep)])
    def stop_run():
        manager.stop()
        return {"running": manager.running}

    @app.post("/api/run/kill", dependencies=[Depends(auth_dep)])
    def kill_run():
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

    @app.post("/api/run/resume", dependencies=[Depends(auth_dep)])
    def resume_run(payload: Optional[dict] = None):
        once = False
        if payload and isinstance(payload, dict):
            once = bool(payload.get("once", False))
        # clear stale lock if needed
        from .engine import clear_stale_lock

        clear_stale_lock(engine.lock_path)
        manager.stop_flag.clear()
        manager.start(once=once)
        return {"running": manager.running, "once": once}

    @app.get("/api/logs", dependencies=[Depends(auth_dep)])
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

    @app.get("/api/logs/stream", dependencies=[Depends(auth_dep)])
    async def stream_logs():
        return StreamingResponse(
            _log_stream(engine.log_path), media_type="text/event-stream"
        )

    @app.post("/api/chat", dependencies=[Depends(auth_dep)])
    async def chat(payload: dict):
        message = payload.get("message")
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        include_todo = bool(payload.get("include_todo", True))
        include_progress = bool(payload.get("include_progress", True))
        include_opinions = bool(payload.get("include_opinions", True))
        prompt = build_chat_prompt(
            engine.docs,
            message,
            include_todo=include_todo,
            include_progress=include_progress,
            include_opinions=include_opinions,
        )
        state = load_state(engine.state_path)
        run_id = (state.last_run_id or 0) + 1
        exit_code, output = engine.run_codex_chat(prompt, run_id)
        if exit_code != 0:
            raise HTTPException(
                status_code=500,
                detail="Codex chat failed",
                headers={"X-Codex-Exit": str(exit_code)},
            )
        return {"run_id": run_id, "response": output}

    @app.post("/api/chat/stream", dependencies=[Depends(auth_dep)])
    async def chat_stream(payload: dict):
        message = payload.get("message")
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        include_todo = bool(payload.get("include_todo", True))
        include_progress = bool(payload.get("include_progress", True))
        include_opinions = bool(payload.get("include_opinions", True))
        prompt = build_chat_prompt(
            engine.docs,
            message,
            include_todo=include_todo,
            include_progress=include_progress,
            include_opinions=include_opinions,
        )
        state = load_state(engine.state_path)
        run_id = (state.last_run_id or 0) + 1

        async def event_stream():
            proc = None
            started = False
            end_logged = False

            def log_end(code: str) -> None:
                nonlocal end_logged
                if end_logged or not started:
                    return
                end_logged = True
                with engine.log_path.open("a", encoding="utf-8") as f:
                    f.write(f"=== run {run_id} chat end (code {code}) ===\n")

            try:
                try:
                    proc = await create_subprocess_exec(
                        engine.config.codex_binary,
                        *engine.config.codex_args,
                        prompt,
                        cwd=str(engine.repo_root),
                        stdout=PIPE,
                        stderr=STDOUT,
                    )
                except FileNotFoundError:
                    yield f"event: error\ndata: Codex binary not found: {engine.config.codex_binary}\n\n"
                    return

                engine.log_path.parent.mkdir(parents=True, exist_ok=True)
                with engine.log_path.open("a", encoding="utf-8") as f:
                    f.write(f"=== run {run_id} chat start ===\n")
                started = True

                if proc.stdout:
                    async for raw in proc.stdout:
                        clean = raw.decode("utf-8", errors="replace").rstrip("\n")
                        engine.log_line(run_id, f"chat: {clean}")
                        yield f"data: {clean}\n\n"

                code = await proc.wait()
                log_end(str(code))
                yield f"event: done\ndata: {code}\n\n"
            finally:
                if proc and proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                if proc and started and not end_logged:
                    log_end(str(proc.returncode if proc.returncode is not None else "terminated"))

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


def doctor_server(repo_root: Path) -> None:
    root = find_repo_root(repo_root)
    doctor(root)
    load_config(root)
