"""
Session settings routes for autorunner overrides.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ....core.state import RunnerState, load_state, save_state, state_lock
from ..schemas import SessionSettingsRequest, SessionSettingsResponse

ALLOWED_APPROVAL_POLICIES = {"never", "unlessTrusted"}
ALLOWED_SANDBOX_MODES = {"dangerFullAccess", "workspaceWrite"}


def _normalize_optional_string(value: object, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field} must be a string")
    cleaned = value.strip()
    return cleaned or None


def build_settings_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/session/settings", response_model=SessionSettingsResponse)
    def get_session_settings(request: Request):
        state = load_state(request.app.state.engine.state_path)
        return {
            "autorunner_model_override": state.autorunner_model_override,
            "autorunner_effort_override": state.autorunner_effort_override,
            "autorunner_approval_policy": state.autorunner_approval_policy,
            "autorunner_sandbox_mode": state.autorunner_sandbox_mode,
            "autorunner_workspace_write_network": state.autorunner_workspace_write_network,
            "runner_stop_after_runs": state.runner_stop_after_runs,
        }

    @router.post("/api/session/settings", response_model=SessionSettingsResponse)
    def update_session_settings(request: Request, payload: SessionSettingsRequest):
        updates = payload.model_dump(exclude_unset=True)
        engine = request.app.state.engine
        manager = request.app.state.manager
        registry = request.app.state.app_server_threads
        with state_lock(engine.state_path):
            state = load_state(engine.state_path)
            model_override = (
                _normalize_optional_string(
                    updates.get("autorunner_model_override"),
                    "autorunner_model_override",
                )
                if "autorunner_model_override" in updates
                else state.autorunner_model_override
            )
            effort_override = (
                _normalize_optional_string(
                    updates.get("autorunner_effort_override"),
                    "autorunner_effort_override",
                )
                if "autorunner_effort_override" in updates
                else state.autorunner_effort_override
            )
            approval_policy = (
                _normalize_optional_string(
                    updates.get("autorunner_approval_policy"),
                    "autorunner_approval_policy",
                )
                if "autorunner_approval_policy" in updates
                else state.autorunner_approval_policy
            )
            if approval_policy and approval_policy not in ALLOWED_APPROVAL_POLICIES:
                raise HTTPException(
                    status_code=400,
                    detail="approval policy must be never or unlessTrusted",
                )
            sandbox_mode = (
                _normalize_optional_string(
                    updates.get("autorunner_sandbox_mode"),
                    "autorunner_sandbox_mode",
                )
                if "autorunner_sandbox_mode" in updates
                else state.autorunner_sandbox_mode
            )
            if sandbox_mode and sandbox_mode not in ALLOWED_SANDBOX_MODES:
                raise HTTPException(
                    status_code=400,
                    detail="sandbox mode must be dangerFullAccess or workspaceWrite",
                )
            workspace_write_network = (
                updates.get("autorunner_workspace_write_network")
                if "autorunner_workspace_write_network" in updates
                else state.autorunner_workspace_write_network
            )
            if (
                "autorunner_workspace_write_network" in updates
                and workspace_write_network is not None
                and not isinstance(workspace_write_network, bool)
            ):
                raise HTTPException(
                    status_code=400,
                    detail="autorunner_workspace_write_network must be a boolean",
                )
            runner_stop_after_runs = (
                updates.get("runner_stop_after_runs")
                if "runner_stop_after_runs" in updates
                else state.runner_stop_after_runs
            )
            if (
                "runner_stop_after_runs" in updates
                and runner_stop_after_runs is not None
                and (
                    not isinstance(runner_stop_after_runs, int)
                    or isinstance(runner_stop_after_runs, bool)
                    or runner_stop_after_runs <= 0
                )
            ):
                raise HTTPException(
                    status_code=400,
                    detail="runner_stop_after_runs must be a positive integer",
                )

            thread_reset_required = any(
                (
                    model_override != state.autorunner_model_override,
                    effort_override != state.autorunner_effort_override,
                    approval_policy != state.autorunner_approval_policy,
                    sandbox_mode != state.autorunner_sandbox_mode,
                    workspace_write_network != state.autorunner_workspace_write_network,
                    runner_stop_after_runs != state.runner_stop_after_runs,
                )
            )
            if thread_reset_required and manager.running:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot change autorunner settings while a run is active",
                )

            new_state = RunnerState(
                last_run_id=state.last_run_id,
                status=state.status,
                last_exit_code=state.last_exit_code,
                last_run_started_at=state.last_run_started_at,
                last_run_finished_at=state.last_run_finished_at,
                autorunner_agent_override=state.autorunner_agent_override,
                autorunner_model_override=model_override,
                autorunner_effort_override=effort_override,
                autorunner_approval_policy=approval_policy,
                autorunner_sandbox_mode=sandbox_mode,
                autorunner_workspace_write_network=workspace_write_network,
                runner_stop_after_runs=runner_stop_after_runs,
                runner_pid=state.runner_pid,
                sessions=state.sessions,
                repo_to_session=state.repo_to_session,
            )
            save_state(engine.state_path, new_state)
            if thread_reset_required:
                registry.reset_thread("autorunner")

        return {
            "autorunner_model_override": model_override,
            "autorunner_effort_override": effort_override,
            "autorunner_approval_policy": approval_policy,
            "autorunner_sandbox_mode": sandbox_mode,
            "autorunner_workspace_write_network": workspace_write_network,
            "runner_stop_after_runs": runner_stop_after_runs,
        }

    return router
