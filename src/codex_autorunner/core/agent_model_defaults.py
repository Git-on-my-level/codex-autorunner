"""Single source of truth for agent model defaults.

Settings-backed model defaults are per agent because model catalogs are not
portable across runtimes.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Optional

from .text_utils import _normalize_optional_text

BUILTIN_DEFAULT_AGENT_MODELS: Mapping[str, str] = MappingProxyType(
    {
        "codex": "gpt-5.5",
        "opencode": "zai-coding-plan/glm-5.1",
    }
)


def _config_codex_model(config: Any) -> Optional[str]:
    return _normalize_optional_text(getattr(config, "codex_model", None))


def _state_default_model(agent: str, state: Any) -> Optional[str]:
    raw_overrides = getattr(state, "autorunner_model_overrides", None)
    if isinstance(raw_overrides, Mapping):
        model = _normalize_optional_text(raw_overrides.get(agent))
        if model:
            return model
    return None


def normalize_agent_id(agent: object) -> str:
    value = str(agent or "").strip().lower()
    return value or "codex"


def builtin_default_model_for_agent(agent: object) -> Optional[str]:
    return BUILTIN_DEFAULT_AGENT_MODELS.get(normalize_agent_id(agent))


def configured_default_model_for_agent(
    agent: object,
    *,
    state: Any = None,
    config: Any = None,
    configured_default: object = None,
    include_builtin: bool = True,
) -> Optional[str]:
    """Resolve the default model for an agent before explicit per-turn overrides.

    Precedence is intentionally centralized:
    1. user-configured hub default from settings/state;
    2. caller-specific configured default such as ``pma.model`` or review config;
    3. Codex repo config for Codex runs;
    4. built-in agent fallback.
    """

    configured = _normalize_optional_text(configured_default)
    normalized_agent = normalize_agent_id(agent)
    state_default = _state_default_model(normalized_agent, state)
    if state_default:
        return state_default
    if configured:
        return configured

    if normalized_agent == "codex":
        codex_model = _config_codex_model(config)
        if codex_model:
            return codex_model

    return (
        builtin_default_model_for_agent(normalized_agent) if include_builtin else None
    )


def resolve_model_for_agent(
    agent: object,
    explicit_model: object = None,
    *,
    state: Any = None,
    config: Any = None,
    configured_default: object = None,
    include_builtin: bool = True,
) -> Optional[str]:
    explicit = _normalize_optional_text(explicit_model)
    if explicit:
        return explicit
    return configured_default_model_for_agent(
        agent,
        state=state,
        config=config,
        configured_default=configured_default,
        include_builtin=include_builtin,
    )


__all__ = [
    "BUILTIN_DEFAULT_AGENT_MODELS",
    "builtin_default_model_for_agent",
    "configured_default_model_for_agent",
    "normalize_agent_id",
    "resolve_model_for_agent",
]
