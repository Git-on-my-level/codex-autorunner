"""
Repository run control routes: start, stop, resume, reset, kill.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..core.engine import LockError, clear_stale_lock
from ..core.state import RunnerState, load_state, now_iso, save_state, state_lock
from ..web.schemas import (
    RunControlRequest,
    RunControlResponse,
    RunResetResponse,
    RunStatusResponse,
)


def build_repos_routes() -> APIRouter:
    """Build routes for run control."""
    router = APIRouter()

    @router.post("/api/run/start", response_model=RunControlResponse)
    def start_run(request: Request, payload: Optional[RunControlRequest] = None):
        manager = request.app.state.manager
        logger = request.app.state.logger
        once = payload.once if payload else False
        try:
            logger.info("run/start once=%s", once)
        except Exception:
            pass
        try:
            manager.start(once=once)
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"running": manager.running, "once": once}

    @router.post("/api/run/stop", response_model=RunStatusResponse)
    def stop_run(request: Request):
        manager = request.app.state.manager
        logger = request.app.state.logger
        try:
            logger.info("run/stop requested")
        except Exception:
            pass
        manager.stop()
        return {"running": manager.running}

    @router.post("/api/run/kill", response_model=RunStatusResponse)
    def kill_run(request: Request):
        engine = request.app.state.engine
        manager = request.app.state.manager
        logger = request.app.state.logger
        try:
            logger.info("run/kill requested")
        except Exception:
            pass
        manager.kill()
        with state_lock(engine.state_path):
            state = load_state(engine.state_path)
            new_state = RunnerState(
                last_run_id=state.last_run_id,
                status="error",
                last_exit_code=137,
                last_run_started_at=state.last_run_started_at,
                last_run_finished_at=now_iso(),
                autorunner_model_override=state.autorunner_model_override,
                autorunner_effort_override=state.autorunner_effort_override,
                autorunner_approval_policy=state.autorunner_approval_policy,
                autorunner_sandbox_mode=state.autorunner_sandbox_mode,
                autorunner_workspace_write_network=state.autorunner_workspace_write_network,
                runner_pid=None,
                sessions=state.sessions,
                repo_to_session=state.repo_to_session,
            )
            save_state(engine.state_path, new_state)
        clear_stale_lock(engine.lock_path)
        return {"running": manager.running}

    @router.post("/api/run/resume", response_model=RunControlResponse)
    def resume_run(request: Request, payload: Optional[RunControlRequest] = None):
        manager = request.app.state.manager
        logger = request.app.state.logger
        once = payload.once if payload else False
        try:
            logger.info("run/resume once=%s", once)
        except Exception:
            pass
        try:
            manager.resume(once=once)
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"running": manager.running, "once": once}

    @router.post("/api/run/reset", response_model=RunResetResponse)
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
        with state_lock(engine.state_path):
            current_state = load_state(engine.state_path)
            engine.lock_path.unlink(missing_ok=True)
            initial_state = RunnerState(
                last_run_id=None,
                status="idle",
                last_exit_code=None,
                last_run_started_at=None,
                last_run_finished_at=None,
                autorunner_model_override=current_state.autorunner_model_override,
                autorunner_effort_override=current_state.autorunner_effort_override,
                autorunner_approval_policy=current_state.autorunner_approval_policy,
                autorunner_sandbox_mode=current_state.autorunner_sandbox_mode,
                autorunner_workspace_write_network=current_state.autorunner_workspace_write_network,
                runner_pid=None,
                sessions=current_state.sessions,
                repo_to_session=current_state.repo_to_session,
            )
            save_state(engine.state_path, initial_state)
        if engine.log_path.exists():
            engine.log_path.unlink()
        return {"status": "ok", "message": "Runner reset complete"}

    return router
