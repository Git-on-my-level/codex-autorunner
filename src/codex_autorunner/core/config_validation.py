"""Validation helpers for configuration loading."""

from __future__ import annotations

import dataclasses
import ipaddress
from pathlib import Path
from typing import Any, Callable, Dict, Tuple, Type, Union, cast

from .config_contract import (
    CONFIG_VERSION,
    ConfigError,
)
from .config_field_schema import (
    AGENT_FIELD_SCHEMAS,
    AGENT_PROFILE_FIELD_SCHEMAS,
    APP_SERVER_CLIENT_FIELD_SCHEMAS,
    APP_SERVER_FIELD_SCHEMAS,
    APP_SERVER_OUTPUT_FIELD_SCHEMAS,
    APP_SERVER_PROMPT_SECTION_SCHEMAS,
    OPENCODE_FIELD_SCHEMAS,
    TICKET_FLOW_FIELD_SCHEMAS,
    UPDATE_FIELD_SCHEMAS,
    UPDATE_LINUX_SERVICE_NAME_SCHEMAS,
    USAGE_FIELD_SCHEMAS,
    normalize_choice_value,
    validate_schema_field,
)
from .mutation_policy import (
    MUTATION_POLICY_ACTION_TYPES,
    MUTATION_POLICY_ALLOWED_VALUES,
    normalize_mutation_policy_value,
)
from .path_utils import ConfigPathError, resolve_config_path


def _normalize_ticket_flow_approval_mode(value: Any, *, scope: str) -> str:
    schema = TICKET_FLOW_FIELD_SCHEMAS["approval_mode"]
    scope_schema = dataclasses.replace(
        schema,
        path=scope,
        type_message=f"{scope} must be a string",
        value_message=(
            schema.value_message.replace("ticket_flow.approval_mode", scope)
            if schema.value_message
            else None
        ),
    )
    return normalize_choice_value(value, scope_schema)


def _validate_version(cfg: Dict[str, Any]) -> None:
    if cfg.get("version") != CONFIG_VERSION:
        raise ConfigError(f"Unsupported config version; expected {CONFIG_VERSION}")


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_RuleCheck = Callable[[Dict[str, Any], str, str], None]


@dataclasses.dataclass(frozen=True)
class _ConfigRule:
    key: str
    check: _RuleCheck

    def apply(self, mapping: Dict[str, Any], *, path: str) -> None:
        self.check(mapping, self.key, path)


def _apply_config_rules(
    mapping: Dict[str, Any], *, path: str, rules: tuple[_ConfigRule, ...]
) -> None:
    for rule in rules:
        rule.apply(mapping, path=path)


def _optional_type_rule(
    key: str,
    expected: Union[Type, Tuple[Type, ...]],
    *,
    allow_none: bool = False,
) -> _ConfigRule:
    def check(mapping: Dict[str, Any], rule_key: str, path: str) -> None:
        _validate_optional_type(
            mapping, rule_key, expected, path=path, allow_none=allow_none
        )

    return _ConfigRule(key, check)


def _optional_int_ge_rule(key: str, min_value: int) -> _ConfigRule:
    def check(mapping: Dict[str, Any], rule_key: str, path: str) -> None:
        _validate_optional_int_ge(mapping, rule_key, min_value, path=path)

    return _ConfigRule(key, check)


def _validate_server_security(server: Dict[str, Any]) -> None:
    allowed_hosts = server.get("allowed_hosts")
    if allowed_hosts is not None and not isinstance(allowed_hosts, list):
        raise ConfigError("server.allowed_hosts must be a list of strings if provided")
    if isinstance(allowed_hosts, list):
        for entry in allowed_hosts:
            if not isinstance(entry, str):
                raise ConfigError("server.allowed_hosts must be a list of strings")

    allowed_origins = server.get("allowed_origins")
    if allowed_origins is not None and not isinstance(allowed_origins, list):
        raise ConfigError(
            "server.allowed_origins must be a list of strings if provided"
        )
    if isinstance(allowed_origins, list):
        for entry in allowed_origins:
            if not isinstance(entry, str):
                raise ConfigError("server.allowed_origins must be a list of strings")

    host = str(server.get("host", ""))
    if not is_loopback_host(host) and not allowed_hosts:
        raise ConfigError(
            "server.allowed_hosts must be set when binding to a non-loopback host"
        )


def _validate_app_server_config(cfg: Dict[str, Any]) -> None:
    app_server_cfg = cfg.get("app_server")
    if app_server_cfg is None:
        return
    if not isinstance(app_server_cfg, dict):
        raise ConfigError("app_server section must be a mapping if provided")
    for key, schema in APP_SERVER_FIELD_SCHEMAS.items():
        if key in app_server_cfg:
            validate_schema_field(app_server_cfg.get(key), schema)
    client_cfg = _require_optional_mapping(
        app_server_cfg, "client", path="app_server.client"
    )
    if client_cfg is not None:
        for key, schema in APP_SERVER_CLIENT_FIELD_SCHEMAS.items():
            if key in client_cfg:
                validate_schema_field(client_cfg.get(key), schema)
    output_cfg = _require_optional_mapping(
        app_server_cfg, "output", path="app_server.output"
    )
    if output_cfg is not None:
        if "policy" in output_cfg:
            validate_schema_field(
                output_cfg.get("policy"),
                APP_SERVER_OUTPUT_FIELD_SCHEMAS["policy"],
            )
    prompts = _require_optional_mapping(
        app_server_cfg, "prompts", path="app_server.prompts"
    )
    if prompts is not None:
        for section, section_schema in APP_SERVER_PROMPT_SECTION_SCHEMAS.items():
            section_cfg = prompts.get(section)
            if section_cfg is None:
                continue
            if not isinstance(section_cfg, dict):
                raise ConfigError(f"app_server.prompts.{section} must be a mapping")
            for key, schema in section_schema.items():
                if key not in section_cfg:
                    continue
                validate_schema_field(section_cfg.get(key), schema)


def _validate_collaboration_policy_config(cfg: Dict[str, Any]) -> None:
    collaboration_cfg = cfg.get("collaboration_policy")
    if collaboration_cfg is None:
        return
    if not isinstance(collaboration_cfg, dict):
        raise ConfigError("collaboration_policy section must be a mapping if provided")

    actors_cfg = _require_optional_mapping(
        collaboration_cfg, "actors", path="collaboration_policy.actors"
    )
    if actors_cfg is not None:
        _validate_id_list(
            actors_cfg,
            "allowed_user_ids",
            path="collaboration_policy.actors.allowed_user_ids",
        )

    _validate_collaboration_surface_config(
        collaboration_cfg.get("telegram"),
        surface="telegram",
        id_fields=("allowed_chat_ids", "allowed_user_ids"),
        destination_id_field="chat_id",
        container_id_field=None,
        subdestination_field="thread_id",
        allow_require_topics=True,
    )
    _validate_collaboration_surface_config(
        collaboration_cfg.get("discord"),
        surface="discord",
        id_fields=("allowed_guild_ids", "allowed_channel_ids", "allowed_user_ids"),
        destination_id_field="channel_id",
        container_id_field="guild_id",
        subdestination_field=None,
        allow_require_topics=False,
    )


def _validate_collaboration_surface_config(
    raw: Any,
    *,
    surface: str,
    id_fields: tuple[str, ...],
    destination_id_field: str,
    container_id_field: str | None,
    subdestination_field: str | None,
    allow_require_topics: bool,
) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ConfigError(
            f"collaboration_policy.{surface} must be a mapping if provided"
        )
    for key in id_fields:
        _validate_id_list(raw, key, path=f"collaboration_policy.{surface}.{key}")
    if "default_mode" in raw:
        _validate_str_choice(
            raw,
            "default_mode",
            {"active", "command_only", "silent", "denied"},
            path=f"collaboration_policy.{surface}.default_mode",
        )
    for key in ("default_plain_text_trigger", "trigger_mode"):
        if key in raw:
            _validate_str_choice(
                raw,
                key,
                {"always", "mentions", "disabled", "all"},
                path=f"collaboration_policy.{surface}.{key}",
            )
    if (
        allow_require_topics
        and "require_topics" in raw
        and not isinstance(raw.get("require_topics"), bool)
    ):
        raise ConfigError(
            f"collaboration_policy.{surface}.require_topics must be boolean"
        )

    destinations = raw.get("destinations")
    if destinations is None:
        return
    if not isinstance(destinations, list):
        raise ConfigError(f"collaboration_policy.{surface}.destinations must be a list")
    for index, item in enumerate(destinations):
        if not isinstance(item, dict):
            raise ConfigError(
                f"collaboration_policy.{surface}.destinations[{index}] must be a mapping"
            )
        _validate_destination_id(
            item,
            destination_id_field,
            path=(
                f"collaboration_policy.{surface}.destinations[{index}]."
                f"{destination_id_field}"
            ),
        )
        if container_id_field is not None and container_id_field in item:
            _validate_destination_id(
                item,
                container_id_field,
                path=(
                    f"collaboration_policy.{surface}.destinations[{index}]."
                    f"{container_id_field}"
                ),
            )
        if subdestination_field is not None and subdestination_field in item:
            _validate_destination_id(
                item,
                subdestination_field,
                path=(
                    f"collaboration_policy.{surface}.destinations[{index}]."
                    f"{subdestination_field}"
                ),
                allow_none=True,
            )
        if "mode" in item:
            _validate_str_choice(
                item,
                "mode",
                {"active", "command_only", "silent", "denied"},
                path=f"collaboration_policy.{surface}.destinations[{index}].mode",
            )
        for key in ("plain_text_trigger", "trigger_mode"):
            if key in item:
                _validate_str_choice(
                    item,
                    key,
                    {"always", "mentions", "disabled", "all"},
                    path=(
                        f"collaboration_policy.{surface}.destinations[{index}].{key}"
                    ),
                )
        if (
            "name" in item
            and item.get("name") is not None
            and not isinstance(item.get("name"), str)
        ):
            raise ConfigError(
                f"collaboration_policy.{surface}.destinations[{index}].name must be a string"
            )


def _validate_id_list(cfg: Dict[str, Any], key: str, *, path: str) -> None:
    value = cfg.get(key)
    if value is None:
        return
    if not isinstance(value, list):
        raise ConfigError(f"{path} must be a list")
    for entry in value:
        if not isinstance(entry, (str, int)):
            raise ConfigError(f"{path} must contain only string/int IDs")


def _validate_str_choice(
    cfg: Dict[str, Any],
    key: str,
    allowed: set[str],
    *,
    path: str,
) -> None:
    value = cfg.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"{path} must be a string")
    if value not in allowed:
        raise ConfigError(f"{path} must be one of {sorted(allowed)}")


def _validate_destination_id(
    cfg: Dict[str, Any],
    key: str,
    *,
    path: str,
    allow_none: bool = False,
) -> None:
    value = cfg.get(key)
    if allow_none and value is None:
        return
    if not isinstance(value, (str, int)):
        raise ConfigError(f"{path} must be a string/int ID")


def _require_optional_mapping(
    raw: Dict[str, Any], key: str, *, path: str
) -> Dict[str, Any] | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping if provided")
    return value


def _validate_opencode_config(cfg: Dict[str, Any]) -> None:
    opencode_cfg = cfg.get("opencode")
    if opencode_cfg is None:
        return
    if not isinstance(opencode_cfg, dict):
        raise ConfigError("opencode section must be a mapping if provided")
    for key, schema in OPENCODE_FIELD_SCHEMAS.items():
        if key in opencode_cfg:
            validate_schema_field(opencode_cfg.get(key), schema)


def _validate_update_config(cfg: Dict[str, Any]) -> None:
    update_cfg = cfg.get("update")
    if update_cfg is None:
        return
    if not isinstance(update_cfg, dict):
        raise ConfigError("update section must be a mapping if provided")
    for key, schema in UPDATE_FIELD_SCHEMAS.items():
        if key in update_cfg:
            validate_schema_field(update_cfg.get(key), schema)
    linux_services = update_cfg.get("linux_service_names")
    if linux_services is None:
        return
    if not isinstance(linux_services, dict):
        raise ConfigError("update.linux_service_names must be a mapping if provided")
    for key, schema in UPDATE_LINUX_SERVICE_NAME_SCHEMAS.items():
        if key in linux_services:
            validate_schema_field(linux_services.get(key), schema)


def _validate_usage_config(cfg: Dict[str, Any], *, root: Path) -> None:
    usage_cfg = cfg.get("usage")
    if usage_cfg is None:
        return
    if not isinstance(usage_cfg, dict):
        raise ConfigError("usage section must be a mapping if provided")
    for key, schema in USAGE_FIELD_SCHEMAS.items():
        if key in usage_cfg:
            validate_schema_field(usage_cfg.get(key), schema, root=root)


def _validate_agents_config(cfg: Dict[str, Any]) -> None:
    agents_cfg = cfg.get("agents")
    if agents_cfg is None:
        return
    if not isinstance(agents_cfg, dict):
        raise ConfigError("agents section must be a mapping if provided")
    for agent_id, agent_cfg in agents_cfg.items():
        if not isinstance(agent_cfg, dict):
            raise ConfigError(f"agents.{agent_id} must be a mapping")
        if "binary" not in agent_cfg:
            raise ConfigError(f"agents.{agent_id}.binary is required")
        for key, raw_schema in AGENT_FIELD_SCHEMAS.items():
            if key not in agent_cfg:
                continue
            schema = dataclasses.replace(
                raw_schema,
                type_message=(
                    raw_schema.type_message.format(agent_id=agent_id)
                    if raw_schema.type_message
                    else None
                ),
                value_message=(
                    raw_schema.value_message.format(agent_id=agent_id)
                    if raw_schema.value_message
                    else None
                ),
            )
            validate_schema_field(agent_cfg.get(key), schema)
        default_profile = agent_cfg.get("default_profile")
        profiles = agent_cfg.get("profiles")
        if isinstance(profiles, dict):
            normalized_profile_ids: set[str] = set()
            for profile_id, profile_cfg in profiles.items():
                normalized_profile_id = str(profile_id or "").strip().lower()
                if not normalized_profile_id:
                    raise ConfigError(
                        f"agents.{agent_id}.profiles keys must be non-empty strings"
                    )
                normalized_profile_ids.add(normalized_profile_id)
                if not isinstance(profile_cfg, dict):
                    raise ConfigError(
                        f"agents.{agent_id}.profiles.{profile_id} must be a mapping"
                    )
                for key, raw_schema in AGENT_PROFILE_FIELD_SCHEMAS.items():
                    if key not in profile_cfg:
                        continue
                    schema = dataclasses.replace(
                        raw_schema,
                        type_message=(
                            raw_schema.type_message.format(
                                agent_id=agent_id,
                                profile_id=profile_id,
                            )
                            if raw_schema.type_message
                            else None
                        ),
                        value_message=(
                            raw_schema.value_message.format(
                                agent_id=agent_id,
                                profile_id=profile_id,
                            )
                            if raw_schema.value_message
                            else None
                        ),
                    )
                    validate_schema_field(profile_cfg.get(key), schema)
            if isinstance(default_profile, str) and default_profile.strip():
                if default_profile.strip().lower() not in normalized_profile_ids:
                    raise ConfigError(
                        f"agents.{agent_id}.default_profile must reference a configured profile"
                    )


def _validate_repo_config(cfg: Dict[str, Any], *, root: Path) -> None:
    _validate_version(cfg)
    if cfg.get("mode") != "repo":
        raise ConfigError("Repo config must set mode: repo")
    docs = cfg.get("docs")
    if not isinstance(docs, dict):
        raise ConfigError("docs must be a mapping")
    for key, value in docs.items():
        if not isinstance(value, str) or not value:
            raise ConfigError(f"docs.{key} must be a non-empty string path")
        try:
            resolve_config_path(
                value,
                root,
                scope=f"docs.{key}",
            )
        except ConfigPathError as exc:
            raise ConfigError(str(exc)) from exc
    for key in ("active_context", "decisions", "spec"):
        if not isinstance(docs.get(key), str) or not docs[key]:
            raise ConfigError(f"docs.{key} must be a non-empty string path")
    _validate_agents_config(cfg)
    codex = cfg.get("codex")
    if not isinstance(codex, dict):
        raise ConfigError("codex section must be a mapping")
    if not codex.get("binary"):
        raise ConfigError("codex.binary is required")
    if not isinstance(codex.get("args", []), list):
        raise ConfigError("codex.args must be a list")
    if "terminal_args" in codex and not isinstance(
        codex.get("terminal_args", []), list
    ):
        raise ConfigError("codex.terminal_args must be a list if provided")
    if (
        "model" in codex
        and codex.get("model") is not None
        and not isinstance(codex.get("model"), str)
    ):
        raise ConfigError("codex.model must be a string or null if provided")
    if (
        "reasoning" in codex
        and codex.get("reasoning") is not None
        and not isinstance(codex.get("reasoning"), str)
    ):
        raise ConfigError("codex.reasoning must be a string or null if provided")
    if "models" in codex:
        models = codex.get("models")
        if models is not None and not isinstance(models, dict):
            raise ConfigError("codex.models must be a mapping or null if provided")
        if isinstance(models, dict):
            for key in ("small", "large"):
                if (
                    key in models
                    and models.get(key) is not None
                    and not isinstance(models.get(key), str)
                ):
                    raise ConfigError(f"codex.models.{key} must be a string or null")
    prompt = cfg.get("prompt")
    if not isinstance(prompt, dict):
        raise ConfigError("prompt section must be a mapping")
    if not isinstance(prompt.get("prev_run_max_chars", 0), int):
        raise ConfigError("prompt.prev_run_max_chars must be an integer")
    runner = cfg.get("runner")
    if not isinstance(runner, dict):
        raise ConfigError("runner section must be a mapping")
    if not isinstance(runner.get("sleep_seconds", 0), int):
        raise ConfigError("runner.sleep_seconds must be an integer")
    for k in ("stop_after_runs", "max_wallclock_seconds"):
        val = runner.get(k)
        if val is not None and not isinstance(val, int):
            raise ConfigError(f"runner.{k} must be an integer or null")
    autorunner_cfg = _require_optional_mapping(cfg, "autorunner", path="autorunner")
    if autorunner_cfg is not None:
        reuse_session = autorunner_cfg.get("reuse_session")
        if reuse_session is not None and not isinstance(reuse_session, bool):
            raise ConfigError("autorunner.reuse_session must be boolean or null")
    ticket_flow_cfg = _require_optional_mapping(cfg, "ticket_flow", path="ticket_flow")
    if ticket_flow_cfg is not None:
        for key, schema in TICKET_FLOW_FIELD_SCHEMAS.items():
            if key in ticket_flow_cfg:
                validate_schema_field(ticket_flow_cfg.get(key), schema)
    ui_cfg = _require_optional_mapping(cfg, "ui", path="ui")
    if ui_cfg is not None:
        if "editor" in ui_cfg and not isinstance(ui_cfg.get("editor"), str):
            raise ConfigError("ui.editor must be a string if provided")
    git = cfg.get("git")
    if not isinstance(git, dict):
        raise ConfigError("git section must be a mapping")
    if not isinstance(git.get("auto_commit", False), bool):
        raise ConfigError("git.auto_commit must be boolean")
    github = cfg.get("github", {})
    if github is not None and not isinstance(github, dict):
        raise ConfigError("github section must be a mapping if provided")
    if isinstance(github, dict):
        if "enabled" in github and not isinstance(github.get("enabled"), bool):
            raise ConfigError("github.enabled must be boolean")
        if "pr_draft_default" in github and not isinstance(
            github.get("pr_draft_default"), bool
        ):
            raise ConfigError("github.pr_draft_default must be boolean")
        if "sync_commit_mode" in github and not isinstance(
            github.get("sync_commit_mode"), str
        ):
            raise ConfigError("github.sync_commit_mode must be a string")
        if "sync_agent_timeout_seconds" in github and not isinstance(
            github.get("sync_agent_timeout_seconds"), int
        ):
            raise ConfigError("github.sync_agent_timeout_seconds must be an integer")
        automation = _require_optional_mapping(
            github, "automation", path="github.automation"
        )
        if automation is not None:
            if "enabled" in automation and not isinstance(
                automation.get("enabled"), bool
            ):
                raise ConfigError("github.automation.enabled must be boolean")
            reactions = _require_optional_mapping(
                automation, "reactions", path="github.automation.reactions"
            )
            if reactions is not None:
                profile = reactions.get("profile")
                if profile is not None:
                    if not isinstance(profile, str):
                        raise ConfigError(
                            "github.automation.reactions.profile must be a string"
                        )
                    normalized_profile = profile.strip().lower()
                    if normalized_profile not in {"all", "minimal_noise"}:
                        raise ConfigError(
                            "github.automation.reactions.profile must be 'all' or 'minimal_noise'"
                        )
                for key in (
                    "github_login_whitelist",
                    "github_login_blacklist",
                    "github_login_allowlist",
                    "github_login_denylist",
                    "whitelist",
                    "blacklist",
                    "allowlist",
                    "denylist",
                ):
                    raw_logins = reactions.get(key)
                    if raw_logins is None:
                        continue
                    if not isinstance(raw_logins, list):
                        raise ConfigError(
                            f"github.automation.reactions.{key} must be a list of strings"
                        )
                    for login in raw_logins:
                        if not isinstance(login, str):
                            raise ConfigError(
                                f"github.automation.reactions.{key} must be a list of strings"
                            )
            policy = _require_optional_mapping(
                automation, "policy", path="github.automation.policy"
            )
            if policy is not None:
                for action_type, value in policy.items():
                    if action_type not in MUTATION_POLICY_ACTION_TYPES:
                        allowed = ", ".join(MUTATION_POLICY_ACTION_TYPES)
                        raise ConfigError(
                            f"github.automation.policy.{action_type} is not supported; "
                            f"expected one of: {allowed}"
                        )
                    if normalize_mutation_policy_value(value) is None:
                        allowed_values = ", ".join(MUTATION_POLICY_ALLOWED_VALUES)
                        raise ConfigError(
                            f"github.automation.policy.{action_type} must be boolean or "
                            f"one of: {allowed_values}"
                        )
            webhook_ingress = _require_optional_mapping(
                automation, "webhook_ingress", path="github.automation.webhook_ingress"
            )
            if webhook_ingress is not None:
                if "enabled" in webhook_ingress and not isinstance(
                    webhook_ingress.get("enabled"), bool
                ):
                    raise ConfigError(
                        "github.automation.webhook_ingress.enabled must be boolean"
                    )
                if "store_raw_payload" in webhook_ingress and not isinstance(
                    webhook_ingress.get("store_raw_payload"), bool
                ):
                    raise ConfigError(
                        "github.automation.webhook_ingress.store_raw_payload must be boolean"
                    )
                max_payload_bytes = webhook_ingress.get("max_payload_bytes")
                if max_payload_bytes is not None and not _is_strict_int(
                    max_payload_bytes
                ):
                    raise ConfigError(
                        "github.automation.webhook_ingress.max_payload_bytes must be an integer"
                    )
                resolved_max_payload_bytes = (
                    max_payload_bytes if _is_strict_int(max_payload_bytes) else None
                )
                if (
                    resolved_max_payload_bytes is not None
                    and resolved_max_payload_bytes <= 0
                ):
                    raise ConfigError(
                        "github.automation.webhook_ingress.max_payload_bytes must be > 0"
                    )
                max_raw_payload_bytes = webhook_ingress.get("max_raw_payload_bytes")
                if max_raw_payload_bytes is not None and not _is_strict_int(
                    max_raw_payload_bytes
                ):
                    raise ConfigError(
                        "github.automation.webhook_ingress.max_raw_payload_bytes must be an integer"
                    )
                resolved_max_raw_payload_bytes = (
                    max_raw_payload_bytes
                    if _is_strict_int(max_raw_payload_bytes)
                    else None
                )
                if (
                    resolved_max_raw_payload_bytes is not None
                    and resolved_max_raw_payload_bytes <= 0
                ):
                    raise ConfigError(
                        "github.automation.webhook_ingress.max_raw_payload_bytes must be > 0"
                    )
                if (
                    resolved_max_payload_bytes is not None
                    and resolved_max_raw_payload_bytes is not None
                    and resolved_max_raw_payload_bytes > resolved_max_payload_bytes
                ):
                    raise ConfigError(
                        "github.automation.webhook_ingress.max_raw_payload_bytes must be <= max_payload_bytes"
                    )
            polling = _require_optional_mapping(
                automation, "polling", path="github.automation.polling"
            )
            if polling is not None:
                if "enabled" in polling and not isinstance(
                    polling.get("enabled"), bool
                ):
                    raise ConfigError(
                        "github.automation.polling.enabled must be boolean"
                    )
                if "discovery_include_manifest_repos" in polling and not isinstance(
                    polling.get("discovery_include_manifest_repos"), bool
                ):
                    raise ConfigError(
                        "github.automation.polling.discovery_include_manifest_repos must be boolean"
                    )
                if "no_activity_tier" in polling:
                    _validate_str_choice(
                        polling,
                        "no_activity_tier",
                        {"hot", "warm", "cold"},
                        path="github.automation.polling.no_activity_tier",
                    )
                for field in (
                    "watch_window_minutes",
                    "interval_seconds",
                    "discovery_interval_seconds",
                    "discovery_workspace_limit",
                    "discovery_terminal_thread_lookback_minutes",
                    "post_open_boost_minutes",
                    "post_open_boost_interval_seconds",
                ):
                    value = polling.get(field)
                    if value is not None and not _is_strict_int(value):
                        raise ConfigError(
                            f"github.automation.polling.{field} must be an integer"
                        )
                    if (
                        isinstance(value, int)
                        and field
                        in (
                            "post_open_boost_minutes",
                            "post_open_boost_interval_seconds",
                        )
                        and value < 0
                    ):
                        raise ConfigError(
                            f"github.automation.polling.{field} must be >= 0"
                        )
                    if (
                        isinstance(value, int)
                        and field
                        not in (
                            "post_open_boost_minutes",
                            "post_open_boost_interval_seconds",
                        )
                        and value <= 0
                    ):
                        raise ConfigError(
                            f"github.automation.polling.{field} must be > 0"
                        )

    server = cfg.get("server")
    if not isinstance(server, dict):
        raise ConfigError("server section must be a mapping")
    if not isinstance(server.get("host", ""), str):
        raise ConfigError("server.host must be a string")
    if not isinstance(server.get("port", 0), int):
        raise ConfigError("server.port must be an integer")
    if "base_path" in server and not isinstance(server.get("base_path", ""), str):
        raise ConfigError("server.base_path must be a string if provided")
    if "access_log" in server and not isinstance(server.get("access_log", False), bool):
        raise ConfigError("server.access_log must be boolean if provided")
    if "auth_token_env" in server and not isinstance(
        server.get("auth_token_env", ""), str
    ):
        raise ConfigError("server.auth_token_env must be a string if provided")
    _validate_server_security(server)
    _validate_app_server_config(cfg)
    _validate_opencode_config(cfg)
    _validate_update_config(cfg)
    _validate_usage_config(cfg, root=root)
    notifications_cfg = cfg.get("notifications")
    if notifications_cfg is not None:
        if not isinstance(notifications_cfg, dict):
            raise ConfigError("notifications section must be a mapping if provided")
        if "enabled" in notifications_cfg:
            enabled_val = notifications_cfg.get("enabled")
            if not (
                isinstance(enabled_val, bool)
                or enabled_val is None
                or (isinstance(enabled_val, str) and enabled_val.lower() == "auto")
            ):
                raise ConfigError(
                    "notifications.enabled must be boolean, null, or 'auto'"
                )
        events = notifications_cfg.get("events")
        if events is not None and not isinstance(events, list):
            raise ConfigError("notifications.events must be a list if provided")
        if isinstance(events, list):
            for entry in events:
                if not isinstance(entry, str):
                    raise ConfigError("notifications.events must be a list of strings")
        tui_idle_seconds = notifications_cfg.get("tui_idle_seconds")
        if tui_idle_seconds is not None:
            if not isinstance(tui_idle_seconds, (int, float)):
                raise ConfigError(
                    "notifications.tui_idle_seconds must be a number if provided"
                )
            if tui_idle_seconds < 0:
                raise ConfigError(
                    "notifications.tui_idle_seconds must be >= 0 if provided"
                )
        timeout_seconds = notifications_cfg.get("timeout_seconds")
        if timeout_seconds is not None:
            if not isinstance(timeout_seconds, (int, float)):
                raise ConfigError(
                    "notifications.timeout_seconds must be a number if provided"
                )
            if timeout_seconds <= 0:
                raise ConfigError(
                    "notifications.timeout_seconds must be > 0 if provided"
                )
        discord_cfg = _require_optional_mapping(
            notifications_cfg, "discord", path="notifications.discord"
        )
        if discord_cfg is not None:
            if "enabled" in discord_cfg and not isinstance(
                discord_cfg.get("enabled"), bool
            ):
                raise ConfigError("notifications.discord.enabled must be boolean")
            if "webhook_url_env" in discord_cfg and not isinstance(
                discord_cfg.get("webhook_url_env"), str
            ):
                raise ConfigError(
                    "notifications.discord.webhook_url_env must be a string"
                )
        telegram_cfg = _require_optional_mapping(
            notifications_cfg, "telegram", path="notifications.telegram"
        )
        if telegram_cfg is not None:
            if "enabled" in telegram_cfg and not isinstance(
                telegram_cfg.get("enabled"), bool
            ):
                raise ConfigError("notifications.telegram.enabled must be boolean")
            if "bot_token_env" in telegram_cfg and not isinstance(
                telegram_cfg.get("bot_token_env"), str
            ):
                raise ConfigError(
                    "notifications.telegram.bot_token_env must be a string"
                )
            if "chat_id_env" in telegram_cfg and not isinstance(
                telegram_cfg.get("chat_id_env"), str
            ):
                raise ConfigError("notifications.telegram.chat_id_env must be a string")
            if "thread_id_env" in telegram_cfg and not isinstance(
                telegram_cfg.get("thread_id_env"), str
            ):
                raise ConfigError(
                    "notifications.telegram.thread_id_env must be a string"
                )
            if "thread_id" in telegram_cfg:
                thread_id = telegram_cfg.get("thread_id")
                if thread_id is not None and not isinstance(thread_id, int):
                    raise ConfigError(
                        "notifications.telegram.thread_id must be an integer or null"
                    )
            if "thread_id_map" in telegram_cfg:
                thread_id_map = telegram_cfg.get("thread_id_map")
                if not isinstance(thread_id_map, dict):
                    raise ConfigError(
                        "notifications.telegram.thread_id_map must be a mapping"
                    )
                for key, value in thread_id_map.items():
                    if not isinstance(key, str) or not isinstance(value, int):
                        raise ConfigError(
                            "notifications.telegram.thread_id_map must map strings to integers"
                        )
    terminal_cfg = _require_optional_mapping(cfg, "terminal", path="terminal")
    if terminal_cfg is not None:
        idle_timeout_seconds = terminal_cfg.get("idle_timeout_seconds")
        if idle_timeout_seconds is not None and not isinstance(
            idle_timeout_seconds, int
        ):
            raise ConfigError(
                "terminal.idle_timeout_seconds must be an integer or null"
            )
        if isinstance(idle_timeout_seconds, int) and idle_timeout_seconds < 0:
            raise ConfigError("terminal.idle_timeout_seconds must be >= 0")
    log_cfg = cfg.get("log")
    if not isinstance(log_cfg, dict):
        raise ConfigError("log section must be a mapping")
    if "path" in log_cfg:
        if not isinstance(log_cfg["path"], str):
            raise ConfigError("log.path must be a string path")
        try:
            resolve_config_path(log_cfg["path"], root, scope="log.path")
        except ConfigPathError as exc:
            raise ConfigError(str(exc)) from exc
    for key in ("max_bytes", "backup_count"):
        if not isinstance(log_cfg.get(key, 0), int):
            raise ConfigError(f"log.{key} must be an integer")
    server_log_cfg = cfg.get("server_log")
    if server_log_cfg is not None and not isinstance(server_log_cfg, dict):
        raise ConfigError("server_log section must be a mapping or null")
    if server_log_cfg is None:
        server_log_cfg = {}
    if "path" in server_log_cfg:
        if not isinstance(server_log_cfg.get("path", ""), str):
            raise ConfigError("server_log.path must be a string path")
        try:
            resolve_config_path(server_log_cfg["path"], root, scope="server_log.path")
        except ConfigPathError as exc:
            raise ConfigError(str(exc)) from exc
    for key in ("max_bytes", "backup_count"):
        if key in server_log_cfg and not isinstance(server_log_cfg.get(key, 0), int):
            raise ConfigError(f"server_log.{key} must be an integer")
    static_cfg = _require_optional_mapping(cfg, "static_assets", path="static_assets")
    if static_cfg is not None and "cache_root" in static_cfg:
        if not isinstance(static_cfg.get("cache_root"), str):
            raise ConfigError("static_assets.cache_root must be a string path")
    if static_cfg is not None and "max_cache_entries" in static_cfg:
        max_cache_entries = static_cfg.get("max_cache_entries")
        if not isinstance(max_cache_entries, int):
            raise ConfigError("static_assets.max_cache_entries must be an integer")
        if max_cache_entries < 0:
            raise ConfigError("static_assets.max_cache_entries must be >= 0")
    if static_cfg is not None and "max_cache_age_days" in static_cfg:
        max_cache_age_days = static_cfg.get("max_cache_age_days")
        if not isinstance(max_cache_age_days, int):
            raise ConfigError("static_assets.max_cache_age_days must be an integer")
        if max_cache_age_days < 0:
            raise ConfigError("static_assets.max_cache_age_days must be >= 0")
    _validate_flow_retention_config(cfg)
    _validate_housekeeping_config(cfg)
    _validate_collaboration_policy_config(cfg)
    _validate_telegram_bot_config(cfg)
    _validate_discord_bot_config(cfg)


def _validate_hub_config(cfg: Dict[str, Any], *, root: Path) -> None:
    _validate_version(cfg)
    if cfg.get("mode") != "hub":
        raise ConfigError("Hub config must set mode: hub")
    if "version" in cfg and cfg.get("version") != CONFIG_VERSION:
        raise ConfigError(f"Unsupported config version; expected {CONFIG_VERSION}")
    repo_defaults = cfg.get("repo_defaults")
    if repo_defaults is not None and not isinstance(repo_defaults, dict):
        raise ConfigError("hub.repo_defaults must be a mapping if provided")
    if cfg.get("update_repo_url") is not None and not isinstance(
        cfg.get("update_repo_url"), str
    ):
        raise ConfigError("hub.update_repo_url must be a string")
    if "update_repo_ref" in cfg and not isinstance(cfg.get("update_repo_ref"), str):
        raise ConfigError("hub.update_repo_ref must be a string")
    hub_cfg = cfg.get("hub")
    if hub_cfg is None or not isinstance(hub_cfg, dict):
        raise ConfigError("hub section must be a mapping")
    repos_root = hub_cfg.get("repos_root")
    if "repos_root" in hub_cfg and not isinstance(repos_root, str):
        raise ConfigError("hub.repos_root must be a string")
    worktrees_root = hub_cfg.get("worktrees_root")
    if "worktrees_root" in hub_cfg and not isinstance(worktrees_root, str):
        raise ConfigError("hub.worktrees_root must be a string")
    manifest = hub_cfg.get("manifest")
    if "manifest" in hub_cfg and not isinstance(manifest, str):
        raise ConfigError("hub.manifest must be a string")
    discover_depth = hub_cfg.get("discover_depth")
    if "discover_depth" in hub_cfg and not isinstance(discover_depth, int):
        raise ConfigError("hub.discover_depth must be an integer")
    auto_init_missing = hub_cfg.get("auto_init_missing")
    if "auto_init_missing" in hub_cfg and not isinstance(auto_init_missing, bool):
        raise ConfigError("hub.auto_init_missing must be boolean")
    include_root_repo = hub_cfg.get("include_root_repo")
    if "include_root_repo" in hub_cfg and not isinstance(include_root_repo, bool):
        raise ConfigError("hub.include_root_repo must be boolean")
    repo_server_inherit = hub_cfg.get("repo_server_inherit")
    if "repo_server_inherit" in hub_cfg and not isinstance(repo_server_inherit, bool):
        raise ConfigError("hub.repo_server_inherit must be boolean")
    if "log" in cfg and not isinstance(cfg.get("log"), dict):
        raise ConfigError("hub.log section must be a mapping")
    log_cfg = cfg.get("log")
    if log_cfg is not None and not isinstance(log_cfg, dict):
        raise ConfigError("hub.log section must be a mapping")
    if log_cfg is None:
        log_cfg = {}
    for key in ("path",):
        if not isinstance(log_cfg.get(key, ""), str):
            raise ConfigError(f"hub.log.{key} must be a string path")
    for key in ("max_bytes", "backup_count"):
        if not isinstance(log_cfg.get(key, 0), int):
            raise ConfigError(f"hub.log.{key} must be an integer")
    server = cfg.get("server")
    if not isinstance(server, dict):
        raise ConfigError("server section must be a mapping")
    if not isinstance(server.get("host", ""), str):
        raise ConfigError("server.host must be a string")
    if not isinstance(server.get("port", 0), int):
        raise ConfigError("server.port must be an integer")
    if "base_path" in server and not isinstance(server.get("base_path", ""), str):
        raise ConfigError("server.base_path must be a string if provided")
    if "access_log" in server and not isinstance(server.get("access_log", False), bool):
        raise ConfigError("server.access_log must be boolean if provided")
    if "auth_token_env" in server and not isinstance(
        server.get("auth_token_env", ""), str
    ):
        raise ConfigError("server.auth_token_env must be a string if provided")
    _validate_server_security(server)
    _validate_agents_config(cfg)
    _validate_app_server_config(cfg)
    _validate_opencode_config(cfg)
    _validate_update_config(cfg)
    _validate_usage_config(cfg, root=root)
    server_log_cfg = _require_optional_mapping(cfg, "server_log", path="server_log")
    if server_log_cfg is None:
        server_log_cfg = {}
    if "path" in server_log_cfg:
        if not isinstance(server_log_cfg.get("path", ""), str):
            raise ConfigError("server_log.path must be a string path")
        try:
            resolve_config_path(server_log_cfg["path"], root, scope="server_log.path")
        except ConfigPathError as exc:
            raise ConfigError(str(exc)) from exc
    for key in ("max_bytes", "backup_count"):
        if key in server_log_cfg and not isinstance(server_log_cfg.get(key, 0), int):
            raise ConfigError(f"server_log.{key} must be an integer")
    _validate_static_assets_config(cfg, scope="hub")
    _validate_housekeeping_config(cfg)
    _validate_pma_config(cfg)
    _validate_collaboration_policy_config(cfg)
    _validate_telegram_bot_config(cfg)
    _validate_discord_bot_config(cfg)


def _validate_optional_type(
    mapping: Dict[str, Any],
    key: str,
    expected: Union[Type, Tuple[Type, ...]],
    *,
    path: str,
    allow_none: bool = False,
) -> None:
    if key in mapping:
        value = mapping.get(key)
        if value is None and allow_none:
            return
        if expected is int and isinstance(value, bool):
            raise ConfigError(f"{path}.{key} must be int if provided")
        if isinstance(value, expected):
            return
        type_name = (
            " or ".join(t.__name__ for t in expected)
            if isinstance(expected, tuple)
            else expected.__name__
        )
        raise ConfigError(f"{path}.{key} must be {type_name} if provided")


def _validate_optional_int_ge(
    mapping: Dict[str, Any], key: str, min_value: int, *, path: str
) -> None:
    if key in mapping:
        value = mapping.get(key)
        if _is_strict_int(value) and cast(int, value) < min_value:
            if min_value == 0:
                raise ConfigError(f"{path}.{key} must be >= 0")
            elif min_value == 1:
                raise ConfigError(f"{path}.{key} must be > 0")
            else:
                raise ConfigError(f"{path}.{key} must be >= {min_value}")


_FLOW_RETENTION_RULES = (
    _ConfigRule(
        "retention_days",
        lambda mapping, key, path: _validate_positive_strict_int(
            mapping, key, path=path
        ),
    ),
    _ConfigRule(
        "sweep_interval_seconds",
        lambda mapping, key, path: _validate_positive_strict_int(
            mapping, key, path=path
        ),
    ),
)

_HOUSEKEEPING_RULES = (
    _optional_type_rule("enabled", bool),
    _optional_type_rule("interval_seconds", int),
    _optional_int_ge_rule("interval_seconds", 1),
    _optional_type_rule("min_file_age_seconds", int),
    _optional_int_ge_rule("min_file_age_seconds", 0),
    _optional_type_rule("dry_run", bool),
)

_HOUSEKEEPING_RULE_FIELD_RULES = tuple(
    rule
    for key in (
        "max_files",
        "max_total_bytes",
        "max_age_days",
        "max_bytes",
        "max_lines",
    )
    for rule in (_optional_type_rule(key, int), _optional_int_ge_rule(key, 0))
)

_PMA_ARCHIVE_RULES = (
    _optional_type_rule("cleanup_require_archive", bool),
    _optional_type_rule("cleanup_auto_delete_orphans", bool),
    _optional_type_rule("worktree_archive_profile", str),
)

_PMA_STATE_CLEANUP_KEYS = (
    "turn_idle_timeout_seconds",
    "turn_timeout_seconds",
    "filebox_inbox_max_age_days",
    "filebox_outbox_max_age_days",
    "worktree_archive_max_snapshots_per_repo",
    "worktree_archive_max_age_days",
    "worktree_archive_max_total_bytes",
    "run_archive_max_entries",
    "run_archive_max_age_days",
    "run_archive_max_total_bytes",
    "orchestration_compaction_max_hot_rows",
    "orchestration_hot_history_retention_days",
    "orchestration_cold_trace_retention_days",
    "report_max_history_files",
    "report_max_total_bytes",
    "app_server_workspace_max_age_days",
    "inbox_auto_dismiss_grace_seconds",
)

_PMA_STATE_CLEANUP_RULES = tuple(
    rule
    for key in _PMA_STATE_CLEANUP_KEYS
    for rule in (
        _optional_type_rule(key, int),
        _optional_int_ge_rule(
            key,
            1 if key in {"turn_idle_timeout_seconds", "turn_timeout_seconds"} else 0,
        ),
    )
)

_STATIC_ASSETS_RULES = (
    _optional_type_rule("cache_root", str, allow_none=True),
    _optional_type_rule("max_cache_entries", int, allow_none=True),
    _optional_int_ge_rule("max_cache_entries", 0),
    _optional_type_rule("max_cache_age_days", int, allow_none=True),
    _optional_int_ge_rule("max_cache_age_days", 0),
)


def _validate_positive_strict_int(
    mapping: Dict[str, Any], key: str, *, path: str
) -> None:
    value = mapping.get(key)
    if value is None:
        return
    if not _is_strict_int(value):
        raise ConfigError(f"{path}.{key} must be an integer")
    if value <= 0:
        raise ConfigError(f"{path}.{key} must be > 0")


def _validate_flow_retention_config(cfg: Dict[str, Any]) -> None:
    flow_retention = cfg.get("flow_retention")
    if flow_retention is None:
        return
    if not isinstance(flow_retention, dict):
        raise ConfigError("flow_retention must be a mapping")
    _apply_config_rules(
        flow_retention, path="flow_retention", rules=_FLOW_RETENTION_RULES
    )


def _validate_housekeeping_config(cfg: Dict[str, Any]) -> None:
    housekeeping_cfg = cfg.get("housekeeping")
    if housekeeping_cfg is None:
        return
    if not isinstance(housekeeping_cfg, dict):
        raise ConfigError("housekeeping section must be a mapping if provided")
    _apply_config_rules(
        housekeeping_cfg, path="housekeeping", rules=_HOUSEKEEPING_RULES
    )
    rules = housekeeping_cfg.get("rules")
    if rules is not None and not isinstance(rules, list):
        raise ConfigError("housekeeping.rules must be a list if provided")
    if isinstance(rules, list):
        for idx, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise ConfigError(
                    f"housekeeping.rules[{idx}] must be a mapping if provided"
                )
            _validate_optional_type(
                rule, "name", str, path=f"housekeeping.rules[{idx}]"
            )
            if "kind" in rule:
                kind = rule.get("kind")
                if not isinstance(kind, str):
                    raise ConfigError(
                        f"housekeeping.rules[{idx}].kind must be a string"
                    )
                if kind not in ("directory", "file"):
                    raise ConfigError(
                        f"housekeeping.rules[{idx}].kind must be 'directory' or 'file'"
                    )
            if "path" in rule:
                path_value = rule.get("path")
                if not isinstance(path_value, str) or not path_value:
                    raise ConfigError(
                        f"housekeeping.rules[{idx}].path must be a non-empty string path"
                    )
                path = Path(path_value)
                if path.is_absolute():
                    raise ConfigError(
                        f"housekeeping.rules[{idx}].path must be relative or start with '~'"
                    )
                if ".." in path.parts:
                    raise ConfigError(
                        f"housekeeping.rules[{idx}].path must not contain '..' segments"
                    )
            _validate_optional_type(
                rule, "glob", str, path=f"housekeeping.rules[{idx}]"
            )
            _validate_optional_type(
                rule, "recursive", bool, path=f"housekeeping.rules[{idx}]"
            )
            _apply_config_rules(
                rule,
                path=f"housekeeping.rules[{idx}]",
                rules=_HOUSEKEEPING_RULE_FIELD_RULES,
            )


def _validate_pma_config(cfg: Dict[str, Any]) -> None:
    pma_cfg = cfg.get("pma")
    if pma_cfg is None:
        return
    if not isinstance(pma_cfg, dict):
        raise ConfigError("pma section must be a mapping if provided")
    _apply_config_rules(pma_cfg, path="pma", rules=_PMA_ARCHIVE_RULES)
    profile = pma_cfg.get("worktree_archive_profile")
    if isinstance(profile, str) and profile.strip().lower() not in {"portable", "full"}:
        raise ConfigError("pma.worktree_archive_profile must be 'portable' or 'full'")
    for timeout_key in ("turn_idle_timeout_seconds", "turn_timeout_seconds"):
        if timeout_key not in pma_cfg:
            continue
        tt = pma_cfg.get(timeout_key)
        if not _is_strict_int(tt):
            raise ConfigError(f"pma.{timeout_key} must be an integer if provided")
        _validate_optional_int_ge(pma_cfg, timeout_key, 1, path="pma")
    _apply_config_rules(pma_cfg, path="pma", rules=_PMA_STATE_CLEANUP_RULES)


def _validate_static_assets_config(cfg: Dict[str, Any], scope: str) -> None:
    static_cfg = cfg.get("static_assets")
    if static_cfg is None:
        return
    if not isinstance(static_cfg, dict):
        raise ConfigError(f"{scope}.static_assets must be a mapping if provided")
    _apply_config_rules(
        static_cfg, path=f"{scope}.static_assets", rules=_STATIC_ASSETS_RULES
    )


def _validate_telegram_bot_config(cfg: Dict[str, Any]) -> None:
    telegram_cfg = cfg.get("telegram_bot")
    if telegram_cfg is None:
        return
    if not isinstance(telegram_cfg, dict):
        raise ConfigError("telegram_bot section must be a mapping if provided")
    if "enabled" in telegram_cfg and not isinstance(telegram_cfg.get("enabled"), bool):
        raise ConfigError("telegram_bot.enabled must be boolean")
    if "mode" in telegram_cfg and not isinstance(telegram_cfg.get("mode"), str):
        raise ConfigError("telegram_bot.mode must be a string")
    if "parse_mode" in telegram_cfg:
        parse_mode = telegram_cfg.get("parse_mode")
        if parse_mode is not None and not isinstance(parse_mode, str):
            raise ConfigError("telegram_bot.parse_mode must be a string or null")
        if isinstance(parse_mode, str):
            normalized = parse_mode.strip().lower()
            if normalized and normalized not in ("html", "markdown", "markdownv2"):
                raise ConfigError(
                    "telegram_bot.parse_mode must be HTML, Markdown, MarkdownV2, or null"
                )
    debug_cfg = _require_optional_mapping(
        telegram_cfg, "debug", path="telegram_bot.debug"
    )
    if debug_cfg is not None:
        if "prefix_context" in debug_cfg and not isinstance(
            debug_cfg.get("prefix_context"), bool
        ):
            raise ConfigError("telegram_bot.debug.prefix_context must be boolean")
    for key in ("bot_token_env", "chat_id_env"):
        if key in telegram_cfg and not isinstance(telegram_cfg.get(key), str):
            raise ConfigError(f"telegram_bot.{key} must be a string")
    for key in ("allowed_chat_ids", "allowed_user_ids"):
        if key in telegram_cfg and not isinstance(telegram_cfg.get(key), list):
            raise ConfigError(f"telegram_bot.{key} must be a list")
    if "require_topics" in telegram_cfg and not isinstance(
        telegram_cfg.get("require_topics"), bool
    ):
        raise ConfigError("telegram_bot.require_topics must be boolean")
    defaults_cfg = _require_optional_mapping(
        telegram_cfg, "defaults", path="telegram_bot.defaults"
    )
    if defaults_cfg is not None:
        if "approval_mode" in defaults_cfg:
            _normalize_ticket_flow_approval_mode(
                defaults_cfg.get("approval_mode"),
                scope="telegram_bot.defaults.approval_mode",
            )
        for key in (
            "approval_policy",
            "sandbox_policy",
            "yolo_approval_policy",
            "yolo_sandbox_policy",
        ):
            if (
                key in defaults_cfg
                and defaults_cfg.get(key) is not None
                and not isinstance(defaults_cfg.get(key), str)
            ):
                raise ConfigError(
                    f"telegram_bot.defaults.{key} must be a string or null"
                )
    concurrency_cfg = _require_optional_mapping(
        telegram_cfg, "concurrency", path="telegram_bot.concurrency"
    )
    if concurrency_cfg is not None:
        if "max_parallel_turns" in concurrency_cfg and not isinstance(
            concurrency_cfg.get("max_parallel_turns"), int
        ):
            raise ConfigError(
                "telegram_bot.concurrency.max_parallel_turns must be an integer"
            )
        if "per_topic_queue" in concurrency_cfg and not isinstance(
            concurrency_cfg.get("per_topic_queue"), bool
        ):
            raise ConfigError(
                "telegram_bot.concurrency.per_topic_queue must be boolean"
            )
    media_cfg = _require_optional_mapping(
        telegram_cfg, "media", path="telegram_bot.media"
    )
    if media_cfg is not None:
        if "enabled" in media_cfg and not isinstance(media_cfg.get("enabled"), bool):
            raise ConfigError("telegram_bot.media.enabled must be boolean")
        if "images" in media_cfg and not isinstance(media_cfg.get("images"), bool):
            raise ConfigError("telegram_bot.media.images must be boolean")
        if "voice" in media_cfg and not isinstance(media_cfg.get("voice"), bool):
            raise ConfigError("telegram_bot.media.voice must be boolean")
        if "files" in media_cfg and not isinstance(media_cfg.get("files"), bool):
            raise ConfigError("telegram_bot.media.files must be boolean")
        for key in ("max_image_bytes", "max_voice_bytes", "max_file_bytes"):
            value = media_cfg.get(key)
            if value is not None and not isinstance(value, int):
                raise ConfigError(f"telegram_bot.media.{key} must be an integer")
            if isinstance(value, int) and value <= 0:
                raise ConfigError(f"telegram_bot.media.{key} must be greater than 0")
        if "image_prompt" in media_cfg and not isinstance(
            media_cfg.get("image_prompt"), str
        ):
            raise ConfigError("telegram_bot.media.image_prompt must be a string")
    shell_cfg = _require_optional_mapping(
        telegram_cfg, "shell", path="telegram_bot.shell"
    )
    if shell_cfg is not None:
        if "enabled" in shell_cfg and not isinstance(shell_cfg.get("enabled"), bool):
            raise ConfigError("telegram_bot.shell.enabled must be boolean")
        for key in ("timeout_ms", "max_output_chars"):
            value = shell_cfg.get(key)
            if value is not None and not isinstance(value, int):
                raise ConfigError(f"telegram_bot.shell.{key} must be an integer")
            if isinstance(value, int) and value <= 0:
                raise ConfigError(f"telegram_bot.shell.{key} must be greater than 0")
    cache_cfg = _require_optional_mapping(
        telegram_cfg, "cache", path="telegram_bot.cache"
    )
    if cache_cfg is not None:
        for key in (
            "cleanup_interval_seconds",
            "coalesce_buffer_ttl_seconds",
            "media_batch_buffer_ttl_seconds",
            "model_pending_ttl_seconds",
            "pending_approval_ttl_seconds",
            "pending_question_ttl_seconds",
            "reasoning_buffer_ttl_seconds",
            "selection_state_ttl_seconds",
            "turn_preview_ttl_seconds",
            "progress_stream_ttl_seconds",
            "oversize_warning_ttl_seconds",
            "update_id_persist_interval_seconds",
        ):
            value = cache_cfg.get(key)
            if value is not None and not isinstance(value, (int, float)):
                raise ConfigError(f"telegram_bot.cache.{key} must be a number")
            if isinstance(value, (int, float)) and value <= 0:
                raise ConfigError(f"telegram_bot.cache.{key} must be > 0")
    command_reg_cfg = _require_optional_mapping(
        telegram_cfg, "command_registration", path="telegram_bot.command_registration"
    )
    if command_reg_cfg is not None:
        if "enabled" in command_reg_cfg and not isinstance(
            command_reg_cfg.get("enabled"), bool
        ):
            raise ConfigError(
                "telegram_bot.command_registration.enabled must be boolean"
            )
        if "scopes" in command_reg_cfg:
            scopes = command_reg_cfg.get("scopes")
            if not isinstance(scopes, list):
                raise ConfigError(
                    "telegram_bot.command_registration.scopes must be a list"
                )
            for scope in scopes:
                if isinstance(scope, str):
                    continue
                if not isinstance(scope, dict):
                    raise ConfigError(
                        "telegram_bot.command_registration.scopes must contain strings or mappings"
                    )
                scope_payload = scope.get("scope")
                if scope_payload is not None and not isinstance(scope_payload, dict):
                    raise ConfigError(
                        "telegram_bot.command_registration.scopes.scope must be a mapping"
                    )
                if "type" in scope and not isinstance(scope.get("type"), str):
                    raise ConfigError(
                        "telegram_bot.command_registration.scopes.type must be a string"
                    )
                language_code = scope.get("language_code")
                if language_code is not None and not isinstance(language_code, str):
                    raise ConfigError(
                        "telegram_bot.command_registration.scopes.language_code must be a string or null"
                    )
    if "trigger_mode" in telegram_cfg and not isinstance(
        telegram_cfg.get("trigger_mode"), str
    ):
        raise ConfigError("telegram_bot.trigger_mode must be a string")
    if "state_file" in telegram_cfg and not isinstance(
        telegram_cfg.get("state_file"), str
    ):
        raise ConfigError("telegram_bot.state_file must be a string path")
    if (
        "opencode_command" in telegram_cfg
        and not isinstance(telegram_cfg.get("opencode_command"), (list, str))
        and telegram_cfg.get("opencode_command") is not None
    ):
        raise ConfigError("telegram_bot.opencode_command must be a list or string")
    if "app_server_command" in telegram_cfg and not isinstance(
        telegram_cfg.get("app_server_command"), (list, str)
    ):
        raise ConfigError("telegram_bot.app_server_command must be a list or string")
    app_server_cfg = _require_optional_mapping(
        telegram_cfg, "app_server", path="telegram_bot.app_server"
    )
    if app_server_cfg is not None:
        if (
            "turn_timeout_seconds" in app_server_cfg
            and app_server_cfg.get("turn_timeout_seconds") is not None
            and not isinstance(app_server_cfg.get("turn_timeout_seconds"), (int, float))
        ):
            raise ConfigError(
                "telegram_bot.app_server.turn_timeout_seconds must be a number or null"
            )
    agent_timeouts_cfg = _require_optional_mapping(
        telegram_cfg, "agent_timeouts", path="telegram_bot.agent_timeouts"
    )
    if agent_timeouts_cfg is not None:
        for _key, value in agent_timeouts_cfg.items():
            if value is None:
                continue
            if not isinstance(value, (int, float)):
                raise ConfigError(
                    "telegram_bot.agent_timeouts values must be numbers or null"
                )
    polling_cfg = _require_optional_mapping(
        telegram_cfg, "polling", path="telegram_bot.polling"
    )
    if polling_cfg is not None:
        if "timeout_seconds" in polling_cfg and not isinstance(
            polling_cfg.get("timeout_seconds"), int
        ):
            raise ConfigError("telegram_bot.polling.timeout_seconds must be an integer")
        timeout_seconds = polling_cfg.get("timeout_seconds")
        if isinstance(timeout_seconds, int) and timeout_seconds <= 0:
            raise ConfigError(
                "telegram_bot.polling.timeout_seconds must be greater than 0"
            )
        if "allowed_updates" in polling_cfg and not isinstance(
            polling_cfg.get("allowed_updates"), list
        ):
            raise ConfigError("telegram_bot.polling.allowed_updates must be a list")


def _validate_discord_bot_config(cfg: Dict[str, Any]) -> None:
    discord_cfg = cfg.get("discord_bot")
    if discord_cfg is None:
        return
    if not isinstance(discord_cfg, dict):
        raise ConfigError("discord_bot section must be a mapping if provided")
    if "enabled" in discord_cfg and not isinstance(discord_cfg.get("enabled"), bool):
        raise ConfigError("discord_bot.enabled must be boolean")
    for key in ("bot_token_env", "app_id_env"):
        if key in discord_cfg and not isinstance(discord_cfg.get(key), str):
            raise ConfigError(f"discord_bot.{key} must be a string")
    for key in ("allowed_guild_ids", "allowed_channel_ids", "allowed_user_ids"):
        value = discord_cfg.get(key)
        if value is not None and not isinstance(value, list):
            raise ConfigError(f"discord_bot.{key} must be a list")
        if isinstance(value, list):
            for entry in value:
                if not isinstance(entry, (str, int)):
                    raise ConfigError(
                        f"discord_bot.{key} must contain only string/int IDs"
                    )
    if "state_file" in discord_cfg and not isinstance(
        discord_cfg.get("state_file"), str
    ):
        raise ConfigError("discord_bot.state_file must be a string path")
    if "intents" in discord_cfg and not isinstance(discord_cfg.get("intents"), int):
        raise ConfigError("discord_bot.intents must be an integer")
    if "max_message_length" in discord_cfg and not isinstance(
        discord_cfg.get("max_message_length"), int
    ):
        raise ConfigError("discord_bot.max_message_length must be an integer")

    command_registration = _require_optional_mapping(
        discord_cfg,
        "command_registration",
        path="discord_bot.command_registration",
    )
    if command_registration is not None:
        if "enabled" in command_registration and not isinstance(
            command_registration.get("enabled"), bool
        ):
            raise ConfigError(
                "discord_bot.command_registration.enabled must be boolean"
            )
        scope = command_registration.get("scope")
        if scope is not None:
            if not isinstance(scope, str):
                raise ConfigError(
                    "discord_bot.command_registration.scope must be a string"
                )
            if scope not in {"global", "guild"}:
                raise ConfigError(
                    "discord_bot.command_registration.scope must be 'global' or 'guild'"
                )
        guild_ids = command_registration.get("guild_ids")
        if guild_ids is not None and not isinstance(guild_ids, list):
            raise ConfigError(
                "discord_bot.command_registration.guild_ids must be a list"
            )
        if isinstance(guild_ids, list):
            for entry in guild_ids:
                if not isinstance(entry, (str, int)):
                    raise ConfigError(
                        "discord_bot.command_registration.guild_ids must contain only string/int IDs"
                    )

    media_cfg = _require_optional_mapping(
        discord_cfg, "media", path="discord_bot.media"
    )
    if media_cfg is not None:
        if "enabled" in media_cfg and not isinstance(media_cfg.get("enabled"), bool):
            raise ConfigError("discord_bot.media.enabled must be boolean")
        if "voice" in media_cfg and not isinstance(media_cfg.get("voice"), bool):
            raise ConfigError("discord_bot.media.voice must be boolean")
        if "max_voice_bytes" in media_cfg and not isinstance(
            media_cfg.get("max_voice_bytes"), int
        ):
            raise ConfigError("discord_bot.media.max_voice_bytes must be an integer")
        if (
            isinstance(media_cfg.get("max_voice_bytes"), int)
            and int(media_cfg["max_voice_bytes"]) <= 0
        ):
            raise ConfigError(
                "discord_bot.media.max_voice_bytes must be greater than 0"
            )
