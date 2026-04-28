from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ..utils import subprocess_env
from .artifacts import (
    collect_declared_tool_artifact_candidates,
    register_app_artifact_candidates,
)
from .install import AppInstallError, InstalledAppInfo, get_installed_app
from .manifest import AppOutput, AppTool
from .paths import AppPathError, validate_app_path

_MAX_LOG_BYTES = 64 * 1024
_MAX_EXCERPT_CHARS = 4096
_LOG_HEAD_BYTES = 24 * 1024
_LOG_TAIL_BYTES = 24 * 1024
_TRUNCATION_MARKER = b"\n...<truncated>...\n"


class AppToolRunnerError(Exception):
    """Raised when CAR cannot prepare or execute an installed app tool."""


class AppToolNotFoundError(AppToolRunnerError):
    """Raised when a requested installed app or tool does not exist."""


class AppToolTimeoutError(AppToolRunnerError):
    """Raised when an app tool exceeds its execution timeout."""


@dataclasses.dataclass(frozen=True)
class InstalledAppToolInfo:
    app_id: str
    tool_id: str
    description: str
    argv: tuple[str, ...]
    timeout_seconds: int
    outputs: tuple[AppOutput, ...]
    bundle_verified: bool


@dataclasses.dataclass(frozen=True)
class AppToolRunOutput:
    kind: str
    label: str
    relative_path: str
    absolute_path: Path


@dataclasses.dataclass(frozen=True)
class AppToolRunResult:
    app_id: str
    tool_id: str
    argv: tuple[str, ...]
    exit_code: int
    duration_seconds: float
    stdout_excerpt: str
    stderr_excerpt: str
    stdout_log_path: Path
    stderr_log_path: Path
    outputs: tuple[AppToolRunOutput, ...]


def list_installed_app_tools(
    repo_root: Path, app_id: str
) -> list[InstalledAppToolInfo]:
    installed = _require_installed_app(repo_root, app_id)
    return [
        InstalledAppToolInfo(
            app_id=installed.app_id,
            tool_id=tool.id,
            description=tool.description,
            argv=tuple(tool.argv),
            timeout_seconds=int(tool.timeout_seconds),
            outputs=tuple(tool.outputs),
            bundle_verified=installed.bundle_verified,
        )
        for tool in installed.manifest.tools.values()
    ]


def run_installed_app_tool(
    repo_root: Path,
    app_id: str,
    tool_id: str,
    *,
    extra_argv: Sequence[str] = (),
    workspace_root: Optional[Path] = None,
    flow_run_id: Optional[str] = None,
    ticket_id: Optional[str] = None,
    ticket_path: Optional[Path] = None,
    hook_point: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> AppToolRunResult:
    repo_root = repo_root.resolve()
    workspace_root = (workspace_root or repo_root).resolve()
    installed = _require_installed_app(repo_root, app_id)
    _ensure_installed_app_is_trusted(installed)
    _ensure_bundle_is_clean(installed)
    tool = _resolve_tool(installed, tool_id)
    runtime_timeout = _resolve_timeout_seconds(tool, timeout_seconds)
    argv = _build_tool_argv(installed, tool, extra_argv)

    for directory in (
        installed.paths.state_root,
        installed.paths.artifacts_root,
        installed.paths.logs_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    run_label = _build_run_label(tool_id, flow_run_id=flow_run_id)
    stdout_log_path = installed.paths.logs_root / f"{run_label}.stdout.log"
    stderr_log_path = installed.paths.logs_root / f"{run_label}.stderr.log"

    env = _build_tool_env(
        installed=installed,
        repo_root=repo_root,
        workspace_root=workspace_root,
        flow_run_id=flow_run_id,
        ticket_id=ticket_id,
        ticket_path=ticket_path,
        hook_point=hook_point,
    )

    stdout_tmp_path: Path | None = None
    stderr_tmp_path: Path | None = None
    started_at = time.monotonic()
    timeout_error: AppToolTimeoutError | None = None
    exit_code = -1

    try:
        with (
            tempfile.NamedTemporaryFile(
                prefix=f"{run_label}.stdout.",
                suffix=".tmp",
                dir=installed.paths.logs_root,
                delete=False,
            ) as stdout_tmp,
            tempfile.NamedTemporaryFile(
                prefix=f"{run_label}.stderr.",
                suffix=".tmp",
                dir=installed.paths.logs_root,
                delete=False,
            ) as stderr_tmp,
        ):
            stdout_tmp_path = Path(stdout_tmp.name)
            stderr_tmp_path = Path(stderr_tmp.name)
            proc = subprocess.Popen(
                list(argv),
                cwd=repo_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_tmp,
                stderr=stderr_tmp,
                shell=False,
            )
            try:
                exit_code = proc.wait(timeout=runtime_timeout)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                exit_code = proc.wait()
                timeout_error = AppToolTimeoutError(
                    f"App tool timed out after {runtime_timeout:.1f}s: {app_id} {tool_id}"
                )
                timeout_error.__cause__ = exc
    finally:
        duration_seconds = time.monotonic() - started_at

    assert stdout_tmp_path is not None
    assert stderr_tmp_path is not None

    try:
        stdout_text = _materialize_bounded_log(stdout_tmp_path, stdout_log_path)
        stderr_text = _materialize_bounded_log(stderr_tmp_path, stderr_log_path)
    finally:
        _remove_if_exists(stdout_tmp_path)
        _remove_if_exists(stderr_tmp_path)

    output_candidates = collect_declared_tool_artifact_candidates(
        installed,
        tool,
        hook_point=hook_point,
    )
    outputs = tuple(_collect_declared_outputs(output_candidates))
    if flow_run_id:
        register_app_artifact_candidates(repo_root, flow_run_id, output_candidates)
    if timeout_error is not None:
        raise timeout_error
    return AppToolRunResult(
        app_id=app_id,
        tool_id=tool_id,
        argv=argv,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        stdout_excerpt=_excerpt_text(stdout_text),
        stderr_excerpt=_excerpt_text(stderr_text),
        stdout_log_path=stdout_log_path,
        stderr_log_path=stderr_log_path,
        outputs=outputs,
    )


def _require_installed_app(repo_root: Path, app_id: str) -> InstalledAppInfo:
    try:
        installed = get_installed_app(repo_root, app_id)
    except AppInstallError as exc:
        raise AppToolRunnerError(str(exc)) from exc
    if installed is None:
        raise AppToolNotFoundError(f"Installed app not found: {app_id}")
    return installed


def _ensure_bundle_is_clean(installed: InstalledAppInfo) -> None:
    if installed.bundle_verified:
        return
    raise AppToolRunnerError(
        "Installed app bundle does not match app.lock.json; reinstall the app "
        f"before running tools: {installed.app_id}"
    )


def _ensure_installed_app_is_trusted(installed: InstalledAppInfo) -> None:
    if installed.lock.trusted:
        return
    raise AppToolRunnerError(
        "Refusing to execute tools for untrusted installed app "
        f"{installed.app_id}. Reinstall from a trusted apps repo before running tools."
    )


def _resolve_tool(installed: InstalledAppInfo, tool_id: str) -> AppTool:
    tool = installed.manifest.tools.get(tool_id)
    if tool is None:
        raise AppToolNotFoundError(
            f"Unknown tool for installed app {installed.app_id}: {tool_id}"
        )
    return tool


def _resolve_timeout_seconds(tool: AppTool, override: Optional[float]) -> float:
    candidate = float(override if override is not None else tool.timeout_seconds)
    if candidate <= 0:
        raise AppToolRunnerError(f"Timeout must be greater than zero, got {candidate}")
    return candidate


def _build_tool_argv(
    installed: InstalledAppInfo, tool: AppTool, extra_argv: Sequence[str]
) -> tuple[str, ...]:
    resolved: list[str] = []
    for idx, raw_arg in enumerate(tool.argv):
        arg = _require_safe_arg(raw_arg, context=f"tool {tool.id} argv[{idx}]")
        resolved.append(
            _resolve_manifest_arg(installed.paths.bundle_root, arg, index=idx)
        )
    for idx, raw_arg in enumerate(extra_argv):
        resolved.append(_require_safe_arg(raw_arg, context=f"extra argv[{idx}]"))
    return tuple(resolved)


def _require_safe_arg(raw_arg: str, *, context: str) -> str:
    if not isinstance(raw_arg, str):
        raise AppToolRunnerError(f"{context} must be a string")
    if not raw_arg:
        raise AppToolRunnerError(f"{context} must not be empty")
    if "\x00" in raw_arg:
        raise AppToolRunnerError(f"{context} must not contain NUL bytes")
    return raw_arg


def _resolve_manifest_arg(bundle_root: Path, arg: str, *, index: int) -> str:
    if index == 0:
        return arg
    if arg.startswith("-"):
        return arg
    if os.path.isabs(arg):
        raise AppToolRunnerError(
            f"tool argv[{index}] must not use absolute paths: {arg!r}"
        )
    path_candidate = _maybe_bundle_relative_path(arg)
    if path_candidate is None:
        return arg
    bundle_path = bundle_root.joinpath(*path_candidate.parts)
    if not bundle_path.exists():
        raise AppToolRunnerError(
            f"tool argv[{index}] references missing bundle path: {arg!r}"
        )
    return str(bundle_path.resolve())


def _maybe_bundle_relative_path(arg: str):
    if "/" not in arg and not arg.startswith("."):
        return None
    try:
        return validate_app_path(arg)
    except AppPathError as exc:
        raise AppToolRunnerError(
            f"invalid bundle-relative argv path {arg!r}: {exc}"
        ) from exc


def _build_tool_env(
    *,
    installed: InstalledAppInfo,
    repo_root: Path,
    workspace_root: Path,
    flow_run_id: Optional[str],
    ticket_id: Optional[str],
    ticket_path: Optional[Path],
    hook_point: Optional[str],
) -> dict[str, str]:
    env = subprocess_env()
    env.update(
        {
            "CAR_REPO_ROOT": str(repo_root),
            "CAR_WORKSPACE_ROOT": str(workspace_root),
            "CAR_APP_ID": installed.app_id,
            "CAR_APP_VERSION": installed.app_version,
            "CAR_APP_ROOT": str(installed.paths.app_root.resolve()),
            "CAR_APP_BUNDLE_DIR": str(installed.paths.bundle_root.resolve()),
            "CAR_APP_STATE_DIR": str(installed.paths.state_root.resolve()),
            "CAR_APP_ARTIFACT_DIR": str(installed.paths.artifacts_root.resolve()),
            "CAR_APP_LOG_DIR": str(installed.paths.logs_root.resolve()),
        }
    )
    optional_values: Mapping[str, Optional[str]] = {
        "CAR_FLOW_RUN_ID": flow_run_id,
        "CAR_TICKET_ID": ticket_id,
        "CAR_TICKET_PATH": str(ticket_path.resolve()) if ticket_path else None,
        "CAR_HOOK_POINT": hook_point,
    }
    for key, value in optional_values.items():
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    return env


def _build_run_label(tool_id: str, *, flow_run_id: Optional[str]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = flow_run_id or uuid.uuid4().hex[:8]
    safe_tool_id = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in tool_id)
    return f"{timestamp}-{safe_tool_id}-{suffix}"


def _materialize_bounded_log(source_path: Path, target_path: Path) -> str:
    size = source_path.stat().st_size if source_path.exists() else 0
    if size <= _MAX_LOG_BYTES:
        shutil.copyfile(source_path, target_path)
        return target_path.read_text(encoding="utf-8", errors="replace")

    with source_path.open("rb") as src, target_path.open("wb") as dst:
        head = src.read(_LOG_HEAD_BYTES)
        dst.write(head)
        dst.write(_TRUNCATION_MARKER)
        src.seek(max(0, size - _LOG_TAIL_BYTES))
        dst.write(src.read(_LOG_TAIL_BYTES))
    return target_path.read_text(encoding="utf-8", errors="replace")


def _excerpt_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    if len(normalized) <= _MAX_EXCERPT_CHARS:
        return normalized
    return normalized[-_MAX_EXCERPT_CHARS:].lstrip()


def _collect_declared_outputs(
    candidates,
) -> list[AppToolRunOutput]:
    return [
        AppToolRunOutput(
            kind=candidate.kind,
            label=candidate.label,
            relative_path=candidate.relative_path,
            absolute_path=candidate.absolute_path,
        )
        for candidate in candidates
    ]


def _remove_if_exists(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
