from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from ...core.logging_utils import log_event
from ...core.supervisor_utils import evict_lru_handle_locked, pop_idle_handles_locked
from ...workspace import canonical_workspace_root, workspace_id_for_path
from .client import ApprovalHandler, CodexAppServerClient, NotificationHandler

EnvBuilder = Callable[[Path, str, Path], Dict[str, str]]


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
        auto_restart: Optional[bool] = None,
        request_timeout: Optional[float] = None,
        turn_stall_timeout_seconds: Optional[float] = None,
        turn_stall_poll_interval_seconds: Optional[float] = None,
        turn_stall_recovery_min_interval_seconds: Optional[float] = None,
        max_message_bytes: Optional[int] = None,
        oversize_preview_bytes: Optional[int] = None,
        max_oversize_drain_bytes: Optional[int] = None,
        restart_backoff_initial_seconds: Optional[float] = None,
        restart_backoff_max_seconds: Optional[float] = None,
        restart_backoff_jitter_ratio: Optional[float] = None,
        default_approval_decision: str = "cancel",
        max_handles: Optional[int] = None,
        idle_ttl_seconds: Optional[float] = None,
    ) -> None:
        self._command = [str(arg) for arg in command]
        self._state_root = state_root
        self._env_builder = env_builder
        self._approval_handler = approval_handler
        self._notification_handler = notification_handler
        self._logger = logger or logging.getLogger(__name__)
        disable_restart_env = os.environ.get(
            "CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS"
        )
        if disable_restart_env:
            self._auto_restart = False
        elif auto_restart is None:
            self._auto_restart = True
        else:
            self._auto_restart = auto_restart
        self._request_timeout = request_timeout
        self._turn_stall_timeout_seconds = turn_stall_timeout_seconds
        self._turn_stall_poll_interval_seconds = turn_stall_poll_interval_seconds
        self._turn_stall_recovery_min_interval_seconds = (
            turn_stall_recovery_min_interval_seconds
        )
        self._max_message_bytes = max_message_bytes
        self._oversize_preview_bytes = oversize_preview_bytes
        self._max_oversize_drain_bytes = max_oversize_drain_bytes
        self._restart_backoff_initial_seconds = restart_backoff_initial_seconds
        self._restart_backoff_max_seconds = restart_backoff_max_seconds
        self._restart_backoff_jitter_ratio = restart_backoff_jitter_ratio
        self._default_approval_decision = default_approval_decision
        self._max_handles = max_handles
        self._idle_ttl_seconds = idle_ttl_seconds
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
                log_event(
                    self._logger,
                    logging.INFO,
                    "app_server.handle.closing",
                    reason="close_all",
                    workspace_id=handle.workspace_id,
                    workspace_root=str(handle.workspace_root),
                    last_used_at=handle.last_used_at,
                )
                await handle.client.close()
            except Exception as exc:
                self._logger.debug("Failed to close handle: %s", exc)
                continue

    async def prune_idle(self) -> int:
        handles = await self._pop_idle_handles()
        if not handles:
            return 0
        closed = 0
        for handle in handles:
            try:
                log_event(
                    self._logger,
                    logging.INFO,
                    "app_server.handle.pruned",
                    reason="idle_ttl",
                    workspace_id=handle.workspace_id,
                    workspace_root=str(handle.workspace_root),
                    idle_ttl_seconds=self._idle_ttl_seconds,
                    last_used_at=handle.last_used_at,
                )
                await handle.client.close()
                closed += 1
            except Exception as exc:
                self._logger.debug("Failed to prune handle: %s", exc)
                continue
        return closed

    async def _ensure_handle(
        self, workspace_id: str, workspace_root: Path
    ) -> AppServerHandle:
        handles_to_close: list[AppServerHandle] = []
        evicted_id: Optional[str] = None
        async with self._lock:
            existing = self._handles.get(workspace_id)
            if existing is not None:
                existing.last_used_at = time.monotonic()
                return existing
            handles_to_close.extend(self._pop_idle_handles_locked())
            evicted = self._evict_lru_handle_locked()
            if evicted is not None:
                evicted_id = evicted.workspace_id
                handles_to_close.append(evicted)
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
                turn_stall_timeout_seconds=self._turn_stall_timeout_seconds,
                turn_stall_poll_interval_seconds=self._turn_stall_poll_interval_seconds,
                turn_stall_recovery_min_interval_seconds=self._turn_stall_recovery_min_interval_seconds,
                max_message_bytes=self._max_message_bytes,
                oversize_preview_bytes=self._oversize_preview_bytes,
                max_oversize_drain_bytes=self._max_oversize_drain_bytes,
                restart_backoff_initial_seconds=self._restart_backoff_initial_seconds,
                restart_backoff_max_seconds=self._restart_backoff_max_seconds,
                restart_backoff_jitter_ratio=self._restart_backoff_jitter_ratio,
                notification_handler=self._notification_handler,
                logger=self._logger,
            )
            handle = AppServerHandle(
                workspace_id=workspace_id,
                workspace_root=workspace_root,
                client=client,
                start_lock=asyncio.Lock(),
                last_used_at=time.monotonic(),
            )
            self._handles[workspace_id] = handle
        for handle in handles_to_close:
            try:
                reason = (
                    "max_handles" if handle.workspace_id == evicted_id else "idle_ttl"
                )
                log_event(
                    self._logger,
                    logging.INFO,
                    "app_server.handle.closing",
                    reason=reason,
                    workspace_id=handle.workspace_id,
                    workspace_root=str(handle.workspace_root),
                    idle_ttl_seconds=self._idle_ttl_seconds,
                    max_handles=self._max_handles,
                    last_used_at=handle.last_used_at,
                )
                await handle.client.close()
            except Exception as exc:
                self._logger.debug("Failed to close handle: %s", exc)
                continue
        return handle

    async def _ensure_started(self, handle: AppServerHandle) -> None:
        async with handle.start_lock:
            if handle.started:
                return
            await handle.client.start()
            handle.started = True

    async def _pop_idle_handles(self) -> list[AppServerHandle]:
        async with self._lock:
            return self._pop_idle_handles_locked()

    def _pop_idle_handles_locked(self) -> list[AppServerHandle]:
        return pop_idle_handles_locked(
            self._handles,
            self._idle_ttl_seconds,
            self._logger,
            "app_server",
            last_used_at_getter=lambda h: h.last_used_at,
        )

    def _evict_lru_handle_locked(self) -> Optional[AppServerHandle]:
        return evict_lru_handle_locked(
            self._handles,
            self._max_handles,
            self._logger,
            "app_server",
            last_used_at_getter=lambda h: h.last_used_at or 0.0,
        )
