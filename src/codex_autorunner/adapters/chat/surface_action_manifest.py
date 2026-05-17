"""Surface-neutral action manifest projection for CAR controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from ...core.agent_capability_projection import (
    CapabilityGateResult,
    build_capability_gate,
    project_thread_capabilities,
)
from ...core.flows import FlowActionPolicySnapshot, build_flow_action_policy
from .action_ux_contract import (
    ChatActionUxContractEntry,
    discord_slash_command_ux_contract_for_id,
)
from .command_contract import COMMAND_CONTRACT, CommandContractEntry

SurfaceKind = Literal["web", "discord", "telegram", "api"]
UiKind = Literal["pma_web", "discord", "telegram", "generic"]
ActionTargetKind = Literal[
    "ticket_flow", "managed_thread", "workspace", "run", "resource"
]

ACTION_MANIFEST_VERSION = "surface-action-manifest-v1"


@dataclass(frozen=True)
class SurfaceActionManifestContext:
    surface_kind: SurfaceKind
    ui_kind: UiKind = "generic"
    target_kind: ActionTargetKind = "resource"
    workspace_id: Optional[str] = None
    thread_id: Optional[str] = None
    run_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    lifecycle_state: Optional[str] = None
    worker_health_status: Optional[str] = None
    retire_mode: str = "blocked"
    has_run: bool = False
    has_open_tickets: bool = False
    capabilities: frozenset[str] = frozenset()
    route_prefix: str = "/api/flows"
    pma_route_prefix: str = "/hub/pma"


@dataclass(frozen=True)
class SurfaceActionManifestAction:
    action_id: str
    label: str
    description: str
    enabled: bool
    disabled_reason: Optional[str]
    requires_confirmation: bool
    priority: str
    tone: str
    method: str
    route: str
    payload_schema: dict[str, Any]
    missing_capabilities: tuple[str, ...] = ()
    command_id: Optional[str] = None
    ux_contract_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "label": self.label,
            "description": self.description,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "missing_capabilities": list(self.missing_capabilities),
            "requires_confirmation": self.requires_confirmation,
            "priority": self.priority,
            "tone": self.tone,
            "method": self.method,
            "route": self.route,
            "payload_schema": self.payload_schema,
            "command_id": self.command_id,
            "ux_contract_id": self.ux_contract_id,
        }


@dataclass(frozen=True)
class SurfaceActionManifest:
    version: str
    surface_kind: SurfaceKind
    ui_kind: UiKind
    target_kind: ActionTargetKind
    workspace_id: Optional[str]
    thread_id: Optional[str]
    run_id: Optional[str]
    resource_kind: Optional[str]
    resource_id: Optional[str]
    lifecycle_state: Optional[str]
    actions: tuple[SurfaceActionManifestAction, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "surface_kind": self.surface_kind,
            "ui_kind": self.ui_kind,
            "target_kind": self.target_kind,
            "workspace_id": self.workspace_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "lifecycle_state": self.lifecycle_state,
            "actions": [action.to_dict() for action in self.actions],
        }


_FLOW_COMMAND_IDS = {
    "start": "car.flow.start",
    "resume": "car.flow.resume",
    "stop": "car.flow.stop",
    "restart": "car.flow.restart",
    "retire": "car.flow.retire",
    "recover": "car.flow.recover",
    "refresh": "car.flow.status",
}

_FLOW_DESCRIPTIONS = {
    "start": "Start the ticket-flow queue for this workspace.",
    "resume": "Resume a paused ticket-flow run.",
    "stop": "Stop the active ticket-flow run.",
    "restart": "Start a new ticket-flow run after stopping the current one.",
    "retire": "Retire a completed ticket-flow run.",
    "recover": "Recover an unhealthy ticket-flow worker.",
    "refresh": "Refresh ticket-flow status.",
}


def build_surface_action_manifest(
    context: SurfaceActionManifestContext,
) -> SurfaceActionManifest:
    actions: tuple[SurfaceActionManifestAction, ...]
    if context.target_kind == "ticket_flow":
        actions = tuple(_ticket_flow_actions(context))
    elif context.target_kind == "managed_thread":
        actions = tuple(_managed_thread_actions(context))
    else:
        actions = ()
    return SurfaceActionManifest(
        version=ACTION_MANIFEST_VERSION,
        surface_kind=context.surface_kind,
        ui_kind=context.ui_kind,
        target_kind=context.target_kind,
        workspace_id=context.workspace_id,
        thread_id=context.thread_id,
        run_id=context.run_id,
        resource_kind=context.resource_kind,
        resource_id=context.resource_id,
        lifecycle_state=context.lifecycle_state,
        actions=actions,
    )


def _ticket_flow_actions(
    context: SurfaceActionManifestContext,
) -> list[SurfaceActionManifestAction]:
    descriptors = build_flow_action_policy(
        FlowActionPolicySnapshot(
            status=context.lifecycle_state,
            worker_health_status=context.worker_health_status,
            retire_mode=context.retire_mode,
            has_run=context.has_run,
            has_open_tickets=context.has_open_tickets,
            has_queue_scope=bool(context.resource_kind and context.resource_id),
        )
    )
    result: list[SurfaceActionManifestAction] = []
    for descriptor in descriptors:
        command_id = _FLOW_COMMAND_IDS.get(descriptor.action)
        command = _command_contract_for_id(command_id)
        ux = (
            discord_slash_command_ux_contract_for_id(command_id) if command_id else None
        )
        gate = _apply_capabilities(
            descriptor.enabled,
            descriptor.disabled_reason,
            command,
            context.capabilities,
        )
        result.append(
            SurfaceActionManifestAction(
                action_id=f"ticket_flow.{descriptor.action}",
                label=descriptor.label,
                description=_FLOW_DESCRIPTIONS.get(
                    descriptor.action, f"{descriptor.label}."
                ),
                enabled=gate.allowed,
                disabled_reason=gate.reason,
                missing_capabilities=gate.missing_capabilities,
                requires_confirmation=descriptor.requires_confirmation,
                priority=_priority_for_ux(ux),
                tone=descriptor.tone,
                method=_flow_method(descriptor.action),
                route=_flow_route(context, descriptor.action),
                payload_schema=_flow_payload_schema(descriptor.action),
                command_id=command_id,
                ux_contract_id=ux.id if ux else None,
            )
        )
    return result


def _managed_thread_actions(
    context: SurfaceActionManifestContext,
) -> list[SurfaceActionManifestAction]:
    is_running = (context.lifecycle_state or "").strip().lower() == "running"
    ux = discord_slash_command_ux_contract_for_id("car.interrupt")
    projection = project_thread_capabilities(
        thread_id=context.thread_id,
        agent_id="",
        capabilities=context.capabilities,
        has_running_turn=is_running,
    )
    gate = projection.gate("interrupt_thread")
    if context.thread_id is None:
        gate = CapabilityGateResult(
            allowed=False,
            missing_capabilities=gate.missing_capabilities,
            reason="Managed thread id is required",
        )
    return [
        SurfaceActionManifestAction(
            action_id="managed_thread.interrupt",
            label="Interrupt",
            description="Interrupt the active managed-thread turn.",
            enabled=gate.allowed,
            disabled_reason=gate.reason,
            missing_capabilities=gate.missing_capabilities,
            requires_confirmation=True,
            priority=_priority_for_ux(ux),
            tone="danger",
            method="POST",
            route=f"{context.pma_route_prefix}/threads/{context.thread_id}/interrupt",
            payload_schema={"type": "object", "additionalProperties": False},
            command_id="car.interrupt",
            ux_contract_id=ux.id if ux else None,
        )
    ]


def _flow_route(context: SurfaceActionManifestContext, action: str) -> str:
    prefix = context.route_prefix.rstrip("/")
    if action == "start":
        return f"{prefix}/ticket_flow/bootstrap"
    if action == "refresh":
        if context.run_id:
            return f"{prefix}/{context.run_id}/status"
        return f"{prefix}/ticket_flow/action-manifest"
    run_id = context.run_id or "{run_id}"
    suffix = "resume" if action == "resume" else action
    return f"{prefix}/{run_id}/{suffix}"


def _flow_method(action: str) -> str:
    return "GET" if action == "refresh" else "POST"


def _flow_payload_schema(action: str) -> dict[str, Any]:
    if action == "start":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "metadata": {
                    "type": "object",
                    "additionalProperties": True,
                }
            },
        }
    return {"type": "object", "additionalProperties": False}


def _command_contract_for_id(
    command_id: Optional[str],
) -> Optional[CommandContractEntry]:
    if not command_id:
        return None
    return next((entry for entry in COMMAND_CONTRACT if entry.id == command_id), None)


def _apply_capabilities(
    enabled: bool,
    disabled_reason: Optional[str],
    command: Optional[CommandContractEntry],
    capabilities: frozenset[str],
) -> CapabilityGateResult:
    required = command.required_capabilities if command else ()
    return build_capability_gate(
        capabilities=capabilities,
        required_capabilities=required,
        action_label=command.id if command else "use this action",
        unavailable_reason=None if enabled else disabled_reason,
    )


def _priority_for_ux(ux: Optional[ChatActionUxContractEntry]) -> str:
    if ux is None:
        return "normal"
    return ux.control_priority


__all__ = [
    "ACTION_MANIFEST_VERSION",
    "SurfaceActionManifest",
    "SurfaceActionManifestAction",
    "SurfaceActionManifestContext",
    "build_surface_action_manifest",
]
