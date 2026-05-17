"""
Session settings routes for autorunner overrides.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ....core.state import RunnerState, load_state, save_state, state_lock
from ..schemas import SessionSettingsRequest, SessionSettingsResponse
from ..services.validation import normalize_optional_string

ALLOWED_APPROVAL_POLICIES = {"never", "unlessTrusted"}
ALLOWED_SANDBOX_MODES = {"dangerFullAccess", "workspaceWrite"}


def _normalize_model_overrides(value: Any, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be an object mapping agent ids to models",
        )
    normalized: dict[str, str] = {}
    for raw_agent, raw_model in value.items():
        if not isinstance(raw_agent, str) or not raw_agent.strip():
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} keys must be non-empty agent ids",
            )
        agent = raw_agent.strip().lower()
        model = normalize_optional_string(
            raw_model,
            f"{field_name}.{agent}",
            allow_blank=True,
        )
        if model:
            normalized[agent] = model
    return normalized


def _session_settings_response(state: RunnerState) -> dict[str, Any]:
    return {
        "autorunner_model_overrides": dict(state.autorunner_model_overrides),
        "autorunner_effort_override": state.autorunner_effort_override,
        "autorunner_approval_policy": state.autorunner_approval_policy,
        "autorunner_sandbox_mode": state.autorunner_sandbox_mode,
        "autorunner_workspace_write_network": state.autorunner_workspace_write_network,
        "ticket_flow_require_commit": state.ticket_flow_require_commit,
        "runner_stop_after_runs": state.runner_stop_after_runs,
    }


def _normalize_session_settings_update(
    updates: dict[str, Any], state: RunnerState
) -> dict[str, Any]:
    model_overrides = (
        _normalize_model_overrides(
            updates.get("autorunner_model_overrides"),
            "autorunner_model_overrides",
        )
        if "autorunner_model_overrides" in updates
        else dict(state.autorunner_model_overrides)
    )
    effort_override = (
        normalize_optional_string(
            updates.get("autorunner_effort_override"),
            "autorunner_effort_override",
            allow_blank=True,
        )
        if "autorunner_effort_override" in updates
        else state.autorunner_effort_override
    )
    approval_policy = (
        normalize_optional_string(
            updates.get("autorunner_approval_policy"),
            "autorunner_approval_policy",
            allow_blank=True,
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
        normalize_optional_string(
            updates.get("autorunner_sandbox_mode"),
            "autorunner_sandbox_mode",
            allow_blank=True,
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
    ticket_flow_require_commit = (
        updates.get("ticket_flow_require_commit")
        if "ticket_flow_require_commit" in updates
        else state.ticket_flow_require_commit
    )
    if "ticket_flow_require_commit" in updates and not isinstance(
        ticket_flow_require_commit, bool
    ):
        raise HTTPException(
            status_code=400,
            detail="ticket_flow_require_commit must be a boolean",
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
    return {
        "autorunner_model_overrides": model_overrides,
        "autorunner_effort_override": effort_override,
        "autorunner_approval_policy": approval_policy,
        "autorunner_sandbox_mode": sandbox_mode,
        "autorunner_workspace_write_network": workspace_write_network,
        "ticket_flow_require_commit": ticket_flow_require_commit,
        "runner_stop_after_runs": runner_stop_after_runs,
    }


def _thread_reset_required(normalized: dict[str, Any], state: RunnerState) -> bool:
    return any(
        (
            normalized["autorunner_model_overrides"]
            != state.autorunner_model_overrides,
            normalized["autorunner_effort_override"]
            != state.autorunner_effort_override,
            normalized["autorunner_approval_policy"]
            != state.autorunner_approval_policy,
            normalized["autorunner_sandbox_mode"] != state.autorunner_sandbox_mode,
            normalized["autorunner_workspace_write_network"]
            != state.autorunner_workspace_write_network,
            normalized["ticket_flow_require_commit"]
            != state.ticket_flow_require_commit,
            normalized["runner_stop_after_runs"] != state.runner_stop_after_runs,
        )
    )


def _apply_session_settings_update(
    request: Request, updates: dict[str, Any]
) -> dict[str, Any]:
    engine = request.app.state.engine
    manager = request.app.state.manager
    registry = request.app.state.app_server_threads
    with state_lock(engine.state_path):
        state = load_state(engine.state_path)
        normalized = _normalize_session_settings_update(updates, state)
        thread_reset_required = _thread_reset_required(normalized, state)
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
            autorunner_model_overrides=normalized["autorunner_model_overrides"],
            autorunner_effort_override=normalized["autorunner_effort_override"],
            autorunner_approval_policy=normalized["autorunner_approval_policy"],
            autorunner_sandbox_mode=normalized["autorunner_sandbox_mode"],
            autorunner_workspace_write_network=normalized[
                "autorunner_workspace_write_network"
            ],
            ticket_flow_require_commit=normalized["ticket_flow_require_commit"],
            runner_stop_after_runs=normalized["runner_stop_after_runs"],
            runner_pid=state.runner_pid,
            sessions=state.sessions,
            repo_to_session=state.repo_to_session,
        )
        save_state(engine.state_path, new_state)
        if thread_reset_required:
            registry.reset_thread("autorunner")
    return normalized


def build_settings_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/session/settings", response_model=SessionSettingsResponse)
    def get_session_settings(request: Request):
        state = load_state(request.app.state.engine.state_path)
        return _session_settings_response(state)

    @router.post("/api/session/settings", response_model=SessionSettingsResponse)
    def update_session_settings(request: Request, payload: SessionSettingsRequest):
        updates = payload.model_dump(exclude_unset=True)
        return _apply_session_settings_update(request, updates)

    return router
