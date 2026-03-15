from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Mapping, Optional, Sequence

from ...core.config import Config
from ...core.utils import atomic_write, resolve_executable
from ...workspace import canonical_workspace_root, workspace_id_for_path
from ..types import TerminalTurnResult
from .client import ZeroClawClient, split_zeroclaw_model

_RUNTIME_WORKSPACE_DIRNAME = "workspace"
_THREADS_DIRNAME = "threads"
_RELAUNCH_METADATA_FILENAME = "relaunch.json"
_SESSION_STATE_FILENAME = "session-state.json"
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class ZeroClawSupervisorError(RuntimeError):
    """Raised when the ZeroClaw supervisor cannot satisfy a session request."""


@dataclass(frozen=True)
class ZeroClawSessionMetadata:
    session_id: str
    workspace_id: str
    title: Optional[str]
    created_at: float
    last_used_at: float
    launch_provider: Optional[str] = None
    launch_model: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ZeroClawSessionMetadata":
        session_id = str(payload.get("session_id") or "").strip()
        workspace_id = str(payload.get("workspace_id") or "").strip()
        if not session_id:
            raise ZeroClawSupervisorError(
                "ZeroClaw relaunch metadata is missing session_id"
            )
        if not workspace_id:
            raise ZeroClawSupervisorError(
                f"ZeroClaw relaunch metadata for '{session_id}' is missing workspace_id"
            )
        try:
            created_at = _coerce_timestamp(payload.get("created_at"), default=0.0)
            last_used_at = _coerce_timestamp(
                payload.get("last_used_at"), default=created_at
            )
        except (TypeError, ValueError) as exc:
            raise ZeroClawSupervisorError(
                f"ZeroClaw relaunch metadata for '{session_id}' has invalid timestamps"
            ) from exc
        title = payload.get("title")
        launch_provider = payload.get("launch_provider")
        launch_model = payload.get("launch_model")
        return cls(
            session_id=session_id,
            workspace_id=workspace_id,
            title=str(title) if isinstance(title, str) and title.strip() else None,
            created_at=created_at,
            last_used_at=last_used_at,
            launch_provider=(
                str(launch_provider)
                if isinstance(launch_provider, str) and launch_provider.strip()
                else None
            ),
            launch_model=(
                str(launch_model)
                if isinstance(launch_model, str) and launch_model.strip()
                else None
            ),
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }
        if self.title:
            payload["title"] = self.title
        if self.launch_provider:
            payload["launch_provider"] = self.launch_provider
        if self.launch_model:
            payload["launch_model"] = self.launch_model
        return payload


def _coerce_timestamp(value: object, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise TypeError("boolean timestamps are not supported")
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"unsupported timestamp type: {type(value).__name__}")


@dataclass
class ZeroClawSessionHandle:
    session_id: str
    workspace_id: str
    workspace_root: Path
    runtime_workspace_root: Path
    session_root: Path
    session_state_file: Path
    client: ZeroClawClient
    title: Optional[str]
    created_at: float
    last_used_at: float
    launch_provider: Optional[str] = None
    launch_model: Optional[str] = None


class ZeroClawSupervisor:
    """Durable ZeroClaw session owner backed by CAR-managed agent workspaces."""

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
        now = time.time()
        metadata = ZeroClawSessionMetadata(
            session_id=session_id,
            workspace_id=workspace_id_for_path(canonical_root),
            title=title,
            created_at=now,
            last_used_at=now,
        )
        handle = self._build_handle(canonical_root, metadata)
        self._persist_metadata(handle)
        async with self._lock:
            self._handles[session_id] = handle
        return session_id

    async def list_sessions(self, workspace_root: Path) -> list[str]:
        canonical_root = canonical_workspace_root(workspace_root)
        sessions_root = self._sessions_root(canonical_root)
        discovered: list[ZeroClawSessionMetadata] = []
        if not sessions_root.exists():
            return []
        for metadata_path in sorted(
            sessions_root.glob(f"*/{_RELAUNCH_METADATA_FILENAME}")
        ):
            session_root = metadata_path.parent
            session_id = session_root.name
            try:
                discovered.append(self._read_metadata(canonical_root, session_id))
            except ZeroClawSupervisorError as exc:
                self._logger.warning(
                    "Skipping invalid ZeroClaw session metadata at %s: %s",
                    metadata_path,
                    exc,
                )
        discovered.sort(key=lambda item: (item.created_at, item.session_id))
        return [item.session_id for item in discovered]

    async def attach_session(self, workspace_root: Path, session_id: str) -> str:
        handle = await self._get_handle(workspace_root, session_id)
        handle.last_used_at = time.time()
        self._persist_metadata(handle)
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
        turn_id = await handle.client.start_turn(
            prompt,
            provider=provider,
            model=model_name,
        )
        handle.launch_provider = handle.client.launch_provider
        handle.launch_model = handle.client.launch_model
        handle.last_used_at = time.time()
        self._persist_metadata(handle)
        return turn_id

    async def wait_for_turn(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        handle = await self._get_handle(workspace_root, session_id)
        handle.last_used_at = time.time()
        self._persist_metadata(handle)
        return await handle.client.wait_for_turn(turn_id, timeout=timeout)

    async def stream_turn_events(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
    ) -> AsyncIterator[str]:
        handle = await self._get_handle(workspace_root, session_id)
        handle.last_used_at = time.time()
        self._persist_metadata(handle)
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
        session_id = self._normalize_session_id(session_id)
        async with self._lock:
            handle = self._handles.get(session_id)
            if handle is not None:
                if handle.workspace_root != canonical_root:
                    raise ZeroClawSupervisorError(
                        f"ZeroClaw session '{session_id}' is bound to a different workspace"
                    )
                return handle
            metadata = self._read_metadata(canonical_root, session_id)
            handle = self._build_handle(canonical_root, metadata)
            self._handles[session_id] = handle
            return handle

    def _build_handle(
        self,
        workspace_root: Path,
        metadata: ZeroClawSessionMetadata,
    ) -> ZeroClawSessionHandle:
        session_root = self._session_root(workspace_root, metadata.session_id)
        runtime_workspace_root = self._runtime_workspace_root(workspace_root)
        client = ZeroClawClient(
            self._command,
            runtime_workspace_root=runtime_workspace_root,
            session_state_file=self._session_state_file(
                workspace_root, metadata.session_id
            ),
            logger=self._logger,
            base_env=self._base_env,
            launch_provider=metadata.launch_provider,
            launch_model=metadata.launch_model,
        )
        return ZeroClawSessionHandle(
            session_id=metadata.session_id,
            workspace_id=metadata.workspace_id,
            workspace_root=workspace_root,
            runtime_workspace_root=runtime_workspace_root,
            session_root=session_root,
            session_state_file=self._session_state_file(
                workspace_root, metadata.session_id
            ),
            client=client,
            title=metadata.title,
            created_at=metadata.created_at,
            last_used_at=metadata.last_used_at,
            launch_provider=metadata.launch_provider,
            launch_model=metadata.launch_model,
        )

    def _persist_metadata(self, handle: ZeroClawSessionHandle) -> None:
        handle.runtime_workspace_root.mkdir(parents=True, exist_ok=True)
        handle.session_root.mkdir(parents=True, exist_ok=True)
        metadata = ZeroClawSessionMetadata(
            session_id=handle.session_id,
            workspace_id=handle.workspace_id,
            title=handle.title,
            created_at=handle.created_at,
            last_used_at=handle.last_used_at,
            launch_provider=handle.launch_provider,
            launch_model=handle.launch_model,
        )
        atomic_write(
            self._metadata_path(handle.workspace_root, handle.session_id),
            json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n",
        )

    def _read_metadata(
        self,
        workspace_root: Path,
        session_id: str,
    ) -> ZeroClawSessionMetadata:
        metadata_path = self._metadata_path(workspace_root, session_id)
        if not metadata_path.exists():
            raise ZeroClawSupervisorError(f"Unknown ZeroClaw session '{session_id}'")
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ZeroClawSupervisorError(
                f"Failed to read ZeroClaw relaunch metadata for '{session_id}'"
            ) from exc
        if not isinstance(payload, dict):
            raise ZeroClawSupervisorError(
                f"ZeroClaw relaunch metadata for '{session_id}' is malformed"
            )
        metadata = ZeroClawSessionMetadata.from_dict(payload)
        expected_workspace_id = workspace_id_for_path(workspace_root)
        if metadata.workspace_id != expected_workspace_id:
            raise ZeroClawSupervisorError(
                f"ZeroClaw session '{session_id}' is bound to a different workspace"
            )
        return metadata

    def _runtime_workspace_root(self, workspace_root: Path) -> Path:
        return workspace_root / _RUNTIME_WORKSPACE_DIRNAME

    def _sessions_root(self, workspace_root: Path) -> Path:
        return workspace_root / _THREADS_DIRNAME

    def _session_root(self, workspace_root: Path, session_id: str) -> Path:
        return self._sessions_root(workspace_root) / self._normalize_session_id(
            session_id
        )

    def _metadata_path(self, workspace_root: Path, session_id: str) -> Path:
        return (
            self._session_root(workspace_root, session_id) / _RELAUNCH_METADATA_FILENAME
        )

    def _session_state_file(self, workspace_root: Path, session_id: str) -> Path:
        return self._session_root(workspace_root, session_id) / _SESSION_STATE_FILENAME

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        normalized = str(session_id or "").strip()
        if not normalized or not _SAFE_SESSION_ID.match(normalized):
            raise ZeroClawSupervisorError(
                "ZeroClaw session ids must be non-empty path-safe identifiers"
            )
        return normalized


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
