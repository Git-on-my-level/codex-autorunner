from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from ...core.config import HubConfig, RepoConfig
from ...core.utils import resolve_executable
from ...workspace import canonical_workspace_root
from ..acp import ACPPromptHandle, ACPSubprocessSupervisor
from ..managed_runtime import RuntimeLaunchMode, RuntimePreflightResult
from ..types import TerminalTurnResult

_logger = logging.getLogger(__name__)

HERMES_RUNTIME_ID = "hermes"
HERMES_ACP_COMMAND = "acp"
HERMES_SESSION_STATE_FILE_MODE: RuntimeLaunchMode = "session_state_file"


class HermesSupervisorError(RuntimeError):
    pass


@dataclass(frozen=True)
class HermesSessionHandle:
    session_id: str
    title: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


class HermesSupervisor:
    """Thin Hermes wrapper over the generic ACP subprocess supervisor."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        base_env: Optional[Mapping[str, str]] = None,
        initialize_params: Optional[dict[str, Any]] = None,
        request_timeout: Optional[float] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if not command:
            raise ValueError("Hermes command must not be empty")
        self._logger = logger or logging.getLogger(__name__)
        self._acp = ACPSubprocessSupervisor(
            command,
            base_env=dict(base_env or {}),
            initialize_params=dict(initialize_params or {}),
            request_timeout=request_timeout,
            logger=self._logger,
        )
        self._turn_handles: dict[tuple[str, str], ACPPromptHandle] = {}
        self._session_turns: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def ensure_ready(self, workspace_root: Path) -> None:
        await self._acp.get_client(workspace_root)

    async def create_session(
        self,
        workspace_root: Path,
        *,
        title: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> HermesSessionHandle:
        session = await self._acp.create_session(
            workspace_root,
            title=title,
            metadata=metadata,
        )
        return HermesSessionHandle(
            session_id=session.session_id,
            title=session.title,
            raw=dict(session.raw),
        )

    async def resume_session(
        self,
        workspace_root: Path,
        session_id: str,
    ) -> HermesSessionHandle:
        session = await self._acp.load_session(workspace_root, session_id)
        return HermesSessionHandle(
            session_id=session.session_id,
            title=session.title,
            raw=dict(session.raw),
        )

    async def list_sessions(self, workspace_root: Path) -> list[HermesSessionHandle]:
        sessions = await self._acp.list_sessions(workspace_root)
        return [
            HermesSessionHandle(
                session_id=session.session_id,
                title=session.title,
                raw=dict(session.raw),
            )
            for session in sessions
        ]

    async def start_turn(
        self,
        workspace_root: Path,
        session_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
    ) -> str:
        handle = await self._acp.start_prompt(
            workspace_root,
            session_id,
            prompt,
            model=_normalize_optional_text(model),
        )
        workspace = _workspace_key(workspace_root)
        async with self._lock:
            previous_turn_id = self._session_turns.get((workspace, session_id))
            if previous_turn_id:
                self._turn_handles.pop((workspace, previous_turn_id), None)
            self._turn_handles[(workspace, handle.turn_id)] = handle
            self._session_turns[(workspace, session_id)] = handle.turn_id
        return handle.turn_id

    async def wait_for_turn(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        resolved_turn_id = await self._resolve_turn_id(
            workspace_root,
            session_id,
            turn_id,
        )
        handle = await self._require_turn_handle(workspace_root, resolved_turn_id)
        result = await handle.wait(timeout=timeout)
        errors = [result.error_message] if result.error_message else []
        raw_events = [
            dict(getattr(event, "raw_notification", {}) or {})
            for event in result.events
            if isinstance(getattr(event, "raw_notification", None), dict)
        ]
        return TerminalTurnResult(
            status=result.status,
            assistant_text=result.final_output,
            errors=errors,
            raw_events=raw_events,
        )

    async def stream_turn_events(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        resolved_turn_id = await self._resolve_turn_id(
            workspace_root,
            session_id,
            turn_id,
        )
        handle = await self._require_turn_handle(workspace_root, resolved_turn_id)
        async for event in handle.events():
            raw_notification = getattr(event, "raw_notification", None)
            if isinstance(raw_notification, dict):
                yield dict(raw_notification)

    async def interrupt_turn(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: Optional[str],
    ) -> None:
        resolved_turn_id = await self._resolve_turn_id(
            workspace_root,
            session_id,
            turn_id,
        )
        await self._acp.cancel_prompt(workspace_root, session_id, resolved_turn_id)

    async def close_workspace(self, workspace_root: Path) -> None:
        workspace = _workspace_key(workspace_root)
        async with self._lock:
            self._turn_handles = {
                key: value
                for key, value in self._turn_handles.items()
                if key[0] != workspace
            }
            self._session_turns = {
                key: value
                for key, value in self._session_turns.items()
                if key[0] != workspace
            }
        await self._acp.close_workspace(workspace_root)

    async def close_all(self) -> None:
        async with self._lock:
            self._turn_handles.clear()
            self._session_turns.clear()
        await self._acp.close_all()

    async def _resolve_turn_id(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: Optional[str],
    ) -> str:
        normalized_turn_id = _normalize_optional_text(turn_id)
        if normalized_turn_id:
            return normalized_turn_id
        workspace = _workspace_key(workspace_root)
        async with self._lock:
            tracked_turn_id = self._session_turns.get((workspace, session_id))
        if tracked_turn_id:
            return tracked_turn_id
        raise HermesSupervisorError(
            f"No active Hermes turn tracked for session '{session_id}'"
        )

    async def _require_turn_handle(
        self,
        workspace_root: Path,
        turn_id: str,
    ) -> ACPPromptHandle:
        workspace = _workspace_key(workspace_root)
        async with self._lock:
            handle = self._turn_handles.get((workspace, turn_id))
        if handle is None:
            raise HermesSupervisorError(f"Unknown Hermes turn '{turn_id}'")
        return handle


def _normalize_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _workspace_key(workspace_root: Path) -> str:
    return str(canonical_workspace_root(workspace_root))


def build_hermes_supervisor_from_config(
    config: RepoConfig | HubConfig,
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[HermesSupervisor]:
    try:
        binary = config.agent_binary("hermes").strip()
    except Exception:
        return None
    if not binary:
        return None
    return HermesSupervisor([binary, HERMES_ACP_COMMAND], logger=logger)


def hermes_binary_available(config: Optional[RepoConfig | HubConfig]) -> bool:
    if config is None:
        return False
    try:
        binary = config.agent_binary("hermes").strip()
    except Exception:
        return False
    if not binary:
        return False
    return resolve_executable(binary) is not None


def hermes_runtime_preflight(
    config: Optional[RepoConfig | HubConfig],
) -> RuntimePreflightResult:
    if config is None:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix="Set agents.hermes.binary in the repo or hub config.",
        )
    try:
        binary = config.agent_binary("hermes").strip()
    except Exception:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix="Set agents.hermes.binary in the repo or hub config.",
        )
    if not binary:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="Hermes binary is not configured.",
            fix="Set agents.hermes.binary in the repo or hub config.",
        )
    binary_path = resolve_executable(binary)
    if binary_path is None:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message=f"Hermes binary '{binary}' is not available on PATH.",
            fix="Install Hermes or update agents.hermes.binary to a working executable path.",
        )
    import subprocess

    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        version = None
    try:
        result = subprocess.run(
            [binary, HERMES_ACP_COMMAND, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode not in (0, 1) or not result.stdout:
            return RuntimePreflightResult(
                runtime_id=HERMES_RUNTIME_ID,
                status="incompatible",
                version=version,
                launch_mode=None,
                message="Hermes ACP mode is not supported by this binary.",
                fix="Install a Hermes build that supports the `hermes acp` command.",
            )
        help_text = result.stdout + result.stderr
        if "--session-state-file" not in help_text:
            return RuntimePreflightResult(
                runtime_id=HERMES_RUNTIME_ID,
                status="incompatible",
                version=version,
                launch_mode=None,
                message="Hermes does not advertise --session-state-file support in `hermes acp --help`.",
                fix="Install a Hermes build that supports durable session state file launches.",
            )
    except Exception as exc:
        return RuntimePreflightResult(
            runtime_id=HERMES_RUNTIME_ID,
            status="incompatible",
            version=version,
            launch_mode=None,
            message=f"Failed to probe Hermes ACP support: {exc}",
            fix="Ensure Hermes binary is executable and supports `hermes acp` command.",
        )
    return RuntimePreflightResult(
        runtime_id=HERMES_RUNTIME_ID,
        status="ready",
        version=version,
        launch_mode=HERMES_SESSION_STATE_FILE_MODE,
        message=f"Hermes {version or 'version unknown'} supports durable ACP sessions.",
    )


__all__ = [
    "HERMES_ACP_COMMAND",
    "HERMES_RUNTIME_ID",
    "HERMES_SESSION_STATE_FILE_MODE",
    "HermesSessionHandle",
    "HermesSupervisor",
    "HermesSupervisorError",
    "build_hermes_supervisor_from_config",
    "hermes_binary_available",
    "hermes_runtime_preflight",
]
