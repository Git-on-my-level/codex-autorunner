from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...integrations.app_server.event_buffer import AppServerEventBuffer
from ...integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from ..base import AgentHarness
from ..types import AgentId, ConversationRef, ModelCatalog, ModelSpec, TurnRef

_DEFAULT_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")


def _coerce_entries(result: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [entry for entry in result if isinstance(entry, dict)]
    if isinstance(result, dict):
        for key in keys:
            value = result.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
    return []


def _select_default_model(result: Any, entries: list[dict[str, Any]]) -> str:
    if isinstance(result, dict):
        for key in (
            "defaultModel",
            "default_model",
            "default",
            "model",
            "modelId",
            "model_id",
        ):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        config = result.get("config")
        if isinstance(config, dict):
            for key in ("defaultModel", "default_model", "model", "modelId"):
                value = config.get(key)
                if isinstance(value, str) and value:
                    return value
    for entry in entries:
        if entry.get("default") or entry.get("isDefault"):
            model_id = entry.get("model") or entry.get("id")
            if isinstance(model_id, str) and model_id:
                return model_id
    for entry in entries:
        model_id = entry.get("model") or entry.get("id")
        if isinstance(model_id, str) and model_id:
            return model_id
    return ""


def _coerce_reasoning_efforts(entry: dict[str, Any]) -> list[str]:
    efforts_raw = entry.get("supportedReasoningEfforts")
    efforts: list[str] = []
    if isinstance(efforts_raw, list):
        for effort in efforts_raw:
            if isinstance(effort, dict):
                value = effort.get("reasoningEffort")
                if isinstance(value, str):
                    efforts.append(value)
            elif isinstance(effort, str):
                efforts.append(effort)
    default_effort = entry.get("defaultReasoningEffort")
    if isinstance(default_effort, str) and default_effort:
        efforts.append(default_effort)
    if not efforts:
        efforts = list(_DEFAULT_REASONING_EFFORTS)
    return list(dict.fromkeys(efforts))


class CodexHarness(AgentHarness):
    agent_id: AgentId = AgentId("codex")
    display_name = "Codex"

    def __init__(
        self,
        supervisor: WorkspaceAppServerSupervisor,
        events: AppServerEventBuffer,
    ) -> None:
        self._supervisor = supervisor
        self._events = events

    async def ensure_ready(self, workspace_root: Path) -> None:
        await self._supervisor.get_client(workspace_root)

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog:
        client = await self._supervisor.get_client(workspace_root)
        result = await client.model_list()
        entries = _coerce_entries(result, ("data", "models", "items", "results"))
        models: list[ModelSpec] = []
        for entry in entries:
            model_id = entry.get("model") or entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            display_name = entry.get("displayName") or entry.get("name") or model_id
            if not isinstance(display_name, str) or not display_name:
                display_name = model_id
            efforts = _coerce_reasoning_efforts(entry)
            models.append(
                ModelSpec(
                    id=model_id,
                    display_name=display_name,
                    supports_reasoning=bool(efforts),
                    reasoning_options=efforts,
                )
            )
        default_model = _select_default_model(result, entries)
        if not default_model and models:
            default_model = models[0].id
        return ModelCatalog(default_model=default_model, models=models)

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        client = await self._supervisor.get_client(workspace_root)
        result = await client.thread_start(str(workspace_root))
        thread_id = result.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("Codex app-server did not return a thread id")
        return ConversationRef(agent=self.agent_id, id=thread_id)

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        client = await self._supervisor.get_client(workspace_root)
        result = await client.thread_list()
        entries = _coerce_entries(result, ("threads", "data", "items", "results"))
        conversations: list[ConversationRef] = []
        for entry in entries:
            thread_id = entry.get("id")
            if isinstance(thread_id, str) and thread_id:
                conversations.append(ConversationRef(agent=self.agent_id, id=thread_id))
        return conversations

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        client = await self._supervisor.get_client(workspace_root)
        result = await client.thread_resume(conversation_id)
        thread_id = result.get("id") or conversation_id
        if not isinstance(thread_id, str) or not thread_id:
            thread_id = conversation_id
        return ConversationRef(agent=self.agent_id, id=thread_id)

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
    ) -> TurnRef:
        client = await self._supervisor.get_client(workspace_root)
        turn_kwargs: dict[str, Any] = {}
        if model:
            turn_kwargs["model"] = model
        if reasoning:
            turn_kwargs["effort"] = reasoning
        handle = await client.turn_start(
            conversation_id,
            prompt,
            approval_policy=approval_mode,
            sandbox_policy=sandbox_policy,
            **turn_kwargs,
        )
        await self._events.register_turn(handle.thread_id, handle.turn_id)
        return TurnRef(conversation_id=handle.thread_id, turn_id=handle.turn_id)

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
        client = await self._supervisor.get_client(workspace_root)
        review_kwargs: dict[str, Any] = {}
        if model:
            review_kwargs["model"] = model
        if reasoning:
            review_kwargs["effort"] = reasoning
        instructions = (prompt or "").strip()
        if instructions:
            target = {"type": "custom", "instructions": instructions}
        else:
            target = {"type": "uncommittedChanges"}
        handle = await client.review_start(
            conversation_id,
            target=target,
            approval_policy=approval_mode,
            sandbox_policy=sandbox_policy,
            **review_kwargs,
        )
        await self._events.register_turn(handle.thread_id, handle.turn_id)
        return TurnRef(conversation_id=handle.thread_id, turn_id=handle.turn_id)

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        if not turn_id:
            return
        client = await self._supervisor.get_client(workspace_root)
        await client.turn_interrupt(turn_id, thread_id=conversation_id)

    def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[str]:
        return self._events.stream(conversation_id, turn_id)


__all__ = ["CodexHarness"]
