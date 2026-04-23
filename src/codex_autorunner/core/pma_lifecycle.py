"""
PMA lifecycle command router.

Provides unified lifecycle commands for PMA across Web and Telegram surfaces:
- /new - new PMA session/thread
- /reset - clear volatile state; keep stable defaults
- /stop - interrupt current work and clear queue for current lane
- /compact - summarize/compact history into durable artifacts

All commands create durable artifacts and emit event records for observability.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
from .logging_utils import log_event
from .managed_thread_identity import (
    PMA_KEY,
    PMA_PREFIX,
    ManagedThreadIdentityStore,
    pma_prefixes_for_reset,
)
from .orchestration.legacy_backfill_gate import ensure_legacy_orchestration_backfill
from .orchestration.sqlite import open_orchestration_sqlite
from .pma_audit import PmaActionType
from .pma_context import (
    clear_pma_prompt_state_sessions,
    list_pma_prompt_state_session_keys,
)
from .pma_queue import PmaQueue
from .pma_safety import PmaSafetyChecker, PmaSafetyConfig
from .time_utils import now_iso
from .utils import atomic_write

logger = logging.getLogger(__name__)
_PMA_NESTED_AGENT_FAMILY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class LifecycleCommand(Enum):
    """PMA lifecycle command types."""

    NEW = "new"
    RESET = "reset"
    STOP = "stop"
    COMPACT = "compact"


@dataclass
class LifecycleCommandResult:
    """Result of executing a lifecycle command."""

    status: str
    command: LifecycleCommand
    message: str
    artifact_path: Optional[Path] = None
    details: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class PmaLifecycleRouter:
    """
    Unified router for PMA lifecycle commands.

    Provides a single adapter-level implementation that can be called from:
    - Web UI API endpoints
    - Telegram slash commands
    - CLI commands

    All commands are idempotent and create durable artifacts.
    """

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = hub_root
        self._artifacts_dir = hub_root / ".codex-autorunner" / "pma" / "lifecycle"
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._events_log = (
            hub_root / ".codex-autorunner" / "pma" / "lifecycle_events.jsonl"
        )
        ensure_legacy_orchestration_backfill(hub_root)
        safety_config = PmaSafetyConfig()
        self._safety_checker = PmaSafetyChecker(hub_root, config=safety_config)

    def _clear_runtime_state_for_agent(
        self, agent: Optional[str], profile: Optional[str] = None
    ) -> tuple[list[str], list[str]]:
        registry = ManagedThreadIdentityStore(self._hub_root)

        cleared_thread_keys: list[str] = []
        cleared_prompt_state_keys: list[str] = []
        prefixes = pma_prefixes_for_reset(agent, profile)
        codex_prefix = pma_prefixes_for_reset("codex")[0]
        preserve_nested_agent_families = agent not in ("all", None, "") and (
            profile is None
        )
        active_nested_agent_prefixes = self._discover_nested_pma_agent_prefixes(
            tuple(registry.load().keys()),
            tuple(list_pma_prompt_state_session_keys(self._hub_root)),
        )

        for prefix in prefixes:
            exclude_prefixes = (
                tuple(
                    sorted(
                        nested_prefix
                        for nested_prefix in active_nested_agent_prefixes
                        if nested_prefix != codex_prefix
                    )
                )
                if prefix == codex_prefix and preserve_nested_agent_families
                else ()
            )
            cleared_thread_keys.extend(
                registry.reset_threads_by_prefix(
                    prefix, exclude_prefixes=exclude_prefixes
                )
            )
            base_key = prefix.rstrip(".")
            if registry.reset_thread(base_key):
                cleared_thread_keys.append(base_key)
            if prefix == codex_prefix and not exclude_prefixes:
                for key in tuple(registry.load().keys()):
                    if not isinstance(key, str) or not key.startswith(prefix):
                        continue
                    remainder = key[len(prefix) :]
                    if not remainder or "." in remainder:
                        continue
                    if registry.reset_thread(key):
                        cleared_thread_keys.append(key)
            cleared_prompt_state_keys.extend(
                clear_pma_prompt_state_sessions(
                    self._hub_root,
                    keys=(base_key,),
                    prefixes=(prefix,),
                    exclude_prefixes=exclude_prefixes,
                )
            )

        return list(dict.fromkeys(cleared_thread_keys)), list(
            dict.fromkeys(cleared_prompt_state_keys)
        )

    def _discover_nested_pma_agent_prefixes(
        self, *key_sets: tuple[str, ...]
    ) -> tuple[str, ...]:
        prefixes: set[str] = set()
        for keys in key_sets:
            for key in keys:
                if not isinstance(key, str) or not key.startswith(PMA_PREFIX):
                    continue
                suffix = key[len(PMA_PREFIX) :]
                if not suffix:
                    continue
                family = suffix.split(".", 1)[0].strip().lower()
                if not _PMA_NESTED_AGENT_FAMILY_RE.fullmatch(family):
                    continue
                prefixes.add(f"{PMA_KEY}.{family}.")
        return tuple(sorted(prefixes))

    async def new(
        self,
        *,
        agent: Optional[str] = None,
        profile: Optional[str] = None,
        lane_id: str = "pma:default",
        metadata: Optional[dict[str, Any]] = None,
    ) -> LifecycleCommandResult:
        """
        Start a new PMA session/thread.

        - If agent is opencode: creates a new OpenCode session
        - If agent is codex or not specified: creates a new app-server thread
        - In PMA mode: resets the PMA thread state

        Args:
            agent: The agent to use (codex|opencode)
            lane_id: The PMA queue lane ID
            metadata: Additional metadata to include in the artifact

        Returns:
            LifecycleCommandResult with artifact path and details
        """
        try:
            event_id = self._generate_event_id()
            timestamp = now_iso()
            cleared_keys, cleared_prompt_state_keys = (
                self._clear_runtime_state_for_agent(agent, profile)
            )

            # Create artifact
            artifact = {
                "event_id": event_id,
                "command": LifecycleCommand.NEW.value,
                "timestamp": timestamp,
                "agent": agent,
                "profile": profile,
                "lane_id": lane_id,
                "cleared_threads": cleared_keys,
                "cleared_prompt_state_keys": cleared_prompt_state_keys,
                "metadata": metadata or {},
            }
            artifact_path = self._write_artifact(event_id, artifact)

            # Record action in safety checker
            self._safety_checker.record_action(
                action_type=PmaActionType.SESSION_NEW,
                agent=agent,
                thread_id=None,
                turn_id=None,
                client_turn_id=None,
                details={
                    "command": "new",
                    "cleared_threads": cleared_keys,
                    "cleared_prompt_state_keys": cleared_prompt_state_keys,
                    "lane_id": lane_id,
                    "profile": profile,
                },
            )

            # Emit event record
            self._emit_event(
                {
                    "event_id": event_id,
                    "event_type": "pma_lifecycle_new",
                    "timestamp": timestamp,
                    "agent": agent,
                    "profile": profile,
                    "lane_id": lane_id,
                    "cleared_threads": cleared_keys,
                    "cleared_prompt_state_keys": cleared_prompt_state_keys,
                    "artifact_path": str(artifact_path),
                }
            )

            log_event(
                logger,
                logging.INFO,
                "pma.lifecycle.new",
                event_id=event_id,
                agent=agent,
                profile=profile,
                lane_id=lane_id,
                cleared_threads=cleared_keys,
                cleared_prompt_state_keys=cleared_prompt_state_keys,
            )

            return LifecycleCommandResult(
                status="ok",
                command=LifecycleCommand.NEW,
                message=f"New PMA session started (agent={agent or 'default'})",
                artifact_path=artifact_path,
                details={
                    "cleared_threads": cleared_keys,
                    "cleared_prompt_state_keys": cleared_prompt_state_keys,
                    "agent": agent,
                    "profile": profile,
                    "lane_id": lane_id,
                },
            )

        except Exception as exc:  # intentional: top-level lifecycle command handler
            log_event(
                logger,
                logging.ERROR,
                "pma.lifecycle.new.failed",
                exc=exc,
                agent=agent,
                profile=profile,
                lane_id=lane_id,
            )
            return LifecycleCommandResult(
                status="error",
                command=LifecycleCommand.NEW,
                message=f"Failed to start new PMA session: {exc}",
                error=str(exc),
            )

    async def reset(
        self,
        *,
        agent: Optional[str] = None,
        profile: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> LifecycleCommandResult:
        """
        Reset PMA thread state (clear volatile state; keep stable defaults).

        Args:
            agent: The agent thread to reset (opencode|codex|all)
            metadata: Additional metadata to include in the artifact

        Returns:
            LifecycleCommandResult with artifact path and details
        """
        try:
            event_id = self._generate_event_id()
            timestamp = now_iso()
            cleared_keys, cleared_prompt_state_keys = (
                self._clear_runtime_state_for_agent(agent, profile)
            )

            # Create artifact
            artifact = {
                "event_id": event_id,
                "command": LifecycleCommand.RESET.value,
                "timestamp": timestamp,
                "agent": agent,
                "profile": profile,
                "cleared_threads": cleared_keys,
                "cleared_prompt_state_keys": cleared_prompt_state_keys,
                "metadata": metadata or {},
            }
            artifact_path = self._write_artifact(event_id, artifact)

            # Record action in safety checker
            self._safety_checker.record_action(
                action_type=PmaActionType.SESSION_RESET,
                agent=agent,
                thread_id=None,
                turn_id=None,
                client_turn_id=None,
                details={
                    "command": "reset",
                    "cleared_threads": cleared_keys,
                    "cleared_prompt_state_keys": cleared_prompt_state_keys,
                    "profile": profile,
                },
            )

            # Emit event record
            self._emit_event(
                {
                    "event_id": event_id,
                    "event_type": "pma_lifecycle_reset",
                    "timestamp": timestamp,
                    "agent": agent,
                    "profile": profile,
                    "cleared_threads": cleared_keys,
                    "cleared_prompt_state_keys": cleared_prompt_state_keys,
                    "artifact_path": str(artifact_path),
                }
            )

            log_event(
                logger,
                logging.INFO,
                "pma.lifecycle.reset",
                event_id=event_id,
                agent=agent,
                profile=profile,
                cleared_threads=cleared_keys,
                cleared_prompt_state_keys=cleared_prompt_state_keys,
            )

            return LifecycleCommandResult(
                status="ok",
                command=LifecycleCommand.RESET,
                message=f"PMA thread reset. Cleared: {', '.join(cleared_keys)}",
                artifact_path=artifact_path,
                details={
                    "cleared_threads": cleared_keys,
                    "cleared_prompt_state_keys": cleared_prompt_state_keys,
                    "agent": agent,
                    "profile": profile,
                },
            )

        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            log_event(
                logger,
                logging.ERROR,
                "pma.lifecycle.reset.failed",
                exc=exc,
                agent=agent,
            )
            return LifecycleCommandResult(
                status="error",
                command=LifecycleCommand.RESET,
                message=f"Failed to reset PMA thread: {exc}",
                error=str(exc),
            )

    async def stop(
        self,
        *,
        lane_id: str = "pma:default",
        metadata: Optional[dict[str, Any]] = None,
    ) -> LifecycleCommandResult:
        """
        Stop PMA lane: interrupt current work and clear queue for current lane.

        Args:
            lane_id: The PMA queue lane ID
            metadata: Additional metadata to include in the artifact

        Returns:
            LifecycleCommandResult with artifact path and details
        """
        try:
            event_id = self._generate_event_id()
            timestamp = now_iso()

            # Cancel queued items
            queue = PmaQueue(self._hub_root)
            cancelled = await queue.cancel_lane(lane_id)

            # Create artifact
            artifact = {
                "event_id": event_id,
                "command": LifecycleCommand.STOP.value,
                "timestamp": timestamp,
                "lane_id": lane_id,
                "cancelled_items": cancelled,
                "metadata": metadata or {},
            }
            artifact_path = self._write_artifact(event_id, artifact)

            # Record action in safety checker
            self._safety_checker.record_action(
                action_type=PmaActionType.SESSION_STOP,
                agent=None,
                thread_id=None,
                turn_id=None,
                client_turn_id=None,
                details={
                    "command": "stop",
                    "lane_id": lane_id,
                    "cancelled_items": cancelled,
                },
            )

            # Emit event record
            self._emit_event(
                {
                    "event_id": event_id,
                    "event_type": "pma_lifecycle_stop",
                    "timestamp": timestamp,
                    "lane_id": lane_id,
                    "cancelled_items": cancelled,
                    "artifact_path": str(artifact_path),
                }
            )

            log_event(
                logger,
                logging.INFO,
                "pma.lifecycle.stop",
                event_id=event_id,
                lane_id=lane_id,
                cancelled=cancelled,
            )

            return LifecycleCommandResult(
                status="ok",
                command=LifecycleCommand.STOP,
                message=f"PMA lane stopped. Cancelled {cancelled} queued items",
                artifact_path=artifact_path,
                details={
                    "lane_id": lane_id,
                    "cancelled_items": cancelled,
                },
            )

        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            log_event(
                logger,
                logging.ERROR,
                "pma.lifecycle.stop.failed",
                exc=exc,
                lane_id=lane_id,
            )
            return LifecycleCommandResult(
                status="error",
                command=LifecycleCommand.STOP,
                message=f"Failed to stop PMA lane: {exc}",
                error=str(exc),
            )

    async def compact(
        self,
        *,
        summary: str,
        agent: Optional[str] = None,
        thread_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> LifecycleCommandResult:
        """
        Compact PMA history (summarize/compact into durable artifacts).

        Args:
            summary: The compact summary text
            agent: The agent used for compacting
            thread_id: The thread ID being compacted
            metadata: Additional metadata to include in the artifact

        Returns:
            LifecycleCommandResult with artifact path and details
        """
        try:
            event_id = self._generate_event_id()
            timestamp = now_iso()

            # Create artifact
            artifact = {
                "event_id": event_id,
                "command": LifecycleCommand.COMPACT.value,
                "timestamp": timestamp,
                "agent": agent,
                "thread_id": thread_id,
                "summary": summary,
                "metadata": metadata or {},
            }
            artifact_path = self._write_artifact(event_id, artifact)

            # Record action in safety checker
            self._safety_checker.record_action(
                action_type=PmaActionType.SESSION_COMPACT,
                agent=agent,
                thread_id=thread_id,
                turn_id=None,
                client_turn_id=None,
                details={
                    "command": "compact",
                    "summary_length": len(summary),
                },
            )

            # Emit event record
            self._emit_event(
                {
                    "event_id": event_id,
                    "event_type": "pma_lifecycle_compact",
                    "timestamp": timestamp,
                    "agent": agent,
                    "thread_id": thread_id,
                    "summary_length": len(summary),
                    "artifact_path": str(artifact_path),
                }
            )

            log_event(
                logger,
                logging.INFO,
                "pma.lifecycle.compact",
                event_id=event_id,
                agent=agent,
                thread_id=thread_id,
                summary_length=len(summary),
            )

            return LifecycleCommandResult(
                status="ok",
                command=LifecycleCommand.COMPACT,
                message="PMA history compacted",
                artifact_path=artifact_path,
                details={
                    "agent": agent,
                    "thread_id": thread_id,
                    "summary_length": len(summary),
                },
            )

        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            log_event(
                logger,
                logging.ERROR,
                "pma.lifecycle.compact.failed",
                exc=exc,
                agent=agent,
                thread_id=thread_id,
            )
            return LifecycleCommandResult(
                status="error",
                command=LifecycleCommand.COMPACT,
                message=f"Failed to compact PMA history: {exc}",
                error=str(exc),
            )

    def _generate_event_id(self) -> str:
        """Generate a unique event ID."""
        import uuid

        return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def _write_artifact(self, event_id: str, artifact: dict[str, Any]) -> Path:
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = self._artifacts_dir / f"{event_id}.json"
        atomic_write(artifact_path, json.dumps(artifact, indent=2))
        return artifact_path

    def _emit_event(self, event: dict[str, Any]) -> None:
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        event_id = str(event.get("event_id") or "").strip()
        event_type = str(event.get("event_type") or "unknown").strip() or "unknown"
        target_id = str(
            event.get("thread_id") or event.get("lane_id") or event.get("agent") or ""
        ).strip()
        target_kind = None
        if event.get("thread_id"):
            target_kind = "thread_target"
        elif event.get("lane_id"):
            target_kind = "lane"
        elif event.get("agent"):
            target_kind = "agent_definition"
        with open_orchestration_sqlite(self._hub_root) as conn:
            conn.execute(
                """
                INSERT INTO orch_event_projections (
                    event_id,
                    event_family,
                    event_type,
                    target_kind,
                    target_id,
                    execution_id,
                    repo_id,
                    run_id,
                    timestamp,
                    status,
                    payload_json,
                    processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    event_family = excluded.event_family,
                    event_type = excluded.event_type,
                    target_kind = excluded.target_kind,
                    target_id = excluded.target_id,
                    execution_id = excluded.execution_id,
                    repo_id = excluded.repo_id,
                    run_id = excluded.run_id,
                    timestamp = excluded.timestamp,
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    processed = excluded.processed
                """,
                (
                    event_id,
                    "pma.lifecycle",
                    event_type,
                    target_kind,
                    target_id or None,
                    None,
                    event.get("repo_id"),
                    event.get("run_id"),
                    event.get("timestamp") or now_iso(),
                    "recorded",
                    json.dumps(event, sort_keys=True),
                    1,
                ),
            )
        lock_path = self._events_log.with_suffix(".jsonl.lock")
        with file_lock(lock_path):
            with open(self._events_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")


__all__ = [
    "LifecycleCommand",
    "LifecycleCommandResult",
    "PmaLifecycleRouter",
]
