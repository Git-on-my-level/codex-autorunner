from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Mapping, Optional, Sequence

from ...core.config import HubConfig, RepoConfig
from ...core.destinations import (
    Destination,
    DockerDestination,
    LocalDestination,
    resolve_effective_agent_workspace_destination,
)
from ...core.utils import atomic_write
from ...manifest import load_manifest
from ...workspace import canonical_workspace_root, workspace_id_for_path
from ..managed_runtime import (
    ManagedWorkspaceRuntimeError,
    RuntimePreflightResult,
    build_managed_workspace_launch_spec,
    preflight_managed_workspace_runtime,
    zeroclaw_managed_workspace_env,
)
from ..types import TerminalTurnResult
from .client import ZeroClawClient, split_zeroclaw_model

_RUNTIME_WORKSPACE_DIRNAME = "workspace"
_THREADS_DIRNAME = "threads"
_RELAUNCH_METADATA_FILENAME = "relaunch.json"
_SESSION_STATE_FILENAME = "session-state.json"
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class ZeroClawSupervisorError(RuntimeError):
    """Raised when the ZeroClaw supervisor cannot satisfy a session request."""


def _wrap_command_for_destination(**kwargs):
    from ...integrations.agents.destination_wrapping import (
        wrap_command_for_destination,
    )

    return wrap_command_for_destination(**kwargs)


def _docker_destination_supplies_env(
    destination: DockerDestination,
    env_key: str,
) -> bool:
    explicit_value = str(destination.env.get(env_key) or "").strip()
    if explicit_value:
        return True
    for pattern in destination.env_passthrough:
        normalized = str(pattern or "").strip()
        if not normalized or not fnmatch.fnmatchcase(env_key, normalized):
            continue
        if os.environ.get(env_key):
            return True
    return False


@dataclass(frozen=True)
class ZeroClawSessionMetadata:
    session_id: str
    workspace_id: str
    title: Optional[str]
    created_at: float
    last_used_at: float
    launch_provider: Optional[str] = None
    launch_model: Optional[str] = None
    runtime_version: Optional[str] = None
    launch_mode: Optional[str] = None

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
        runtime_version = payload.get("runtime_version")
        launch_mode = payload.get("launch_mode")
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
            runtime_version=(
                str(runtime_version)
                if isinstance(runtime_version, str) and runtime_version.strip()
                else None
            ),
            launch_mode=(
                str(launch_mode)
                if isinstance(launch_mode, str) and launch_mode.strip()
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
        if self.runtime_version:
            payload["runtime_version"] = self.runtime_version
        if self.launch_mode:
            payload["launch_mode"] = self.launch_mode
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
    runtime_version: Optional[str] = None
    launch_mode: Optional[str] = None


class ZeroClawSupervisor:
    """Durable ZeroClaw session owner backed by CAR-managed agent workspaces."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        logger: Optional[logging.Logger] = None,
        base_env: Optional[Mapping[str, str]] = None,
        destination_resolver: Optional[Callable[[Path], Destination]] = None,
    ) -> None:
        if not command:
            raise ValueError("ZeroClaw command must not be empty")
        self._command = [str(part) for part in command]
        self._logger = logger or logging.getLogger(__name__)
        self._base_env = base_env
        self._destination_resolver = destination_resolver
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

    async def list_turn_events(
        self,
        workspace_root: Path,
        session_id: str,
        turn_id: str,
    ) -> list[dict[str, str]]:
        handle = await self._get_handle(workspace_root, session_id)
        handle.last_used_at = time.time()
        self._persist_metadata(handle)
        return handle.client.list_turn_events(turn_id)

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
        client_command = list(self._command)
        destination = self._resolve_destination(workspace_root)
        embed_workspace_env = True
        if isinstance(destination, DockerDestination):
            docker_extra_env = zeroclaw_managed_workspace_env(
                runtime_workspace_root,
                base_env=self._base_env,
            )
            if _docker_destination_supplies_env(
                destination,
                "ZEROCLAW_CONFIG_DIR",
            ):
                docker_extra_env.pop("ZEROCLAW_CONFIG_DIR", None)
            wrapped = _wrap_command_for_destination(
                command=self._command,
                destination=destination,
                repo_root=workspace_root,
                command_workdir=runtime_workspace_root,
                extra_env=docker_extra_env,
            )
            client_command = wrapped.command
            embed_workspace_env = False
        session_state_file = self._session_state_file(
            workspace_root, metadata.session_id
        )
        try:
            launch_spec = build_managed_workspace_launch_spec(
                "zeroclaw",
                command=client_command,
                runtime_workspace_root=runtime_workspace_root,
                session_state_file=session_state_file,
                base_env=self._base_env,
                embed_workspace_env=embed_workspace_env,
            )
        except ManagedWorkspaceRuntimeError as exc:
            raise ZeroClawSupervisorError(str(exc)) from exc
        if (
            metadata.launch_mode
            and launch_spec.launch_mode
            and metadata.launch_mode != launch_spec.launch_mode
        ):
            raise ZeroClawSupervisorError(
                "ZeroClaw relaunch metadata is incompatible with the current runtime "
                f"launch mode ({metadata.launch_mode} != {launch_spec.launch_mode})"
            )
        client = ZeroClawClient(
            launch_spec,
            logger=self._logger,
            launch_provider=metadata.launch_provider,
            launch_model=metadata.launch_model,
        )
        return ZeroClawSessionHandle(
            session_id=metadata.session_id,
            workspace_id=metadata.workspace_id,
            workspace_root=workspace_root,
            runtime_workspace_root=runtime_workspace_root,
            session_root=session_root,
            session_state_file=session_state_file,
            client=client,
            title=metadata.title,
            created_at=metadata.created_at,
            last_used_at=metadata.last_used_at,
            launch_provider=metadata.launch_provider,
            launch_model=metadata.launch_model,
            runtime_version=launch_spec.runtime_version or metadata.runtime_version,
            launch_mode=launch_spec.launch_mode or metadata.launch_mode,
        )

    def _resolve_destination(self, workspace_root: Path) -> Destination:
        if self._destination_resolver is None:
            return LocalDestination()
        try:
            return self._destination_resolver(workspace_root)
        except Exception as exc:
            self._logger.warning(
                "Falling back to local ZeroClaw launch for %s after destination resolution failure: %s",
                workspace_root,
                exc,
            )
            return LocalDestination()

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
            runtime_version=handle.runtime_version,
            launch_mode=handle.launch_mode,
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
    config: RepoConfig | HubConfig,
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[ZeroClawSupervisor]:
    try:
        binary = config.agent_binary("zeroclaw")
    except Exception:
        return None
    destination_resolver: Optional[Callable[[Path], Destination]] = None
    if isinstance(config, HubConfig):
        resolved_logger = logger or logging.getLogger(__name__)

        def _resolve_workspace_destination(workspace_root: Path) -> Destination:
            manifest = load_manifest(config.manifest_path, config.root)
            workspace = manifest.get_agent_workspace_by_path(
                config.root, workspace_root
            )
            if workspace is None:
                return LocalDestination()
            resolution = resolve_effective_agent_workspace_destination(workspace)
            for issue in resolution.issues:
                resolved_logger.warning(
                    "Invalid agent workspace destination for %s; using local: %s",
                    workspace.id,
                    issue,
                )
            return resolution.destination

        destination_resolver = _resolve_workspace_destination
    return ZeroClawSupervisor(
        [binary],
        logger=logger,
        destination_resolver=destination_resolver,
    )


def zeroclaw_runtime_preflight(
    config: Optional[RepoConfig | HubConfig],
) -> RuntimePreflightResult:
    if config is None:
        return RuntimePreflightResult(
            runtime_id="zeroclaw",
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="ZeroClaw binary is not configured.",
            fix="Set agents.zeroclaw.binary in the repo or hub config.",
        )
    try:
        binary = config.agent_binary("zeroclaw").strip()
    except Exception:
        return RuntimePreflightResult(
            runtime_id="zeroclaw",
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="ZeroClaw binary is not configured.",
            fix="Set agents.zeroclaw.binary in the repo or hub config.",
        )
    if not binary:
        return RuntimePreflightResult(
            runtime_id="zeroclaw",
            status="missing_binary",
            version=None,
            launch_mode=None,
            message="ZeroClaw binary is not configured.",
            fix="Set agents.zeroclaw.binary in the repo or hub config.",
        )
    return preflight_managed_workspace_runtime(
        "zeroclaw",
        command=[binary],
    )


def zeroclaw_binary_available(config: Optional[RepoConfig | HubConfig]) -> bool:
    result = zeroclaw_runtime_preflight(config)
    return result.status == "ready"


__all__ = [
    "ZeroClawSessionHandle",
    "ZeroClawSupervisor",
    "ZeroClawSupervisorError",
    "build_zeroclaw_supervisor_from_config",
    "zeroclaw_binary_available",
    "zeroclaw_runtime_preflight",
]
