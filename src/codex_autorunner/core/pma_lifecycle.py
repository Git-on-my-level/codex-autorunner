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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .app_server_threads import (
    PMA_KEY,
    PMA_OPENCODE_KEY,
    AppServerThreadRegistry,
)
from .logging_utils import log_event
from .pma_audit import PmaActionType
from .pma_queue import PmaQueue
from .pma_safety import PmaSafetyChecker, PmaSafetyConfig
from .time_utils import now_iso

logger = logging.getLogger(__name__)


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
        safety_config = PmaSafetyConfig()
        self._safety_checker = PmaSafetyChecker(hub_root, config=safety_config)

    async def new(
        self,
        *,
        agent: Optional[str] = None,
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

            # Reset thread state
            registry = AppServerThreadRegistry(
                self._hub_root / ".codex-autorunner" / "app_server_threads.json"
            )

            cleared_keys = []
            if agent == "opencode" or not agent:
                if registry.reset_thread(PMA_OPENCODE_KEY):
                    cleared_keys.append(PMA_OPENCODE_KEY)

            if agent != "opencode" or not agent:
                if registry.reset_thread(PMA_KEY):
                    cleared_keys.append(PMA_KEY)

            # Create artifact
            artifact = {
                "event_id": event_id,
                "command": LifecycleCommand.NEW.value,
                "timestamp": timestamp,
                "agent": agent,
                "lane_id": lane_id,
                "cleared_threads": cleared_keys,
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
                    "lane_id": lane_id,
                },
            )

            # Emit event record
            self._emit_event(
                {
                    "event_id": event_id,
                    "event_type": "pma_lifecycle_new",
                    "timestamp": timestamp,
                    "agent": agent,
                    "lane_id": lane_id,
                    "cleared_threads": cleared_keys,
                    "artifact_path": str(artifact_path),
                }
            )

            log_event(
                logger,
                logging.INFO,
                "pma.lifecycle.new",
                event_id=event_id,
                agent=agent,
                lane_id=lane_id,
                cleared_threads=cleared_keys,
            )

            return LifecycleCommandResult(
                status="ok",
                command=LifecycleCommand.NEW,
                message=f"New PMA session started (agent={agent or 'default'})",
                artifact_path=artifact_path,
                details={
                    "cleared_threads": cleared_keys,
                    "agent": agent,
                    "lane_id": lane_id,
                },
            )

        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "pma.lifecycle.new.failed",
                exc=exc,
                agent=agent,
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

            # Reset thread state
            registry = AppServerThreadRegistry(
                self._hub_root / ".codex-autorunner" / "app_server_threads.json"
            )

            cleared_keys = []
            if agent in ("", "all", None):
                if registry.reset_thread(PMA_KEY):
                    cleared_keys.append(PMA_KEY)
                if registry.reset_thread(PMA_OPENCODE_KEY):
                    cleared_keys.append(PMA_OPENCODE_KEY)
            elif agent == "opencode":
                if registry.reset_thread(PMA_OPENCODE_KEY):
                    cleared_keys.append(PMA_OPENCODE_KEY)
            else:
                if registry.reset_thread(PMA_KEY):
                    cleared_keys.append(PMA_KEY)

            # Create artifact
            artifact = {
                "event_id": event_id,
                "command": LifecycleCommand.RESET.value,
                "timestamp": timestamp,
                "agent": agent,
                "cleared_threads": cleared_keys,
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
                },
            )

            # Emit event record
            self._emit_event(
                {
                    "event_id": event_id,
                    "event_type": "pma_lifecycle_reset",
                    "timestamp": timestamp,
                    "agent": agent,
                    "cleared_threads": cleared_keys,
                    "artifact_path": str(artifact_path),
                }
            )

            log_event(
                logger,
                logging.INFO,
                "pma.lifecycle.reset",
                event_id=event_id,
                agent=agent,
                cleared_threads=cleared_keys,
            )

            return LifecycleCommandResult(
                status="ok",
                command=LifecycleCommand.RESET,
                message=f"PMA thread reset. Cleared: {', '.join(cleared_keys)}",
                artifact_path=artifact_path,
                details={
                    "cleared_threads": cleared_keys,
                    "agent": agent,
                },
            )

        except Exception as exc:
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

        except Exception as exc:
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

        except Exception as exc:
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
        """Write a durable artifact to disk."""
        artifact_path = self._artifacts_dir / f"{event_id}.json"
        artifact_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        return artifact_path

    def _emit_event(self, event: dict[str, Any]) -> None:
        """Emit an event record to the lifecycle events log."""
        try:
            with open(self._events_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except Exception:
            logger.exception("Failed to emit lifecycle event")


__all__ = [
    "LifecycleCommand",
    "LifecycleCommandResult",
    "PmaLifecycleRouter",
]
