from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional, cast

from .types import (
    AgentId,
    ConversationRef,
    ModelCatalog,
    RuntimeCapability,
    RuntimeCapabilityReport,
    TerminalTurnResult,
    TranscriptEntry,
    TurnRef,
    normalize_runtime_capabilities,
)


class UnsupportedAgentCapabilityError(RuntimeError):
    """Raised when a harness helper is called without the required capability."""

    def __init__(self, capability: str, *, agent_id: Optional[str] = None) -> None:
        normalized = next(
            iter(normalize_runtime_capabilities([capability])), capability
        )
        message = f"Agent capability '{normalized}' is not supported"
        if agent_id:
            message = f"Agent '{agent_id}' does not support capability '{normalized}'"
        super().__init__(message)
        self.capability = str(normalized)
        self.agent_id = agent_id


class AgentHarness(ABC):
    """Runtime-backed durable thread/session seam used beneath orchestration services.

    Surface code should depend on orchestration service interfaces rather than
    calling harnesses directly. Concrete harness implementations remain the
    backend adapter layer for runtime-thread operations.
    """

    agent_id: AgentId
    display_name: str
    capabilities: frozenset[RuntimeCapability] = frozenset()

    @abstractmethod
    async def ensure_ready(self, workspace_root: Path) -> None:
        raise NotImplementedError

    async def runtime_capability_report(
        self, workspace_root: Path
    ) -> RuntimeCapabilityReport:
        _ = workspace_root
        return RuntimeCapabilityReport(capabilities=self.capabilities)

    def supports(self, capability: str) -> bool:
        normalized = normalize_runtime_capabilities([capability])
        if not normalized:
            return False
        return next(iter(normalized)) in self.capabilities

    def allows_parallel_event_stream(self) -> bool:
        return self.supports("event_streaming")

    def progress_event_stream(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[Any]:
        if not self.allows_parallel_event_stream():
            _ = workspace_root, conversation_id, turn_id

            async def _unsupported() -> AsyncIterator[Any]:
                raise UnsupportedAgentCapabilityError(
                    "event_streaming",
                    agent_id=str(self.agent_id),
                )
                if False:
                    yield None

            return _unsupported()
        return self.stream_events(workspace_root, conversation_id, turn_id)

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog:
        _ = workspace_root
        raise UnsupportedAgentCapabilityError(
            "model_listing",
            agent_id=str(self.agent_id),
        )

    @abstractmethod
    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        raise NotImplementedError

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        _ = workspace_root
        raise UnsupportedAgentCapabilityError(
            "active_thread_discovery",
            agent_id=str(self.agent_id),
        )

    @abstractmethod
    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        raise NotImplementedError

    @abstractmethod
    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> TurnRef:
        raise NotImplementedError

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> TurnRef:
        _ = (
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode,
            sandbox_policy,
        )
        raise UnsupportedAgentCapabilityError(
            "review",
            agent_id=str(self.agent_id),
        )

    @abstractmethod
    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        raise NotImplementedError

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        _ = workspace_root, conversation_id, turn_id
        raise UnsupportedAgentCapabilityError(
            "interrupt",
            agent_id=str(self.agent_id),
        )

    async def transcript_history(
        self,
        workspace_root: Path,
        conversation_id: str,
        *,
        limit: Optional[int] = None,
    ) -> list[TranscriptEntry]:
        _ = workspace_root, conversation_id, limit
        raise UnsupportedAgentCapabilityError(
            "transcript_history",
            agent_id=str(self.agent_id),
        )

    def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[Any]:
        _ = workspace_root, conversation_id, turn_id

        async def _unsupported() -> AsyncIterator[Any]:
            raise UnsupportedAgentCapabilityError(
                "event_streaming",
                agent_id=str(self.agent_id),
            )
            if False:
                yield None

        return _unsupported()


def harness_allows_parallel_event_stream(harness: Any) -> bool:
    supports = getattr(harness, "supports", None)
    if not callable(supports) or not supports("event_streaming"):
        return False
    allows_parallel = getattr(harness, "allows_parallel_event_stream", None)
    if callable(allows_parallel):
        return bool(allows_parallel())
    return True


def harness_supports_progress_event_stream(harness: Any) -> bool:
    progress_stream = getattr(harness, "progress_event_stream", None)
    if callable(progress_stream):
        progress_func = getattr(progress_stream, "__func__", None)
        if (
            progress_func is None
            or progress_func is not AgentHarness.progress_event_stream
        ):
            return True
    return harness_allows_parallel_event_stream(harness)


def harness_progress_event_stream(
    harness: Any,
    workspace_root: Path,
    conversation_id: str,
    turn_id: str,
) -> AsyncIterator[Any]:
    progress_stream = cast(
        Optional[Callable[[Path, str, str], AsyncIterator[Any]]],
        getattr(harness, "progress_event_stream", None),
    )
    if callable(progress_stream):
        progress_func = getattr(progress_stream, "__func__", None)
        if (
            progress_func is None
            or progress_func is not AgentHarness.progress_event_stream
        ):
            return cast(
                AsyncIterator[Any],
                progress_stream(workspace_root, conversation_id, turn_id),
            )
    stream_events = cast(
        Optional[Callable[[Path, str, str], AsyncIterator[Any]]],
        getattr(harness, "stream_events", None),
    )
    if callable(stream_events) and harness_allows_parallel_event_stream(harness):
        return cast(
            AsyncIterator[Any], stream_events(workspace_root, conversation_id, turn_id)
        )

    async def _unsupported() -> AsyncIterator[Any]:
        raise UnsupportedAgentCapabilityError(
            "event_streaming",
            agent_id=str(getattr(harness, "agent_id", "") or ""),
        )
        if False:
            yield None

    return _unsupported()


__all__ = [
    "AgentHarness",
    "UnsupportedAgentCapabilityError",
    "harness_allows_parallel_event_stream",
    "harness_progress_event_stream",
    "harness_supports_progress_event_stream",
]
