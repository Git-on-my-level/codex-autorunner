from __future__ import annotations

import importlib
from typing import Any, Callable, Optional, cast


def configured_agent_execution_targets(context: Any = None) -> tuple[Any, ...]:
    module = importlib.import_module("codex_autorunner.agents.registry")
    func = cast(
        Callable[[Any], tuple[Any, ...]], module.configured_agent_execution_targets
    )
    return func(context)


def descriptor_runtime_kind(agent_id: str, descriptor: Any) -> str:
    module = importlib.import_module("codex_autorunner.agents.registry")
    func = cast(Callable[[str, Any], str], module.descriptor_runtime_kind)
    return func(agent_id, descriptor)


def get_agent_descriptor(agent_id: str, context: Any = None) -> Any:
    module = importlib.import_module("codex_autorunner.agents.registry")
    func = cast(Callable[[str, Any], Any], module.get_agent_descriptor)
    return func(agent_id, context)


def get_registered_agents(context: Any = None) -> dict[str, Any]:
    module = importlib.import_module("codex_autorunner.agents.registry")
    func = cast(Callable[[Any], dict[str, Any]], module.get_registered_agents)
    return func(context)


def run_agent_runtime_preflight(
    agent_id: str,
    profile: Optional[str] = None,
    *,
    context: Any = None,
) -> Any:
    module = importlib.import_module("codex_autorunner.agents.registry")
    func = cast(
        Callable[[str, Optional[str], Any], Any],
        lambda requested_agent_id, requested_profile, requested_context: (
            module.run_agent_runtime_preflight(
                requested_agent_id,
                requested_profile,
                context=requested_context,
            )
        ),
    )
    return func(agent_id, profile, context)


def validate_agent_id(agent_id: str, context: Any = None) -> str:
    module = importlib.import_module("codex_autorunner.agents.registry")
    func = cast(Callable[[str, Any], str], module.validate_agent_id)
    return func(agent_id, context)
