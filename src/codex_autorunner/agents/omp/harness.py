from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...core.orchestration.interfaces import FreshConversationRequiredError
from ...core.text_utils import _normalize_optional_text
from ..acp import ACPMissingSessionError
from ..acp.protocol import extract_model_catalog
from ..base import AgentHarness, UnsupportedAgentCapabilityError
from ..types import (
    AgentId,
    ConversationRef,
    ModelCatalog,
    RuntimeCapability,
    RuntimeCapabilityReport,
    TerminalTurnResult,
    TurnRef,
)
from .supervisor import OMP_RUNTIME_ID, OMPSupervisor

_logger = logging.getLogger(__name__)

# Capabilities proven against `omp acp` (protocol v1). Verified live:
# durable threads, message turns, active-thread discovery (session/list), event
# streaming (agent_message_chunk), model listing (configOptions), and approvals
# (omp emits session/request_permission for tool calls). Not supported by OMP's
# ACP surface and therefore left off: interrupt (session/cancel is rejected),
# review (no session/setMode), and transcript_history (no transcript method).
# OMP ACP also has no session/setModel, so per-turn model selection is not
# honored (the runtime uses OMP's configured default); see OMPSupervisor.
OMP_CAPABILITIES = frozenset(
    [
        RuntimeCapability("durable_threads"),
        RuntimeCapability("message_turns"),
        RuntimeCapability("active_thread_discovery"),
        RuntimeCapability("event_streaming"),
        RuntimeCapability("model_listing"),
        RuntimeCapability("approvals"),
    ]
)


def _config_options_from_raw(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    config_options = raw.get("configOptions")
    return [option for option in (config_options or []) if isinstance(option, dict)]


def _current_model_id_from_raw(raw: Mapping[str, Any]) -> Optional[str]:
    for option in _config_options_from_raw(raw):
        if option.get("id") == "model" or option.get("category") == "model":
            return _normalize_optional_text(option.get("currentValue"))
    return None


def _runtime_model_from_session_raw(
    raw: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    current_model_id = _current_model_id_from_raw(raw)
    if current_model_id is None:
        return None
    display_name: Optional[str] = None
    for option in _config_options_from_raw(raw):
        if option.get("id") == "model" or option.get("category") == "model":
            for entry in option.get("options") or []:
                if not isinstance(entry, dict):
                    continue
                if _normalize_optional_text(entry.get("value")) == current_model_id:
                    display_name = _normalize_optional_text(entry.get("name"))
                    break
            break
    return _runtime_model_payload(current_model_id, display_name=display_name)


def _runtime_model_from_model_id(
    model_id: Optional[str],
) -> Optional[dict[str, Any]]:
    current_model_id = _normalize_optional_text(model_id)
    if current_model_id is None:
        return None
    return _runtime_model_payload(current_model_id, display_name=None)


def _runtime_model_payload(
    current_model_id: str, *, display_name: Optional[str]
) -> dict[str, Any]:
    provider_id: Optional[str] = None
    provider_model_id: Optional[str] = None
    if ":" in current_model_id:
        provider_raw, model_raw = current_model_id.split(":", 1)
        provider_id = _normalize_optional_text(provider_raw)
        provider_model_id = _normalize_optional_text(model_raw)
    elif "/" in current_model_id:
        provider_raw, model_raw = current_model_id.split("/", 1)
        provider_id = _normalize_optional_text(provider_raw)
        provider_model_id = _normalize_optional_text(model_raw)
    return {
        "logical_agent": OMP_RUNTIME_ID,
        "runtime_agent": OMP_RUNTIME_ID,
        "canonical_model_label": display_name or current_model_id,
        "provider_id": provider_id,
        "provider_model_id": provider_model_id or current_model_id,
        "provider_payload": {
            "modelID": current_model_id,
            "displayName": display_name,
        },
        "metadata": {
            "omp_current_model_id": current_model_id,
        },
    }


class OMPHarness(AgentHarness):
    agent_id: AgentId = AgentId("omp")
    display_name = "OMP"
    capabilities = OMP_CAPABILITIES

    def __init__(self, supervisor: OMPSupervisor) -> None:
        self._supervisor = supervisor
        self._session_runtime_models: dict[str, dict[str, Any]] = {}
        self._turn_runtime_models: dict[tuple[str, str], dict[str, Any]] = {}
        self._model_catalog_cache: dict[str, Optional[ModelCatalog]] = {}

    def _conversation_ref_from_session(self, session: Any) -> ConversationRef:
        raw = getattr(session, "raw", None)
        raw_mapping = raw if isinstance(raw, dict) else {}
        session_id = str(getattr(session, "session_id", "") or "")
        runtime_model = _runtime_model_from_session_raw(raw_mapping)
        if session_id and runtime_model is not None:
            self._session_runtime_models[session_id] = runtime_model
        # configOptions travel with every session descriptor; cache the catalog
        # so model_listing does not need a throwaway session on the hot path.
        if session_id:
            self._model_catalog_cache.setdefault(
                session_id, extract_model_catalog(_config_options_from_raw(raw_mapping))
            )
        return ConversationRef(
            agent=self.agent_id,
            id=session_id,
            title=_normalize_optional_text(getattr(session, "title", None)),
            summary=getattr(session, "summary", None),
        )

    async def ensure_ready(self, workspace_root: Path) -> None:
        await self._supervisor.ensure_ready(workspace_root)

    async def runtime_capability_report(
        self, workspace_root: Path
    ) -> RuntimeCapabilityReport:
        _ = workspace_root
        return RuntimeCapabilityReport(capabilities=self.capabilities)

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog:
        for cached in self._model_catalog_cache.values():
            if cached is not None:
                return cached
        # ACP defines no model-listing RPC; configOptions travel on session
        # descriptors. OMP loads its model registry asynchronously, so a session
        # created immediately after spawn may omit the model select — retry
        # briefly until the registry is populated.
        session = await self._supervisor.create_session(workspace_root, title=None)
        session_id = session.session_id
        catalog: Optional[ModelCatalog] = None
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(0.5)
                session = await self._supervisor.resume_session(
                    workspace_root, session_id
                )
            catalog = extract_model_catalog(
                _config_options_from_raw(getattr(session, "raw", {}) or {})
            )
            if catalog is not None:
                break
        if catalog is None:
            raise UnsupportedAgentCapabilityError(
                "model_listing",
                agent_id=str(self.agent_id),
            )
        self._model_catalog_cache[session_id] = catalog
        return catalog

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        session = await self._supervisor.create_session(workspace_root, title=title)
        return self._conversation_ref_from_session(session)

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        sessions = await self._supervisor.list_sessions(workspace_root)
        return [self._conversation_ref_from_session(session) for session in sessions]

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        try:
            session = await self._supervisor.resume_session(
                workspace_root, conversation_id
            )
        except ACPMissingSessionError as exc:
            raise FreshConversationRequiredError(
                (
                    f"OMP session '{conversation_id}' could not be resumed; "
                    "refresh the conversation binding"
                ),
                conversation_id=conversation_id,
                operation="resume_conversation",
                status_code=exc.code,
            ) from exc
        return self._conversation_ref_from_session(session)

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
        _ = reasoning, sandbox_policy, input_items
        turn_id = await self._supervisor.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model=model,
            approval_mode=approval_mode,
        )
        runtime_model = _runtime_model_from_model_id(model)
        if runtime_model is not None:
            self._turn_runtime_models[(conversation_id, turn_id)] = runtime_model
        return TurnRef(conversation_id=conversation_id, turn_id=turn_id)

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        resolved_turn_id = str(turn_id or "").strip()
        if not resolved_turn_id:
            raise ValueError("OMP wait_for_turn requires a turn id")
        result = await self._supervisor.wait_for_turn(
            workspace_root,
            conversation_id,
            resolved_turn_id,
            timeout=timeout,
        )
        if result.effective_runtime is None:
            runtime_source = "omp_turn_model_override"
            runtime_model = self._turn_runtime_models.get(
                (conversation_id, resolved_turn_id)
            )
            if runtime_model is None:
                runtime_source = "omp_session_models"
                try:
                    session = await self._supervisor.resume_session(
                        workspace_root, conversation_id
                    )
                except ACPMissingSessionError:
                    runtime_model = None
                else:
                    self._conversation_ref_from_session(session)
                    runtime_model = self._session_runtime_models.get(conversation_id)
            if runtime_model is not None:
                return TerminalTurnResult(
                    status=result.status,
                    assistant_text=result.assistant_text,
                    errors=list(result.errors),
                    raw_events=list(result.raw_events),
                    effective_runtime={
                        **runtime_model,
                        "stage": "effective",
                        "source": runtime_source,
                    },
                )
        return result

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in self._supervisor.stream_turn_events(
            workspace_root,
            conversation_id,
            turn_id,
        ):
            yield event

    async def list_progress_events(
        self, conversation_id: str, turn_id: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        _ = conversation_id
        after_id = int(kwargs.get("after_id") or 0)
        limit = kwargs.get("limit")
        normalized_limit = int(limit) if isinstance(limit, int) and limit >= 0 else None
        return await self._supervisor.list_turn_events_snapshot(
            turn_id,
            after_id=after_id,
            limit=normalized_limit,
        )


__all__ = ["OMP_CAPABILITIES", "OMPHarness"]
