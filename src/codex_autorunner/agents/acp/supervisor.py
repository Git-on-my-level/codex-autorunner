from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from ...workspace import canonical_workspace_root
from .client import ACPClient, ACPPromptHandle, NotificationHandler, PermissionHandler
from .protocol import ACPSessionDescriptor


@dataclass(frozen=True)
class ACPSupervisorHandleSnapshot:
    workspace_root: str
    started: bool


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
                    notification_handler=self._notification_handler,
                    permission_handler=self._permission_handler,
                    logger=self._logger,
                )
                self._clients[key] = client
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

    async def cancel_prompt(
        self, workspace_root: Path, session_id: str, turn_id: str
    ) -> Any:
        client = await self.get_client(workspace_root)
        return await client.cancel_prompt(session_id, turn_id)

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
        if client is not None:
            await client.close()

    async def close_all(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients = {}
        for client in clients:
            await client.close()

    async def lifecycle_snapshot(self) -> tuple[ACPSupervisorHandleSnapshot, ...]:
        async with self._lock:
            return tuple(
                ACPSupervisorHandleSnapshot(
                    workspace_root=workspace_root,
                    started=client.initialize_result is not None,
                )
                for workspace_root, client in sorted(self._clients.items())
            )


__all__ = [
    "ACPSubprocessSupervisor",
    "ACPSupervisorHandleSnapshot",
]
