import dataclasses
import shlex
from typing import Any, Dict, List, Literal, Optional


@dataclasses.dataclass(frozen=True)
class AgentProfileConfig:
    display_name: Optional[str] = None
    backend: Optional[str] = None
    binary: Optional[str] = None
    serve_command: Optional[List[str]] = None
    base_url: Optional[str] = None
    subagent_models: Optional[Dict[str, str]] = None


@dataclasses.dataclass(frozen=True)
class AgentConfig:
    backend: Optional[str]
    binary: str
    serve_command: Optional[List[str]]
    base_url: Optional[str]
    subagent_models: Optional[Dict[str, str]]
    default_profile: Optional[str] = None
    profiles: Optional[Dict[str, AgentProfileConfig]] = None


@dataclasses.dataclass(frozen=True)
class ResolvedAgentTarget:
    logical_agent_id: str
    logical_profile: Optional[str]
    runtime_agent_id: str
    runtime_profile: Optional[str]
    resolution_kind: Literal["passthrough", "canonical_profile", "alias_profile"]


def _parse_command(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    if isinstance(raw, str):
        return [part for part in shlex.split(raw) if part]
    return []


def parse_agents_config(
    cfg: Optional[Dict[str, Any]], defaults: Dict[str, Any]
) -> Dict[str, AgentConfig]:
    raw_agents = cfg.get("agents") if cfg else None
    if not isinstance(raw_agents, dict):
        raw_agents = defaults.get("agents", {})
    agents: Dict[str, AgentConfig] = {}
    for agent_id, agent_cfg in raw_agents.items():
        if not isinstance(agent_cfg, dict):
            continue
        backend = agent_cfg.get("backend")
        if not isinstance(backend, str) or not backend.strip():
            backend = None
        binary = agent_cfg.get("binary")
        if not isinstance(binary, str) or not binary.strip():
            continue
        serve_command = None
        if "serve_command" in agent_cfg:
            serve_command = _parse_command(agent_cfg.get("serve_command"))
        base_url = agent_cfg.get("base_url")
        subagent_models = agent_cfg.get("subagent_models")
        if not isinstance(subagent_models, dict):
            subagent_models = None
        default_profile = agent_cfg.get("default_profile")
        if not isinstance(default_profile, str) or not default_profile.strip():
            default_profile = None
        else:
            default_profile = default_profile.strip().lower()
        profiles_raw = agent_cfg.get("profiles")
        profiles: Optional[Dict[str, AgentProfileConfig]] = None
        if isinstance(profiles_raw, dict):
            parsed_profiles: Dict[str, AgentProfileConfig] = {}
            for profile_id, profile_cfg in profiles_raw.items():
                normalized_profile_id = str(profile_id or "").strip().lower()
                if not normalized_profile_id or not isinstance(profile_cfg, dict):
                    continue
                profile_backend = profile_cfg.get("backend")
                if not isinstance(profile_backend, str) or not profile_backend.strip():
                    profile_backend = None
                profile_serve_command = None
                if "serve_command" in profile_cfg:
                    profile_serve_command = _parse_command(
                        profile_cfg.get("serve_command")
                    )
                profile_base_url = profile_cfg.get("base_url")
                profile_subagent_models = profile_cfg.get("subagent_models")
                if not isinstance(profile_subagent_models, dict):
                    profile_subagent_models = None
                display_name = profile_cfg.get("display_name")
                if not isinstance(display_name, str) or not display_name.strip():
                    display_name = None
                binary_override = profile_cfg.get("binary")
                if not isinstance(binary_override, str) or not binary_override.strip():
                    binary_override = None
                parsed_profiles[normalized_profile_id] = AgentProfileConfig(
                    display_name=display_name.strip() if display_name else None,
                    backend=profile_backend,
                    binary=binary_override,
                    serve_command=profile_serve_command,
                    base_url=profile_base_url,
                    subagent_models=profile_subagent_models,
                )
            profiles = parsed_profiles or None
        agents[str(agent_id)] = AgentConfig(
            backend=backend,
            binary=binary,
            serve_command=serve_command,
            base_url=base_url,
            subagent_models=subagent_models,
            default_profile=default_profile,
            profiles=profiles,
        )
    return agents
