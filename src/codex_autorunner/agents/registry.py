from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from .base import AgentHarness
from .codex.harness import CodexHarness
from .opencode.harness import OpenCodeHarness

_logger = logging.getLogger(__name__)

AgentCapability = Literal[
    "threads",
    "turns",
    "review",
    "model_listing",
    "event_streaming",
    "approvals",
]


@dataclass(frozen=True)
class AgentDescriptor:
    id: str
    name: str
    capabilities: frozenset[AgentCapability]
    make_harness: Callable[[Any], AgentHarness]
    healthcheck: Optional[Callable[[Any], bool]] = None


def _make_codex_harness(ctx: Any) -> AgentHarness:
    supervisor = ctx.app_server_supervisor
    events = ctx.app_server_events
    if supervisor is None or events is None:
        raise RuntimeError("Codex harness unavailable: supervisor or events missing")
    return CodexHarness(supervisor, events)


def _make_opencode_harness(ctx: Any) -> AgentHarness:
    supervisor = ctx.opencode_supervisor
    if supervisor is None:
        raise RuntimeError("OpenCode harness unavailable: supervisor missing")
    return OpenCodeHarness(supervisor)


def _check_codex_health(ctx: Any) -> bool:
    supervisor = ctx.app_server_supervisor
    return supervisor is not None


def _check_opencode_health(ctx: Any) -> bool:
    supervisor = ctx.opencode_supervisor
    return supervisor is not None


_REGISTERED_AGENTS: dict[str, AgentDescriptor] = {
    "codex": AgentDescriptor(
        id="codex",
        name="Codex",
        capabilities=frozenset(
            [
                "threads",
                "turns",
                "review",
                "model_listing",
                "event_streaming",
                "approvals",
            ]
        ),
        make_harness=_make_codex_harness,
        healthcheck=_check_codex_health,
    ),
    "opencode": AgentDescriptor(
        id="opencode",
        name="OpenCode",
        capabilities=frozenset(
            [
                "threads",
                "turns",
                "review",
                "model_listing",
                "event_streaming",
            ]
        ),
        make_harness=_make_opencode_harness,
        healthcheck=_check_opencode_health,
    ),
}


def get_registered_agents() -> dict[str, AgentDescriptor]:
    return _REGISTERED_AGENTS.copy()


def get_available_agents(app_ctx: Any) -> dict[str, AgentDescriptor]:
    available = {}
    for agent_id, descriptor in _REGISTERED_AGENTS.items():
        if descriptor.healthcheck is None or descriptor.healthcheck(app_ctx):
            available[agent_id] = descriptor
    return available


def get_agent_descriptor(agent_id: str) -> Optional[AgentDescriptor]:
    return _REGISTERED_AGENTS.get(agent_id)


def validate_agent_id(agent_id: str) -> str:
    normalized = (agent_id or "").strip().lower()
    if normalized not in _REGISTERED_AGENTS:
        raise ValueError(f"Unknown agent: {agent_id!r}")
    return normalized


def has_capability(agent_id: str, capability: AgentCapability) -> bool:
    descriptor = _REGISTERED_AGENTS.get(agent_id)
    if descriptor is None:
        return False
    return capability in descriptor.capabilities


__all__ = [
    "AgentCapability",
    "AgentDescriptor",
    "get_registered_agents",
    "get_available_agents",
    "get_agent_descriptor",
    "validate_agent_id",
    "has_capability",
]
