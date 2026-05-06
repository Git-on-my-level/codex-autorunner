"""
Session settings routes for autorunner overrides.
"""

import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ....core.interaction_inbox import (
    InteractionInboxError,
    InteractionInboxStore,
    InteractionOption,
    InteractionPrompt,
    default_interaction_inbox_path,
)
from ....core.state import RunnerState, load_state, save_state, state_lock
from ....core.time_utils import now_iso
from ..schemas import (
    SessionSettingsApprovalDecisionRequest,
    SessionSettingsRequest,
    SessionSettingsResponse,
)
from ..services.validation import normalize_optional_string

ALLOWED_APPROVAL_POLICIES = {"never", "unlessTrusted"}
ALLOWED_SANDBOX_MODES = {"dangerFullAccess", "workspaceWrite"}
APPROVAL_KIND = "session_settings_update"


def _interaction_store(request: Request) -> InteractionInboxStore:
    return InteractionInboxStore(
        default_interaction_inbox_path(request.app.state.engine.state_path)
    )


def _session_settings_response(state: RunnerState) -> dict[str, Any]:
    return {
        "autorunner_model_override": state.autorunner_model_override,
        "autorunner_effort_override": state.autorunner_effort_override,
        "autorunner_approval_policy": state.autorunner_approval_policy,
        "autorunner_sandbox_mode": state.autorunner_sandbox_mode,
        "autorunner_workspace_write_network": state.autorunner_workspace_write_network,
        "runner_stop_after_runs": state.runner_stop_after_runs,
    }


def _normalize_session_settings_update(
    updates: dict[str, Any], state: RunnerState
) -> dict[str, Any]:
    model_override = (
        normalize_optional_string(
            updates.get("autorunner_model_override"),
            "autorunner_model_override",
            allow_blank=True,
        )
        if "autorunner_model_override" in updates
        else state.autorunner_model_override
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
        "autorunner_model_override": model_override,
        "autorunner_effort_override": effort_override,
        "autorunner_approval_policy": approval_policy,
        "autorunner_sandbox_mode": sandbox_mode,
        "autorunner_workspace_write_network": workspace_write_network,
        "runner_stop_after_runs": runner_stop_after_runs,
    }


def _thread_reset_required(normalized: dict[str, Any], state: RunnerState) -> bool:
    return any(
        (
            normalized["autorunner_model_override"] != state.autorunner_model_override,
            normalized["autorunner_effort_override"]
            != state.autorunner_effort_override,
            normalized["autorunner_approval_policy"]
            != state.autorunner_approval_policy,
            normalized["autorunner_sandbox_mode"] != state.autorunner_sandbox_mode,
            normalized["autorunner_workspace_write_network"]
            != state.autorunner_workspace_write_network,
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
            autorunner_model_override=normalized["autorunner_model_override"],
            autorunner_effort_override=normalized["autorunner_effort_override"],
            autorunner_approval_policy=normalized["autorunner_approval_policy"],
            autorunner_sandbox_mode=normalized["autorunner_sandbox_mode"],
            autorunner_workspace_write_network=normalized[
                "autorunner_workspace_write_network"
            ],
            runner_stop_after_runs=normalized["runner_stop_after_runs"],
            runner_pid=state.runner_pid,
            sessions=state.sessions,
            repo_to_session=state.repo_to_session,
        )
        save_state(engine.state_path, new_state)
        if thread_reset_required:
            registry.reset_thread("autorunner")
    return normalized


def _approval_from_update(request: Request, updates: dict[str, Any]) -> dict[str, Any]:
    engine = request.app.state.engine
    with state_lock(engine.state_path):
        state = load_state(engine.state_path)
        normalized = _normalize_session_settings_update(updates, state)
        if not _thread_reset_required(normalized, state):
            raise HTTPException(
                status_code=400,
                detail="No runtime preference changes were requested",
            )
    created_at = now_iso()
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    approval_id = f"{APPROVAL_KIND}:{int(time.time() * 1000)}:{digest}"
    return {
        "id": approval_id,
        "approval_id": approval_id,
        "item_type": "sensitive_car_approval",
        "action": "modify_car_config",
        "title": "Modify CAR runtime preferences",
        "summary": "Modify CAR runtime preferences",
        "description": (
            "Apply persistent PMA model, reasoning, sandbox, approval, network, "
            "or run-limit overrides. This can reset the autorunner managed thread."
        ),
        "risk": "high",
        "sensitivity": "high",
        "scope": "session settings",
        "target_scope": "session settings",
        "created_at": created_at,
        "decision_url": f"/api/session/settings/approvals/{approval_id}/decision",
        "route": f"/api/session/settings/approvals/{approval_id}/decision",
        "payload": normalized,
        "status": "pending",
    }


def _prompt_from_approval(approval: dict[str, Any]) -> InteractionPrompt:
    return InteractionPrompt(
        id=str(approval["id"]),
        kind="approval",
        title=str(approval.get("title") or "Approval requested"),
        message=str(approval.get("description") or approval.get("summary") or ""),
        owner={"kind": "session", "id": "settings"},
        target_scope={"kind": "session_settings", "key": "session settings"},
        options=(
            InteractionOption(id="approve", label="Approve"),
            InteractionOption(id="decline", label="Decline"),
        ),
        source={
            "surface": "web",
            "kind": APPROVAL_KIND,
            "route": approval.get("route"),
        },
        metadata={"session_settings_approval": approval},
        created_at=str(approval.get("created_at") or now_iso()),
    )


def _approval_from_prompt(prompt: Any) -> dict[str, Any]:
    approval = (prompt.metadata or {}).get("session_settings_approval")
    if isinstance(approval, dict):
        copied = dict(approval)
    else:
        copied = {
            "id": prompt.id,
            "approval_id": prompt.id,
            "item_type": "sensitive_car_approval",
            "action": "modify_car_config",
            "title": prompt.title,
            "summary": prompt.title,
            "description": prompt.message,
            "scope": "session settings",
            "target_scope": "session settings",
            "created_at": prompt.created_at,
            "decision_url": f"/api/session/settings/approvals/{prompt.id}/decision",
            "route": f"/api/session/settings/approvals/{prompt.id}/decision",
            "payload": {},
        }
    copied["status"] = prompt.status
    copied["interaction_prompt_id"] = prompt.id
    return copied


def _interaction_http_error(exc: InteractionInboxError) -> HTTPException:
    status = {
        "not_found": 404,
        "already_answered": 404,
        "expired": 409,
        "unauthorized_actor": 403,
        "invalid_response": 400,
    }.get(exc.code, 400)
    return HTTPException(status_code=status, detail=exc.message)


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

    @router.get("/api/session/settings/approvals")
    def list_session_settings_approvals(request: Request):
        prompts = _interaction_store(request).list_prompts(
            statuses=["pending"], kind="approval"
        )
        return {
            "approvals": [
                _approval_from_prompt(prompt)
                for prompt in prompts
                if prompt.source.get("kind") == APPROVAL_KIND
            ]
        }

    @router.post("/api/session/settings/approvals")
    def request_session_settings_approval(
        request: Request, payload: SessionSettingsRequest
    ):
        approval = _approval_from_update(
            request, payload.model_dump(exclude_unset=True)
        )
        _interaction_store(request).upsert_prompt(_prompt_from_approval(approval))
        return approval

    @router.post("/api/session/settings/approvals/{approval_id}/decision")
    def decide_session_settings_approval(
        request: Request,
        approval_id: str,
        payload: SessionSettingsApprovalDecisionRequest,
    ):
        if payload.approval_id is not None and payload.approval_id != approval_id:
            raise HTTPException(status_code=400, detail="approval_id mismatch")
        store = _interaction_store(request)
        prompt = store.get_prompt(approval_id)
        if prompt is None or prompt.status != "pending":
            raise HTTPException(status_code=404, detail="Approval not found")
        approval = _approval_from_prompt(prompt)
        try:
            decided_prompt = store.respond(
                approval_id,
                actor_user_id=None,
                response={"decision": payload.decision},
            )
        except InteractionInboxError as exc:
            raise _interaction_http_error(exc) from exc
        if payload.decision == "decline":
            return {"status": "declined", "approval_id": approval_id}
        updates = approval.get("payload")
        if not isinstance(updates, dict):
            raise HTTPException(status_code=500, detail="Approval payload is invalid")
        applied = _apply_session_settings_update(request, updates)
        if decided_prompt.status != "answered":
            raise HTTPException(
                status_code=500, detail="Approval decision was not saved"
            )
        return {"status": "approved", "approval_id": approval_id, "settings": applied}

    return router
