from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence

from ...workspace import canonical_workspace_root
from .client import ACPClient, ACPPromptHandle
from .events import ACPEvent, ACPPermissionRequestEvent
from .protocol import (
    ACPAdvertisedCommand,
    ACPSessionCapabilities,
    ACPSessionDescriptor,
    ACPSessionForkResult,
    ACPSetModelResult,
    ACPSetModeResult,
)

NotificationHandler = Callable[[Path, ACPEvent], Awaitable[None]]
PermissionHandler = Callable[[Path, ACPPermissionRequestEvent], Awaitable[Any]]


@dataclass(frozen=True)
class ACPSupervisorHandleSnapshot:
    runtime_kind: str
    server_scope: str
    handle_id: str
    workspace_root: str
    pid: Optional[int]
    pgid: Optional[int]
    base_url: Optional[str]
    active_prompts: int
    started: bool
    healthy: bool
    last_used_at: float
    state_dir: Optional[str]


class ACPSubprocessSupervisor:
    def __init__(
        self,
        command: Sequence[str],
        *,
        base_env: Optional[dict[str, str]] = None,
        initialize_params: Optional[dict[str, Any]] = None,
        request_timeout: Optional[float] = None,
        notification_handler: Optional[NotificationHandler] = None,
        permission_handler: Optional[PermissionHandler] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._command = [str(part) for part in command]
        self._base_env = dict(base_env or {})
        self._initialize_params = dict(initialize_params or {})
        self._request_timeout = request_timeout
        self._notification_handler = notification_handler
        self._permission_handler = permission_handler
        self._logger = logger or logging.getLogger(__name__)
        self._clients: dict[str, ACPClient] = {}
        self._last_used_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, workspace_root: Path) -> ACPClient:
        canonical_root = canonical_workspace_root(workspace_root)
        key = str(canonical_root)
        async with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = ACPClient(
                    self._command,
                    cwd=canonical_root,
                    env=self._base_env or None,
                    initialize_params=self._initialize_params,
                    request_timeout=self._request_timeout,
                    notification_handler=(
                        None
                        if self._notification_handler is None
                        else _workspace_notification_handler(
                            canonical_root, self._notification_handler
                        )
                    ),
                    permission_handler=(
                        None
                        if self._permission_handler is None
                        else _workspace_permission_handler(
                            canonical_root, self._permission_handler
                        )
                    ),
                    logger=self._logger,
                )
                self._clients[key] = client
            self._last_used_at[key] = asyncio.get_running_loop().time()
        await client.start()
        return client

    async def create_session(
        self,
        workspace_root: Path,
        *,
        title: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ACPSessionDescriptor:
        client = await self.get_client(workspace_root)
        return await client.create_session(
            cwd=str(canonical_workspace_root(workspace_root)),
            title=title,
            metadata=metadata,
        )

    async def load_session(
        self, workspace_root: Path, session_id: str
    ) -> ACPSessionDescriptor:
        client = await self.get_client(workspace_root)
        return await client.load_session(session_id)

    async def list_sessions(self, workspace_root: Path) -> list[ACPSessionDescriptor]:
        client = await self.get_client(workspace_root)
        return await client.list_sessions()

    async def fork_session(
        self,
        workspace_root: Path,
        session_id: str,
        *,
        title: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ACPSessionForkResult:
        client = await self.get_client(workspace_root)
        return await client.fork_session(session_id, title=title, metadata=metadata)

    async def set_session_model(
        self, workspace_root: Path, session_id: str, model_id: str
    ) -> ACPSetModelResult:
        client = await self.get_client(workspace_root)
        return await client.set_session_model(session_id, model_id)

    async def set_session_mode(
        self, workspace_root: Path, session_id: str, mode: str
    ) -> ACPSetModeResult:
        client = await self.get_client(workspace_root)
        return await client.set_session_mode(session_id, mode)

    async def advertised_commands(
        self,
        workspace_root: Path,
    ) -> list[ACPAdvertisedCommand]:
        client = await self.get_client(workspace_root)
        return client.advertised_commands

    async def session_capabilities(
        self,
        workspace_root: Path,
    ) -> ACPSessionCapabilities:
        client = await self.get_client(workspace_root)
        return client.session_capabilities

    async def start_prompt(
        self,
        workspace_root: Path,
        session_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ACPPromptHandle:
        client = await self.get_client(workspace_root)
        return await client.start_prompt(
            session_id,
            prompt,
            model=model,
            metadata=metadata,
        )

    async def prompt_events_snapshot(
        self, workspace_root: Path, turn_id: str
    ) -> tuple[ACPEvent, ...]:
        client = await self.get_client(workspace_root)
        return client.prompt_events_snapshot(turn_id)

    async def cancel_prompt(
        self, workspace_root: Path, session_id: str, turn_id: str
    ) -> Any:
        client = await self.get_client(workspace_root)
        return await client.cancel_prompt(session_id, turn_id)

    async def recover_prompt_completion(
        self,
        workspace_root: Path,
        turn_id: str,
        *,
        final_output: str,
        recovery_source: str = "session_store",
    ) -> bool:
        client = await self.get_client(workspace_root)
        return await client.recover_prompt_completion(
            turn_id,
            final_output=final_output,
            recovery_source=recovery_source,
        )

    async def call_optional(
        self,
        workspace_root: Path,
        method: str,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        client = await self.get_client(workspace_root)
        return await client.call_optional(method, params)

    async def close_workspace(self, workspace_root: Path) -> None:
        canonical_root = canonical_workspace_root(workspace_root)
        key = str(canonical_root)
        async with self._lock:
            client = self._clients.pop(key, None)
            self._last_used_at.pop(key, None)
        if client is not None:
            await client.close()

    async def close_all(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients = {}
            self._last_used_at = {}
        for client in clients:
            await client.close()

    async def lifecycle_snapshot(self) -> tuple[ACPSupervisorHandleSnapshot, ...]:
        async with self._lock:
            return tuple(
                ACPSupervisorHandleSnapshot(
                    runtime_kind="acp",
                    server_scope="workspace",
                    handle_id=workspace_root,
                    workspace_root=workspace_root,
                    pid=_client_pid(client),
                    pgid=_client_pgid(client),
                    base_url=None,
                    active_prompts=_client_active_prompts(client),
                    started=client.initialize_result is not None,
                    healthy=client.initialize_result is not None
                    and _client_returncode(client) is None,
                    last_used_at=self._last_used_at.get(workspace_root, 0.0),
                    state_dir=None,
                )
                for workspace_root, client in sorted(self._clients.items())
            )


def _workspace_notification_handler(
    workspace_root: Path,
    handler: NotificationHandler,
) -> Callable[[ACPEvent], Awaitable[None]]:
    async def _wrapped(event: ACPEvent) -> None:
        await handler(workspace_root, event)

    return _wrapped


def _workspace_permission_handler(
    workspace_root: Path,
    handler: PermissionHandler,
) -> Callable[[ACPPermissionRequestEvent], Awaitable[Any]]:
    async def _wrapped(event: ACPPermissionRequestEvent) -> Any:
        return await handler(workspace_root, event)

    return _wrapped


def _client_process(client: ACPClient) -> Any:
    return getattr(client, "_process", None)


def _client_pid(client: ACPClient) -> Optional[int]:
    process = _client_process(client)
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) else None


def _client_pgid(client: ACPClient) -> Optional[int]:
    pid = _client_pid(client)
    if pid is None:
        return None
    try:
        import os

        return os.getpgid(pid) if os.name != "nt" else None
    except OSError:
        return None


def _client_returncode(client: ACPClient) -> Optional[int]:
    process = _client_process(client)
    returncode = getattr(process, "returncode", None)
    return returncode if isinstance(returncode, int) else None


def _client_active_prompts(client: ACPClient) -> int:
    prompts = getattr(client, "_prompts", None)
    return len(prompts) if isinstance(prompts, dict) else 0


__all__ = [
    "ACPSubprocessSupervisor",
    "ACPSupervisorHandleSnapshot",
]
