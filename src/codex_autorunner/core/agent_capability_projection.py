"""Shared backend capability gates for agent and thread UI controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from ..runtime_capabilities import normalize_runtime_capabilities


@dataclass(frozen=True)
class CapabilityGateResult:
    allowed: bool
    missing_capabilities: tuple[str, ...] = ()
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "missing_capabilities": list(self.missing_capabilities),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AgentCapabilityProjection:
    agent_id: str
    capabilities: frozenset[str]
    actions: Mapping[str, CapabilityGateResult]

    def gate(self, action: str) -> CapabilityGateResult:
        return self.actions.get(
            action,
            CapabilityGateResult(
                allowed=False,
                reason=f"Unknown capability-gated action: {action}",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "capabilities": sorted(self.capabilities),
            "actions": {
                action: result.to_dict()
                for action, result in sorted(self.actions.items())
            },
        }


@dataclass(frozen=True)
class ThreadCapabilityProjection:
    thread_id: Optional[str]
    agent_id: str
    capabilities: frozenset[str]
    actions: Mapping[str, CapabilityGateResult]

    def gate(self, action: str) -> CapabilityGateResult:
        return self.actions.get(
            action,
            CapabilityGateResult(
                allowed=False,
                reason=f"Unknown capability-gated action: {action}",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "agent_id": self.agent_id,
            "capabilities": sorted(self.capabilities),
            "actions": {
                action: result.to_dict()
                for action, result in sorted(self.actions.items())
            },
        }


AGENT_ACTION_REQUIREMENTS: Mapping[str, tuple[str, ...]] = {
    "list_models": ("model_listing",),
    "use_durable_threads": ("durable_threads",),
    "use_message_turns": ("message_turns",),
    "use_file_mentions": ("message_turns",),
    "use_approvals": ("approvals",),
    "use_sandbox_policy": ("message_turns",),
    "use_model_overrides": ("message_turns",),
}

THREAD_ACTION_REQUIREMENTS: Mapping[str, tuple[str, ...]] = {
    "interrupt_thread": ("interrupt",),
    "use_durable_threads": ("durable_threads",),
    "use_message_turns": ("message_turns",),
    "use_file_mentions": ("message_turns",),
    "use_approvals": ("approvals",),
    "use_sandbox_policy": ("message_turns",),
    "use_model_overrides": ("message_turns",),
}

_ACTION_LABELS: Mapping[str, str] = {
    "list_models": "list models",
    "interrupt_thread": "interrupt this thread",
    "use_durable_threads": "use durable threads",
    "use_message_turns": "use message turns",
    "use_file_mentions": "use file mentions",
    "use_approvals": "use approvals",
    "use_sandbox_policy": "set sandbox policy",
    "use_model_overrides": "set model overrides",
}


def build_capability_gate(
    *,
    capabilities: Iterable[str],
    required_capabilities: Iterable[str],
    action_label: str,
    unavailable_reason: Optional[str] = None,
) -> CapabilityGateResult:
    normalized_capabilities = _normalize_capabilities(capabilities)
    required = tuple(sorted(_normalize_capabilities(required_capabilities)))
    missing = tuple(
        capability
        for capability in required
        if capability not in normalized_capabilities
    )
    if missing:
        return CapabilityGateResult(
            allowed=False,
            missing_capabilities=missing,
            reason=(
                f"Cannot {action_label}; missing capability: {missing[0]}"
                if len(missing) == 1
                else f"Cannot {action_label}; missing capabilities: {', '.join(missing)}"
            ),
        )
    if unavailable_reason:
        return CapabilityGateResult(allowed=False, reason=unavailable_reason)
    return CapabilityGateResult(allowed=True)


def project_agent_capabilities(
    agent_id: str,
    capabilities: Iterable[str],
) -> AgentCapabilityProjection:
    normalized = _normalize_capabilities(capabilities)
    return AgentCapabilityProjection(
        agent_id=(agent_id or "").strip().lower(),
        capabilities=normalized,
        actions={
            action: build_capability_gate(
                capabilities=normalized,
                required_capabilities=required,
                action_label=_ACTION_LABELS[action],
            )
            for action, required in AGENT_ACTION_REQUIREMENTS.items()
        },
    )


def project_thread_capabilities(
    *,
    thread_id: Optional[str],
    agent_id: str,
    capabilities: Iterable[str],
    has_running_turn: bool = False,
) -> ThreadCapabilityProjection:
    normalized = _normalize_capabilities(capabilities)
    actions = {
        action: build_capability_gate(
            capabilities=normalized,
            required_capabilities=required,
            action_label=_ACTION_LABELS[action],
        )
        for action, required in THREAD_ACTION_REQUIREMENTS.items()
    }
    if actions["interrupt_thread"].allowed and not has_running_turn:
        actions["interrupt_thread"] = CapabilityGateResult(
            allowed=False,
            reason="Managed thread has no active turn",
        )
    return ThreadCapabilityProjection(
        thread_id=thread_id,
        agent_id=(agent_id or "").strip().lower(),
        capabilities=normalized,
        actions=actions,
    )


def _normalize_capabilities(capabilities: Iterable[str]) -> frozenset[str]:
    return frozenset(
        str(capability) for capability in normalize_runtime_capabilities(capabilities)
    )


__all__ = [
    "AGENT_ACTION_REQUIREMENTS",
    "THREAD_ACTION_REQUIREMENTS",
    "AgentCapabilityProjection",
    "CapabilityGateResult",
    "ThreadCapabilityProjection",
    "build_capability_gate",
    "project_agent_capabilities",
    "project_thread_capabilities",
]
