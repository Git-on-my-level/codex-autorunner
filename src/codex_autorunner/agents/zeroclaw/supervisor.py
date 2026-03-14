from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Mapping, Optional, Sequence

from ...core.config import Config
from ...core.utils import resolve_executable
from ...workspace import canonical_workspace_root, workspace_id_for_path
from ..types import TerminalTurnResult
from .client import ZeroClawClient, split_zeroclaw_model


class ZeroClawSupervisorError(RuntimeError):
    """Raised when the ZeroClaw supervisor cannot satisfy a session request."""


@dataclass
class ZeroClawSessionHandle:
    session_id: str
    workspace_id: str
    workspace_root: Path
    client: ZeroClawClient
    title: Optional[str]
    created_at: float
    last_used_at: float


class ZeroClawSupervisor:
    """Wrapper-managed volatile session owner for interactive ZeroClaw agent runs."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        logger: Optional[logging.Logger] = None,
        base_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        if not command:
            raise ValueError("ZeroClaw command must not be empty")
        self._command = [str(part) for part in command]
        self._logger = logger or logging.getLogger(__name__)
        self._base_env = base_env
        self._handles: dict[str, ZeroClawSessionHandle] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        workspace_root: Path,
        *,
        title: Optional[str] = None,
    ) -> str:
        canonical_root = canonical_workspace_root(workspace_root)
        session_id = f"zeroclaw-session-{uuid.uuid4()}"
        handle = ZeroClawSessionHandle(
            session_id=session_id,
            workspace_id=workspace_id_for_path(canonical_root),
            workspace_root=canonical_root,
            client=ZeroClawClient(
                self._command,
                workspace_root=canonical_root,
                logger=self._logger,
                base_env=self._base_env,
            ),
            title=title,
            created_at=time.monotonic(),
            last_used_at=time.monotonic(),
        )
        async with self._lock:
            self._handles[session_id] = handle
        return session_id

    async def list_sessions(self, workspace_root: Path) -> list[str]:
        canonical_root = canonical_workspace_root(workspace_root)
        async with self._lock:
            return [
                handle.session_id
                for handle in self._handles.values()
                if handle.workspace_root == canonical_root
            ]

    async def attach_session(self, workspace_root: Path, session_id: str) -> str:
        handle = await self._get_handle(workspace_root, session_id)
        handle.last_used_at = time.monotonic()
        return handle.session_id

    async def start_turn(
        self,
        workspace_root: Path,
        session_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
    ) -> str:
        handle = await self._get_handle(workspace_root, session_id)
        provider, model_name = split_zeroclaw_model(model)
        handle.last_used_at = time.monotonic()
        return await handle.client.start_turn(
            prompt,
            provider=provider,
            model=model_name,
        )

    async def wait_for_turn(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        handle = await self._get_handle(workspace_root, session_id)
        handle.last_used_at = time.monotonic()
        return await handle.client.wait_for_turn(turn_id, timeout=timeout)

    async def stream_turn_events(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
    ) -> AsyncIterator[str]:
        handle = await self._get_handle(workspace_root, session_id)
        handle.last_used_at = time.monotonic()
        async for event in handle.client.stream_turn_events(turn_id):
            yield event

    async def close_all(self) -> None:
        async with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            await handle.client.close()

    async def _get_handle(
        self, workspace_root: Path, session_id: str
    ) -> ZeroClawSessionHandle:
        canonical_root = canonical_workspace_root(workspace_root)
        async with self._lock:
            handle = self._handles.get(session_id)
        if handle is None:
            raise ZeroClawSupervisorError(f"Unknown ZeroClaw session '{session_id}'")
        if handle.workspace_root != canonical_root:
            raise ZeroClawSupervisorError(
                f"ZeroClaw session '{session_id}' is bound to a different workspace"
            )
        return handle


def build_zeroclaw_supervisor_from_config(
    config: Config,
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[ZeroClawSupervisor]:
    try:
        binary = config.agent_binary("zeroclaw")
    except Exception:
        return None
    return ZeroClawSupervisor([binary], logger=logger)


def zeroclaw_binary_available(config: Optional[Config]) -> bool:
    if config is None:
        return False
    try:
        binary = config.agent_binary("zeroclaw")
    except Exception:
        return False
    return resolve_executable(binary) is not None


__all__ = [
    "ZeroClawSessionHandle",
    "ZeroClawSupervisor",
    "ZeroClawSupervisorError",
    "build_zeroclaw_supervisor_from_config",
    "zeroclaw_binary_available",
]
