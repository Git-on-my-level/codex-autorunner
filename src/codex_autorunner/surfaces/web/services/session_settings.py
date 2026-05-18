from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ....core.state import RunnerState, load_state, save_state, state_lock

ALLOWED_APPROVAL_POLICIES = {"never", "unlessTrusted"}
ALLOWED_SANDBOX_MODES = {"dangerFullAccess", "workspaceWrite"}


@dataclass(frozen=True)
class SessionSettings:
    autorunner_model_overrides: dict[str, str]
    autorunner_effort_override: str | None
    autorunner_approval_policy: str | None
    autorunner_sandbox_mode: str | None
    autorunner_workspace_write_network: bool | None
    ticket_flow_require_commit: bool
    runner_stop_after_runs: int | None

    def to_response(self) -> dict[str, Any]:
        return {
            "autorunner_model_overrides": dict(self.autorunner_model_overrides),
            "autorunner_effort_override": self.autorunner_effort_override,
            "autorunner_approval_policy": self.autorunner_approval_policy,
            "autorunner_sandbox_mode": self.autorunner_sandbox_mode,
            "autorunner_workspace_write_network": (
                self.autorunner_workspace_write_network
            ),
            "ticket_flow_require_commit": self.ticket_flow_require_commit,
            "runner_stop_after_runs": self.runner_stop_after_runs,
        }


@dataclass(frozen=True)
class SessionSettingsUpdateResult:
    settings: SessionSettings
    thread_reset_required: bool
    thread_reset: bool


class SessionSettingsError(ValueError):
    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _normalize_optional_string(
    value: object,
    field: str,
    *,
    allow_blank: bool = True,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SessionSettingsError(f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned:
        if allow_blank:
            return None
        raise SessionSettingsError(f"{field} must not be empty")
    return cleaned


def session_settings_from_state(state: RunnerState) -> SessionSettings:
    return SessionSettings(
        autorunner_model_overrides=dict(state.autorunner_model_overrides),
        autorunner_effort_override=state.autorunner_effort_override,
        autorunner_approval_policy=state.autorunner_approval_policy,
        autorunner_sandbox_mode=state.autorunner_sandbox_mode,
        autorunner_workspace_write_network=state.autorunner_workspace_write_network,
        ticket_flow_require_commit=state.ticket_flow_require_commit,
        runner_stop_after_runs=state.runner_stop_after_runs,
    )


def _normalize_model_overrides(value: Any, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SessionSettingsError(
            f"{field_name} must be an object mapping agent ids to models",
        )
    normalized: dict[str, str] = {}
    for raw_agent, raw_model in value.items():
        if not isinstance(raw_agent, str) or not raw_agent.strip():
            raise SessionSettingsError(
                f"{field_name} keys must be non-empty agent ids",
            )
        agent = raw_agent.strip().lower()
        model = _normalize_optional_string(
            raw_model,
            f"{field_name}.{agent}",
            allow_blank=True,
        )
        if model:
            normalized[agent] = model
    return normalized


def normalize_session_settings_update(
    updates: dict[str, Any], state: RunnerState
) -> SessionSettings:
    model_overrides = (
        _normalize_model_overrides(
            updates.get("autorunner_model_overrides"),
            "autorunner_model_overrides",
        )
        if "autorunner_model_overrides" in updates
        else dict(state.autorunner_model_overrides)
    )
    effort_override = (
        _normalize_optional_string(
            updates.get("autorunner_effort_override"),
            "autorunner_effort_override",
            allow_blank=True,
        )
        if "autorunner_effort_override" in updates
        else state.autorunner_effort_override
    )
    approval_policy = (
        _normalize_optional_string(
            updates.get("autorunner_approval_policy"),
            "autorunner_approval_policy",
            allow_blank=True,
        )
        if "autorunner_approval_policy" in updates
        else state.autorunner_approval_policy
    )
    if approval_policy and approval_policy not in ALLOWED_APPROVAL_POLICIES:
        raise SessionSettingsError("approval policy must be never or unlessTrusted")
    sandbox_mode = (
        _normalize_optional_string(
            updates.get("autorunner_sandbox_mode"),
            "autorunner_sandbox_mode",
            allow_blank=True,
        )
        if "autorunner_sandbox_mode" in updates
        else state.autorunner_sandbox_mode
    )
    if sandbox_mode and sandbox_mode not in ALLOWED_SANDBOX_MODES:
        raise SessionSettingsError(
            "sandbox mode must be dangerFullAccess or workspaceWrite"
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
        raise SessionSettingsError(
            "autorunner_workspace_write_network must be a boolean"
        )
    ticket_flow_require_commit = (
        updates.get("ticket_flow_require_commit")
        if "ticket_flow_require_commit" in updates
        else state.ticket_flow_require_commit
    )
    if "ticket_flow_require_commit" in updates and not isinstance(
        ticket_flow_require_commit, bool
    ):
        raise SessionSettingsError("ticket_flow_require_commit must be a boolean")
    normalized_ticket_flow_require_commit = bool(ticket_flow_require_commit)
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
        raise SessionSettingsError("runner_stop_after_runs must be a positive integer")
    return SessionSettings(
        autorunner_model_overrides=model_overrides,
        autorunner_effort_override=effort_override,
        autorunner_approval_policy=approval_policy,
        autorunner_sandbox_mode=sandbox_mode,
        autorunner_workspace_write_network=workspace_write_network,
        ticket_flow_require_commit=normalized_ticket_flow_require_commit,
        runner_stop_after_runs=runner_stop_after_runs,
    )


def thread_reset_required(settings: SessionSettings, state: RunnerState) -> bool:
    return settings != session_settings_from_state(state)


def _state_with_settings(state: RunnerState, settings: SessionSettings) -> RunnerState:
    return dataclasses.replace(
        state,
        autorunner_model_overrides=dict(settings.autorunner_model_overrides),
        autorunner_effort_override=settings.autorunner_effort_override,
        autorunner_approval_policy=settings.autorunner_approval_policy,
        autorunner_sandbox_mode=settings.autorunner_sandbox_mode,
        autorunner_workspace_write_network=settings.autorunner_workspace_write_network,
        ticket_flow_require_commit=settings.ticket_flow_require_commit,
        runner_stop_after_runs=settings.runner_stop_after_runs,
    )


def update_session_settings(
    *,
    state_path: Path,
    updates: dict[str, Any],
    is_run_active: Callable[[], bool],
    reset_thread: Callable[[str], bool | None],
) -> SessionSettingsUpdateResult:
    with state_lock(state_path):
        state = load_state(state_path)
        settings = normalize_session_settings_update(updates, state)
        reset_required = thread_reset_required(settings, state)
        if reset_required and is_run_active():
            raise SessionSettingsError(
                "Cannot change autorunner settings while a run is active",
                status_code=409,
            )

        if reset_required:
            save_state(state_path, _state_with_settings(state, settings))
            reset_result = bool(reset_thread("autorunner"))
        else:
            reset_result = False

    return SessionSettingsUpdateResult(
        settings=settings,
        thread_reset_required=reset_required,
        thread_reset=reset_result,
    )


__all__ = [
    "SessionSettings",
    "SessionSettingsError",
    "SessionSettingsUpdateResult",
    "normalize_session_settings_update",
    "session_settings_from_state",
    "thread_reset_required",
    "update_session_settings",
]
