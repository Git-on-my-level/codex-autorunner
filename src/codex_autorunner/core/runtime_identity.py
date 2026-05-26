from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .text_utils import _normalize_optional_text

RUNTIME_IDENTITY_CONTRACT_VERSION = 1

RUNTIME_STAGE_REQUESTED = "requested"
RUNTIME_STAGE_RESOLVED = "resolved"
RUNTIME_STAGE_LAUNCH = "launch"
RUNTIME_STAGE_EFFECTIVE = "effective"
RUNTIME_STAGE_PROJECTED = "projected"
RUNTIME_IDENTITY_STAGES = frozenset(
    {
        RUNTIME_STAGE_REQUESTED,
        RUNTIME_STAGE_RESOLVED,
        RUNTIME_STAGE_LAUNCH,
        RUNTIME_STAGE_EFFECTIVE,
        RUNTIME_STAGE_PROJECTED,
    }
)


class RuntimeIdentityContractError(ValueError):
    """Raised when the runtime identity contract is invalid."""


def _json_safe(value: Any, *, field_name: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeIdentityContractError(
                    f"{field_name} must use string object keys"
                )
            safe[key] = _json_safe(item, field_name=field_name)
        return safe
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, field_name=field_name) for item in value]
    raise RuntimeIdentityContractError(f"{field_name} must be JSON-safe")


def _mapping(value: object, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RuntimeIdentityContractError(f"{field_name} must be an object")
    return dict(_json_safe(dict(value), field_name=field_name))


def _optional_mapping(value: object, *, field_name: str) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    return _mapping(value, field_name=field_name)


def _stage(value: object, *, field_name: str = "stage") -> str:
    normalized = _normalize_optional_text(value)
    if normalized not in RUNTIME_IDENTITY_STAGES:
        expected = ", ".join(sorted(RUNTIME_IDENTITY_STAGES))
        raise RuntimeIdentityContractError(f"{field_name} must be one of: {expected}")
    return normalized


def _timestamp(value: object, *, field_name: str) -> Optional[str]:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeIdentityContractError(
            f"{field_name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _provider_model_from_payload(value: object) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(value, Mapping):
        return None, None
    provider_id = _normalize_optional_text(
        value.get("providerID") or value.get("provider_id") or value.get("provider")
    )
    model_id = _normalize_optional_text(
        value.get("modelID") or value.get("model_id") or value.get("model")
    )
    return provider_id, model_id


@dataclass(frozen=True)
class RuntimeIdentityStage:
    stage: str
    logical_agent: Optional[str] = None
    runtime_agent: Optional[str] = None
    provider_id: Optional[str] = None
    canonical_model_label: Optional[str] = None
    provider_model_id: Optional[str] = None
    profile: Optional[str] = None
    reasoning: Optional[str] = None
    approval_policy: Optional[str] = None
    sandbox_policy: Any = None
    workspace_scope: Optional[dict[str, Any]] = None
    prompt_ref: Optional[dict[str, Any]] = None
    input_ref: Optional[dict[str, Any]] = None
    backend_runtime_id: Optional[str] = None
    provider_payload: Optional[dict[str, Any]] = None
    source: Optional[str] = None
    observed_at: Optional[str] = None
    provenance: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        provider_payload = _optional_mapping(
            self.provider_payload, field_name=f"{self.stage}.provider_payload"
        )
        provider_id = _normalize_optional_text(self.provider_id)
        provider_model_id = _normalize_optional_text(self.provider_model_id)
        payload_provider_id, payload_model_id = _provider_model_from_payload(
            provider_payload
        )
        if provider_id is None:
            provider_id = payload_provider_id
        if provider_model_id is None:
            provider_model_id = payload_model_id
        object.__setattr__(self, "stage", _stage(self.stage))
        object.__setattr__(
            self, "logical_agent", _normalize_optional_text(self.logical_agent)
        )
        object.__setattr__(
            self, "runtime_agent", _normalize_optional_text(self.runtime_agent)
        )
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(
            self,
            "canonical_model_label",
            _normalize_optional_text(self.canonical_model_label),
        )
        object.__setattr__(self, "provider_model_id", provider_model_id)
        object.__setattr__(self, "profile", _normalize_optional_text(self.profile))
        object.__setattr__(self, "reasoning", _normalize_optional_text(self.reasoning))
        object.__setattr__(
            self, "approval_policy", _normalize_optional_text(self.approval_policy)
        )
        object.__setattr__(
            self,
            "sandbox_policy",
            _json_safe(self.sandbox_policy, field_name="sandbox_policy"),
        )
        object.__setattr__(
            self,
            "workspace_scope",
            _optional_mapping(
                self.workspace_scope, field_name=f"{self.stage}.workspace_scope"
            ),
        )
        object.__setattr__(
            self,
            "prompt_ref",
            _optional_mapping(self.prompt_ref, field_name=f"{self.stage}.prompt_ref"),
        )
        object.__setattr__(
            self,
            "input_ref",
            _optional_mapping(self.input_ref, field_name=f"{self.stage}.input_ref"),
        )
        object.__setattr__(
            self,
            "backend_runtime_id",
            _normalize_optional_text(self.backend_runtime_id),
        )
        object.__setattr__(self, "provider_payload", provider_payload)
        object.__setattr__(self, "source", _normalize_optional_text(self.source))
        object.__setattr__(
            self, "observed_at", _timestamp(self.observed_at, field_name="observed_at")
        )
        object.__setattr__(
            self, "provenance", _mapping(self.provenance, field_name="provenance")
        )
        object.__setattr__(
            self, "metadata", _mapping(self.metadata, field_name="metadata")
        )

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        stage: Optional[str] = None,
        field_name: str = "runtime_stage",
    ) -> "RuntimeIdentityStage":
        payload = _mapping(data, field_name=field_name)
        resolved_stage = stage or payload.get("stage")
        provider_payload = payload.get("provider_payload")
        return cls(
            stage=_stage(resolved_stage, field_name=f"{field_name}.stage"),
            logical_agent=payload.get("logical_agent", payload.get("agent")),
            runtime_agent=payload.get("runtime_agent"),
            provider_id=payload.get("provider_id"),
            canonical_model_label=payload.get(
                "canonical_model_label", payload.get("model")
            ),
            provider_model_id=payload.get("provider_model_id"),
            profile=payload.get("profile"),
            reasoning=payload.get("reasoning"),
            approval_policy=payload.get("approval_policy"),
            sandbox_policy=payload.get("sandbox_policy"),
            workspace_scope=payload.get("workspace_scope"),
            prompt_ref=payload.get("prompt_ref"),
            input_ref=payload.get("input_ref"),
            backend_runtime_id=payload.get("backend_runtime_id"),
            provider_payload=provider_payload,
            source=payload.get("source"),
            observed_at=payload.get("observed_at"),
            provenance=payload.get("provenance") or {},
            metadata=payload.get("metadata") or {},
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        stage: Optional[str] = None,
        field_name: str = "runtime_stage",
    ) -> "RuntimeIdentityStage":
        return cls.from_mapping(data, stage=stage, field_name=field_name)

    @classmethod
    def from_automation_runtime(
        cls, data: Mapping[str, Any], *, stage: str = RUNTIME_STAGE_REQUESTED
    ) -> "RuntimeIdentityStage":
        return cls.from_mapping(data, stage=stage, field_name="automation_runtime")

    @classmethod
    def from_turn_execution_request(
        cls, request: Mapping[str, Any], *, stage: str = RUNTIME_STAGE_RESOLVED
    ) -> "RuntimeIdentityStage":
        model_payload = request.get("model_payload")
        return cls(
            stage=stage,
            logical_agent=request.get("agent"),
            runtime_agent=request.get("agent"),
            canonical_model_label=request.get("model"),
            profile=request.get("profile") or request.get("agent_profile"),
            reasoning=request.get("reasoning"),
            approval_policy=request.get("approval_policy"),
            sandbox_policy=request.get("sandbox_policy"),
            workspace_scope=(
                {"workspace_root": request.get("workspace_root")}
                if _normalize_optional_text(request.get("workspace_root")) is not None
                else None
            ),
            prompt_ref=(
                {
                    "kind": "turn_execution_request",
                    "request_id": request.get("request_id"),
                }
                if _normalize_optional_text(request.get("request_id")) is not None
                else None
            ),
            input_ref=(
                {"kind": "turn_execution_input_items"}
                if request.get("input_items")
                else None
            ),
            provider_payload=(
                dict(model_payload) if isinstance(model_payload, Mapping) else None
            ),
            source="turn_execution_request",
            provenance={
                "request_id": request.get("request_id"),
                "target_id": request.get("target_id"),
                "target_kind": request.get("target_kind"),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "logical_agent": self.logical_agent,
            "runtime_agent": self.runtime_agent,
            "provider_id": self.provider_id,
            "canonical_model_label": self.canonical_model_label,
            "provider_model_id": self.provider_model_id,
            "profile": self.profile,
            "reasoning": self.reasoning,
            "approval_policy": self.approval_policy,
            "sandbox_policy": self.sandbox_policy,
            "workspace_scope": self.workspace_scope,
            "prompt_ref": self.prompt_ref,
            "input_ref": self.input_ref,
            "backend_runtime_id": self.backend_runtime_id,
            "provider_payload": self.provider_payload,
            "source": self.source,
            "observed_at": self.observed_at,
            "provenance": dict(self.provenance),
            "metadata": dict(self.metadata),
        }

    def to_automation_runtime_dict(self) -> dict[str, Any]:
        return {
            "agent": self.logical_agent,
            "model": self.canonical_model_label,
            "profile": self.profile,
            "reasoning": self.reasoning,
            "approval_policy": self.approval_policy,
            "sandbox_policy": self.sandbox_policy,
            "prompt_ref": self.prompt_ref,
            "input_ref": self.input_ref,
            "workspace_scope": self.workspace_scope,
            "backend_runtime_id": self.backend_runtime_id,
            "provider_payload": self.provider_payload,
        }


@dataclass(frozen=True)
class RuntimeIdentityEnvelope:
    requested: Optional[RuntimeIdentityStage] = None
    resolved: Optional[RuntimeIdentityStage] = None
    launch: Optional[RuntimeIdentityStage] = None
    effective: Optional[RuntimeIdentityStage] = None
    projected: Optional[RuntimeIdentityStage] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = RUNTIME_IDENTITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for stage_name in sorted(RUNTIME_IDENTITY_STAGES):
            value = getattr(self, stage_name)
            if value is None:
                continue
            if isinstance(value, Mapping):
                value = RuntimeIdentityStage.from_mapping(value, stage=stage_name)
            if not isinstance(value, RuntimeIdentityStage):
                raise RuntimeIdentityContractError(
                    f"{stage_name} must be a RuntimeIdentityStage"
                )
            if value.stage != stage_name:
                raise RuntimeIdentityContractError(
                    f"{stage_name} stage object must have stage={stage_name!r}"
                )
            object.__setattr__(self, stage_name, value)
        object.__setattr__(self, "contract_version", RUNTIME_IDENTITY_CONTRACT_VERSION)
        object.__setattr__(
            self, "metadata", _mapping(self.metadata, field_name="metadata")
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RuntimeIdentityEnvelope":
        payload = _mapping(data, field_name="runtime_identity")
        version = payload.get("contract_version")
        if version != RUNTIME_IDENTITY_CONTRACT_VERSION:
            raise RuntimeIdentityContractError(
                f"unsupported runtime identity contract version: {version!r}"
            )
        stages: dict[str, Optional[RuntimeIdentityStage]] = {}
        for stage_name in sorted(RUNTIME_IDENTITY_STAGES):
            raw = payload.get(stage_name)
            if raw is None:
                stages[stage_name] = None
                continue
            if not isinstance(raw, Mapping):
                raise RuntimeIdentityContractError(f"{stage_name} must be an object")
            raw_stage = _normalize_optional_text(raw.get("stage"))
            if raw_stage is not None and raw_stage != stage_name:
                raise RuntimeIdentityContractError(
                    f"{stage_name}.stage must be {stage_name!r}"
                )
            stages[stage_name] = RuntimeIdentityStage.from_mapping(
                raw, stage=stage_name, field_name=stage_name
            )
        return cls(
            requested=stages.get(RUNTIME_STAGE_REQUESTED),
            resolved=stages.get(RUNTIME_STAGE_RESOLVED),
            launch=stages.get(RUNTIME_STAGE_LAUNCH),
            effective=stages.get(RUNTIME_STAGE_EFFECTIVE),
            projected=stages.get(RUNTIME_STAGE_PROJECTED),
            metadata=payload.get("metadata") or {},
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuntimeIdentityEnvelope":
        return cls.from_mapping(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "requested": self.requested.to_dict() if self.requested else None,
            "resolved": self.resolved.to_dict() if self.resolved else None,
            "launch": self.launch.to_dict() if self.launch else None,
            "effective": self.effective.to_dict() if self.effective else None,
            "projected": self.projected.to_dict() if self.projected else None,
            "metadata": dict(self.metadata),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )

    @classmethod
    def from_json(cls, payload: str) -> "RuntimeIdentityEnvelope":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeIdentityContractError("invalid runtime identity JSON") from exc
        if not isinstance(data, Mapping):
            raise RuntimeIdentityContractError(
                "runtime identity JSON must encode an object"
            )
        return cls.from_mapping(data)


__all__ = [
    "RUNTIME_IDENTITY_CONTRACT_VERSION",
    "RUNTIME_IDENTITY_STAGES",
    "RUNTIME_STAGE_EFFECTIVE",
    "RUNTIME_STAGE_LAUNCH",
    "RUNTIME_STAGE_PROJECTED",
    "RUNTIME_STAGE_REQUESTED",
    "RUNTIME_STAGE_RESOLVED",
    "RuntimeIdentityContractError",
    "RuntimeIdentityEnvelope",
    "RuntimeIdentityStage",
]
