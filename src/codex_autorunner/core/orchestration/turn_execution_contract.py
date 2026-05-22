from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional

from ..car_context import CarContextProfile, normalize_car_context_profile
from ..text_utils import _normalize_optional_text
from .turn_assistant_output import TurnAssistantOutput

TURN_EXECUTION_CONTRACT_VERSION = 1

TurnExecutionTargetKind = Literal["thread", "flow"]
TurnExecutionRequestKind = Literal[
    "message",
    "review",
    "automation",
    "publish",
    "recovery",
    "lifecycle",
]
TurnExecutionBusyPolicy = Literal["queue", "interrupt", "reject"]
TurnExecutionOriginKind = Literal[
    "surface", "automation", "publish", "recovery", "system"
]
TurnExecutionStatus = Literal[
    "queued",
    "claiming",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "lost",
]

_TARGET_KINDS = frozenset({"thread", "flow"})
_REQUEST_KINDS = frozenset(
    {"message", "review", "automation", "publish", "recovery", "lifecycle"}
)
_BUSY_POLICIES = frozenset({"queue", "interrupt", "reject"})
_ORIGIN_KINDS = frozenset({"surface", "automation", "publish", "recovery", "system"})
_RECORD_STATUSES = frozenset(
    {
        "queued",
        "claiming",
        "running",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "lost",
    }
)


class TurnExecutionContractError(ValueError):
    """Raised when a canonical turn execution contract is invalid."""


def _required_text(value: object, field_name: str) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        raise TurnExecutionContractError(f"{field_name} is required")
    return normalized


def _optional_text(value: object) -> Optional[str]:
    return _normalize_optional_text(value)


def _optional_prompt_text(value: object) -> Optional[str]:
    if isinstance(value, str):
        return value if value.strip() else None
    return _normalize_optional_text(value)


def _normalize_choice(
    value: object,
    *,
    field_name: str,
    allowed: frozenset[str],
) -> str:
    normalized = _required_text(value, field_name)
    if normalized not in allowed:
        expected = ", ".join(sorted(allowed))
        raise TurnExecutionContractError(f"{field_name} must be one of: {expected}")
    return normalized


def _json_safe(value: Any, *, field_name: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TurnExecutionContractError(
                    f"{field_name} must use string object keys"
                )
            safe[key] = _json_safe(item, field_name=field_name)
        return safe
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, field_name=field_name) for item in value]
    raise TurnExecutionContractError(f"{field_name} must be JSON-safe")


def _mapping(value: object, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TurnExecutionContractError(f"{field_name} must be an object")
    return dict(_json_safe(dict(value), field_name=field_name))


def _mapping_tuple(value: object, *, field_name: str) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TurnExecutionContractError(f"{field_name} must be a list")
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TurnExecutionContractError(f"{field_name} entries must be objects")
        items.append(_mapping(item, field_name=field_name))
    return tuple(items)


def _validate_opencode_model(
    *,
    agent: str,
    model: Optional[str],
    model_payload: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _mapping(model_payload, field_name="model_payload")
    if agent != "opencode":
        return payload
    if model is None:
        raise TurnExecutionContractError(
            "opencode requests require resolved provider/model before execution"
        )
    if "/" not in model:
        raise TurnExecutionContractError(
            "opencode model must include provider/model before execution"
        )
    provider_from_model, model_from_model = (
        part.strip() for part in model.split("/", 1)
    )
    provider_id = _optional_text(payload.get("providerID"))
    model_id = _optional_text(payload.get("modelID"))
    if (
        not provider_from_model
        or not model_from_model
        or provider_id is None
        or model_id is None
    ):
        raise TurnExecutionContractError(
            "opencode model_payload requires providerID and modelID before execution"
        )
    if provider_id != provider_from_model or model_id != model_from_model:
        raise TurnExecutionContractError(
            "opencode model_payload must match the resolved provider/model"
        )
    return {"providerID": provider_id, "modelID": model_id}


@dataclass(frozen=True)
class TurnExecutionOrigin:
    kind: TurnExecutionOriginKind
    source_id: str
    surface_kind: Optional[str] = None
    surface_key: Optional[str] = None
    automation_rule_id: Optional[str] = None
    publish_operation_id: Optional[str] = None
    parent_request_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kind = _normalize_choice(
            self.kind, field_name="origin.kind", allowed=_ORIGIN_KINDS
        )
        source_id = _required_text(self.source_id, "origin.source_id")
        surface_kind = _optional_text(self.surface_kind)
        surface_key = _optional_text(self.surface_key)
        automation_rule_id = _optional_text(self.automation_rule_id)
        publish_operation_id = _optional_text(self.publish_operation_id)
        parent_request_id = _optional_text(self.parent_request_id)
        if kind == "surface" and (surface_kind is None or surface_key is None):
            raise TurnExecutionContractError(
                "surface origin requires surface_kind and surface_key"
            )
        if kind == "automation" and automation_rule_id is None:
            raise TurnExecutionContractError(
                "automation origin requires automation_rule_id"
            )
        if kind == "publish" and publish_operation_id is None:
            raise TurnExecutionContractError(
                "publish origin requires publish_operation_id"
            )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "surface_kind", surface_kind)
        object.__setattr__(self, "surface_key", surface_key)
        object.__setattr__(self, "automation_rule_id", automation_rule_id)
        object.__setattr__(self, "publish_operation_id", publish_operation_id)
        object.__setattr__(self, "parent_request_id", parent_request_id)
        object.__setattr__(
            self, "metadata", _mapping(self.metadata, field_name="origin.metadata")
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TurnExecutionOrigin":
        return cls(
            kind=data.get("kind"),  # type: ignore[arg-type]
            source_id=_required_text(data.get("source_id"), "origin.source_id"),
            surface_kind=data.get("surface_kind"),
            surface_key=data.get("surface_key"),
            automation_rule_id=data.get("automation_rule_id"),
            publish_operation_id=data.get("publish_operation_id"),
            parent_request_id=data.get("parent_request_id"),
            metadata=_mapping(data.get("metadata"), field_name="origin.metadata"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source_id": self.source_id,
            "surface_kind": self.surface_kind,
            "surface_key": self.surface_key,
            "automation_rule_id": self.automation_rule_id,
            "publish_operation_id": self.publish_operation_id,
            "parent_request_id": self.parent_request_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DeliveryIntentRef:
    kind: str
    intent_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _required_text(self.kind, "delivery.kind"))
        object.__setattr__(
            self, "intent_id", _required_text(self.intent_id, "delivery.intent_id")
        )
        object.__setattr__(
            self, "metadata", _mapping(self.metadata, field_name="delivery.metadata")
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "DeliveryIntentRef":
        return cls(
            kind=_required_text(data.get("kind"), "delivery.kind"),
            intent_id=_required_text(data.get("intent_id"), "delivery.intent_id"),
            metadata=_mapping(data.get("metadata"), field_name="delivery.metadata"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "intent_id": self.intent_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TurnExecutionRequest:
    request_id: str
    target_id: str
    target_kind: TurnExecutionTargetKind
    request_kind: TurnExecutionRequestKind
    busy_policy: TurnExecutionBusyPolicy
    agent: str
    approval_policy: str
    sandbox_policy: Any
    origin: TurnExecutionOrigin
    workspace_root: Optional[str] = None
    prompt_text: Optional[str] = None
    input_items: tuple[dict[str, Any], ...] = ()
    context_profile: Optional[CarContextProfile] = None
    profile: Optional[str] = None
    model: Optional[str] = None
    model_payload: dict[str, Any] = field(default_factory=dict)
    reasoning: Optional[str] = None
    approval_mode: Optional[str] = None
    client_request_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    correlation_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    delivery_intents: tuple[DeliveryIntentRef, ...] = ()
    contract_version: int = TURN_EXECUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        request_id = _required_text(self.request_id, "request_id")
        target_id = _required_text(self.target_id, "target_id")
        target_kind = _normalize_choice(
            self.target_kind, field_name="target_kind", allowed=_TARGET_KINDS
        )
        request_kind = _normalize_choice(
            self.request_kind, field_name="request_kind", allowed=_REQUEST_KINDS
        )
        busy_policy = _normalize_choice(
            self.busy_policy, field_name="busy_policy", allowed=_BUSY_POLICIES
        )
        agent = _required_text(self.agent, "agent")
        approval_policy = _required_text(self.approval_policy, "approval_policy")
        sandbox_policy = _json_safe(self.sandbox_policy, field_name="sandbox_policy")
        prompt_text = _optional_prompt_text(self.prompt_text)
        input_items = _mapping_tuple(self.input_items, field_name="input_items")
        if prompt_text is None and not input_items:
            raise TurnExecutionContractError("prompt_text or input_items is required")
        origin = self.origin
        if isinstance(origin, Mapping):
            origin = TurnExecutionOrigin.from_mapping(origin)
        if not isinstance(origin, TurnExecutionOrigin):
            raise TurnExecutionContractError("origin must be a TurnExecutionOrigin")
        if target_kind == "flow" and origin.kind in {"surface", "publish"}:
            raise TurnExecutionContractError(
                f"{origin.kind} origin cannot target a flow execution"
            )
        model = _optional_text(self.model)
        model_payload = _validate_opencode_model(
            agent=agent,
            model=model,
            model_payload=self.model_payload,
        )
        delivery_intents = tuple(
            (
                item
                if isinstance(item, DeliveryIntentRef)
                else DeliveryIntentRef.from_mapping(item)
            )
            for item in self.delivery_intents
        )
        object.__setattr__(self, "contract_version", TURN_EXECUTION_CONTRACT_VERSION)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "target_kind", target_kind)
        object.__setattr__(self, "request_kind", request_kind)
        object.__setattr__(self, "busy_policy", busy_policy)
        object.__setattr__(self, "agent", agent)
        object.__setattr__(self, "approval_policy", approval_policy)
        object.__setattr__(self, "sandbox_policy", sandbox_policy)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "workspace_root", _optional_text(self.workspace_root))
        object.__setattr__(self, "prompt_text", prompt_text)
        object.__setattr__(self, "input_items", input_items)
        object.__setattr__(
            self,
            "context_profile",
            normalize_car_context_profile(self.context_profile),
        )
        object.__setattr__(self, "profile", _optional_text(self.profile))
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "model_payload", model_payload)
        object.__setattr__(self, "reasoning", _optional_text(self.reasoning))
        object.__setattr__(self, "approval_mode", _optional_text(self.approval_mode))
        object.__setattr__(
            self, "client_request_id", _optional_text(self.client_request_id)
        )
        object.__setattr__(
            self, "idempotency_key", _optional_text(self.idempotency_key)
        )
        object.__setattr__(self, "correlation_id", _optional_text(self.correlation_id))
        object.__setattr__(
            self, "metadata", _mapping(self.metadata, field_name="metadata")
        )
        object.__setattr__(self, "delivery_intents", delivery_intents)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TurnExecutionRequest":
        version = data.get("contract_version")
        if version != TURN_EXECUTION_CONTRACT_VERSION:
            raise TurnExecutionContractError(
                f"unsupported turn execution contract version: {version!r}"
            )
        origin = data.get("origin")
        if not isinstance(origin, Mapping):
            raise TurnExecutionContractError("origin is required")
        return cls(
            request_id=_required_text(data.get("request_id"), "request_id"),
            target_id=_required_text(data.get("target_id"), "target_id"),
            target_kind=data.get("target_kind"),  # type: ignore[arg-type]
            request_kind=data.get("request_kind"),  # type: ignore[arg-type]
            busy_policy=data.get("busy_policy"),  # type: ignore[arg-type]
            agent=_required_text(data.get("agent"), "agent"),
            approval_policy=_required_text(
                data.get("approval_policy"), "approval_policy"
            ),
            sandbox_policy=data.get("sandbox_policy"),
            origin=TurnExecutionOrigin.from_mapping(origin),
            workspace_root=data.get("workspace_root"),
            prompt_text=data.get("prompt_text"),
            input_items=_mapping_tuple(
                data.get("input_items"), field_name="input_items"
            ),
            context_profile=normalize_car_context_profile(data.get("context_profile")),
            profile=data.get("profile"),
            model=data.get("model"),
            model_payload=_mapping(
                data.get("model_payload"), field_name="model_payload"
            ),
            reasoning=data.get("reasoning"),
            approval_mode=data.get("approval_mode"),
            client_request_id=data.get("client_request_id"),
            idempotency_key=data.get("idempotency_key"),
            correlation_id=data.get("correlation_id"),
            metadata=_mapping(data.get("metadata"), field_name="metadata"),
            delivery_intents=tuple(
                DeliveryIntentRef.from_mapping(item)
                for item in _mapping_tuple(
                    data.get("delivery_intents"), field_name="delivery_intents"
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "request_id": self.request_id,
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "workspace_root": self.workspace_root,
            "request_kind": self.request_kind,
            "busy_policy": self.busy_policy,
            "prompt_text": self.prompt_text,
            "input_items": [dict(item) for item in self.input_items],
            "context_profile": self.context_profile,
            "agent": self.agent,
            "profile": self.profile,
            "model": self.model,
            "model_payload": dict(self.model_payload),
            "reasoning": self.reasoning,
            "approval_policy": self.approval_policy,
            "approval_mode": self.approval_mode,
            "sandbox_policy": self.sandbox_policy,
            "client_request_id": self.client_request_id,
            "idempotency_key": self.idempotency_key,
            "correlation_id": self.correlation_id,
            "origin": self.origin.to_dict(),
            "metadata": dict(self.metadata),
            "delivery_intents": [item.to_dict() for item in self.delivery_intents],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )

    @classmethod
    def from_json(cls, payload: str) -> "TurnExecutionRequest":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TurnExecutionContractError("invalid request JSON") from exc
        if not isinstance(data, Mapping):
            raise TurnExecutionContractError("request JSON must encode an object")
        return cls.from_mapping(data)


@dataclass(frozen=True)
class TurnExecutionRecord:
    request: TurnExecutionRequest
    execution_id: str
    status: TurnExecutionStatus
    queued_at: Optional[str] = None
    claimed_at: Optional[str] = None
    started_at: Optional[str] = None
    terminal_at: Optional[str] = None
    backend_conversation_id: Optional[str] = None
    backend_turn_id: Optional[str] = None
    assistant_text: Optional[str] = None
    assistant_output: Optional[TurnAssistantOutput] = None
    error_text: Optional[str] = None
    transcript_ref: Optional[str] = None
    timeline_ref: Optional[str] = None
    cold_trace_ref: Optional[str] = None
    conflict_evidence: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = TURN_EXECUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        request = self.request
        if isinstance(request, Mapping):
            request = TurnExecutionRequest.from_mapping(request)
        if not isinstance(request, TurnExecutionRequest):
            raise TurnExecutionContractError(
                "record.request must be a TurnExecutionRequest"
            )
        object.__setattr__(self, "contract_version", TURN_EXECUTION_CONTRACT_VERSION)
        object.__setattr__(self, "request", request)
        object.__setattr__(
            self, "execution_id", _required_text(self.execution_id, "execution_id")
        )
        object.__setattr__(
            self,
            "status",
            _normalize_choice(
                self.status, field_name="status", allowed=_RECORD_STATUSES
            ),
        )
        for field_name in (
            "queued_at",
            "claimed_at",
            "started_at",
            "terminal_at",
            "backend_conversation_id",
            "backend_turn_id",
            "assistant_text",
            "error_text",
            "transcript_ref",
            "timeline_ref",
            "cold_trace_ref",
        ):
            object.__setattr__(
                self, field_name, _optional_text(getattr(self, field_name))
            )
        assistant_output = self.assistant_output
        if isinstance(assistant_output, Mapping):
            assistant_output = TurnAssistantOutput.from_mapping(dict(assistant_output))
        if assistant_output is not None and not isinstance(
            assistant_output, TurnAssistantOutput
        ):
            raise TurnExecutionContractError(
                "record.assistant_output must be a TurnAssistantOutput"
            )
        if assistant_output is not None:
            object.__setattr__(self, "assistant_output", assistant_output)
            object.__setattr__(self, "assistant_text", assistant_output.text)
        object.__setattr__(
            self,
            "conflict_evidence",
            _mapping(self.conflict_evidence, field_name="conflict_evidence"),
        )
        object.__setattr__(
            self, "metadata", _mapping(self.metadata, field_name="metadata")
        )

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TurnExecutionRecord":
        version = data.get("contract_version")
        if version != TURN_EXECUTION_CONTRACT_VERSION:
            raise TurnExecutionContractError(
                f"unsupported turn execution contract version: {version!r}"
            )
        request = data.get("request")
        if not isinstance(request, Mapping):
            raise TurnExecutionContractError("record.request is required")
        return cls(
            request=TurnExecutionRequest.from_mapping(request),
            execution_id=_required_text(data.get("execution_id"), "execution_id"),
            status=data.get("status"),  # type: ignore[arg-type]
            queued_at=data.get("queued_at"),
            claimed_at=data.get("claimed_at"),
            started_at=data.get("started_at"),
            terminal_at=data.get("terminal_at"),
            backend_conversation_id=data.get("backend_conversation_id"),
            backend_turn_id=data.get("backend_turn_id"),
            assistant_text=data.get("assistant_text"),
            assistant_output=(
                TurnAssistantOutput.from_mapping(dict(data["assistant_output"]))
                if isinstance(data.get("assistant_output"), Mapping)
                else None
            ),
            error_text=data.get("error_text"),
            transcript_ref=data.get("transcript_ref"),
            timeline_ref=data.get("timeline_ref"),
            cold_trace_ref=data.get("cold_trace_ref"),
            conflict_evidence=_mapping(
                data.get("conflict_evidence"), field_name="conflict_evidence"
            ),
            metadata=_mapping(data.get("metadata"), field_name="metadata"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "status": self.status,
            "queued_at": self.queued_at,
            "claimed_at": self.claimed_at,
            "started_at": self.started_at,
            "terminal_at": self.terminal_at,
            "backend_conversation_id": self.backend_conversation_id,
            "backend_turn_id": self.backend_turn_id,
            "assistant_text": self.assistant_text,
            "assistant_output": (
                self.assistant_output.to_durable_dict()
                if self.assistant_output is not None
                else None
            ),
            "error_text": self.error_text,
            "transcript_ref": self.transcript_ref,
            "timeline_ref": self.timeline_ref,
            "cold_trace_ref": self.cold_trace_ref,
            "conflict_evidence": dict(self.conflict_evidence),
            "metadata": dict(self.metadata),
            "request": self.request.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )

    @classmethod
    def from_json(cls, payload: str) -> "TurnExecutionRecord":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TurnExecutionContractError("invalid record JSON") from exc
        if not isinstance(data, Mapping):
            raise TurnExecutionContractError("record JSON must encode an object")
        return cls.from_mapping(data)


__all__ = [
    "TURN_EXECUTION_CONTRACT_VERSION",
    "DeliveryIntentRef",
    "TurnExecutionBusyPolicy",
    "TurnExecutionContractError",
    "TurnExecutionOrigin",
    "TurnExecutionOriginKind",
    "TurnExecutionRecord",
    "TurnExecutionRequest",
    "TurnExecutionRequestKind",
    "TurnExecutionStatus",
    "TurnExecutionTargetKind",
]
