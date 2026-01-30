from __future__ import annotations

import importlib.metadata
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Optional

from ..plugin_api import CAR_AGENT_ENTRYPOINT_GROUP, CAR_PLUGIN_API_VERSION
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
    """A registered agent backend.

    Built-in backends live in `_BUILTIN_AGENTS`. Additional backends MAY be loaded
    via Python entry points (see `CAR_AGENT_ENTRYPOINT_GROUP`).

    Plugins SHOULD set `plugin_api_version` to `CAR_PLUGIN_API_VERSION`.
    """

    id: str
    name: str
    capabilities: frozenset[AgentCapability]
    make_harness: Callable[[Any], AgentHarness]
    healthcheck: Optional[Callable[[Any], bool]] = None
    plugin_api_version: int = CAR_PLUGIN_API_VERSION


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


_BUILTIN_AGENTS: dict[str, AgentDescriptor] = {
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

# Lazy-loaded cache of built-in + plugin agents.
_AGENT_CACHE: Optional[dict[str, AgentDescriptor]] = None


def _select_entry_points(group: str) -> Iterable[importlib.metadata.EntryPoint]:
    """Compatibility wrapper for `importlib.metadata.entry_points()` across py versions."""

    eps = importlib.metadata.entry_points()
    # Python 3.9: may return a dict
    if isinstance(eps, dict):
        return eps.get(group, [])
    if hasattr(eps, "select"):
        return list(eps.select(group=group))
    return []


def _load_agent_plugins() -> dict[str, AgentDescriptor]:
    loaded: dict[str, AgentDescriptor] = {}
    for ep in _select_entry_points(CAR_AGENT_ENTRYPOINT_GROUP):
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Failed to load agent plugin entry point %s:%s: %s",
                ep.group,
                ep.name,
                exc,
            )
            continue

        descriptor: Optional[AgentDescriptor] = None
        if isinstance(obj, AgentDescriptor):
            descriptor = obj
        elif callable(obj):
            try:
                maybe = obj()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Agent plugin entry point %s:%s factory failed: %s",
                    ep.group,
                    ep.name,
                    exc,
                )
                continue
            if isinstance(maybe, AgentDescriptor):
                descriptor = maybe

        if descriptor is None:
            _logger.warning(
                "Ignoring agent plugin entry point %s:%s: expected AgentDescriptor or factory",
                ep.group,
                ep.name,
            )
            continue

        agent_id = (descriptor.id or "").strip().lower()
        if not agent_id:
            _logger.warning(
                "Ignoring agent plugin entry point %s:%s: missing id",
                ep.group,
                ep.name,
            )
            continue

        api_version_raw = getattr(descriptor, "plugin_api_version", None)
        try:
            api_version = int(api_version_raw)
        except Exception:
            api_version = None
        if api_version is None:
            _logger.warning(
                "Ignoring agent plugin %s: invalid api_version %s",
                agent_id,
                api_version_raw,
            )
            continue
        if api_version > CAR_PLUGIN_API_VERSION:
            _logger.warning(
                "Ignoring agent plugin %s (api_version=%s) requires newer core (%s)",
                agent_id,
                api_version,
                CAR_PLUGIN_API_VERSION,
            )
            continue
        if api_version < CAR_PLUGIN_API_VERSION:
            _logger.info(
                "Loaded agent plugin %s with older api_version=%s (current=%s)",
                agent_id,
                api_version,
                CAR_PLUGIN_API_VERSION,
            )

        if agent_id in _BUILTIN_AGENTS:
            _logger.warning(
                "Ignoring agent plugin %s: conflicts with built-in agent id",
                agent_id,
            )
            continue
        if agent_id in loaded:
            _logger.warning(
                "Ignoring duplicate agent plugin id %s from entry point %s:%s",
                agent_id,
                ep.group,
                ep.name,
            )
            continue

        loaded[agent_id] = descriptor
        _logger.info("Loaded agent plugin: %s (%s)", agent_id, descriptor.name)

    return loaded


def _all_agents() -> dict[str, AgentDescriptor]:
    global _AGENT_CACHE
    if _AGENT_CACHE is None:
        agents = _BUILTIN_AGENTS.copy()
        agents.update(_load_agent_plugins())
        _AGENT_CACHE = agents
    return _AGENT_CACHE


def reload_agents() -> dict[str, AgentDescriptor]:
    """Clear the plugin cache and reload agent backends.

    This is primarily useful for tests and local development.
    """

    global _AGENT_CACHE
    _AGENT_CACHE = None
    return get_registered_agents()


def get_registered_agents() -> dict[str, AgentDescriptor]:
    return _all_agents().copy()


def get_available_agents(app_ctx: Any) -> dict[str, AgentDescriptor]:
    available: dict[str, AgentDescriptor] = {}
    for agent_id, descriptor in _all_agents().items():
        if descriptor.healthcheck is None or descriptor.healthcheck(app_ctx):
            available[agent_id] = descriptor
    return available


def get_agent_descriptor(agent_id: str) -> Optional[AgentDescriptor]:
    normalized = (agent_id or "").strip().lower()
    return _all_agents().get(normalized)


def validate_agent_id(agent_id: str) -> str:
    normalized = (agent_id or "").strip().lower()
    if normalized not in _all_agents():
        raise ValueError(f"Unknown agent: {agent_id!r}")
    return normalized


def has_capability(agent_id: str, capability: AgentCapability) -> bool:
    descriptor = get_agent_descriptor(agent_id)
    if descriptor is None:
        return False
    return capability in descriptor.capabilities


__all__ = [
    "AgentCapability",
    "AgentDescriptor",
    "CAR_PLUGIN_API_VERSION",
    "CAR_AGENT_ENTRYPOINT_GROUP",
    "get_registered_agents",
    "get_available_agents",
    "get_agent_descriptor",
    "validate_agent_id",
    "has_capability",
    "reload_agents",
]
