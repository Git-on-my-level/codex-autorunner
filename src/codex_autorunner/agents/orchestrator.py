from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from ..core.app_server_events import AppServerEventBuffer
from .codex.harness import CodexHarness
from .opencode.harness import OpenCodeHarness
from .opencode.runtime import collect_opencode_output
from .types import ConversationRef, TurnRef

_logger = logging.getLogger(__name__)


class TurnStatus(str):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    TIMEOUT = "timeout"


class AgentOrchestrator:
    async def create_or_resume_conversation(
        self,
        workspace_root: Path,
        agent_id: str,
        *,
        conversation_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> ConversationRef:
        raise NotImplementedError

    async def run_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def stream_turn_events(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError

    async def interrupt_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str] = None,
        grace_seconds: float = 30.0,
    ) -> bool:
        raise NotImplementedError

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: Optional[str] = None,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
    ) -> TurnRef:
        raise NotImplementedError


class CodexOrchestrator(AgentOrchestrator):
    def __init__(
        self,
        harness: CodexHarness,
        events: AppServerEventBuffer,
    ):
        self._harness = harness
        self._events = events

    async def create_or_resume_conversation(
        self,
        workspace_root: Path,
        agent_id: str,
        *,
        conversation_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> ConversationRef:
        if conversation_id:
            return await self._harness.resume_conversation(
                workspace_root, conversation_id
            )
        return await self._harness.new_conversation(workspace_root, title)

    async def run_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> dict[str, Any]:
        turn_ref = await self._harness.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )

        output_lines = []
        async for event_str in self._harness.stream_events(
            workspace_root, turn_ref.conversation_id, turn_ref.turn_id
        ):
            try:
                import json

                event = json.loads(event_str)
                if "message" in event:
                    msg = event["message"]
                    if "params" in msg:
                        params = msg["params"]
                        if isinstance(params, dict):
                            if "output" in params:
                                output_lines.append(params["output"])
            except Exception:
                pass

        return {
            "turn_id": turn_ref.turn_id,
            "conversation_id": turn_ref.conversation_id,
            "status": TurnStatus.COMPLETED,
            "output": "\n".join(output_lines),
        }

    async def stream_turn_events(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        turn_ref = await self._harness.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )
        yield {"type": "turn_started", "data": {"turn_id": turn_ref.turn_id}}

        async for event_str in self._harness.stream_events(
            workspace_root, turn_ref.conversation_id, turn_ref.turn_id
        ):
            yield {"type": "event", "data": event_str}

        yield {"type": "turn_completed", "data": {"turn_id": turn_ref.turn_id}}

    async def interrupt_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str] = None,
        grace_seconds: float = 30.0,
    ) -> bool:
        await self._harness.interrupt(workspace_root, conversation_id, turn_id)
        return True

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: Optional[str] = None,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
    ) -> TurnRef:
        return await self._harness.start_review(
            workspace_root,
            conversation_id,
            prompt or "",
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )


class OpenCodeOrchestrator(AgentOrchestrator):
    def __init__(
        self,
        harness: OpenCodeHarness,
    ):
        self._harness = harness

    async def create_or_resume_conversation(
        self,
        workspace_root: Path,
        agent_id: str,
        *,
        conversation_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> ConversationRef:
        if conversation_id:
            return await self._harness.resume_conversation(
                workspace_root, conversation_id
            )
        return await self._harness.new_conversation(workspace_root, title)

    async def run_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> dict[str, Any]:
        turn_ref = await self._harness.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )

        client = await self._harness._supervisor.get_client(workspace_root)
        output_result = await collect_opencode_output(
            client,
            session_id=conversation_id,
            workspace_path=str(workspace_root),
            permission_policy=approval_mode or "allow",
            question_policy="auto_first_option",
            should_stop=should_stop or (lambda: False),
        )

        status = TurnStatus.COMPLETED if not output_result.error else TurnStatus.FAILED

        return {
            "turn_id": turn_ref.turn_id,
            "conversation_id": turn_ref.conversation_id,
            "status": status,
            "output": output_result.text,
            "error": output_result.error,
        }

    async def stream_turn_events(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        turn_ref = await self._harness.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )
        yield {"type": "turn_started", "data": {"turn_id": turn_ref.turn_id}}

        async for event_str in self._harness.stream_events(
            workspace_root, conversation_id, turn_ref.turn_id
        ):
            yield {"type": "event", "data": event_str}

        yield {"type": "turn_completed", "data": {"turn_id": turn_ref.turn_id}}

    async def interrupt_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str] = None,
        grace_seconds: float = 30.0,
    ) -> bool:
        await self._harness.interrupt(workspace_root, conversation_id, turn_id)
        return True

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: Optional[str] = None,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
    ) -> TurnRef:
        return await self._harness.start_review(
            workspace_root,
            conversation_id,
            prompt or "",
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )


__all__ = [
    "AgentOrchestrator",
    "CodexOrchestrator",
    "OpenCodeOrchestrator",
    "TurnStatus",
]
