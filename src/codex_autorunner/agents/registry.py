from __future__ import annotations

import importlib.metadata
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, cast

from ..core.config import load_hub_config, load_repo_config
from ..plugin_api import CAR_AGENT_ENTRYPOINT_GROUP, CAR_PLUGIN_API_VERSION
from .aliased_harness import AliasedAgentHarness
from .base import AgentHarness
from .codex.harness import CodexHarness
from .hermes.harness import HERMES_CAPABILITIES, HermesHarness
from .hermes.supervisor import (
    HermesApprovalHandler,
    build_hermes_supervisor_from_config,
    hermes_binary_available,
    hermes_runtime_preflight,
)
from .opencode.harness import OpenCodeHarness
from .types import RuntimeCapability, normalize_runtime_capabilities
from .zeroclaw.harness import ZEROCLAW_CAPABILITIES, ZeroClawHarness
from .zeroclaw.supervisor import (
    build_zeroclaw_supervisor_from_config,
    zeroclaw_binary_available,
    zeroclaw_runtime_preflight,
)

_logger = logging.getLogger(__name__)
AgentCapability = RuntimeCapability


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
    runtime_kind: Optional[str] = None
    plugin_api_version: int = CAR_PLUGIN_API_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capabilities",
            normalize_agent_capabilities(self.capabilities),
        )
        runtime_kind = str(self.runtime_kind or self.id or "").strip().lower()
        object.__setattr__(
            self,
            "runtime_kind",
            runtime_kind or str(self.id).strip().lower(),
        )


class _RequestedAgentContext:
    def __init__(
        self, delegate: Any, *, agent_id: str, profile: Optional[str] = None
    ) -> None:
        object.__setattr__(self, "_delegate", delegate)
        object.__setattr__(self, "_requested_agent_id", agent_id)
        object.__setattr__(self, "_requested_agent_profile", profile)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._delegate, name, value)


def normalize_agent_capabilities(
    capabilities: Iterable[str],
) -> frozenset[AgentCapability]:
    return normalize_runtime_capabilities(capabilities)


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


def _make_zeroclaw_harness(ctx: Any) -> AgentHarness:
    supervisor = getattr(ctx, "zeroclaw_supervisor", None)
    if supervisor is None:
        config = _resolve_runtime_agent_config(ctx)
        logger = getattr(ctx, "logger", None)
        if config is None:
            raise RuntimeError("ZeroClaw harness unavailable: config missing")
        supervisor = build_zeroclaw_supervisor_from_config(config, logger=logger)
        if supervisor is None:
            raise RuntimeError("ZeroClaw harness unavailable: binary not configured")
        try:
            ctx.zeroclaw_supervisor = supervisor
        except Exception:
            pass
    return ZeroClawHarness(supervisor)


def _check_zeroclaw_health(ctx: Any) -> bool:
    supervisor = getattr(ctx, "zeroclaw_supervisor", None)
    if supervisor is not None:
        return True
    config = _resolve_runtime_agent_config(ctx)
    if config is not None:
        return zeroclaw_runtime_preflight(config).status == "ready"
    binary = getattr(ctx, "zeroclaw_binary", None)
    if isinstance(binary, str) and binary.strip():
        return zeroclaw_binary_available(
            type(
                "_InlineConfig",
                (),
                {"agent_binary": staticmethod(lambda _agent_id: binary.strip())},
            )()
        )
    return False


def _resolve_requested_agent_id(ctx: Any, *, default: str) -> str:
    requested = getattr(ctx, "_requested_agent_id", None)
    if isinstance(requested, str) and requested.strip():
        return requested.strip().lower()
    return default


def _resolve_requested_agent_profile(ctx: Any) -> Optional[str]:
    requested = getattr(ctx, "_requested_agent_profile", None)
    if isinstance(requested, str) and requested.strip():
        return requested.strip().lower()
    return None


def _runtime_supervisor_cache(ctx: Any) -> dict[tuple[str, str, str], Any]:
    cache = getattr(ctx, "_agent_runtime_supervisors", None)
    if isinstance(cache, dict):
        return cache
    cache = {}
    try:
        ctx._agent_runtime_supervisors = cache
    except Exception:
        pass
    return cache


def _run_hermes_preflight(
    config: Any, *, agent_id: str, profile: Optional[str] = None
) -> Any:
    try:
        return hermes_runtime_preflight(
            config,
            agent_id=agent_id,
            profile=profile,
        )
    except TypeError as exc:
        if "agent_id" not in str(exc) and "profile" not in str(exc):
            raise
        return hermes_runtime_preflight(config)


def _make_hermes_harness(ctx: Any) -> AgentHarness:
    requested_agent_id = _resolve_requested_agent_id(ctx, default="hermes")
    requested_profile = _resolve_requested_agent_profile(ctx)
    cache = _runtime_supervisor_cache(ctx)
    cache_key = ("hermes", requested_agent_id, requested_profile or "")
    supervisor = cache.get(cache_key)
    if (
        supervisor is None
        and requested_agent_id == "hermes"
        and requested_profile is None
    ):
        supervisor = getattr(ctx, "hermes_supervisor", None)
    if supervisor is None:
        config = _resolve_runtime_agent_config(ctx)
        logger = getattr(ctx, "logger", None)
        if config is None:
            raise RuntimeError("Hermes harness unavailable: config missing")
        supervisor = build_hermes_supervisor_from_config(
            config,
            agent_id=requested_agent_id,
            profile=requested_profile,
            logger=logger,
            approval_handler=_resolve_surface_approval_handler(ctx),
            default_approval_decision=_resolve_default_approval_decision(ctx),
        )
        if supervisor is None:
            raise RuntimeError("Hermes harness unavailable: binary not configured")
        cache[cache_key] = supervisor
        if requested_agent_id == "hermes" and requested_profile is None:
            try:
                ctx.hermes_supervisor = supervisor
            except Exception:
                pass
    return HermesHarness(supervisor)


def _check_hermes_health(ctx: Any) -> bool:
    requested_agent_id = _resolve_requested_agent_id(ctx, default="hermes")
    requested_profile = _resolve_requested_agent_profile(ctx)
    cache = _runtime_supervisor_cache(ctx)
    supervisor = cache.get(("hermes", requested_agent_id, requested_profile or ""))
    if (
        supervisor is None
        and requested_agent_id == "hermes"
        and requested_profile is None
    ):
        supervisor = getattr(ctx, "hermes_supervisor", None)
    if supervisor is not None:
        return True
    config = _resolve_runtime_agent_config(ctx)
    if config is not None:
        result = _run_hermes_preflight(
            config,
            agent_id=requested_agent_id,
            profile=requested_profile,
        )
        return bool(getattr(result, "status", None) == "ready")
    binary = getattr(ctx, "hermes_binary", None)
    if isinstance(binary, str) and binary.strip():
        return hermes_binary_available(
            type(
                "_InlineConfig",
                (),
                {
                    "agent_binary": staticmethod(lambda _agent_id: binary.strip()),
                    "agent_backend": staticmethod(lambda _agent_id: "hermes"),
                },
            )()
        )
    return False


def _resolve_surface_approval_handler(ctx: Any) -> Optional[HermesApprovalHandler]:
    for attr in ("_handle_backend_approval_request", "_handle_approval_request"):
        handler = getattr(ctx, attr, None)
        if callable(handler):
            return cast(HermesApprovalHandler, handler)
    return None


def _resolve_default_approval_decision(ctx: Any) -> str:
    config = _resolve_runtime_agent_config(ctx)
    ticket_flow = getattr(config, "ticket_flow", None)
    value = getattr(ticket_flow, "default_approval_decision", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "cancel"


def _resolve_runtime_agent_config(ctx: Any) -> Any:
    if callable(getattr(ctx, "agent_binary", None)):
        return ctx
    for attr in ("config", "_config"):
        config = getattr(ctx, attr, None)
        if callable(getattr(config, "agent_binary", None)):
            return config

    root = _resolve_context_root(ctx)
    if root is None:
        return None

    loaders: tuple[Callable[[Path], Any], ...] = (
        lambda path: load_hub_config(path),
        lambda path: load_repo_config(path),
    )
    if isinstance(ctx, Path):
        loaders = tuple(reversed(loaders))

    for loader in loaders:
        try:
            config = loader(root)
        except Exception:
            continue
        if callable(getattr(config, "agent_binary", None)):
            return config
    return None


def _resolve_context_root(ctx: Any) -> Optional[Path]:
    if isinstance(ctx, Path):
        return ctx
    for attr in ("root", "repo_root"):
        root = getattr(ctx, attr, None)
        if isinstance(root, Path):
            return root
    for attr in ("config", "_config"):
        config = getattr(ctx, attr, None)
        root = getattr(config, "root", None)
        if isinstance(root, Path):
            return root
    return None


def wrap_requested_agent_context(
    delegate: Any, *, agent_id: str, profile: Optional[str] = None
) -> Any:
    return _RequestedAgentContext(delegate, agent_id=agent_id, profile=profile)


def _alias_display_name(
    agent_id: str,
    backend_descriptor: AgentDescriptor,
) -> str:
    if agent_id == backend_descriptor.id:
        return backend_descriptor.name
    return f"{backend_descriptor.name} ({agent_id})"


def _infer_backend_id_by_prefix(
    backend_id: str,
    base_agents: dict[str, AgentDescriptor],
) -> Optional[str]:
    """If *backend_id* is not a known backend, try shorter prefixes at ``-``/``_`` boundaries."""
    if not backend_id:
        return None
    boundary_prefixes: list[str] = []
    for i, ch in enumerate(backend_id):
        if ch in "-_" and i > 0:
            boundary_prefixes.append(backend_id[:i])
    if not boundary_prefixes:
        return None
    for prefix in sorted(boundary_prefixes, key=len, reverse=True):
        if prefix in base_agents:
            return prefix
    return None


def _build_config_alias_agents(context: Any) -> dict[str, AgentDescriptor]:
    config = _resolve_runtime_agent_config(context)
    if config is None:
        return {}
    configured_agents = getattr(config, "agents", None)
    if not isinstance(configured_agents, dict):
        return {}
    base_agents = _all_agents()
    aliases: dict[str, AgentDescriptor] = {}
    for raw_agent_id in configured_agents:
        agent_id = str(raw_agent_id or "").strip().lower()
        if not agent_id:
            continue
        if agent_id in base_agents:
            continue
        backend_id = agent_id
        if callable(getattr(config, "agent_backend", None)):
            backend_id = str(config.agent_backend(agent_id) or "").strip().lower()
        if not backend_id:
            continue
        backend_descriptor = base_agents.get(backend_id)
        if backend_descriptor is None:
            inferred = _infer_backend_id_by_prefix(backend_id, base_agents)
            if inferred is not None:
                _logger.info(
                    "Inferred backend %r for configured agent %r (prefix of unresolved id %r)",
                    inferred,
                    agent_id,
                    backend_id,
                )
                backend_id = inferred
                backend_descriptor = base_agents.get(backend_id)
        if backend_descriptor is None:
            _logger.warning(
                "Configured agent %s references unknown backend %s",
                agent_id,
                backend_id,
            )
            continue
        resolved_backend_descriptor = backend_descriptor
        alias_name = _alias_display_name(agent_id, resolved_backend_descriptor)

        def _make_harness(
            ctx: Any,
            *,
            alias_id: str = agent_id,
            alias_name: str = alias_name,
            backend: AgentDescriptor = resolved_backend_descriptor,
        ) -> AgentHarness:
            base_harness = backend.make_harness(
                _RequestedAgentContext(ctx, agent_id=alias_id)
            )
            return AliasedAgentHarness(
                base_harness,
                agent_id=alias_id,
                display_name=alias_name,
            )

        healthcheck = None
        backend_healthcheck = resolved_backend_descriptor.healthcheck
        if backend_healthcheck is not None:
            resolved_backend_healthcheck = cast(
                Callable[[Any], bool],
                backend_healthcheck,
            )

            def _healthcheck(
                ctx: Any,
                *,
                alias_id: str = agent_id,
                backend_healthcheck: Callable[[Any], bool] = (
                    resolved_backend_healthcheck
                ),
            ) -> bool:
                return backend_healthcheck(
                    _RequestedAgentContext(ctx, agent_id=alias_id)
                )

            healthcheck = _healthcheck

        aliases[agent_id] = AgentDescriptor(
            id=agent_id,
            name=alias_name,
            capabilities=resolved_backend_descriptor.capabilities,
            make_harness=_make_harness,
            healthcheck=healthcheck,
            runtime_kind=resolved_backend_descriptor.runtime_kind,
            plugin_api_version=resolved_backend_descriptor.plugin_api_version,
        )
    return aliases


_BUILTIN_AGENTS: dict[str, AgentDescriptor] = {
    "codex": AgentDescriptor(
        id="codex",
        name="Codex",
        capabilities=frozenset(
            [
                RuntimeCapability("durable_threads"),
                RuntimeCapability("message_turns"),
                RuntimeCapability("interrupt"),
                RuntimeCapability("active_thread_discovery"),
                RuntimeCapability("review"),
                RuntimeCapability("model_listing"),
                RuntimeCapability("event_streaming"),
                RuntimeCapability("approvals"),
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
                RuntimeCapability("durable_threads"),
                RuntimeCapability("message_turns"),
                RuntimeCapability("interrupt"),
                RuntimeCapability("active_thread_discovery"),
                RuntimeCapability("review"),
                RuntimeCapability("model_listing"),
                RuntimeCapability("event_streaming"),
            ]
        ),
        make_harness=_make_opencode_harness,
        healthcheck=_check_opencode_health,
    ),
    "zeroclaw": AgentDescriptor(
        id="zeroclaw",
        name="ZeroClaw",
        capabilities=ZEROCLAW_CAPABILITIES,
        make_harness=_make_zeroclaw_harness,
        healthcheck=_check_zeroclaw_health,
    ),
    "hermes": AgentDescriptor(
        id="hermes",
        name="Hermes",
        capabilities=HERMES_CAPABILITIES,
        make_harness=_make_hermes_harness,
        healthcheck=_check_hermes_health,
        runtime_kind="hermes",
    ),
}

# Lazy-loaded cache of built-in + plugin agents.
_AGENT_CACHE: Optional[dict[str, AgentDescriptor]] = None

# Lock to protect cache initialization and reload from concurrent access.
_AGENT_CACHE_LOCK = threading.Lock()


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
        if api_version_raw is None:
            api_version = None
        else:
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
        with _AGENT_CACHE_LOCK:
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
    with _AGENT_CACHE_LOCK:
        _AGENT_CACHE = None
    return get_registered_agents()


def get_registered_agents(context: Any = None) -> dict[str, AgentDescriptor]:
    agents = _all_agents().copy()
    agents.update(_build_config_alias_agents(context))
    return agents


def get_available_agents(app_ctx: Any) -> dict[str, AgentDescriptor]:
    available: dict[str, AgentDescriptor] = {}
    for agent_id, descriptor in get_registered_agents(app_ctx).items():
        if descriptor.healthcheck is None or descriptor.healthcheck(app_ctx):
            available[agent_id] = descriptor
    return available


def get_agent_descriptor(
    agent_id: str,
    context: Any = None,
) -> Optional[AgentDescriptor]:
    normalized = (agent_id or "").strip().lower()
    return get_registered_agents(context).get(normalized)


def validate_agent_id(agent_id: str, context: Any = None) -> str:
    normalized = (agent_id or "").strip().lower()
    if normalized not in get_registered_agents(context):
        raise ValueError(f"Unknown agent: {agent_id!r}")
    return normalized


def has_capability(agent_id: str, capability: str, context: Any = None) -> bool:
    descriptor = get_agent_descriptor(agent_id, context)
    if descriptor is None:
        return False
    normalized = normalize_agent_capabilities([capability])
    if not normalized:
        return False
    return next(iter(normalized)) in descriptor.capabilities


__all__ = [
    "CAR_AGENT_ENTRYPOINT_GROUP",
    "CAR_PLUGIN_API_VERSION",
    "AgentCapability",
    "AgentDescriptor",
    "get_agent_descriptor",
    "get_available_agents",
    "get_registered_agents",
    "has_capability",
    "normalize_agent_capabilities",
    "reload_agents",
    "validate_agent_id",
    "wrap_requested_agent_context",
]
