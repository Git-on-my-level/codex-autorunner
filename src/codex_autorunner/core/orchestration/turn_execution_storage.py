from __future__ import annotations

from typing import Any, Mapping, Optional, cast

from ..text_utils import _json_loads_object, _normalize_optional_text
from .turn_execution_contract import (
    TURN_EXECUTION_CONTRACT_VERSION,
    TurnExecutionBusyPolicy,
    TurnExecutionOrigin,
    TurnExecutionRecord,
    TurnExecutionRequest,
    TurnExecutionRequestKind,
    TurnExecutionStatus,
)

TURN_EXECUTION_REQUEST_COLUMN = "turn_request_json"
TURN_EXECUTION_RECORD_COLUMN = "turn_record_json"
TURN_EXECUTION_CONTRACT_VERSION_COLUMN = "turn_contract_version"
LEGACY_OPENCODE_PROVIDER_ID = "legacy"
LEGACY_OPENCODE_MODEL_ID = "unresolved"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        return dict(metadata)
    return _json_loads_object(row.get("metadata_json"))


def _request_data(queue_payload: Mapping[str, Any]) -> dict[str, Any]:
    request = queue_payload.get("request")
    if not isinstance(request, Mapping):
        request = queue_payload.get("turn_request")
    return dict(request) if isinstance(request, Mapping) else {}


def _input_items(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _request_kind(value: Any) -> TurnExecutionRequestKind:
    normalized = _normalize_optional_text(value)
    if normalized in {
        "message",
        "review",
        "automation",
        "publish",
        "recovery",
        "lifecycle",
    }:
        return cast(TurnExecutionRequestKind, normalized)
    return "message"


def _busy_policy(value: Any, *, status: Any) -> TurnExecutionBusyPolicy:
    normalized = _normalize_optional_text(value)
    if normalized in {"queue", "interrupt", "reject"}:
        return cast(TurnExecutionBusyPolicy, normalized)
    return "queue" if _normalize_optional_text(status) == "queued" else "reject"


def _record_status(value: Any) -> TurnExecutionStatus:
    normalized = _normalize_optional_text(value)
    if normalized in {"queued", "claiming", "running", "failed", "interrupted"}:
        return cast(TurnExecutionStatus, normalized)
    if normalized in {"ok", "completed", "complete", "success", "succeeded"}:
        return "completed"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized == "lost":
        return "lost"
    return "failed"


def _model_and_payload(
    *,
    agent: str,
    model: Any,
    metadata_payload: Any,
) -> tuple[Optional[str], dict[str, Any]]:
    resolved_model = _normalize_optional_text(model)
    payload = _mapping(metadata_payload)
    if agent != "opencode":
        return resolved_model, payload
    if resolved_model is None:
        return (
            f"{LEGACY_OPENCODE_PROVIDER_ID}/{LEGACY_OPENCODE_MODEL_ID}",
            {
                "providerID": LEGACY_OPENCODE_PROVIDER_ID,
                "modelID": LEGACY_OPENCODE_MODEL_ID,
            },
        )
    if "/" in resolved_model:
        provider_id, model_id = (part.strip() for part in resolved_model.split("/", 1))
    else:
        provider_id = LEGACY_OPENCODE_PROVIDER_ID
        model_id = resolved_model
        resolved_model = f"{provider_id}/{model_id}"
    if provider_id and model_id:
        return resolved_model, {"providerID": provider_id, "modelID": model_id}
    return (
        f"{LEGACY_OPENCODE_PROVIDER_ID}/{LEGACY_OPENCODE_MODEL_ID}",
        {
            "providerID": LEGACY_OPENCODE_PROVIDER_ID,
            "modelID": LEGACY_OPENCODE_MODEL_ID,
        },
    )


def _origin_from_thread(thread: Mapping[str, Any]) -> TurnExecutionOrigin:
    surface_urn = _normalize_optional_text(thread.get("surface_urn"))
    if surface_urn and ":" in surface_urn:
        surface_kind, surface_key = surface_urn.split(":", 1)
        if surface_kind and surface_key:
            return TurnExecutionOrigin(
                kind="surface",
                source_id=surface_urn,
                surface_kind=surface_kind,
                surface_key=surface_key,
            )
    return TurnExecutionOrigin(
        kind="system",
        source_id="orchestration-storage",
        metadata={"source": "managed_thread_store"},
    )


def build_turn_execution_request_from_storage(
    *,
    execution: Mapping[str, Any],
    thread: Mapping[str, Any],
    queue_payload: Optional[Mapping[str, Any]] = None,
) -> TurnExecutionRequest:
    payload = _mapping(queue_payload)
    request_data = _request_data(payload)
    execution_metadata = _metadata(execution)
    thread_metadata = _metadata(thread)
    sandbox_policy = (
        payload.get("sandbox_policy")
        if "sandbox_policy" in payload
        else execution_metadata.get(
            "sandbox_policy", thread_metadata.get("sandbox_policy")
        )
    )
    if sandbox_policy is None:
        sandbox_policy = "dangerFullAccess"
    approval_mode = (
        request_data.get("approval_mode")
        or execution_metadata.get("approval_mode")
        or thread_metadata.get("approval_mode")
    )
    approval_policy = (
        execution_metadata.get("approval_policy")
        or thread_metadata.get("approval_policy")
        or approval_mode
        or "never"
    )
    agent = str(thread.get("agent_id") or thread.get("agent") or "unknown")
    model, model_payload = _model_and_payload(
        agent=agent,
        model=(
            execution.get("model_id")
            or request_data.get("model")
            or execution_metadata.get("model")
            or thread_metadata.get("model")
        ),
        metadata_payload=(
            request_data.get("model_payload") or execution_metadata.get("model_payload")
        ),
    )
    return TurnExecutionRequest(
        request_id=str(
            execution.get("execution_id")
            or execution.get("managed_turn_id")
            or request_data.get("request_id")
        ),
        target_id=str(
            execution.get("thread_target_id")
            or execution.get("managed_thread_id")
            or request_data.get("target_id")
            or thread.get("thread_target_id")
            or thread.get("managed_thread_id")
        ),
        target_kind="thread",
        workspace_root=(
            _normalize_optional_text(request_data.get("workspace_root"))
            or _normalize_optional_text(thread.get("workspace_root"))
        ),
        request_kind=_request_kind(
            execution.get("request_kind")
            or request_data.get("kind")
            or request_data.get("request_kind")
        ),
        busy_policy=_busy_policy(
            request_data.get("busy_policy"), status=execution.get("status")
        ),
        prompt_text=(
            _normalize_optional_text(execution.get("prompt_text"))
            or _normalize_optional_text(request_data.get("message_text"))
            or _normalize_optional_text(request_data.get("prompt_text"))
            or "[legacy request prompt unavailable]"
        ),
        input_items=_input_items(request_data.get("input_items")),
        context_profile=(
            request_data.get("context_profile")
            or execution_metadata.get("context_profile")
            or thread_metadata.get("context_profile")
        ),
        agent=agent,
        profile=(
            request_data.get("agent_profile")
            or request_data.get("profile")
            or thread_metadata.get("agent_profile")
            or execution_metadata.get("agent_profile")
        ),
        model=model,
        model_payload=model_payload,
        reasoning=execution.get("reasoning_level") or request_data.get("reasoning"),
        approval_policy=str(approval_policy),
        approval_mode=approval_mode,
        sandbox_policy=sandbox_policy,
        client_request_id=execution.get("client_request_id")
        or payload.get("client_request_id"),
        idempotency_key=execution.get("client_request_id")
        or execution.get("execution_id"),
        correlation_id=execution_metadata.get("correlation_id"),
        origin=_origin_from_thread(thread),
        metadata=execution_metadata,
    )


def build_turn_execution_record_from_storage(
    *,
    execution: Mapping[str, Any],
    thread: Mapping[str, Any],
    request: TurnExecutionRequest,
    queue_item: Optional[Mapping[str, Any]] = None,
) -> TurnExecutionRecord:
    queue = _mapping(queue_item)
    status = _record_status(execution.get("status"))
    return TurnExecutionRecord(
        request=request,
        execution_id=str(
            execution.get("execution_id") or execution.get("managed_turn_id")
        ),
        status=status,
        queued_at=(
            _normalize_optional_text(queue.get("created_at"))
            or _normalize_optional_text(execution.get("created_at"))
        ),
        claimed_at=_normalize_optional_text(queue.get("claimed_at")),
        started_at=_normalize_optional_text(execution.get("started_at")),
        terminal_at=(
            _normalize_optional_text(execution.get("finished_at"))
            if status in {"completed", "failed", "cancelled", "interrupted", "lost"}
            else None
        ),
        backend_conversation_id=_normalize_optional_text(
            thread.get("backend_thread_id")
        ),
        backend_turn_id=_normalize_optional_text(execution.get("backend_turn_id")),
        assistant_text=_normalize_optional_text(execution.get("assistant_text")),
        error_text=_normalize_optional_text(execution.get("error_text")),
        transcript_ref=_normalize_optional_text(execution.get("transcript_mirror_id")),
        metadata=_metadata(execution),
    )


__all__ = [
    "TURN_EXECUTION_CONTRACT_VERSION",
    "TURN_EXECUTION_CONTRACT_VERSION_COLUMN",
    "TURN_EXECUTION_RECORD_COLUMN",
    "TURN_EXECUTION_REQUEST_COLUMN",
    "build_turn_execution_record_from_storage",
    "build_turn_execution_request_from_storage",
]
