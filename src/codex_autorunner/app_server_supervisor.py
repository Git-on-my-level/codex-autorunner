from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, Sequence

from .app_server_client import CodexAppServerClient
from .workspace import canonical_workspace_root, workspace_id_for_path

EnvBuilder = Callable[[Path, str, Path], Dict[str, str]]
ApprovalHandler = Callable[[Dict[str, object]], Awaitable[object]]
NotificationHandler = Callable[[Dict[str, object]], Awaitable[None]]


@dataclass
class AppServerHandle:
    workspace_id: str
    workspace_root: Path
    client: CodexAppServerClient
    start_lock: asyncio.Lock
    started: bool = False
    last_used_at: float = 0.0


class WorkspaceAppServerSupervisor:
    def __init__(
        self,
        command: Sequence[str],
        *,
        state_root: Path,
        env_builder: EnvBuilder,
        approval_handler: Optional[ApprovalHandler] = None,
        notification_handler: Optional[NotificationHandler] = None,
        logger: Optional[logging.Logger] = None,
        auto_restart: bool = True,
        request_timeout: Optional[float] = None,
        default_approval_decision: str = "cancel",
    ) -> None:
        self._command = [str(arg) for arg in command]
        self._state_root = state_root
        self._env_builder = env_builder
        self._approval_handler = approval_handler
        self._notification_handler = notification_handler
        self._logger = logger or logging.getLogger(__name__)
        self._auto_restart = auto_restart
        self._request_timeout = request_timeout
        self._default_approval_decision = default_approval_decision
        self._handles: dict[str, AppServerHandle] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, workspace_root: Path) -> CodexAppServerClient:
        canonical_root = canonical_workspace_root(workspace_root)
        workspace_id = workspace_id_for_path(canonical_root)
        handle = await self._ensure_handle(workspace_id, canonical_root)
        await self._ensure_started(handle)
        handle.last_used_at = time.monotonic()
        return handle.client

    async def close_all(self) -> None:
        async with self._lock:
            handles = list(self._handles.values())
            self._handles = {}
        for handle in handles:
            try:
                await handle.client.close()
            except Exception:
                continue

    async def _ensure_handle(
        self, workspace_id: str, workspace_root: Path
    ) -> AppServerHandle:
        async with self._lock:
            existing = self._handles.get(workspace_id)
            if existing is not None:
                return existing
            state_dir = self._state_root / workspace_id
            env = self._env_builder(workspace_root, workspace_id, state_dir)
            client = CodexAppServerClient(
                self._command,
                cwd=workspace_root,
                env=env,
                approval_handler=self._approval_handler,
                default_approval_decision=self._default_approval_decision,
                auto_restart=self._auto_restart,
                request_timeout=self._request_timeout,
                notification_handler=self._notification_handler,
                logger=self._logger,
            )
            handle = AppServerHandle(
                workspace_id=workspace_id,
                workspace_root=workspace_root,
                client=client,
                start_lock=asyncio.Lock(),
            )
            self._handles[workspace_id] = handle
            return handle

    async def _ensure_started(self, handle: AppServerHandle) -> None:
        async with handle.start_lock:
            if handle.started:
                return
            await handle.client.start()
            handle.started = True
