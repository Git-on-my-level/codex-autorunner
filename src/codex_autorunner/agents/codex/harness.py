from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...integrations.app_server import is_missing_thread_error
from ...integrations.app_server.client import (
    CodexAppServerResponseError,
)
from ...integrations.app_server.event_buffer import AppServerEventBuffer
from ...integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from ...integrations.chat.model_selection import REASONING_EFFORT_VALUES
from ..base import AgentHarness
from ..types import (
    AgentId,
    ConversationRef,
    ModelCatalog,
    ModelSpec,
    RuntimeCapability,
    TerminalTurnResult,
    TurnRef,
)

_INVALID_PARAMS_ERROR_CODES = {-32600, -32602}
logger = logging.getLogger(__name__)


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
        efforts = list(REASONING_EFFORT_VALUES)
    return list(dict.fromkeys(efforts))


def _normalize_model_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _select_display_name(model_id: str, display_name_raw: Any) -> str:
    if not isinstance(display_name_raw, str) or not display_name_raw:
        return model_id
    if _normalize_model_name(display_name_raw) == _normalize_model_name(model_id):
        return model_id
    return display_name_raw


class CodexHarness(AgentHarness):
    agent_id: AgentId = AgentId("codex")
    display_name = "Codex"
    capabilities = frozenset(
        [
            RuntimeCapability("durable_threads"),
            RuntimeCapability("message_turns"),
            RuntimeCapability("interrupt"),
            RuntimeCapability("active_thread_discovery"),
            RuntimeCapability("review"),
            RuntimeCapability("model_listing"),
            RuntimeCapability("event_streaming"),
            RuntimeCapability("approvals"),
        ]
    )

    def __init__(
        self,
        supervisor: WorkspaceAppServerSupervisor,
        events: AppServerEventBuffer,
    ) -> None:
        self._supervisor = supervisor
        self._events = events
        self._turn_handles: dict[tuple[str, str], Any] = {}

    async def ensure_ready(self, workspace_root: Path) -> None:
        await self._supervisor.get_client(workspace_root)

    async def backend_runtime_instance_id(self, workspace_root: Path) -> Optional[str]:
        client = await self._supervisor.get_client(workspace_root)
        await client.start()
        runtime_instance_id = getattr(client, "runtime_instance_id", None)
        if not isinstance(runtime_instance_id, str) or not runtime_instance_id:
            return None
        return runtime_instance_id

    async def _model_list_with_agent_compat(self, client: Any) -> Any:
        """Prefer codex-agent compatible models, but degrade for older servers."""
        try:
            return await client.model_list(agent="codex")
        except CodexAppServerResponseError as exc:
            # Older app-server versions may reject the `agent` filter.
            if exc.code not in _INVALID_PARAMS_ERROR_CODES:
                raise
            return await client.model_list()

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog:
        client = await self._supervisor.get_client(workspace_root)
        result = await self._model_list_with_agent_compat(client)
        entries = _coerce_entries(result, ("data", "models", "items", "results"))
        models: list[ModelSpec] = []
        for entry in entries:
            model_id = entry.get("model") or entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            display_name = _select_display_name(
                model_id, entry.get("displayName") or entry.get("name")
            )
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
        resume = getattr(client, "thread_resume", None)
        if not callable(resume):
            return ConversationRef(agent=self.agent_id, id=conversation_id)
        try:
            result = await resume(conversation_id)
        except (
            Exception
        ) as exc:  # intentional: resume may fail with varied error types checked by is_missing_thread_error
            if is_missing_thread_error(exc):
                return ConversationRef(agent=self.agent_id, id=conversation_id)
            raise
        thread_id = conversation_id
        if isinstance(result, dict):
            candidate = result.get("id")
            if isinstance(candidate, str) and candidate:
                thread_id = candidate
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
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> TurnRef:
        client = await self._supervisor.get_client(workspace_root)
        turn_kwargs: dict[str, Any] = {}
        if model:
            turn_kwargs["model"] = model
        if reasoning:
            turn_kwargs["effort"] = reasoning
        if input_items is not None:
            turn_kwargs["input_items"] = input_items
        handle = await client.turn_start(
            conversation_id,
            prompt,
            approval_policy=approval_mode,
            sandbox_policy=sandbox_policy,
            **turn_kwargs,
        )
        resolved_thread_id = getattr(handle, "thread_id", conversation_id)
        if not isinstance(resolved_thread_id, str) or not resolved_thread_id:
            resolved_thread_id = conversation_id
        self._turn_handles[(resolved_thread_id, handle.turn_id)] = handle
        register_turn = getattr(self._events, "register_turn", None)
        if callable(register_turn):
            try:
                await register_turn(resolved_thread_id, handle.turn_id)
            except (
                Exception
            ):  # intentional: best-effort event registration must not block turn start
                logger.debug("codex register_turn failed", exc_info=True)
        return TurnRef(conversation_id=resolved_thread_id, turn_id=handle.turn_id)

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
        resolved_thread_id = getattr(handle, "thread_id", conversation_id)
        if not isinstance(resolved_thread_id, str) or not resolved_thread_id:
            resolved_thread_id = conversation_id
        self._turn_handles[(resolved_thread_id, handle.turn_id)] = handle
        register_turn = getattr(self._events, "register_turn", None)
        if callable(register_turn):
            await register_turn(resolved_thread_id, handle.turn_id)
        return TurnRef(conversation_id=resolved_thread_id, turn_id=handle.turn_id)

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        if not turn_id:
            return
        client = await self._supervisor.get_client(workspace_root)
        await client.turn_interrupt(turn_id, thread_id=conversation_id)

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        _ = workspace_root
        stream_entries = getattr(self._events, "stream_entries", None)
        if not callable(stream_entries):
            logger.warning(
                "stream_events: stream_entries not callable on event buffer; "
                "conversation_id=%r turn_id=%r",
                conversation_id,
                turn_id,
            )
            return
        async for entry in stream_entries(conversation_id, turn_id):
            if entry is not None:
                yield entry

    async def list_progress_events(
        self,
        conversation_id: str,
        turn_id: str,
        *,
        after_id: int = 0,
        limit: Optional[int] = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        _ = kwargs
        return await self._events.list_events(
            conversation_id, turn_id, after_id=after_id, limit=limit
        )

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = workspace_root
        if not turn_id:
            raise ValueError("turn_id is required")
        handle = self._turn_handles.get((conversation_id, turn_id))
        if handle is None:
            raise KeyError(
                f"Unknown Codex turn handle for thread={conversation_id} turn={turn_id}"
            )
        try:
            result = await handle.wait(timeout=timeout)
        finally:
            self._turn_handles.pop((conversation_id, turn_id), None)
        agent_messages = [
            message.strip()
            for message in getattr(result, "agent_messages", []) or []
            if isinstance(message, str) and message.strip()
        ]
        assistant_text = ""
        final_message = getattr(result, "final_message", "")
        if isinstance(final_message, str):
            assistant_text = final_message.strip()
        if not assistant_text:
            assistant_text = "\n\n".join(agent_messages).strip()
        return TerminalTurnResult(
            status=(
                str(result.status)
                if getattr(result, "status", None) is not None
                else None
            ),
            assistant_text=assistant_text,
            errors=[
                str(error)
                for error in (getattr(result, "errors", []) or [])
                if str(error).strip()
            ],
            raw_events=[
                event
                for event in (getattr(result, "raw_events", []) or [])
                if isinstance(event, dict)
            ],
        )


__all__ = ["CodexHarness"]
