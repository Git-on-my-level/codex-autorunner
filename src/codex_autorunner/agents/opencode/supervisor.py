from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from ...core.logging_utils import log_event
from ...workspace import canonical_workspace_root, workspace_id_for_path
from .client import OpenCodeClient

_LISTENING_RE = re.compile(r"listening on (http://[^\s]+)")


class OpenCodeSupervisorError(Exception):
    pass


@dataclass
class OpenCodeHandle:
    workspace_id: str
    workspace_root: Path
    process: Optional[asyncio.subprocess.Process]
    client: Optional[OpenCodeClient]
    base_url: Optional[str]
    start_lock: asyncio.Lock
    stdout_task: Optional[asyncio.Task[None]] = None
    started: bool = False
    last_used_at: float = 0.0


class OpenCodeSupervisor:
    def __init__(
        self,
        command: Sequence[str],
        *,
        logger: Optional[logging.Logger] = None,
        request_timeout: Optional[float] = None,
        max_handles: Optional[int] = None,
        idle_ttl_seconds: Optional[float] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self._command = [str(arg) for arg in command]
        self._logger = logger or logging.getLogger(__name__)
        self._request_timeout = request_timeout
        self._max_handles = max_handles
        self._idle_ttl_seconds = idle_ttl_seconds
        self._auth = (username, password) if username and password else None
        self._handles: dict[str, OpenCodeHandle] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, workspace_root: Path) -> OpenCodeClient:
        canonical_root = canonical_workspace_root(workspace_root)
        workspace_id = workspace_id_for_path(canonical_root)
        handle = await self._ensure_handle(workspace_id, canonical_root)
        await self._ensure_started(handle)
        handle.last_used_at = time.monotonic()
        if handle.client is None:
            raise OpenCodeSupervisorError("OpenCode client not initialized")
        return handle.client

    async def close_all(self) -> None:
        async with self._lock:
            handles = list(self._handles.values())
            self._handles = {}
        for handle in handles:
            await self._close_handle(handle, reason="close_all")

    async def prune_idle(self) -> int:
        handles = await self._pop_idle_handles()
        if not handles:
            return 0
        closed = 0
        for handle in handles:
            await self._close_handle(handle, reason="idle_ttl")
            closed += 1
        return closed

    async def _close_handle(self, handle: OpenCodeHandle, *, reason: str) -> None:
        try:
            log_event(
                self._logger,
                logging.INFO,
                "opencode.handle.closing",
                reason=reason,
                workspace_id=handle.workspace_id,
                workspace_root=str(handle.workspace_root),
                last_used_at=handle.last_used_at,
            )
            if handle.client is not None:
                await handle.client.close()
        finally:
            stdout_task = handle.stdout_task
            handle.stdout_task = None
            if stdout_task is not None and not stdout_task.done():
                stdout_task.cancel()
                try:
                    await stdout_task
                except asyncio.CancelledError:
                    pass
            if handle.process and handle.process.returncode is None:
                handle.process.terminate()
                try:
                    await asyncio.wait_for(handle.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    handle.process.kill()
                    await handle.process.wait()

    async def _ensure_handle(
        self, workspace_id: str, workspace_root: Path
    ) -> OpenCodeHandle:
        handles_to_close: list[OpenCodeHandle] = []
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
            handle = OpenCodeHandle(
                workspace_id=workspace_id,
                workspace_root=workspace_root,
                process=None,
                client=None,
                base_url=None,
                start_lock=asyncio.Lock(),
                stdout_task=None,
                last_used_at=time.monotonic(),
            )
            self._handles[workspace_id] = handle
        for handle in handles_to_close:
            await self._close_handle(
                handle,
                reason=(
                    "max_handles" if handle.workspace_id == evicted_id else "idle_ttl"
                ),
            )
        return handle

    async def _ensure_started(self, handle: OpenCodeHandle) -> None:
        async with handle.start_lock:
            if handle.started and handle.process and handle.process.returncode is None:
                return
            await self._start_process(handle)

    async def _start_process(self, handle: OpenCodeHandle) -> None:
        env = dict(os.environ)
        process = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=handle.workspace_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        handle.process = process
        try:
            base_url = await self._read_base_url(process)
            if not base_url:
                raise OpenCodeSupervisorError(
                    "OpenCode server failed to report base URL"
                )
            handle.base_url = base_url
            handle.client = OpenCodeClient(
                base_url,
                auth=self._auth,
                timeout=self._request_timeout,
            )
            self._start_stdout_drain(handle)
            handle.started = True
        except Exception:
            handle.started = False
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise

    def _start_stdout_drain(self, handle: OpenCodeHandle) -> None:
        """
        Ensure we continuously drain the subprocess stdout pipe.

        OpenCode often logs after startup; if stdout is piped but never drained,
        the OS pipe buffer can fill and stall the child process.
        """
        process = handle.process
        if process is None or process.stdout is None:
            return
        existing = handle.stdout_task
        if existing is not None and not existing.done():
            return
        handle.stdout_task = asyncio.create_task(self._drain_stdout(handle))

    async def _drain_stdout(self, handle: OpenCodeHandle) -> None:
        process = handle.process
        if process is None or process.stdout is None:
            return
        stream = process.stdout
        debug_logs = self._logger.isEnabledFor(logging.DEBUG)
        while True:
            line = await stream.readline()
            if not line:
                break
            if not debug_logs:
                continue
            decoded = line.decode("utf-8", errors="ignore").rstrip()
            if not decoded:
                continue
            log_event(
                self._logger,
                logging.DEBUG,
                "opencode.stdout",
                workspace_id=handle.workspace_id,
                workspace_root=str(handle.workspace_root),
                line=decoded[:2000],
            )

    async def _read_base_url(
        self, process: asyncio.subprocess.Process, timeout: float = 20.0
    ) -> Optional[str]:
        if process.stdout is None:
            return None
        start = time.monotonic()
        while True:
            if process.returncode is not None:
                raise OpenCodeSupervisorError("OpenCode server exited before ready")
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                return None
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=timeout - elapsed
                )
            except asyncio.TimeoutError:
                return None
            if not line:
                continue
            decoded = line.decode("utf-8", errors="ignore").strip()
            match = _LISTENING_RE.search(decoded)
            if match:
                return match.group(1)

    async def _pop_idle_handles(self) -> list[OpenCodeHandle]:
        async with self._lock:
            return self._pop_idle_handles_locked()

    def _pop_idle_handles_locked(self) -> list[OpenCodeHandle]:
        if not self._idle_ttl_seconds or self._idle_ttl_seconds <= 0:
            return []
        cutoff = time.monotonic() - self._idle_ttl_seconds
        stale: list[OpenCodeHandle] = []
        for handle in list(self._handles.values()):
            if handle.last_used_at and handle.last_used_at < cutoff:
                self._handles.pop(handle.workspace_id, None)
                stale.append(handle)
        return stale

    def _evict_lru_handle_locked(self) -> Optional[OpenCodeHandle]:
        if not self._max_handles or self._max_handles <= 0:
            return None
        if len(self._handles) < self._max_handles:
            return None
        lru_handle = min(
            self._handles.values(),
            key=lambda handle: handle.last_used_at or 0.0,
        )
        log_event(
            self._logger,
            logging.INFO,
            "opencode.handle.evicted",
            reason="max_handles",
            workspace_id=lru_handle.workspace_id,
            workspace_root=str(lru_handle.workspace_root),
            max_handles=self._max_handles,
            handle_count=len(self._handles),
            last_used_at=lru_handle.last_used_at,
        )
        self._handles.pop(lru_handle.workspace_id, None)
        return lru_handle


__all__ = ["OpenCodeHandle", "OpenCodeSupervisor", "OpenCodeSupervisorError"]
