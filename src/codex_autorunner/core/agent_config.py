import dataclasses
import shlex
from typing import Any, Dict, List, Literal, Mapping, Optional

from .config_contract import ConfigError
from .config_field_schema import (
    AGENT_FIELD_SCHEMAS,
    AGENT_PROFILE_FIELD_SCHEMAS,
    FieldSchema,
    parse_schema_field,
    schema_paths,
)

SHARED_AGENT_PARSER_FIELD_PATHS = frozenset(
    path
    for path_set in (
        schema_paths(AGENT_FIELD_SCHEMAS),
        schema_paths(AGENT_PROFILE_FIELD_SCHEMAS),
    )
    for path in path_set
)


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


def _normalize_token(value: object) -> str:
    return str(value or "").strip().lower()


def _strip_agent_prefix(agent_id: str, runtime_kind: str) -> Optional[str]:
    normalized_agent_id = _normalize_token(agent_id)
    normalized_runtime_kind = _normalize_token(runtime_kind)
    if not normalized_agent_id or not normalized_runtime_kind:
        return None
    for prefix in (
        f"{normalized_runtime_kind}-",
        f"{normalized_runtime_kind}_",
    ):
        if normalized_agent_id.startswith(prefix):
            suffix = normalized_agent_id[len(prefix) :].strip()
            return suffix or None
    return None


def _infer_backend_id_by_prefix(
    backend_id: str,
    known_agent_ids: Mapping[str, object],
) -> Optional[str]:
    normalized_backend_id = _normalize_token(backend_id)
    if not normalized_backend_id:
        return None
    boundary_prefixes: list[str] = []
    for i, ch in enumerate(normalized_backend_id):
        if ch in "-_" and i > 0:
            boundary_prefixes.append(normalized_backend_id[:i])
    if not boundary_prefixes:
        return None
    normalized_agent_ids = {_normalize_token(agent_id) for agent_id in known_agent_ids}
    for prefix in sorted(boundary_prefixes, key=len, reverse=True):
        if prefix in normalized_agent_ids:
            return prefix
    return None


def _normalize_requested_agent_target(
    agents: Mapping[str, AgentConfig],
    agent_id: str,
    *,
    profile: Optional[str] = None,
    runtime_alias_kinds: Optional[Mapping[str, str]] = None,
) -> tuple[str, Optional[str]]:
    logical_agent_id = _normalize_token(agent_id)
    logical_profile = _normalize_token(profile) or None
    if logical_profile is not None:
        return logical_agent_id, logical_profile

    configured_agent = agents.get(logical_agent_id)
    if configured_agent is not None:
        runtime_kind = _normalize_token(configured_agent.backend or logical_agent_id)
        if runtime_kind == logical_agent_id:
            inferred_runtime_kind = _infer_backend_id_by_prefix(
                logical_agent_id, agents
            )
            if inferred_runtime_kind is not None:
                runtime_kind = inferred_runtime_kind
        derived_profile = _strip_agent_prefix(logical_agent_id, runtime_kind)
        if derived_profile is not None:
            return runtime_kind, derived_profile

    if runtime_alias_kinds is not None:
        runtime_kind = _normalize_token(runtime_alias_kinds.get(logical_agent_id))
        derived_profile = _strip_agent_prefix(logical_agent_id, runtime_kind)
        if derived_profile is not None:
            return runtime_kind, derived_profile

    return logical_agent_id, None


def resolve_agent_target_from_agents(
    agents: Dict[str, AgentConfig],
    agent_id: str,
    *,
    profile: Optional[str] = None,
    runtime_alias_kinds: Optional[Mapping[str, str]] = None,
    allow_runtime_alias_fallback: bool = False,
) -> ResolvedAgentTarget:
    logical_agent_id, logical_profile = _normalize_requested_agent_target(
        agents,
        agent_id,
        profile=profile,
        runtime_alias_kinds=runtime_alias_kinds,
    )
    if not logical_agent_id:
        from .config_contract import ConfigError

        raise ConfigError("agent_id is required")

    if logical_profile is not None:
        agent = agents.get(logical_agent_id)
        configured_profiles = agent.profiles if agent is not None else None
        if (
            isinstance(configured_profiles, dict)
            and logical_profile in configured_profiles
        ):
            return ResolvedAgentTarget(
                logical_agent_id=logical_agent_id,
                logical_profile=logical_profile,
                runtime_agent_id=logical_agent_id,
                runtime_profile=logical_profile,
                resolution_kind="canonical_profile",
            )

        for raw_runtime_agent_id, runtime_agent in agents.items():
            runtime_agent_id = _normalize_token(raw_runtime_agent_id)
            if runtime_agent_id == logical_agent_id:
                continue
            runtime_kind = _normalize_token(runtime_agent.backend or runtime_agent_id)
            if runtime_kind == runtime_agent_id:
                inferred_runtime_kind = _infer_backend_id_by_prefix(
                    runtime_agent_id, agents
                )
                if inferred_runtime_kind is not None:
                    runtime_kind = inferred_runtime_kind
            if runtime_kind != logical_agent_id:
                continue
            derived_profile = _strip_agent_prefix(runtime_agent_id, logical_agent_id)
            if derived_profile != logical_profile:
                continue
            return ResolvedAgentTarget(
                logical_agent_id=logical_agent_id,
                logical_profile=logical_profile,
                runtime_agent_id=runtime_agent_id,
                runtime_profile=None,
                resolution_kind="alias_profile",
            )
        if allow_runtime_alias_fallback and runtime_alias_kinds is not None:
            for raw_runtime_agent_id, runtime_kind in runtime_alias_kinds.items():
                runtime_agent_id = _normalize_token(raw_runtime_agent_id)
                if runtime_agent_id == logical_agent_id:
                    continue
                if _normalize_token(runtime_kind) != logical_agent_id:
                    continue
                derived_profile = _strip_agent_prefix(
                    runtime_agent_id, logical_agent_id
                )
                if derived_profile != logical_profile:
                    continue
                return ResolvedAgentTarget(
                    logical_agent_id=logical_agent_id,
                    logical_profile=logical_profile,
                    runtime_agent_id=runtime_agent_id,
                    runtime_profile=None,
                    resolution_kind="alias_profile",
                )

    return ResolvedAgentTarget(
        logical_agent_id=logical_agent_id,
        logical_profile=logical_profile,
        runtime_agent_id=logical_agent_id,
        runtime_profile=logical_profile,
        resolution_kind="passthrough",
    )


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
            raise ConfigError(f"agents.{agent_id} must be a mapping")

        def _agent_schema(key: str, *, _agent_id: str = agent_id) -> FieldSchema:
            raw_schema = AGENT_FIELD_SCHEMAS[key]
            return dataclasses.replace(
                raw_schema,
                type_message=(
                    raw_schema.type_message.format(agent_id=_agent_id)
                    if raw_schema.type_message
                    else None
                ),
                value_message=(
                    raw_schema.value_message.format(agent_id=_agent_id)
                    if raw_schema.value_message
                    else None
                ),
            )

        backend = parse_schema_field(
            agent_cfg.get("backend"),
            _agent_schema("backend"),
        )
        binary = parse_schema_field(
            agent_cfg.get("binary"),
            _agent_schema("binary"),
        )
        serve_command = None
        if "serve_command" in agent_cfg:
            serve_command = _parse_command(agent_cfg.get("serve_command"))
        base_url = parse_schema_field(
            agent_cfg.get("base_url"),
            _agent_schema("base_url"),
        )
        subagent_models = parse_schema_field(
            agent_cfg.get("subagent_models"),
            _agent_schema("subagent_models"),
        )
        default_profile = parse_schema_field(
            agent_cfg.get("default_profile"),
            _agent_schema("default_profile"),
        )
        profiles_raw = agent_cfg.get("profiles")
        profiles: Optional[Dict[str, AgentProfileConfig]] = None
        if isinstance(profiles_raw, dict):
            parsed_profiles: Dict[str, AgentProfileConfig] = {}
            for profile_id, profile_cfg in profiles_raw.items():
                normalized_profile_id = str(profile_id or "").strip().lower()
                if not normalized_profile_id:
                    raise ConfigError(
                        f"agents.{agent_id}.profiles keys must be non-empty strings"
                    )
                if not isinstance(profile_cfg, dict):
                    raise ConfigError(
                        f"agents.{agent_id}.profiles.{profile_id} must be a mapping"
                    )

                def _profile_schema(
                    key: str,
                    *,
                    _agent_id: str = agent_id,
                    _profile_id: str = profile_id,
                ) -> FieldSchema:
                    raw_schema = AGENT_PROFILE_FIELD_SCHEMAS[key]
                    return dataclasses.replace(
                        raw_schema,
                        type_message=(
                            raw_schema.type_message.format(
                                agent_id=_agent_id,
                                profile_id=_profile_id,
                            )
                            if raw_schema.type_message
                            else None
                        ),
                        value_message=(
                            raw_schema.value_message.format(
                                agent_id=_agent_id,
                                profile_id=_profile_id,
                            )
                            if raw_schema.value_message
                            else None
                        ),
                    )

                profile_backend = parse_schema_field(
                    profile_cfg.get("backend"),
                    _profile_schema("backend"),
                )
                profile_serve_command = None
                if "serve_command" in profile_cfg:
                    profile_serve_command = _parse_command(
                        profile_cfg.get("serve_command")
                    )
                profile_base_url = parse_schema_field(
                    profile_cfg.get("base_url"),
                    _profile_schema("base_url"),
                )
                profile_subagent_models = parse_schema_field(
                    profile_cfg.get("subagent_models"),
                    _profile_schema("subagent_models"),
                )
                display_name = parse_schema_field(
                    profile_cfg.get("display_name"),
                    _profile_schema("display_name"),
                )
                binary_override = parse_schema_field(
                    profile_cfg.get("binary"),
                    _profile_schema("binary"),
                )
                parsed_profiles[normalized_profile_id] = AgentProfileConfig(
                    display_name=display_name,
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
