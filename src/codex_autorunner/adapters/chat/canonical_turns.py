from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from ...agents.runtime_options import resolve_opencode_model_payload
from ...core.orchestration import (
    DeliveryIntentRef,
    MessageRequest,
    TurnExecutionOrigin,
    TurnExecutionRequest,
)
from .agents import DEFAULT_CHAT_AGENT_MODELS


def _surface_source_id(surface_kind: str, surface_key: str) -> str:
    return f"{surface_kind}:{surface_key}"


def _resolved_model_payload(
    agent: str, model: Optional[str], configured_default_model: Optional[str]
) -> tuple[Optional[str], dict[str, str]]:
    resolved_model = (
        model or configured_default_model or DEFAULT_CHAT_AGENT_MODELS.get(agent)
    )
    payload = (
        resolve_opencode_model_payload(resolved_model) if agent == "opencode" else None
    )
    return resolved_model, dict(payload or {})


def build_surface_turn_execution_request(
    request: MessageRequest,
    *,
    request_id: str,
    workspace_root: Path | str,
    surface_kind: str,
    surface_key: str,
    agent: str,
    approval_policy: str,
    sandbox_policy: Any,
    profile: Optional[str] = None,
    client_request_id: Optional[str] = None,
    configured_default_model: Optional[str] = None,
    origin_metadata: Optional[Mapping[str, Any]] = None,
    delivery_surface_key: Optional[str] = None,
    delivery_metadata: Optional[Mapping[str, Any]] = None,
) -> TurnExecutionRequest:
    """Build the durable turn contract at chat-surface ingress."""

    model, model_payload = _resolved_model_payload(
        agent, request.model, configured_default_model
    )
    delivery_key = delivery_surface_key or surface_key
    return TurnExecutionRequest(
        request_id=request_id,
        target_id=request.target_id,
        target_kind=request.target_kind,
        workspace_root=str(workspace_root),
        request_kind=request.kind,
        busy_policy=request.busy_policy,
        prompt_text=request.message_text,
        input_items=tuple(dict(item) for item in request.input_items or ()),
        context_profile=request.context_profile,
        agent=agent,
        profile=profile if profile is not None else request.agent_profile,
        model=model,
        model_payload=model_payload,
        reasoning=request.reasoning,
        approval_policy=approval_policy,
        approval_mode=request.approval_mode or approval_policy,
        sandbox_policy=sandbox_policy,
        client_request_id=client_request_id,
        idempotency_key=client_request_id or request_id,
        correlation_id=client_request_id or request_id,
        origin=TurnExecutionOrigin(
            kind="surface",
            source_id=_surface_source_id(surface_kind, surface_key),
            surface_kind=surface_kind,
            surface_key=surface_key,
            metadata=dict(origin_metadata or {}),
        ),
        metadata=dict(request.metadata),
        delivery_intents=(
            DeliveryIntentRef(
                kind="chat_surface",
                intent_id=_surface_source_id(surface_kind, delivery_key),
                metadata={
                    "surface_kind": surface_kind,
                    "surface_key": delivery_key,
                    **dict(delivery_metadata or {}),
                },
            ),
        ),
    )
