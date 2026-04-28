from __future__ import annotations

import dataclasses
import logging
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..flows.models import FlowEventType
from .install import AppInstallError, list_installed_apps
from .tool_runner import (
    AppToolRunnerError,
    AppToolRunResult,
    AppToolTimeoutError,
    run_installed_app_tool,
)

_logger = logging.getLogger(__name__)


class AppHookPoint(str, Enum):
    AFTER_TICKET_DONE = "after_ticket_done"
    AFTER_FLOW_TERMINAL = "after_flow_terminal"
    BEFORE_CHAT_WRAPUP = "before_chat_wrapup"


class AppHookExecutionError(Exception):
    """Raised when CAR cannot discover or execute installed app hooks."""


@dataclasses.dataclass(frozen=True)
class InstalledAppHook:
    app_id: str
    tool_id: str
    hook_point: AppHookPoint
    failure: str
    when: Mapping[str, Any]


@dataclasses.dataclass(frozen=True)
class AppHookExecution:
    app_id: str
    tool_id: str
    hook_point: AppHookPoint
    failure: str
    exit_code: Optional[int]
    duration_seconds: Optional[float]
    stdout_log_path: Optional[Path]
    stderr_log_path: Optional[Path]
    error: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class AppHookInvocationResult:
    hook_point: AppHookPoint
    executions: tuple[AppHookExecution, ...] = ()
    paused: bool = False
    failed: bool = False
    reason: Optional[str] = None
    reason_details: Optional[str] = None


EmitHookEventFn = Callable[[FlowEventType, dict[str, Any]], None]


def normalize_hook_point(value: AppHookPoint | str) -> AppHookPoint:
    if isinstance(value, AppHookPoint):
        return value
    try:
        return AppHookPoint(str(value).strip())
    except ValueError as exc:
        raise AppHookExecutionError(f"Unknown app hook point: {value!r}") from exc


def list_installed_app_hooks(
    repo_root: Path, hook_point: AppHookPoint | str
) -> list[InstalledAppHook]:
    point = normalize_hook_point(hook_point)
    repo_root = repo_root.resolve()
    try:
        apps = list_installed_apps(repo_root)
    except AppInstallError as exc:
        raise AppHookExecutionError(str(exc)) from exc

    discovered: list[InstalledAppHook] = []
    for app in apps:
        for hook in app.manifest.hooks:
            if hook.point != point.value:
                continue
            for entry in hook.entries:
                if not entry.tool:
                    continue
                discovered.append(
                    InstalledAppHook(
                        app_id=app.app_id,
                        tool_id=entry.tool,
                        hook_point=point,
                        failure=entry.failure,
                        when=dict(entry.when or {}),
                    )
                )
    return discovered


def matches_installed_app_hook(
    hook: InstalledAppHook,
    *,
    ticket_frontmatter: Any = None,
    flow_status: Any = None,
) -> bool:
    when = dict(hook.when or {})
    ticket_matcher = when.get("ticket_frontmatter")
    if ticket_matcher is not None and not _match_ticket_frontmatter(
        ticket_matcher, ticket_frontmatter
    ):
        return False

    status_matcher = when.get("status")
    if status_matcher is not None and not _match_status(status_matcher, flow_status):
        return False

    return True


def execute_matching_installed_app_hooks(
    repo_root: Path,
    hook_point: AppHookPoint | str,
    *,
    workspace_root: Optional[Path] = None,
    flow_run_id: Optional[str] = None,
    ticket_id: Optional[str] = None,
    ticket_path: Optional[Path] = None,
    ticket_frontmatter: Any = None,
    flow_status: Any = None,
    emit_event: Optional[EmitHookEventFn] = None,
) -> AppHookInvocationResult:
    point = normalize_hook_point(hook_point)
    repo_root = repo_root.resolve()
    resolved_ticket_path = ticket_path.resolve() if ticket_path is not None else None
    executions: list[AppHookExecution] = []

    for hook in list_installed_app_hooks(repo_root, point):
        if not matches_installed_app_hook(
            hook,
            ticket_frontmatter=ticket_frontmatter,
            flow_status=flow_status,
        ):
            continue

        _emit_hook_event(
            emit_event,
            FlowEventType.APP_HOOK_STARTED,
            {
                "app_id": hook.app_id,
                "tool_id": hook.tool_id,
                "hook_point": point.value,
                "ticket_id": ticket_id,
                "ticket_path": _display_path(resolved_ticket_path, repo_root),
                "flow_run_id": flow_run_id,
                "flow_status": _normalize_status(flow_status),
            },
        )

        execution = _run_single_hook(
            repo_root=repo_root,
            workspace_root=(workspace_root or repo_root),
            hook=hook,
            flow_run_id=flow_run_id,
            ticket_id=ticket_id,
            ticket_path=resolved_ticket_path,
        )
        executions.append(execution)

        _emit_hook_event(
            emit_event,
            FlowEventType.APP_HOOK_RESULT,
            {
                "app_id": execution.app_id,
                "tool_id": execution.tool_id,
                "hook_point": execution.hook_point.value,
                "failure": execution.failure,
                "exit_code": execution.exit_code,
                "duration_seconds": execution.duration_seconds,
                "stdout_log_path": _display_path(execution.stdout_log_path, repo_root),
                "stderr_log_path": _display_path(execution.stderr_log_path, repo_root),
                "error": execution.error,
            },
        )

        if not _execution_failed(execution):
            continue

        if execution.failure == "warn":
            _logger.warning(
                "App hook failed but continuing: app=%s tool=%s point=%s error=%s exit_code=%s",
                execution.app_id,
                execution.tool_id,
                execution.hook_point.value,
                execution.error,
                execution.exit_code,
            )
            continue

        reason, details = _build_hook_failure_message(
            repo_root=repo_root,
            execution=execution,
        )
        return AppHookInvocationResult(
            hook_point=point,
            executions=tuple(executions),
            paused=execution.failure == "pause",
            failed=execution.failure == "fail",
            reason=reason,
            reason_details=details,
        )

    return AppHookInvocationResult(
        hook_point=point,
        executions=tuple(executions),
    )


def _run_single_hook(
    *,
    repo_root: Path,
    workspace_root: Path,
    hook: InstalledAppHook,
    flow_run_id: Optional[str],
    ticket_id: Optional[str],
    ticket_path: Optional[Path],
) -> AppHookExecution:
    try:
        result = run_installed_app_tool(
            repo_root,
            hook.app_id,
            hook.tool_id,
            workspace_root=workspace_root,
            flow_run_id=flow_run_id,
            ticket_id=ticket_id,
            ticket_path=ticket_path,
            hook_point=hook.hook_point.value,
        )
    except AppToolTimeoutError as exc:
        return AppHookExecution(
            app_id=hook.app_id,
            tool_id=hook.tool_id,
            hook_point=hook.hook_point,
            failure=hook.failure,
            exit_code=None,
            duration_seconds=None,
            stdout_log_path=getattr(exc, "stdout_log_path", None),
            stderr_log_path=getattr(exc, "stderr_log_path", None),
            error=str(exc),
        )
    except AppToolRunnerError as exc:
        return AppHookExecution(
            app_id=hook.app_id,
            tool_id=hook.tool_id,
            hook_point=hook.hook_point,
            failure=hook.failure,
            exit_code=None,
            duration_seconds=None,
            stdout_log_path=None,
            stderr_log_path=None,
            error=str(exc),
        )

    return _execution_from_result(hook=hook, result=result)


def _execution_from_result(
    *, hook: InstalledAppHook, result: AppToolRunResult
) -> AppHookExecution:
    return AppHookExecution(
        app_id=hook.app_id,
        tool_id=hook.tool_id,
        hook_point=hook.hook_point,
        failure=hook.failure,
        exit_code=result.exit_code,
        duration_seconds=result.duration_seconds,
        stdout_log_path=result.stdout_log_path,
        stderr_log_path=result.stderr_log_path,
        error=(
            None
            if result.exit_code == 0
            else f"App hook tool exited with status {result.exit_code}"
        ),
    )


def _execution_failed(execution: AppHookExecution) -> bool:
    return execution.error is not None or (execution.exit_code not in (None, 0))


def _match_ticket_frontmatter(selector: Any, ticket_frontmatter: Any) -> bool:
    if not isinstance(selector, Mapping):
        return False
    payload = _ticket_frontmatter_payload(ticket_frontmatter)
    for key, expected in selector.items():
        if payload.get(str(key)) != expected:
            return False
    return True


def _ticket_frontmatter_payload(ticket_frontmatter: Any) -> dict[str, Any]:
    if ticket_frontmatter is None:
        return {}
    payload: dict[str, Any] = {}
    for key in (
        "ticket_id",
        "agent",
        "done",
        "title",
        "goal",
        "model",
        "reasoning",
        "profile",
    ):
        if hasattr(ticket_frontmatter, key):
            payload[key] = getattr(ticket_frontmatter, key)
    extra = getattr(ticket_frontmatter, "extra", None)
    if isinstance(extra, Mapping):
        for key, value in extra.items():
            payload[str(key)] = value
    return payload


def _match_status(selector: Any, flow_status: Any) -> bool:
    actual = _normalize_status(flow_status)
    if actual is None:
        return False
    if isinstance(selector, str):
        return actual == selector.strip().lower()
    if isinstance(selector, list):
        return actual in {
            str(item).strip().lower() for item in selector if str(item).strip()
        }
    return False


def _normalize_status(value: Any) -> Optional[str]:
    raw = getattr(value, "value", value)
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


def _display_path(path: Optional[Path], repo_root: Path) -> Optional[str]:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path.resolve())


def _build_hook_failure_message(
    *, repo_root: Path, execution: AppHookExecution
) -> tuple[str, str]:
    reason = (
        f"App hook failed: {execution.app_id} {execution.tool_id} "
        f"({execution.hook_point.value})."
    )
    detail_lines = [
        f"Failure policy: {execution.failure}",
        f"App: {execution.app_id}",
        f"Tool: {execution.tool_id}",
        f"Hook point: {execution.hook_point.value}",
    ]
    if execution.exit_code is not None:
        detail_lines.append(f"Exit code: {execution.exit_code}")
    if execution.duration_seconds is not None:
        detail_lines.append(f"Duration: {execution.duration_seconds:.3f}s")
    if execution.error:
        detail_lines.append(f"Error: {execution.error}")
    if execution.stdout_log_path is not None:
        detail_lines.append(
            f"stdout log: {_display_path(execution.stdout_log_path, repo_root)}"
        )
    if execution.stderr_log_path is not None:
        detail_lines.append(
            f"stderr log: {_display_path(execution.stderr_log_path, repo_root)}"
        )
    return reason, "\n".join(detail_lines)


def _emit_hook_event(
    emit_event: Optional[EmitHookEventFn],
    event_type: FlowEventType,
    payload: dict[str, Any],
) -> None:
    if emit_event is None:
        return
    try:
        emit_event(event_type, payload)
    except Exception:
        _logger.debug("failed to emit app hook event", exc_info=True)
