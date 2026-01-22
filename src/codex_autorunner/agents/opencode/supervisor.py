from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import httpx

from ...core.logging_utils import log_event
from ...core.supervisor_utils import evict_lru_handle_locked, pop_idle_handles_locked
from ...core.utils import infer_home_from_workspace, subprocess_env
from ...workspace import canonical_workspace_root, workspace_id_for_path
from .client import OpenCodeClient

_LISTENING_RE = re.compile(r"listening on (https?://[^\s]+)")


class OpenCodeSupervisorError(Exception):
    pass


@dataclass
class OpenCodeHandle:
    workspace_id: str
    workspace_root: Path
    process: Optional[asyncio.subprocess.Process]
    client: Optional[OpenCodeClient]
    base_url: Optional[str]
    health_info: Optional[dict[str, Any]]
    version: Optional[str]
    openapi_spec: Optional[dict[str, Any]]
    start_lock: asyncio.Lock
    stdout_task: Optional[asyncio.Task[None]] = None
    started: bool = False
    last_used_at: float = 0.0
    active_turns: int = 0


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
        base_env: Optional[Mapping[str, str]] = None,
        base_url: Optional[str] = None,
        subagent_models: Optional[Mapping[str, str]] = None,
        session_stall_timeout_seconds: Optional[float] = None,
    ) -> None:
        self._command = [str(arg) for arg in command]
        self._logger = logger or logging.getLogger(__name__)
        self._request_timeout = request_timeout
        self._max_handles = max_handles
        self._idle_ttl_seconds = idle_ttl_seconds
        self._session_stall_timeout_seconds = session_stall_timeout_seconds
        if password and not username:
            username = "opencode"
        self._auth: Optional[tuple[str, str]] = (
            (username, password) if password and username else None
        )
        self._base_env = base_env
        self._base_url = base_url
        self._subagent_models = subagent_models or {}
        self._handles: dict[str, OpenCodeHandle] = {}
        self._lock: Optional[asyncio.Lock] = None

    @property
    def session_stall_timeout_seconds(self) -> Optional[float]:
        return self._session_stall_timeout_seconds

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
        async with self._get_lock():
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

    async def mark_turn_started(self, workspace_root: Path) -> None:
        canonical_root = canonical_workspace_root(workspace_root)
        workspace_id = workspace_id_for_path(canonical_root)
        async with self._get_lock():
            handle = self._handles.get(workspace_id)
            if handle is None:
                return
            handle.active_turns += 1
            handle.last_used_at = time.monotonic()

    async def mark_turn_finished(self, workspace_root: Path) -> None:
        canonical_root = canonical_workspace_root(workspace_root)
        workspace_id = workspace_id_for_path(canonical_root)
        async with self._get_lock():
            handle = self._handles.get(workspace_id)
            if handle is None:
                return
            if handle.active_turns > 0:
                handle.active_turns -= 1
            handle.last_used_at = time.monotonic()

    async def ensure_subagent_config(
        self,
        workspace_root: Path,
        agent_id: str,
        model: Optional[str] = None,
    ) -> None:
        """Ensure subagent agent config file exists with correct model.

        Args:
            workspace_root: Path to workspace root
            agent_id: Agent ID to configure (e.g., "subagent")
            model: Optional model override (defaults to subagent_models if not provided)
        """
        if model is None:
            model = self._subagent_models.get(agent_id)
        if not model:
            return

        from .agent_config import ensure_agent_config

        await ensure_agent_config(
            workspace_root=workspace_root,
            agent_id=agent_id,
            model=model,
            title=agent_id,
            description=f"Subagent for {agent_id} tasks",
        )

    async def _close_handle(self, handle: OpenCodeHandle, *, reason: str) -> None:
        try:
            idle_seconds = None
            if reason == "idle_ttl" and handle.last_used_at:
                idle_seconds = max(0.0, time.monotonic() - handle.last_used_at)
            log_event(
                self._logger,
                logging.INFO,
                "opencode.handle.closing",
                reason=reason,
                workspace_id=handle.workspace_id,
                workspace_root=str(handle.workspace_root),
                last_used_at=handle.last_used_at,
                idle_seconds=idle_seconds,
                active_turns=handle.active_turns,
                returncode=(
                    handle.process.returncode if handle.process is not None else None
                ),
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
        async with self._get_lock():
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
                health_info=None,
                version=None,
                openapi_spec=None,
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
            if self._base_url:
                await self._ensure_started_base_url(handle)
            else:
                await self._start_process(handle)

    async def _ensure_started_base_url(self, handle: OpenCodeHandle) -> None:
        base_url = self._base_url
        handle.health_info = None
        handle.version = None

        if not base_url:
            return

        try:
            health_url = f"{base_url.rstrip('/')}/global/health"
            async with httpx.AsyncClient(
                timeout=self._request_timeout or 10.0
            ) as client:
                response = await client.get(health_url)
                response.raise_for_status()

            try:
                handle.health_info = response.json() if response.content else {}
            except Exception:
                handle.health_info = {}

            handle.version = str(handle.health_info.get("version", "unknown"))

            log_event(
                self._logger,
                logging.INFO,
                "opencode.health_check",
                base_url=base_url,
                version=handle.version,
                health_info=bool(handle.health_info),
                exc=None,
            )
            handle.base_url = base_url
            handle.client = OpenCodeClient(
                base_url,
                auth=self._auth,
                timeout=self._request_timeout,
                logger=self._logger,
            )
            try:
                handle.openapi_spec = await handle.client.fetch_openapi_spec()
                log_event(
                    self._logger,
                    logging.INFO,
                    "opencode.openapi.fetched",
                    base_url=base_url,
                    endpoints=(
                        len(handle.openapi_spec.get("paths", {}))
                        if isinstance(handle.openapi_spec, dict)
                        else 0
                    ),
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "opencode.openapi.fetch_failed",
                    base_url=base_url,
                    exc=exc,
                )
                handle.openapi_spec = {}
            handle.started = True
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "opencode.health_check.failed",
                base_url=base_url,
                exc=exc,
            )
            raise OpenCodeSupervisorError(
                f"OpenCode health check failed: {exc}"
            ) from exc

    async def _start_process(self, handle: OpenCodeHandle) -> None:
        if self._base_url:
            handle.health_info = {}
            handle.version = "external"
            log_event(
                self._logger,
                logging.INFO,
                "opencode.external_mode",
                base_url=self._base_url,
            )
            return

        env = self._build_opencode_env(handle.workspace_root)
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
                logger=self._logger,
            )
            try:
                handle.openapi_spec = await handle.client.fetch_openapi_spec()
                log_event(
                    self._logger,
                    logging.INFO,
                    "opencode.openapi.fetched",
                    base_url=base_url,
                    endpoints=(
                        len(handle.openapi_spec.get("paths", {}))
                        if isinstance(handle.openapi_spec, dict)
                        else 0
                    ),
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "opencode.openapi.fetch_failed",
                    base_url=base_url,
                    exc=exc,
                )
                handle.openapi_spec = {}
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

    def _build_opencode_env(self, workspace_root: Path) -> dict[str, str]:
        env = subprocess_env(base_env=self._base_env)
        inferred_home = infer_home_from_workspace(workspace_root)
        if inferred_home is None:
            return env
        inferred_auth = inferred_home / ".local" / "share" / "opencode" / "auth.json"
        if not inferred_auth.exists():
            return env
        env_auth = self._opencode_auth_path_for_env(env)
        if env_auth is not None and env_auth.exists():
            return env
        env["HOME"] = str(inferred_home)
        env["XDG_DATA_HOME"] = str(inferred_home / ".local" / "share")
        log_event(
            self._logger,
            logging.INFO,
            "opencode.env.inferred",
            workspace_root=str(workspace_root),
            inferred_home=str(inferred_home),
            auth_path=str(inferred_auth),
        )
        return env

    def _opencode_auth_path_for_env(self, env: dict[str, str]) -> Optional[Path]:
        data_home = env.get("XDG_DATA_HOME")
        if not data_home:
            home = env.get("HOME")
            if not home:
                return None
            data_home = str(Path(home) / ".local" / "share")
        return Path(data_home) / "opencode" / "auth.json"

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
        async with self._get_lock():
            return self._pop_idle_handles_locked()

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _pop_idle_handles_locked(self) -> list[OpenCodeHandle]:
        return pop_idle_handles_locked(
            self._handles,
            self._idle_ttl_seconds,
            self._logger,
            "opencode",
            last_used_at_getter=lambda h: h.last_used_at,
            should_skip_prune=lambda h: h.active_turns > 0,
        )

    def _evict_lru_handle_locked(self) -> Optional[OpenCodeHandle]:
        return evict_lru_handle_locked(
            self._handles,
            self._max_handles,
            self._logger,
            "opencode",
            last_used_at_getter=lambda h: h.last_used_at or 0.0,
        )


__all__ = ["OpenCodeHandle", "OpenCodeSupervisor", "OpenCodeSupervisorError"]
