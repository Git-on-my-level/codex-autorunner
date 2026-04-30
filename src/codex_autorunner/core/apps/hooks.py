from __future__ import annotations

import dataclasses
import logging
import shutil
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from ..flows.models import FlowEventType
from .install import AppInstallError, InstalledAppInfo, list_installed_apps
from .paths import AppPathError, validate_app_glob
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
    AFTER_FLOW_ARCHIVE = "after_flow_archive"
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


@dataclasses.dataclass(frozen=True)
class AppArchiveCleanupEntry:
    app_id: str
    hook_point: AppHookPoint
    cleanup_paths: tuple[str, ...]
    removed_paths: tuple[str, ...] = ()
    missing_paths: tuple[str, ...] = ()
    error: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class AppArchiveCleanupResult:
    hook_point: AppHookPoint
    entries: tuple[AppArchiveCleanupEntry, ...] = ()

    @property
    def failed(self) -> bool:
        return any(entry.error is not None for entry in self.entries)


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


def execute_app_archive_cleanup_hooks(
    repo_root: Path,
    *,
    flow_run_id: Optional[str] = None,
    flow_status: Any = None,
) -> AppArchiveCleanupResult:
    """Apply constrained cleanup declarations for installed apps after archiving.

    This lifecycle hook is intentionally data-driven: app manifests can declare
    app-local runtime paths to delete, but CAR performs the deletion after
    validating that every target is under that app's own state/artifacts/logs
    roots. Hook cleanup cannot target the bundle, lockfile, tickets, or repo
    files.
    """

    point = AppHookPoint.AFTER_FLOW_ARCHIVE
    repo_root = repo_root.resolve()
    try:
        apps = list_installed_apps(repo_root)
    except AppInstallError as exc:
        raise AppHookExecutionError(str(exc)) from exc

    entries: list[AppArchiveCleanupEntry] = []
    for installed in apps:
        for hook in installed.manifest.hooks:
            if hook.point != point.value:
                continue
            for hook_entry in hook.entries:
                installed_hook = InstalledAppHook(
                    app_id=installed.app_id,
                    tool_id="",
                    hook_point=point,
                    failure=hook_entry.failure,
                    when=dict(hook_entry.when or {}),
                )
                if not matches_installed_app_hook(
                    installed_hook,
                    flow_status=flow_status,
                ):
                    continue
                cleanup_paths = tuple(hook_entry.cleanup_paths)
                if not cleanup_paths:
                    continue
                try:
                    removed, missing = _cleanup_installed_app_paths(
                        installed,
                        cleanup_paths,
                    )
                    entries.append(
                        AppArchiveCleanupEntry(
                            app_id=installed.app_id,
                            hook_point=point,
                            cleanup_paths=cleanup_paths,
                            removed_paths=tuple(removed),
                            missing_paths=tuple(missing),
                        )
                    )
                except Exception as exc:
                    _logger.warning(
                        "App archive cleanup failed: app=%s flow_run_id=%s error=%s",
                        installed.app_id,
                        flow_run_id,
                        exc,
                    )
                    entries.append(
                        AppArchiveCleanupEntry(
                            app_id=installed.app_id,
                            hook_point=point,
                            cleanup_paths=cleanup_paths,
                            error=str(exc).strip() or exc.__class__.__name__,
                        )
                    )
    return AppArchiveCleanupResult(hook_point=point, entries=tuple(entries))


def _cleanup_installed_app_paths(
    installed: InstalledAppInfo,
    cleanup_paths: Iterable[str],
) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    missing: list[str] = []
    targets: dict[Path, str] = {}

    for raw_path in cleanup_paths:
        for target, display_path in _resolve_cleanup_targets(installed, raw_path):
            targets[target] = display_path
        if not any(
            True for _ in _resolve_existing_cleanup_targets(installed, raw_path)
        ):
            missing.append(raw_path)

    for target, display_path in sorted(
        targets.items(),
        key=lambda item: len(item[0].parts),
        reverse=True,
    ):
        _ensure_cleanup_target_allowed(installed, target)
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        removed.append(display_path)

    return sorted(set(removed)), sorted(set(missing))


def _resolve_cleanup_targets(
    installed: InstalledAppInfo, raw_path: str
) -> Iterable[tuple[Path, str]]:
    normalized = _normalize_cleanup_path(raw_path)
    app_root = installed.paths.app_root.resolve()
    if _contains_glob(normalized):
        for match in app_root.glob(normalized):
            resolved = match.resolve()
            _ensure_cleanup_target_allowed(installed, resolved)
            yield resolved, resolved.relative_to(app_root).as_posix()
        return
    resolved = (app_root / normalized).resolve()
    _ensure_cleanup_target_allowed(installed, resolved)
    yield resolved, normalized


def _resolve_existing_cleanup_targets(
    installed: InstalledAppInfo, raw_path: str
) -> Iterable[Path]:
    for target, _display_path in _resolve_cleanup_targets(installed, raw_path):
        if target.exists() or target.is_symlink():
            yield target


def _normalize_cleanup_path(raw_path: str) -> str:
    try:
        normalized = validate_app_glob(raw_path)
    except AppPathError as exc:
        raise AppHookExecutionError(
            f"Invalid archive cleanup path {raw_path!r}: {exc}"
        ) from exc
    parts = normalized.parts
    if not parts or parts[0] not in {"state", "artifacts", "logs"}:
        raise AppHookExecutionError(
            "Archive cleanup paths must be under state/, artifacts/, or logs/: "
            f"{raw_path!r}"
        )
    if len(parts) == 1:
        raise AppHookExecutionError(
            f"Archive cleanup path must not target a runtime root directly: {raw_path!r}"
        )
    return normalized.as_posix()


def _contains_glob(path: str) -> bool:
    return "*" in path or "?" in path


def _ensure_cleanup_target_allowed(
    installed: InstalledAppInfo,
    target: Path,
) -> None:
    resolved_target = target.resolve()
    allowed_roots = (
        installed.paths.state_root.resolve(),
        installed.paths.artifacts_root.resolve(),
        installed.paths.logs_root.resolve(),
    )
    if not any(
        resolved_target == root or resolved_target.is_relative_to(root)
        for root in allowed_roots
    ):
        raise AppHookExecutionError(
            f"Archive cleanup target escapes app runtime roots: {resolved_target}"
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
