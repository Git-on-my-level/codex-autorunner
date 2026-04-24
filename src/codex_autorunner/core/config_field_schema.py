"""Shared field schemas for config validation and parser construction.

These schemas exist specifically for fields whose semantics previously drifted
between ``config_validation.py``, ``config_parsers.py``, and ``agent_config.py``.
The schema is intentionally small and explicit: each field declares its type
shape, default/fallback behaviour, bounds, aliases, path resolution rules, and
compatibility coercion policy.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable, Mapping

from .config_contract import (
    _TICKET_FLOW_APPROVAL_MODE_ALIASES,
    _TICKET_FLOW_APPROVAL_MODE_ALLOWED,
    APP_SERVER_OUTPUT_POLICIES,
    OPENCODE_SERVER_SCOPE_VALUES,
    UPDATE_BACKEND_VALUES,
    USAGE_CACHE_SCOPE_VALUES,
    ConfigError,
)
from .path_utils import ConfigPathError, resolve_config_path

SchemaDefault = Any
SchemaParseFn = Callable[[Any, "FieldParseContext"], Any]
SchemaValidateFn = Callable[[Any, "FieldValidationContext"], None]
SchemaDefaultFactory = Callable[[], Any]

SCHEMA_MISSING = object()


@dataclasses.dataclass(frozen=True)
class PathResolutionBehavior:
    allow_absolute: bool = False
    allow_home: bool = False
    allow_dotdot: bool = False


@dataclasses.dataclass(frozen=True)
class FieldSchema:
    path: str
    kind: str
    default: SchemaDefault = SCHEMA_MISSING
    allow_none: bool = False
    required: bool = False
    nonempty: bool = False
    lowercase: bool = False
    min_value: float | None = None
    aliases: Mapping[str, str] = dataclasses.field(default_factory=dict)
    allowed_values: tuple[str, ...] = ()
    path_behavior: PathResolutionBehavior | None = None
    parse_policy: str = "strict"
    type_message: str | None = None
    value_message: str | None = None
    range_message: str | None = None
    parse_fn: SchemaParseFn | None = None
    validate_fn: SchemaValidateFn | None = None
    reject_bool: bool = False


@dataclasses.dataclass(frozen=True)
class FieldValidationContext:
    schema: FieldSchema
    root: Path | None


@dataclasses.dataclass(frozen=True)
class FieldParseContext:
    schema: FieldSchema
    root: Path | None
    default_value: Any


def _materialize_default(default: SchemaDefault) -> Any:
    if default is SCHEMA_MISSING:
        return SCHEMA_MISSING
    if callable(default):
        factory = default
        return factory()
    return default


def schema_paths(*schema_groups: Mapping[str, FieldSchema]) -> frozenset[str]:
    return frozenset(
        schema.path for group in schema_groups for schema in group.values()
    )


def normalize_choice_value(value: Any, schema: FieldSchema) -> str:
    if not isinstance(value, str):
        raise ConfigError(schema.type_message or f"{schema.path} must be a string")
    normalized = value.strip().lower()
    canonical = schema.aliases.get(normalized, normalized)
    if schema.allowed_values and canonical not in schema.allowed_values:
        raise ConfigError(
            schema.value_message
            or f"{schema.path} must be one of: {', '.join(schema.allowed_values)}"
        )
    return canonical


def validate_schema_field(
    value: Any,
    schema: FieldSchema,
    *,
    root: Path | None = None,
) -> None:
    if schema.validate_fn is not None:
        schema.validate_fn(value, FieldValidationContext(schema=schema, root=root))
        return
    if value is None:
        if schema.allow_none:
            return
        raise ConfigError(schema.type_message or f"{schema.path} is required")

    if schema.kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(schema.type_message or f"{schema.path} must be boolean")
        return

    if schema.kind == "mapping":
        if not isinstance(value, dict):
            raise ConfigError(schema.type_message or f"{schema.path} must be a mapping")
        return

    if schema.kind == "command":
        if not isinstance(value, (list, str)):
            raise ConfigError(
                schema.type_message or f"{schema.path} must be a list or string"
            )
        return

    if schema.kind == "path":
        if not isinstance(value, str):
            raise ConfigError(
                schema.type_message or f"{schema.path} must be a string path"
            )
        if root is None:
            return
        behavior = schema.path_behavior or PathResolutionBehavior()
        try:
            resolve_config_path(
                value,
                root,
                allow_absolute=behavior.allow_absolute,
                allow_home=behavior.allow_home,
                allow_dotdot=behavior.allow_dotdot,
                scope=schema.path,
            )
        except ConfigPathError as exc:
            raise ConfigError(str(exc)) from exc
        return

    if schema.kind == "choice":
        normalize_choice_value(value, schema)
        return

    if schema.kind == "string":
        if not isinstance(value, str):
            raise ConfigError(schema.type_message or f"{schema.path} must be a string")
        if schema.nonempty and not value.strip():
            raise ConfigError(
                schema.value_message or f"{schema.path} must be a non-empty string"
            )
        return

    if schema.kind in {"int", "number"}:
        allowed_types: tuple[type[Any], ...] = (
            (int, float) if schema.kind == "number" else (int,)
        )
        if schema.reject_bool and isinstance(value, bool):
            raise ConfigError(schema.type_message or f"{schema.path} must be numeric")
        if not isinstance(value, allowed_types):
            raise ConfigError(schema.type_message or f"{schema.path} must be numeric")
        if schema.min_value is not None and value < schema.min_value:
            raise ConfigError(
                schema.range_message or f"{schema.path} must be >= {schema.min_value:g}"
            )
        return

    raise ConfigError(f"Unsupported schema kind for {schema.path}: {schema.kind}")


def parse_schema_field(
    value: Any,
    schema: FieldSchema,
    *,
    default_value: Any = SCHEMA_MISSING,
    root: Path | None = None,
) -> Any:
    default_value = (
        _materialize_default(schema.default)
        if default_value is SCHEMA_MISSING
        else default_value
    )
    if schema.parse_fn is not None:
        return schema.parse_fn(
            value,
            FieldParseContext(schema=schema, root=root, default_value=default_value),
        )
    try:
        validate_schema_field(value, schema, root=root)
    except ConfigError:
        if schema.parse_policy == "fallback_default":
            return default_value
        if schema.parse_policy == "fallback_none":
            return None
        raise

    if value is None:
        return None

    if schema.kind == "bool":
        return value
    if schema.kind == "mapping":
        return dict(value)
    if schema.kind == "command":
        return value
    if schema.kind == "path":
        behavior = schema.path_behavior or PathResolutionBehavior()
        try:
            return resolve_config_path(
                value,
                root or Path.cwd(),
                allow_absolute=behavior.allow_absolute,
                allow_home=behavior.allow_home,
                allow_dotdot=behavior.allow_dotdot,
                scope=schema.path,
            )
        except ConfigPathError as exc:
            raise ConfigError(str(exc)) from exc
    if schema.kind == "choice":
        return normalize_choice_value(value, schema)
    if schema.kind == "string":
        normalized = value.strip() if schema.nonempty or schema.lowercase else value
        return normalized.lower() if schema.lowercase else normalized
    if schema.kind == "int":
        return int(value)
    if schema.kind == "number":
        return float(value)
    raise ConfigError(f"Unsupported schema kind for {schema.path}: {schema.kind}")


def default_from_mapping(
    defaults: Mapping[str, Any] | None,
    key: str,
    schema: FieldSchema,
) -> Any:
    if isinstance(defaults, Mapping) and key in defaults:
        return defaults[key]
    return _materialize_default(schema.default)


_HOME_ALLOWED = PathResolutionBehavior(allow_home=True)
_ABSOLUTE_HOME_ALLOWED = PathResolutionBehavior(allow_absolute=True, allow_home=True)

TICKET_FLOW_FIELD_SCHEMAS: dict[str, FieldSchema] = {
    "approval_mode": FieldSchema(
        path="ticket_flow.approval_mode",
        kind="choice",
        default="yolo",
        aliases=_TICKET_FLOW_APPROVAL_MODE_ALIASES,
        allowed_values=("yolo", "review"),
        type_message="ticket_flow.approval_mode must be a string",
        value_message=f"ticket_flow.approval_mode must be one of: {_TICKET_FLOW_APPROVAL_MODE_ALLOWED}",
    ),
    "default_approval_decision": FieldSchema(
        path="ticket_flow.default_approval_decision",
        kind="string",
        default="accept",
        nonempty=False,
        type_message="ticket_flow.default_approval_decision must be a string",
    ),
    "include_previous_ticket_context": FieldSchema(
        path="ticket_flow.include_previous_ticket_context",
        kind="bool",
        default=False,
        type_message="ticket_flow.include_previous_ticket_context must be boolean",
    ),
    "auto_resume": FieldSchema(
        path="ticket_flow.auto_resume",
        kind="bool",
        default=False,
        type_message="ticket_flow.auto_resume must be boolean",
    ),
    "max_total_turns": FieldSchema(
        path="ticket_flow.max_total_turns",
        kind="int",
        default=None,
        allow_none=True,
        min_value=1,
        type_message="ticket_flow.max_total_turns must be a positive integer or null",
        range_message="ticket_flow.max_total_turns must be a positive integer or null",
        reject_bool=True,
    ),
}

APP_SERVER_FIELD_SCHEMAS: dict[str, FieldSchema] = {
    "command": FieldSchema(
        path="app_server.command",
        kind="command",
        default=("codex", "app-server"),
        allow_none=True,
        type_message="app_server.command must be a list or string if provided",
    ),
    "state_root": FieldSchema(
        path="app_server.state_root",
        kind="path",
        default="~/.codex-autorunner/workspaces",
        path_behavior=_HOME_ALLOWED,
        type_message="app_server.state_root must be a string path",
    ),
    "auto_restart": FieldSchema(
        path="app_server.auto_restart",
        kind="bool",
        default=True,
        allow_none=True,
        type_message="app_server.auto_restart must be boolean or null",
    ),
    "max_handles": FieldSchema(
        path="app_server.max_handles",
        kind="int",
        default=20,
        allow_none=True,
        min_value=1,
        parse_policy="fallback_none",
        type_message="app_server.max_handles must be an integer or null",
        range_message="app_server.max_handles must be > 0 or null",
    ),
    "idle_ttl_seconds": FieldSchema(
        path="app_server.idle_ttl_seconds",
        kind="int",
        default=3600,
        allow_none=True,
        min_value=1,
        parse_policy="fallback_none",
        type_message="app_server.idle_ttl_seconds must be an integer or null",
        range_message="app_server.idle_ttl_seconds must be > 0 or null",
    ),
    "turn_timeout_seconds": FieldSchema(
        path="app_server.turn_timeout_seconds",
        kind="number",
        default=7200,
        allow_none=True,
        min_value=1,
        parse_policy="fallback_none",
        type_message="app_server.turn_timeout_seconds must be a number or null",
        range_message="app_server.turn_timeout_seconds must be > 0 or null",
    ),
    "turn_stall_timeout_seconds": FieldSchema(
        path="app_server.turn_stall_timeout_seconds",
        kind="number",
        default=60,
        allow_none=True,
        min_value=1,
        parse_policy="fallback_none",
        type_message="app_server.turn_stall_timeout_seconds must be a number or null",
        range_message="app_server.turn_stall_timeout_seconds must be > 0 or null",
    ),
    "turn_stall_poll_interval_seconds": FieldSchema(
        path="app_server.turn_stall_poll_interval_seconds",
        kind="number",
        default=2,
        allow_none=True,
        min_value=1,
        parse_policy="fallback_default",
        type_message="app_server.turn_stall_poll_interval_seconds must be a number or null",
        range_message="app_server.turn_stall_poll_interval_seconds must be > 0 or null",
    ),
    "turn_stall_recovery_min_interval_seconds": FieldSchema(
        path="app_server.turn_stall_recovery_min_interval_seconds",
        kind="number",
        default=10,
        allow_none=True,
        min_value=0,
        parse_policy="fallback_default",
        type_message="app_server.turn_stall_recovery_min_interval_seconds must be a number or null",
        range_message="app_server.turn_stall_recovery_min_interval_seconds must be >= 0 or null",
    ),
    "turn_stall_max_recovery_attempts": FieldSchema(
        path="app_server.turn_stall_max_recovery_attempts",
        kind="int",
        default=8,
        allow_none=True,
        min_value=1,
        parse_policy="fallback_none",
        type_message="app_server.turn_stall_max_recovery_attempts must be an integer or null",
        range_message="app_server.turn_stall_max_recovery_attempts must be > 0 or null",
    ),
    "request_timeout": FieldSchema(
        path="app_server.request_timeout",
        kind="number",
        default=None,
        allow_none=True,
        min_value=1,
        parse_policy="fallback_none",
        type_message="app_server.request_timeout must be a number or null",
        range_message="app_server.request_timeout must be > 0 or null",
    ),
}

APP_SERVER_CLIENT_FIELD_SCHEMAS: dict[str, FieldSchema] = {
    "max_message_bytes": FieldSchema(
        path="app_server.client.max_message_bytes",
        kind="int",
        default=50 * 1024 * 1024,
        min_value=1,
        parse_policy="fallback_default",
        type_message="app_server.client.max_message_bytes must be an integer",
        range_message="app_server.client.max_message_bytes must be > 0",
    ),
    "oversize_preview_bytes": FieldSchema(
        path="app_server.client.oversize_preview_bytes",
        kind="int",
        default=4096,
        min_value=1,
        parse_policy="fallback_default",
        type_message="app_server.client.oversize_preview_bytes must be an integer",
        range_message="app_server.client.oversize_preview_bytes must be > 0",
    ),
    "max_oversize_drain_bytes": FieldSchema(
        path="app_server.client.max_oversize_drain_bytes",
        kind="int",
        default=100 * 1024 * 1024,
        min_value=1,
        parse_policy="fallback_default",
        type_message="app_server.client.max_oversize_drain_bytes must be an integer",
        range_message="app_server.client.max_oversize_drain_bytes must be > 0",
    ),
    "restart_backoff_initial_seconds": FieldSchema(
        path="app_server.client.restart_backoff_initial_seconds",
        kind="number",
        default=0.5,
        min_value=1e-12,
        parse_policy="fallback_default",
        type_message="app_server.client.restart_backoff_initial_seconds must be a number if provided",
        range_message="app_server.client.restart_backoff_initial_seconds must be > 0",
    ),
    "restart_backoff_max_seconds": FieldSchema(
        path="app_server.client.restart_backoff_max_seconds",
        kind="number",
        default=30.0,
        min_value=1e-12,
        parse_policy="fallback_default",
        type_message="app_server.client.restart_backoff_max_seconds must be a number if provided",
        range_message="app_server.client.restart_backoff_max_seconds must be > 0",
    ),
    "restart_backoff_jitter_ratio": FieldSchema(
        path="app_server.client.restart_backoff_jitter_ratio",
        kind="number",
        default=0.1,
        min_value=0,
        parse_policy="fallback_default",
        type_message="app_server.client.restart_backoff_jitter_ratio must be a number if provided",
        range_message="app_server.client.restart_backoff_jitter_ratio must be >= 0",
    ),
}

APP_SERVER_OUTPUT_FIELD_SCHEMAS: dict[str, FieldSchema] = {
    "policy": FieldSchema(
        path="app_server.output.policy",
        kind="choice",
        default="final_only",
        allowed_values=tuple(APP_SERVER_OUTPUT_POLICIES),
        type_message="app_server.output.policy must be a string",
        value_message=(
            "app_server.output.policy must be one of: "
            + ", ".join(sorted(APP_SERVER_OUTPUT_POLICIES))
        ),
    )
}

APP_SERVER_PROMPT_SECTION_SCHEMAS: dict[str, dict[str, FieldSchema]] = {
    "doc_chat": {
        "max_chars": FieldSchema(
            path="app_server.prompts.doc_chat.max_chars",
            kind="int",
            default=12000,
            min_value=1,
            parse_policy="fallback_default",
            type_message="app_server.prompts.doc_chat.max_chars must be an integer",
            range_message="app_server.prompts.doc_chat.max_chars must be >= 1",
        ),
        "message_max_chars": FieldSchema(
            path="app_server.prompts.doc_chat.message_max_chars",
            kind="int",
            default=2000,
            min_value=1,
            parse_policy="fallback_default",
            type_message="app_server.prompts.doc_chat.message_max_chars must be an integer",
            range_message="app_server.prompts.doc_chat.message_max_chars must be >= 1",
        ),
        "target_excerpt_max_chars": FieldSchema(
            path="app_server.prompts.doc_chat.target_excerpt_max_chars",
            kind="int",
            default=4000,
            min_value=0,
            parse_policy="fallback_default",
            type_message="app_server.prompts.doc_chat.target_excerpt_max_chars must be an integer",
            range_message="app_server.prompts.doc_chat.target_excerpt_max_chars must be >= 0",
        ),
        "recent_summary_max_chars": FieldSchema(
            path="app_server.prompts.doc_chat.recent_summary_max_chars",
            kind="int",
            default=2000,
            min_value=0,
            parse_policy="fallback_default",
            type_message="app_server.prompts.doc_chat.recent_summary_max_chars must be an integer",
            range_message="app_server.prompts.doc_chat.recent_summary_max_chars must be >= 0",
        ),
    },
    "spec_ingest": {
        "max_chars": FieldSchema(
            path="app_server.prompts.spec_ingest.max_chars",
            kind="int",
            default=12000,
            min_value=1,
            parse_policy="fallback_default",
            type_message="app_server.prompts.spec_ingest.max_chars must be an integer",
            range_message="app_server.prompts.spec_ingest.max_chars must be >= 1",
        ),
        "message_max_chars": FieldSchema(
            path="app_server.prompts.spec_ingest.message_max_chars",
            kind="int",
            default=2000,
            min_value=1,
            parse_policy="fallback_default",
            type_message="app_server.prompts.spec_ingest.message_max_chars must be an integer",
            range_message="app_server.prompts.spec_ingest.message_max_chars must be >= 1",
        ),
        "spec_excerpt_max_chars": FieldSchema(
            path="app_server.prompts.spec_ingest.spec_excerpt_max_chars",
            kind="int",
            default=5000,
            min_value=0,
            parse_policy="fallback_default",
            type_message="app_server.prompts.spec_ingest.spec_excerpt_max_chars must be an integer",
            range_message="app_server.prompts.spec_ingest.spec_excerpt_max_chars must be >= 0",
        ),
    },
    "autorunner": {
        "max_chars": FieldSchema(
            path="app_server.prompts.autorunner.max_chars",
            kind="int",
            default=16000,
            min_value=1,
            parse_policy="fallback_default",
            type_message="app_server.prompts.autorunner.max_chars must be an integer",
            range_message="app_server.prompts.autorunner.max_chars must be >= 1",
        ),
        "message_max_chars": FieldSchema(
            path="app_server.prompts.autorunner.message_max_chars",
            kind="int",
            default=2000,
            min_value=1,
            parse_policy="fallback_default",
            type_message="app_server.prompts.autorunner.message_max_chars must be an integer",
            range_message="app_server.prompts.autorunner.message_max_chars must be >= 1",
        ),
        "todo_excerpt_max_chars": FieldSchema(
            path="app_server.prompts.autorunner.todo_excerpt_max_chars",
            kind="int",
            default=4000,
            min_value=0,
            parse_policy="fallback_default",
            type_message="app_server.prompts.autorunner.todo_excerpt_max_chars must be an integer",
            range_message="app_server.prompts.autorunner.todo_excerpt_max_chars must be >= 0",
        ),
        "prev_run_max_chars": FieldSchema(
            path="app_server.prompts.autorunner.prev_run_max_chars",
            kind="int",
            default=3000,
            min_value=0,
            parse_policy="fallback_default",
            type_message="app_server.prompts.autorunner.prev_run_max_chars must be an integer",
            range_message="app_server.prompts.autorunner.prev_run_max_chars must be >= 0",
        ),
    },
}

OPENCODE_FIELD_SCHEMAS: dict[str, FieldSchema] = {
    "server_scope": FieldSchema(
        path="opencode.server_scope",
        kind="choice",
        default="workspace",
        allowed_values=tuple(OPENCODE_SERVER_SCOPE_VALUES),
        type_message="opencode.server_scope must be a string or null",
        value_message="opencode.server_scope must be 'workspace' or 'global'",
    ),
    "session_stall_timeout_seconds": FieldSchema(
        path="opencode.session_stall_timeout_seconds",
        kind="number",
        default=300,
        allow_none=True,
        min_value=1,
        parse_policy="fallback_none",
        type_message="opencode.session_stall_timeout_seconds must be a number or null",
        range_message="opencode.session_stall_timeout_seconds must be > 0 or null",
    ),
    "max_text_chars": FieldSchema(
        path="opencode.max_text_chars",
        kind="int",
        default=20000,
        allow_none=True,
        min_value=1,
        parse_policy="fallback_none",
        type_message="opencode.max_text_chars must be an integer or null",
        range_message="opencode.max_text_chars must be > 0 or null",
    ),
    "max_handles": FieldSchema(
        path="opencode.max_handles",
        kind="int",
        default=4,
        allow_none=True,
        min_value=1,
        parse_policy="fallback_none",
        type_message="opencode.max_handles must be an integer or null",
        range_message="opencode.max_handles must be > 0 or null",
    ),
    "idle_ttl_seconds": FieldSchema(
        path="opencode.idle_ttl_seconds",
        kind="int",
        default=900,
        allow_none=True,
        min_value=1,
        parse_policy="fallback_none",
        type_message="opencode.idle_ttl_seconds must be an integer or null",
        range_message="opencode.idle_ttl_seconds must be > 0 or null",
    ),
}

UPDATE_FIELD_SCHEMAS: dict[str, FieldSchema] = {
    "backend": FieldSchema(
        path="update.backend",
        kind="choice",
        default="auto",
        allow_none=True,
        allowed_values=tuple(UPDATE_BACKEND_VALUES),
        parse_policy="fallback_default",
        type_message="update.backend must be a string",
        value_message="update.backend must be one of: auto, launchd, systemd-user",
    ),
    "skip_checks": FieldSchema(
        path="update.skip_checks",
        kind="bool",
        default=False,
        allow_none=True,
        type_message="update.skip_checks must be boolean or null",
    ),
}

UPDATE_LINUX_SERVICE_NAME_SCHEMAS: dict[str, FieldSchema] = {
    "hub": FieldSchema(
        path="update.linux_service_names.hub",
        kind="string",
        nonempty=True,
        type_message="update.linux_service_names.hub must be a non-empty string",
        value_message="update.linux_service_names.hub must be a non-empty string",
    ),
    "telegram": FieldSchema(
        path="update.linux_service_names.telegram",
        kind="string",
        nonempty=True,
        type_message="update.linux_service_names.telegram must be a non-empty string",
        value_message="update.linux_service_names.telegram must be a non-empty string",
    ),
    "discord": FieldSchema(
        path="update.linux_service_names.discord",
        kind="string",
        nonempty=True,
        type_message="update.linux_service_names.discord must be a non-empty string",
        value_message="update.linux_service_names.discord must be a non-empty string",
    ),
}

USAGE_FIELD_SCHEMAS: dict[str, FieldSchema] = {
    "cache_scope": FieldSchema(
        path="usage.cache_scope",
        kind="choice",
        default="global",
        allowed_values=tuple(USAGE_CACHE_SCOPE_VALUES),
        parse_policy="fallback_default",
        type_message="usage.cache_scope must be a string if provided",
        value_message="usage.cache_scope must be 'global' or 'repo'",
    ),
    "global_cache_root": FieldSchema(
        path="usage.global_cache_root",
        kind="path",
        default="~/.codex",
        allow_none=True,
        path_behavior=_ABSOLUTE_HOME_ALLOWED,
        type_message="usage.global_cache_root must be a string or null",
    ),
    "repo_cache_path": FieldSchema(
        path="usage.repo_cache_path",
        kind="path",
        default=".codex-autorunner/usage/usage_series_cache.json",
        allow_none=True,
        type_message="usage.repo_cache_path must be a string or null",
    ),
}

AGENT_FIELD_SCHEMAS: dict[str, FieldSchema] = {
    "backend": FieldSchema(
        path="agents.*.backend",
        kind="string",
        allow_none=True,
        nonempty=True,
        parse_policy="fallback_none",
        type_message="agents.{agent_id}.backend must be a non-empty string when provided",
        value_message="agents.{agent_id}.backend must be a non-empty string when provided",
    ),
    "binary": FieldSchema(
        path="agents.*.binary",
        kind="string",
        required=True,
        nonempty=True,
        type_message="agents.{agent_id}.binary is required",
        value_message="agents.{agent_id}.binary is required",
    ),
    "serve_command": FieldSchema(
        path="agents.*.serve_command",
        kind="command",
        allow_none=True,
        type_message="agents.{agent_id}.serve_command must be a list or str",
    ),
    "base_url": FieldSchema(
        path="agents.*.base_url",
        kind="string",
        allow_none=True,
        nonempty=True,
        parse_policy="fallback_none",
        type_message="agents.{agent_id}.base_url must be a non-empty string when provided",
        value_message="agents.{agent_id}.base_url must be a non-empty string when provided",
    ),
    "subagent_models": FieldSchema(
        path="agents.*.subagent_models",
        kind="mapping",
        allow_none=True,
        parse_policy="fallback_none",
        type_message="agents.{agent_id}.subagent_models must be a mapping when provided",
    ),
    "default_profile": FieldSchema(
        path="agents.*.default_profile",
        kind="string",
        allow_none=True,
        nonempty=True,
        lowercase=True,
        parse_policy="fallback_none",
        type_message="agents.{agent_id}.default_profile must be a non-empty string when provided",
        value_message="agents.{agent_id}.default_profile must be a non-empty string when provided",
    ),
    "profiles": FieldSchema(
        path="agents.*.profiles",
        kind="mapping",
        allow_none=True,
        type_message="agents.{agent_id}.profiles must be a mapping when provided",
    ),
}

AGENT_PROFILE_FIELD_SCHEMAS: dict[str, FieldSchema] = {
    "backend": FieldSchema(
        path="agents.*.profiles.*.backend",
        kind="string",
        allow_none=True,
        nonempty=True,
        parse_policy="fallback_none",
        type_message="agents.{agent_id}.profiles.{profile_id}.backend must be a non-empty string when provided",
        value_message="agents.{agent_id}.profiles.{profile_id}.backend must be a non-empty string when provided",
    ),
    "binary": FieldSchema(
        path="agents.*.profiles.*.binary",
        kind="string",
        allow_none=True,
        nonempty=True,
        parse_policy="fallback_none",
        type_message="agents.{agent_id}.profiles.{profile_id}.binary must be a non-empty string when provided",
        value_message="agents.{agent_id}.profiles.{profile_id}.binary must be a non-empty string when provided",
    ),
    "serve_command": FieldSchema(
        path="agents.*.profiles.*.serve_command",
        kind="command",
        allow_none=True,
        type_message="agents.{agent_id}.profiles.{profile_id}.serve_command must be a list or str",
    ),
    "base_url": FieldSchema(
        path="agents.*.profiles.*.base_url",
        kind="string",
        allow_none=True,
        nonempty=True,
        parse_policy="fallback_none",
        type_message="agents.{agent_id}.profiles.{profile_id}.base_url must be a non-empty string when provided",
        value_message="agents.{agent_id}.profiles.{profile_id}.base_url must be a non-empty string when provided",
    ),
    "subagent_models": FieldSchema(
        path="agents.*.profiles.*.subagent_models",
        kind="mapping",
        allow_none=True,
        parse_policy="fallback_none",
        type_message="agents.{agent_id}.profiles.{profile_id}.subagent_models must be a mapping when provided",
    ),
    "display_name": FieldSchema(
        path="agents.*.profiles.*.display_name",
        kind="string",
        allow_none=True,
        nonempty=True,
        parse_policy="fallback_none",
        type_message="agents.{agent_id}.profiles.{profile_id}.display_name must be a non-empty string when provided",
        value_message="agents.{agent_id}.profiles.{profile_id}.display_name must be a non-empty string when provided",
    ),
}

SHARED_SCHEMA_FIELD_PATHS = frozenset(
    path
    for path_set in (
        schema_paths(TICKET_FLOW_FIELD_SCHEMAS),
        schema_paths(APP_SERVER_FIELD_SCHEMAS),
        schema_paths(APP_SERVER_CLIENT_FIELD_SCHEMAS),
        schema_paths(APP_SERVER_OUTPUT_FIELD_SCHEMAS),
        frozenset(
            path
            for section in APP_SERVER_PROMPT_SECTION_SCHEMAS.values()
            for path in schema_paths(section)
        ),
        schema_paths(OPENCODE_FIELD_SCHEMAS),
        schema_paths(UPDATE_FIELD_SCHEMAS),
        schema_paths(UPDATE_LINUX_SERVICE_NAME_SCHEMAS),
        schema_paths(USAGE_FIELD_SCHEMAS),
        schema_paths(AGENT_FIELD_SCHEMAS),
        schema_paths(AGENT_PROFILE_FIELD_SCHEMAS),
    )
    for path in path_set
)

SHARED_CONFIG_PARSER_FIELD_PATHS = SHARED_SCHEMA_FIELD_PATHS - schema_paths(
    AGENT_FIELD_SCHEMAS, AGENT_PROFILE_FIELD_SCHEMAS
)
