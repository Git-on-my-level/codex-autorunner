from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ..core.agent_model_defaults import resolve_model_for_agent
from .opencode.protocol_payload import split_model_id
from .registry import (
    AgentExecutionTarget,
    get_agent_descriptor,
    resolve_agent_execution_target,
)

_APPROVAL_POLICIES = frozenset({"never", "unlessTrusted", "on-request"})
_SANDBOX_MODES = frozenset({"dangerFullAccess", "workspaceWrite", "readOnly"})


class AgentRuntimeOptionsError(ValueError):
    """Raised when runtime options cannot be represented by a backend protocol."""


@dataclass(frozen=True)
class AgentSandboxOptions:
    mode: str
    policy: Any


@dataclass(frozen=True)
class AgentRuntimeTimeouts:
    turn_timeout_seconds: Optional[float]
    request_timeout_seconds: Optional[float]
    session_stall_timeout_seconds: Optional[float]


@dataclass(frozen=True)
class AgentRuntimeOptions:
    requested_agent_id: str
    requested_profile: Optional[str]
    logical_agent_id: str
    logical_profile: Optional[str]
    runtime_agent_id: str
    runtime_profile: Optional[str]
    runtime_kind: str
    resolution_kind: str
    model: Optional[str]
    opencode_model_payload: Optional[dict[str, str]]
    reasoning: Optional[str]
    approval_policy: Optional[str]
    approval_policy_default: str
    sandbox_policy: Optional[str]
    sandbox: AgentSandboxOptions
    sandbox_policy_default: str
    reuse_session: bool
    timeouts: AgentRuntimeTimeouts
    output_policy: str
    default_approval_decision: str
    destination_metadata: Mapping[str, Any]

    @property
    def effective_approval_policy(self) -> str:
        return self.approval_policy or self.approval_policy_default

    @property
    def turn_timeout_seconds(self) -> Optional[float]:
        return self.timeouts.turn_timeout_seconds

    @property
    def request_timeout_seconds(self) -> Optional[float]:
        return self.timeouts.request_timeout_seconds

    @property
    def session_stall_timeout_seconds(self) -> Optional[float]:
        return self.timeouts.session_stall_timeout_seconds

    def backend_configure_kwargs(self) -> dict[str, Any]:
        return {
            "approval_policy": self.approval_policy,
            "approval_policy_default": self.approval_policy_default,
            "sandbox_policy": self.sandbox_policy,
            "sandbox_policy_default": self.sandbox_policy_default,
            "reuse_session": self.reuse_session,
            "model": self.model,
            "reasoning": self.reasoning,
            "reasoning_effort": self.reasoning,
            "turn_timeout_seconds": self.turn_timeout_seconds,
            "default_approval_decision": self.default_approval_decision,
        }


def _normalize_optional_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalize_runtime_text(value: object) -> Optional[str]:
    text = _normalize_optional_text(value)
    return text.lower() if text is not None else None


def _validate_choice(
    *,
    name: str,
    value: Optional[str],
    allowed: frozenset[str],
) -> None:
    if value is not None and value not in allowed:
        expected = ", ".join(sorted(allowed))
        raise AgentRuntimeOptionsError(f"{name} must be one of: {expected}")


def _sandbox_options(
    *,
    mode: Optional[str],
    default: str,
    workspace_root: Optional[Path],
    workspace_write_network: object,
) -> AgentSandboxOptions:
    sandbox_mode = mode or default
    _validate_choice(name="sandbox policy", value=sandbox_mode, allowed=_SANDBOX_MODES)
    if sandbox_mode == "workspaceWrite":
        if workspace_root is None:
            raise AgentRuntimeOptionsError(
                "workspaceWrite sandbox requires a workspace root"
            )
        return AgentSandboxOptions(
            mode=sandbox_mode,
            policy={
                "type": "workspaceWrite",
                "writableRoots": [str(workspace_root)],
                "networkAccess": bool(workspace_write_network),
            },
        )
    return AgentSandboxOptions(mode=sandbox_mode, policy=sandbox_mode)


def _runtime_kind(target: AgentExecutionTarget, config: Any) -> str:
    descriptor = get_agent_descriptor(target.runtime_agent_id, config)
    runtime_kind = str(getattr(descriptor, "runtime_kind", "") or "").strip().lower()
    return runtime_kind or target.runtime_agent_id


def resolve_opencode_model_payload(model: Optional[str]) -> Optional[dict[str, str]]:
    payload = split_model_id(model)
    if model and payload is None:
        raise AgentRuntimeOptionsError(
            "OpenCode model must include provider/model, for example 'zai/glm-5.1'"
        )
    return payload


def resolve_agent_runtime_options(
    agent_id: str,
    *,
    profile: Optional[str] = None,
    state: Any = None,
    config: Any = None,
    workspace_root: Optional[Path] = None,
    explicit_model: object = None,
    configured_model_default: object = None,
    explicit_reasoning: object = None,
    approval_policy: object = None,
    approval_policy_default: str = "never",
    sandbox_policy: object = None,
    sandbox_policy_default: str = "dangerFullAccess",
    reuse_session: Optional[bool] = None,
    include_builtin_model: bool = True,
    destination_metadata: Optional[Mapping[str, Any]] = None,
) -> AgentRuntimeOptions:
    target = resolve_agent_execution_target(agent_id, profile, context=config)
    runtime_kind = _runtime_kind(target, config)

    default_reasoning = None
    if runtime_kind == "codex":
        default_reasoning = getattr(config, "codex_reasoning", None)
    reasoning = (
        _normalize_optional_text(explicit_reasoning)
        or _normalize_optional_text(getattr(state, "autorunner_effort_override", None))
        or _normalize_optional_text(default_reasoning)
    )
    model = resolve_model_for_agent(
        target.runtime_agent_id,
        explicit_model,
        state=state,
        config=config,
        configured_default=configured_model_default,
        include_builtin=include_builtin_model,
    )

    approval = _normalize_optional_text(approval_policy)
    if approval is None:
        approval = _normalize_optional_text(
            getattr(state, "autorunner_approval_policy", None)
        )
    approval_default = _normalize_optional_text(approval_policy_default) or "never"
    _validate_choice(name="approval policy", value=approval, allowed=_APPROVAL_POLICIES)
    _validate_choice(
        name="approval policy default",
        value=approval_default,
        allowed=_APPROVAL_POLICIES,
    )

    sandbox = _normalize_optional_text(sandbox_policy)
    if sandbox is None:
        sandbox = _normalize_optional_text(
            getattr(state, "autorunner_sandbox_mode", None)
        )
    sandbox_default = (
        _normalize_optional_text(sandbox_policy_default) or "dangerFullAccess"
    )
    _validate_choice(
        name="sandbox policy default", value=sandbox_default, allowed=_SANDBOX_MODES
    )
    sandbox_options = _sandbox_options(
        mode=sandbox,
        default=sandbox_default,
        workspace_root=workspace_root,
        workspace_write_network=getattr(
            state, "autorunner_workspace_write_network", None
        ),
    )

    if reuse_session is None:
        reuse_session = bool(getattr(config, "autorunner_reuse_session", False))

    app_server_cfg = getattr(config, "app_server", None)
    opencode_cfg = getattr(config, "opencode", None)
    ticket_flow_cfg = getattr(config, "ticket_flow", None)
    output_cfg = getattr(app_server_cfg, "output", None)

    opencode_model_payload = (
        resolve_opencode_model_payload(model) if runtime_kind == "opencode" else None
    )

    return AgentRuntimeOptions(
        requested_agent_id=target.requested_agent_id,
        requested_profile=target.requested_profile,
        logical_agent_id=target.logical_agent_id,
        logical_profile=target.logical_profile,
        runtime_agent_id=target.runtime_agent_id,
        runtime_profile=target.runtime_profile,
        runtime_kind=runtime_kind,
        resolution_kind=target.resolution_kind,
        model=model,
        opencode_model_payload=opencode_model_payload,
        reasoning=reasoning,
        approval_policy=approval,
        approval_policy_default=approval_default,
        sandbox_policy=sandbox,
        sandbox=sandbox_options,
        sandbox_policy_default=sandbox_default,
        reuse_session=bool(reuse_session),
        timeouts=AgentRuntimeTimeouts(
            turn_timeout_seconds=getattr(app_server_cfg, "turn_timeout_seconds", None),
            request_timeout_seconds=getattr(app_server_cfg, "request_timeout", None),
            session_stall_timeout_seconds=getattr(
                opencode_cfg, "session_stall_timeout_seconds", None
            ),
        ),
        output_policy=str(getattr(output_cfg, "policy", "final_only")),
        default_approval_decision=(
            _normalize_optional_text(
                getattr(ticket_flow_cfg, "default_approval_decision", None)
            )
            or "accept"
        ),
        destination_metadata=dict(destination_metadata or {}),
    )


__all__ = [
    "AgentRuntimeOptions",
    "AgentRuntimeOptionsError",
    "AgentRuntimeTimeouts",
    "AgentSandboxOptions",
    "resolve_agent_runtime_options",
    "resolve_opencode_model_payload",
]
