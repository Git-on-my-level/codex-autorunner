"""Typed section parsers for configuration construction.

Ownership contract (TICKET-1040):
- ``config_validation.py`` owns rejection of invalid authored config values.
  Canonical load paths (``load_hub_config``, ``derive_repo_config``) validate
  before calling these parsers, so the parsers should never see truly invalid
  authored data in the canonical path.
- Parser-side coercion or default repair below is kept **only** for fields
  where direct callers historically passed unvalidated data.  These are
  documented inline as "compatibility repair" and should not expand.
- Parsers that have been tightened (e.g. output policy, server_scope) now
  raise ``ConfigError`` on invalid values rather than silently falling back.
"""

import dataclasses
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from .app_server_command import resolve_app_server_command
from .config_contract import APP_SERVER_OUTPUT_POLICIES, ConfigError
from .config_field_schema import (
    APP_SERVER_CLIENT_FIELD_SCHEMAS,
    APP_SERVER_FIELD_SCHEMAS,
    APP_SERVER_OUTPUT_FIELD_SCHEMAS,
    APP_SERVER_PROMPT_SECTION_SCHEMAS,
    OPENCODE_FIELD_SCHEMAS,
    SHARED_CONFIG_PARSER_FIELD_PATHS,
    TICKET_FLOW_FIELD_SCHEMAS,
    UPDATE_FIELD_SCHEMAS,
    UPDATE_LINUX_SERVICE_NAME_SCHEMAS,
    USAGE_FIELD_SCHEMAS,
    default_from_mapping,
    parse_schema_field,
)
from .config_layering import (
    PMA_DEFAULT_MAX_TEXT_CHARS,
    PMA_DEFAULT_TURN_IDLE_TIMEOUT_SECONDS,
    _default_update_linux_service_names,
)
from .config_types import (
    _DEFAULT_FLOW_RETENTION_DAYS,
    _DEFAULT_FLOW_SWEEP_INTERVAL_SECONDS,
    AppServerAutorunnerPromptConfig,
    AppServerClientConfig,
    AppServerConfig,
    AppServerDocChatPromptConfig,
    AppServerOutputConfig,
    AppServerPromptsConfig,
    AppServerSpecIngestPromptConfig,
    DestinationConfigSection,
    FlowRetentionConfig,
    NotificationsConfigSection,
    NotificationTargetSection,
    OpenCodeConfig,
    PmaConfig,
    SecurityConfigSection,
    StaticAssetsConfig,
    TemplateRepoConfig,
    TemplatesConfig,
    TicketFlowConfig,
    UsageConfig,
    VoiceConfigSection,
)
from .path_utils import ConfigPathError, resolve_config_path
from .report_retention import (
    DEFAULT_REPORT_MAX_HISTORY_FILES,
    DEFAULT_REPORT_MAX_TOTAL_BYTES,
)

_APP_SERVER_OUTPUT_POLICIES = set(APP_SERVER_OUTPUT_POLICIES)


def _parse_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def parse_flow_retention_config(raw: Optional[Dict[str, Any]]) -> FlowRetentionConfig:
    if not isinstance(raw, dict):
        return FlowRetentionConfig()
    retention_days = raw.get("retention_days")
    sweep_interval_seconds = raw.get("sweep_interval_seconds")
    return FlowRetentionConfig(
        retention_days=(
            int(retention_days)
            if retention_days is not None
            else _DEFAULT_FLOW_RETENTION_DAYS
        ),
        sweep_interval_seconds=(
            int(sweep_interval_seconds)
            if sweep_interval_seconds is not None
            else _DEFAULT_FLOW_SWEEP_INTERVAL_SECONDS
        ),
    )


def normalize_base_path(path: Optional[str]) -> str:
    if not path:
        return ""
    normalized = str(path).strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    normalized = normalized.rstrip("/")
    return normalized or ""


_normalize_base_path = normalize_base_path


def _parse_prompt_int(cfg: Dict[str, Any], defaults: Dict[str, Any], key: str) -> int:
    raw = cfg.get(key)
    if raw is None:
        raw = defaults.get(key, 0)
    return int(raw)


def _parse_security_config_section(raw: object) -> SecurityConfigSection:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, object] = dict(raw)
    redact_run_logs = raw.get("redact_run_logs")
    if isinstance(redact_run_logs, bool):
        normalized["redact_run_logs"] = redact_run_logs
    else:
        normalized.pop("redact_run_logs", None)

    redact_patterns = raw.get("redact_patterns")
    if isinstance(redact_patterns, list):
        normalized["redact_patterns"] = [
            value.strip()
            for value in redact_patterns
            if isinstance(value, str) and value.strip()
        ]
    else:
        normalized.pop("redact_patterns", None)
    return cast(SecurityConfigSection, normalized)


def _parse_notification_target_section(raw: object) -> NotificationTargetSection:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, object] = dict(raw)
    enabled = raw.get("enabled")
    if isinstance(enabled, bool):
        normalized["enabled"] = enabled
    else:
        normalized.pop("enabled", None)

    for key in ("webhook_url_env", "bot_token_env", "chat_id_env"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
        else:
            normalized.pop(key, None)
    return cast(NotificationTargetSection, normalized)


def _parse_notifications_config_section(raw: object) -> NotificationsConfigSection:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, object] = dict(raw)

    enabled = raw.get("enabled")
    if isinstance(enabled, bool):
        normalized["enabled"] = enabled
    elif isinstance(enabled, str) and enabled.strip().lower() == "auto":
        normalized["enabled"] = "auto"
    else:
        normalized.pop("enabled", None)

    events = raw.get("events")
    if isinstance(events, list):
        normalized["events"] = [
            value.strip()
            for value in events
            if isinstance(value, str) and value.strip()
        ]
    else:
        normalized.pop("events", None)

    tui_idle_seconds = raw.get("tui_idle_seconds")
    if isinstance(tui_idle_seconds, (int, float)) and int(tui_idle_seconds) > 0:
        normalized["tui_idle_seconds"] = int(tui_idle_seconds)
    else:
        normalized.pop("tui_idle_seconds", None)

    timeout_seconds = raw.get("timeout_seconds")
    if isinstance(timeout_seconds, (int, float)) and float(timeout_seconds) > 0:
        normalized["timeout_seconds"] = float(timeout_seconds)
    else:
        normalized.pop("timeout_seconds", None)

    discord = _parse_notification_target_section(raw.get("discord"))
    if discord:
        normalized["discord"] = discord
    else:
        normalized.pop("discord", None)

    telegram = _parse_notification_target_section(raw.get("telegram"))
    if telegram:
        normalized["telegram"] = telegram
    else:
        normalized.pop("telegram", None)
    return cast(NotificationsConfigSection, normalized)


def _parse_voice_config_section(raw: object) -> VoiceConfigSection:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, object] = dict(raw)
    bool_keys = ("enabled", "warn_on_remote_api")
    for key in bool_keys:
        value = raw.get(key)
        if isinstance(value, bool):
            normalized[key] = value
        else:
            normalized.pop(key, None)

    for key in ("provider", "latency_mode"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
        else:
            normalized.pop(key, None)

    for key in ("chunk_ms", "sample_rate"):
        value = raw.get(key)
        if isinstance(value, (int, float)):
            normalized[key] = int(value)
        else:
            normalized.pop(key, None)

    push_to_talk = raw.get("push_to_talk")
    if isinstance(push_to_talk, dict):
        normalized["push_to_talk"] = {
            str(key): value
            for key, value in push_to_talk.items()
            if isinstance(key, str)
        }
    else:
        normalized.pop("push_to_talk", None)

    providers_raw = raw.get("providers")
    if isinstance(providers_raw, dict):
        providers: dict[str, dict[str, object]] = {}
        for provider_name, provider_cfg in providers_raw.items():
            if not isinstance(provider_name, str) or not provider_name.strip():
                continue
            if not isinstance(provider_cfg, dict):
                continue
            providers[provider_name.strip()] = {
                str(key): value
                for key, value in provider_cfg.items()
                if isinstance(key, str)
            }
        normalized["providers"] = providers
    else:
        normalized.pop("providers", None)
    return cast(VoiceConfigSection, normalized)


_LOCAL_DESTINATION_DICT: DestinationConfigSection = {"kind": "local"}


def _parse_destination_config_section(raw: object) -> DestinationConfigSection:
    if not isinstance(raw, dict):
        return cast(DestinationConfigSection, dict(_LOCAL_DESTINATION_DICT))
    return cast(DestinationConfigSection, dict(raw))


def _parse_ticket_flow_config(
    cfg: Optional[Dict[str, Any]],
    defaults: Optional[Dict[str, Any]],
) -> TicketFlowConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    approval_mode = parse_schema_field(
        cfg.get(
            "approval_mode",
            default_from_mapping(
                defaults, "approval_mode", TICKET_FLOW_FIELD_SCHEMAS["approval_mode"]
            ),
        ),
        TICKET_FLOW_FIELD_SCHEMAS["approval_mode"],
    )
    default_approval_decision = parse_schema_field(
        cfg.get(
            "default_approval_decision",
            default_from_mapping(
                defaults,
                "default_approval_decision",
                TICKET_FLOW_FIELD_SCHEMAS["default_approval_decision"],
            ),
        ),
        TICKET_FLOW_FIELD_SCHEMAS["default_approval_decision"],
    )
    include_previous_ticket_context = parse_schema_field(
        cfg.get(
            "include_previous_ticket_context",
            default_from_mapping(
                defaults,
                "include_previous_ticket_context",
                TICKET_FLOW_FIELD_SCHEMAS["include_previous_ticket_context"],
            ),
        ),
        TICKET_FLOW_FIELD_SCHEMAS["include_previous_ticket_context"],
    )
    auto_resume = parse_schema_field(
        cfg.get(
            "auto_resume",
            default_from_mapping(
                defaults,
                "auto_resume",
                TICKET_FLOW_FIELD_SCHEMAS["auto_resume"],
            ),
        ),
        TICKET_FLOW_FIELD_SCHEMAS["auto_resume"],
    )
    max_total_turns = parse_schema_field(
        cfg.get(
            "max_total_turns",
            default_from_mapping(
                defaults,
                "max_total_turns",
                TICKET_FLOW_FIELD_SCHEMAS["max_total_turns"],
            ),
        ),
        TICKET_FLOW_FIELD_SCHEMAS["max_total_turns"],
    )
    return TicketFlowConfig(
        approval_mode=approval_mode,
        default_approval_decision=default_approval_decision,
        include_previous_ticket_context=include_previous_ticket_context,
        auto_resume=auto_resume,
        max_total_turns=max_total_turns,
    )


# Compatibility repair: _parse_update_backend returns "auto" for missing,
# empty, or unrecognised backend values.  The validator rejects invalid
# authored values in the canonical path; this fallback covers direct callers.
def _parse_update_backend(update_cfg: Dict[str, Any]) -> str:
    raw_backend = update_cfg.get("backend")
    if raw_backend is None:
        raw_backend = default_from_mapping(
            None, "backend", UPDATE_FIELD_SCHEMAS["backend"]
        )
    return cast(
        str,
        parse_schema_field(
            raw_backend,
            UPDATE_FIELD_SCHEMAS["backend"],
            default_value=default_from_mapping(
                None, "backend", UPDATE_FIELD_SCHEMAS["backend"]
            ),
        ),
    )


def _parse_update_skip_checks(update_cfg: Dict[str, Any]) -> bool:
    return bool(
        parse_schema_field(
            update_cfg.get("skip_checks"),
            UPDATE_FIELD_SCHEMAS["skip_checks"],
            default_value=default_from_mapping(
                None, "skip_checks", UPDATE_FIELD_SCHEMAS["skip_checks"]
            ),
        )
    )


def _parse_update_linux_service_names(update_cfg: Dict[str, Any]) -> Dict[str, str]:
    merged = dict(_default_update_linux_service_names())
    raw = update_cfg.get("linux_service_names")
    if not isinstance(raw, dict):
        return merged
    for key, schema in UPDATE_LINUX_SERVICE_NAME_SCHEMAS.items():
        if key not in raw:
            continue
        parsed = parse_schema_field(
            raw.get(key),
            schema,
            default_value=merged[key],
        )
        if isinstance(parsed, str) and parsed.strip():
            merged[key] = parsed
    return merged


def _parse_app_server_prompts_config(
    cfg: Optional[Dict[str, Any]],
    defaults: Optional[Dict[str, Any]],
) -> AppServerPromptsConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    doc_chat_cfg = cfg.get("doc_chat")
    doc_chat_defaults = defaults.get("doc_chat")
    doc_chat_cfg = doc_chat_cfg if isinstance(doc_chat_cfg, dict) else {}
    doc_chat_defaults = doc_chat_defaults if isinstance(doc_chat_defaults, dict) else {}
    spec_ingest_cfg = cfg.get("spec_ingest")
    spec_ingest_defaults = defaults.get("spec_ingest")
    spec_ingest_cfg = spec_ingest_cfg if isinstance(spec_ingest_cfg, dict) else {}
    spec_ingest_defaults = (
        spec_ingest_defaults if isinstance(spec_ingest_defaults, dict) else {}
    )
    autorunner_cfg = cfg.get("autorunner")
    autorunner_defaults = defaults.get("autorunner")
    autorunner_cfg = autorunner_cfg if isinstance(autorunner_cfg, dict) else {}
    autorunner_defaults = (
        autorunner_defaults if isinstance(autorunner_defaults, dict) else {}
    )

    def _prompt_value(
        section_name: str,
        key: str,
        section_cfg: Dict[str, Any],
        section_defaults: Dict[str, Any],
    ) -> int:
        schema = APP_SERVER_PROMPT_SECTION_SCHEMAS[section_name][key]
        return cast(
            int,
            parse_schema_field(
                section_cfg.get(
                    key,
                    default_from_mapping(section_defaults, key, schema),
                ),
                schema,
            ),
        )

    return AppServerPromptsConfig(
        doc_chat=AppServerDocChatPromptConfig(
            max_chars=_prompt_value(
                "doc_chat", "max_chars", doc_chat_cfg, doc_chat_defaults
            ),
            message_max_chars=_prompt_value(
                "doc_chat",
                "message_max_chars",
                doc_chat_cfg,
                doc_chat_defaults,
            ),
            target_excerpt_max_chars=_prompt_value(
                "doc_chat",
                "target_excerpt_max_chars",
                doc_chat_cfg,
                doc_chat_defaults,
            ),
            recent_summary_max_chars=_prompt_value(
                "doc_chat",
                "recent_summary_max_chars",
                doc_chat_cfg,
                doc_chat_defaults,
            ),
        ),
        spec_ingest=AppServerSpecIngestPromptConfig(
            max_chars=_prompt_value(
                "spec_ingest",
                "max_chars",
                spec_ingest_cfg,
                spec_ingest_defaults,
            ),
            message_max_chars=_prompt_value(
                "spec_ingest",
                "message_max_chars",
                spec_ingest_cfg,
                spec_ingest_defaults,
            ),
            spec_excerpt_max_chars=_prompt_value(
                "spec_ingest",
                "spec_excerpt_max_chars",
                spec_ingest_cfg,
                spec_ingest_defaults,
            ),
        ),
        autorunner=AppServerAutorunnerPromptConfig(
            max_chars=_prompt_value(
                "autorunner", "max_chars", autorunner_cfg, autorunner_defaults
            ),
            message_max_chars=_prompt_value(
                "autorunner",
                "message_max_chars",
                autorunner_cfg,
                autorunner_defaults,
            ),
            todo_excerpt_max_chars=_prompt_value(
                "autorunner",
                "todo_excerpt_max_chars",
                autorunner_cfg,
                autorunner_defaults,
            ),
            prev_run_max_chars=_prompt_value(
                "autorunner",
                "prev_run_max_chars",
                autorunner_cfg,
                autorunner_defaults,
            ),
        ),
    )


def _parse_app_server_output_config(
    cfg: Optional[Dict[str, Any]],
    defaults: Optional[Dict[str, Any]],
) -> AppServerOutputConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    policy = cast(
        str,
        parse_schema_field(
            cfg.get(
                "policy",
                default_from_mapping(
                    defaults,
                    "policy",
                    APP_SERVER_OUTPUT_FIELD_SCHEMAS["policy"],
                ),
            ),
            APP_SERVER_OUTPUT_FIELD_SCHEMAS["policy"],
        ),
    )
    if policy not in _APP_SERVER_OUTPUT_POLICIES:
        allowed = ", ".join(sorted(_APP_SERVER_OUTPUT_POLICIES))
        raise ConfigError(f"app_server.output.policy must be one of: {allowed}")
    return AppServerOutputConfig(policy=policy)


def _parse_app_server_config(
    cfg: Optional[Dict[str, Any]],
    root: Path,
    defaults: Dict[str, Any],
) -> AppServerConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    raw_command = cfg.get("command", dataclasses.MISSING)
    if raw_command is dataclasses.MISSING:
        command = resolve_app_server_command(
            default_from_mapping(
                defaults, "command", APP_SERVER_FIELD_SCHEMAS["command"]
            ),
            env=os.environ,
        )
    else:
        command = resolve_app_server_command(
            raw_command,
            env=os.environ,
            fallback=(),
        )
    state_root = cast(
        Path,
        parse_schema_field(
            cfg.get(
                "state_root",
                default_from_mapping(
                    defaults, "state_root", APP_SERVER_FIELD_SCHEMAS["state_root"]
                ),
            ),
            APP_SERVER_FIELD_SCHEMAS["state_root"],
            root=root,
        ),
    )
    auto_restart = cast(
        Optional[bool],
        parse_schema_field(
            cfg.get(
                "auto_restart",
                default_from_mapping(
                    defaults,
                    "auto_restart",
                    APP_SERVER_FIELD_SCHEMAS["auto_restart"],
                ),
            ),
            APP_SERVER_FIELD_SCHEMAS["auto_restart"],
        ),
    )
    max_handles = cast(
        Optional[int],
        parse_schema_field(
            cfg.get(
                "max_handles",
                default_from_mapping(
                    defaults,
                    "max_handles",
                    APP_SERVER_FIELD_SCHEMAS["max_handles"],
                ),
            ),
            APP_SERVER_FIELD_SCHEMAS["max_handles"],
        ),
    )
    idle_ttl_seconds = cast(
        Optional[int],
        parse_schema_field(
            cfg.get(
                "idle_ttl_seconds",
                default_from_mapping(
                    defaults,
                    "idle_ttl_seconds",
                    APP_SERVER_FIELD_SCHEMAS["idle_ttl_seconds"],
                ),
            ),
            APP_SERVER_FIELD_SCHEMAS["idle_ttl_seconds"],
        ),
    )
    turn_timeout_seconds = cast(
        Optional[float],
        parse_schema_field(
            cfg.get(
                "turn_timeout_seconds",
                default_from_mapping(
                    defaults,
                    "turn_timeout_seconds",
                    APP_SERVER_FIELD_SCHEMAS["turn_timeout_seconds"],
                ),
            ),
            APP_SERVER_FIELD_SCHEMAS["turn_timeout_seconds"],
        ),
    )
    turn_stall_timeout_seconds = cast(
        Optional[float],
        parse_schema_field(
            cfg.get(
                "turn_stall_timeout_seconds",
                default_from_mapping(
                    defaults,
                    "turn_stall_timeout_seconds",
                    APP_SERVER_FIELD_SCHEMAS["turn_stall_timeout_seconds"],
                ),
            ),
            APP_SERVER_FIELD_SCHEMAS["turn_stall_timeout_seconds"],
        ),
    )
    turn_stall_poll_interval_seconds = cast(
        Optional[float],
        parse_schema_field(
            cfg.get(
                "turn_stall_poll_interval_seconds",
                default_from_mapping(
                    defaults,
                    "turn_stall_poll_interval_seconds",
                    APP_SERVER_FIELD_SCHEMAS["turn_stall_poll_interval_seconds"],
                ),
            ),
            APP_SERVER_FIELD_SCHEMAS["turn_stall_poll_interval_seconds"],
        ),
    )
    turn_stall_recovery_min_interval_seconds = cast(
        Optional[float],
        parse_schema_field(
            cfg.get(
                "turn_stall_recovery_min_interval_seconds",
                default_from_mapping(
                    defaults,
                    "turn_stall_recovery_min_interval_seconds",
                    APP_SERVER_FIELD_SCHEMAS[
                        "turn_stall_recovery_min_interval_seconds"
                    ],
                ),
            ),
            APP_SERVER_FIELD_SCHEMAS["turn_stall_recovery_min_interval_seconds"],
        ),
    )
    turn_stall_max_recovery_attempts = cast(
        Optional[int],
        parse_schema_field(
            cfg.get(
                "turn_stall_max_recovery_attempts",
                default_from_mapping(
                    defaults,
                    "turn_stall_max_recovery_attempts",
                    APP_SERVER_FIELD_SCHEMAS["turn_stall_max_recovery_attempts"],
                ),
            ),
            APP_SERVER_FIELD_SCHEMAS["turn_stall_max_recovery_attempts"],
        ),
    )
    request_timeout = cast(
        Optional[float],
        parse_schema_field(
            cfg.get(
                "request_timeout",
                default_from_mapping(
                    defaults,
                    "request_timeout",
                    APP_SERVER_FIELD_SCHEMAS["request_timeout"],
                ),
            ),
            APP_SERVER_FIELD_SCHEMAS["request_timeout"],
        ),
    )
    client_defaults = defaults.get("client")
    client_defaults = client_defaults if isinstance(client_defaults, dict) else {}
    client_cfg_raw = cfg.get("client")
    client_cfg = client_cfg_raw if isinstance(client_cfg_raw, dict) else {}

    def _client_value(key: str) -> Any:
        schema = APP_SERVER_CLIENT_FIELD_SCHEMAS[key]
        return parse_schema_field(
            client_cfg.get(key, default_from_mapping(client_defaults, key, schema)),
            schema,
        )

    output_defaults = defaults.get("output")
    output_cfg_raw = cfg.get("output")
    output = _parse_app_server_output_config(output_cfg_raw, output_defaults)
    prompt_defaults = defaults.get("prompts")
    prompts = _parse_app_server_prompts_config(cfg.get("prompts"), prompt_defaults)
    return AppServerConfig(
        command=command,
        state_root=state_root,
        auto_restart=auto_restart,
        max_handles=max_handles,
        idle_ttl_seconds=idle_ttl_seconds,
        turn_timeout_seconds=turn_timeout_seconds,
        turn_stall_timeout_seconds=turn_stall_timeout_seconds,
        turn_stall_poll_interval_seconds=turn_stall_poll_interval_seconds,
        turn_stall_recovery_min_interval_seconds=turn_stall_recovery_min_interval_seconds,
        turn_stall_max_recovery_attempts=turn_stall_max_recovery_attempts,
        request_timeout=request_timeout,
        client=AppServerClientConfig(
            max_message_bytes=cast(int, _client_value("max_message_bytes")),
            oversize_preview_bytes=cast(int, _client_value("oversize_preview_bytes")),
            max_oversize_drain_bytes=cast(
                int, _client_value("max_oversize_drain_bytes")
            ),
            restart_backoff_initial_seconds=cast(
                float, _client_value("restart_backoff_initial_seconds")
            ),
            restart_backoff_max_seconds=cast(
                float, _client_value("restart_backoff_max_seconds")
            ),
            restart_backoff_jitter_ratio=cast(
                float, _client_value("restart_backoff_jitter_ratio")
            ),
        ),
        output=output,
        prompts=prompts,
    )


def _parse_opencode_config(
    cfg: Optional[Dict[str, Any]],
    _root: Path,
    defaults: Optional[Dict[str, Any]],
) -> OpenCodeConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    server_scope = cast(
        str,
        parse_schema_field(
            cfg.get(
                "server_scope",
                default_from_mapping(
                    defaults,
                    "server_scope",
                    OPENCODE_FIELD_SCHEMAS["server_scope"],
                ),
            ),
            OPENCODE_FIELD_SCHEMAS["server_scope"],
        ),
    )
    stall_timeout_seconds = cast(
        Optional[float],
        parse_schema_field(
            cfg.get(
                "session_stall_timeout_seconds",
                default_from_mapping(
                    defaults,
                    "session_stall_timeout_seconds",
                    OPENCODE_FIELD_SCHEMAS["session_stall_timeout_seconds"],
                ),
            ),
            OPENCODE_FIELD_SCHEMAS["session_stall_timeout_seconds"],
        ),
    )
    max_text_chars = cast(
        Optional[int],
        parse_schema_field(
            cfg.get(
                "max_text_chars",
                default_from_mapping(
                    defaults,
                    "max_text_chars",
                    OPENCODE_FIELD_SCHEMAS["max_text_chars"],
                ),
            ),
            OPENCODE_FIELD_SCHEMAS["max_text_chars"],
        ),
    )
    max_handles = cast(
        Optional[int],
        parse_schema_field(
            cfg.get(
                "max_handles",
                default_from_mapping(
                    defaults,
                    "max_handles",
                    OPENCODE_FIELD_SCHEMAS["max_handles"],
                ),
            ),
            OPENCODE_FIELD_SCHEMAS["max_handles"],
        ),
    )
    idle_ttl_seconds = cast(
        Optional[int],
        parse_schema_field(
            cfg.get(
                "idle_ttl_seconds",
                default_from_mapping(
                    defaults,
                    "idle_ttl_seconds",
                    OPENCODE_FIELD_SCHEMAS["idle_ttl_seconds"],
                ),
            ),
            OPENCODE_FIELD_SCHEMAS["idle_ttl_seconds"],
        ),
    )
    return OpenCodeConfig(
        server_scope=server_scope,
        session_stall_timeout_seconds=stall_timeout_seconds,
        max_text_chars=max_text_chars,
        max_handles=max_handles,
        idle_ttl_seconds=idle_ttl_seconds,
    )


def _parse_pma_config(
    cfg: Optional[Dict[str, Any]],
    _root: Path,
    defaults: Optional[Dict[str, Any]],
) -> PmaConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    enabled = bool(cfg.get("enabled", defaults.get("enabled", True)))
    default_agent = str(
        cfg.get("default_agent", defaults.get("default_agent", "codex"))
    )
    profile_raw = cfg.get("profile", defaults.get("profile"))
    profile = str(profile_raw).strip().lower() or None if profile_raw else None
    model_raw = cfg.get("model", defaults.get("model"))
    model = str(model_raw).strip() or None if model_raw else None
    reasoning_raw = cfg.get("reasoning", defaults.get("reasoning"))
    reasoning = str(reasoning_raw).strip() or None if reasoning_raw else None
    managed_thread_terminal_followup_default = bool(
        cfg.get(
            "managed_thread_terminal_followup_default",
            defaults.get("managed_thread_terminal_followup_default", True),
        )
    )
    max_upload_bytes_raw = cfg.get(
        "max_upload_bytes", defaults.get("max_upload_bytes", 10_000_000)
    )
    # Compatibility repair: max_upload_bytes is coerced to the default on
    # non-int or non-positive input.  The validator rejects bad authored
    # values in the canonical path; this fallback covers direct callers.
    try:
        max_upload_bytes = int(max_upload_bytes_raw)
    except (ValueError, TypeError):
        max_upload_bytes = 10_000_000
    if max_upload_bytes <= 0:
        max_upload_bytes = 10_000_000

    # Compatibility repair: _parse_positive_int and _parse_nonnegative_int
    # silently fall back to defaults on non-int or out-of-range input.
    # The validator rejects bad authored values in the canonical path.
    def _parse_positive_int(key: str, fallback: int) -> int:
        raw = cfg.get(key, defaults.get(key, fallback))
        if isinstance(raw, bool):
            return fallback
        try:
            value = int(raw)
        except (ValueError, TypeError):
            return fallback
        return value if value > 0 else fallback

    idle_timeout_raw = cfg.get(
        "turn_idle_timeout_seconds",
        cfg.get(
            "turn_timeout_seconds",
            defaults.get(
                "turn_idle_timeout_seconds",
                defaults.get(
                    "turn_timeout_seconds",
                    PMA_DEFAULT_TURN_IDLE_TIMEOUT_SECONDS,
                ),
            ),
        ),
    )
    if isinstance(idle_timeout_raw, bool):
        turn_idle_timeout_seconds = PMA_DEFAULT_TURN_IDLE_TIMEOUT_SECONDS
    else:
        try:
            parsed_idle_timeout = int(idle_timeout_raw)
        except (ValueError, TypeError):
            parsed_idle_timeout = PMA_DEFAULT_TURN_IDLE_TIMEOUT_SECONDS
        turn_idle_timeout_seconds = (
            parsed_idle_timeout
            if parsed_idle_timeout > 0
            else PMA_DEFAULT_TURN_IDLE_TIMEOUT_SECONDS
        )
    max_repos = _parse_positive_int("max_repos", 25)
    max_messages = _parse_positive_int("max_messages", 10)
    max_text_chars = _parse_positive_int("max_text_chars", PMA_DEFAULT_MAX_TEXT_CHARS)
    docs_max_chars = _parse_positive_int("docs_max_chars", 12_000)
    active_context_max_lines = _parse_positive_int("active_context_max_lines", 200)
    context_log_tail_lines = _parse_positive_int("context_log_tail_lines", 120)
    freshness_stale_threshold_seconds = _parse_positive_int(
        "freshness_stale_threshold_seconds", 1800
    )
    dispatch_interception_enabled = bool(
        cfg.get(
            "dispatch_interception_enabled",
            defaults.get("dispatch_interception_enabled", False),
        )
    )
    reactive_enabled = bool(
        cfg.get("reactive_enabled", defaults.get("reactive_enabled", True))
    )
    reactive_event_types_raw = cfg.get(
        "reactive_event_types", defaults.get("reactive_event_types", [])
    )
    if isinstance(reactive_event_types_raw, list):
        reactive_event_types = [
            str(value).strip()
            for value in reactive_event_types_raw
            if str(value).strip()
        ]
    else:
        reactive_event_types = []
    reactive_debounce_seconds_raw = cfg.get(
        "reactive_debounce_seconds", defaults.get("reactive_debounce_seconds", 300)
    )
    try:
        reactive_debounce_seconds = int(reactive_debounce_seconds_raw)
    except (ValueError, TypeError):
        reactive_debounce_seconds = 300
    if reactive_debounce_seconds < 0:
        reactive_debounce_seconds = 0
    reactive_origin_blocklist_raw = cfg.get(
        "reactive_origin_blocklist",
        defaults.get("reactive_origin_blocklist", ["pma"]),
    )
    if isinstance(reactive_origin_blocklist_raw, list):
        reactive_origin_blocklist = [
            str(value).strip()
            for value in reactive_origin_blocklist_raw
            if str(value).strip()
        ]
    else:
        reactive_origin_blocklist = []
    cleanup_require_archive = bool(
        cfg.get(
            "cleanup_require_archive", defaults.get("cleanup_require_archive", True)
        )
    )
    cleanup_auto_delete_orphans = bool(
        cfg.get(
            "cleanup_auto_delete_orphans",
            defaults.get("cleanup_auto_delete_orphans", False),
        )
    )
    worktree_archive_profile = (
        str(
            cfg.get(
                "worktree_archive_profile",
                defaults.get("worktree_archive_profile", "portable"),
            )
        )
        .strip()
        .lower()
    )
    if worktree_archive_profile not in {"portable", "full"}:
        worktree_archive_profile = "portable"

    def _parse_nonnegative_int(name: str, fallback: int) -> int:
        raw = cfg.get(name, defaults.get(name, fallback))
        if isinstance(raw, bool):
            return fallback
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = fallback
        return max(0, value)

    filebox_inbox_max_age_days = _parse_nonnegative_int("filebox_inbox_max_age_days", 7)
    filebox_outbox_max_age_days = _parse_nonnegative_int(
        "filebox_outbox_max_age_days", 7
    )
    report_max_history_files = _parse_nonnegative_int(
        "report_max_history_files", DEFAULT_REPORT_MAX_HISTORY_FILES
    )
    report_max_total_bytes = _parse_nonnegative_int(
        "report_max_total_bytes", DEFAULT_REPORT_MAX_TOTAL_BYTES
    )
    app_server_workspace_max_age_days = _parse_nonnegative_int(
        "app_server_workspace_max_age_days", 7
    )
    inbox_auto_dismiss_grace_seconds = _parse_nonnegative_int(
        "inbox_auto_dismiss_grace_seconds", 3600
    )
    worktree_archive_max_snapshots_per_repo = _parse_nonnegative_int(
        "worktree_archive_max_snapshots_per_repo", 10
    )
    worktree_archive_max_age_days = _parse_nonnegative_int(
        "worktree_archive_max_age_days", 30
    )
    worktree_archive_max_total_bytes = _parse_nonnegative_int(
        "worktree_archive_max_total_bytes", 1_000_000_000
    )
    run_archive_max_entries = _parse_nonnegative_int("run_archive_max_entries", 200)
    run_archive_max_age_days = _parse_nonnegative_int("run_archive_max_age_days", 30)
    run_archive_max_total_bytes = _parse_nonnegative_int(
        "run_archive_max_total_bytes", 1_000_000_000
    )
    orchestration_compaction_max_hot_rows = _parse_nonnegative_int(
        "orchestration_compaction_max_hot_rows",
        16,
    )
    if orchestration_compaction_max_hot_rows <= 0:
        orchestration_compaction_max_hot_rows = 16
    orchestration_hot_history_retention_days = _parse_nonnegative_int(
        "orchestration_hot_history_retention_days",
        30,
    )
    orchestration_cold_trace_retention_days = _parse_nonnegative_int(
        "orchestration_cold_trace_retention_days",
        90,
    )
    return PmaConfig(
        enabled=enabled,
        default_agent=default_agent,
        profile=profile,
        model=model,
        reasoning=reasoning,
        turn_idle_timeout_seconds=turn_idle_timeout_seconds,
        managed_thread_terminal_followup_default=managed_thread_terminal_followup_default,
        max_upload_bytes=max_upload_bytes,
        max_repos=max_repos,
        max_messages=max_messages,
        max_text_chars=max_text_chars,
        docs_max_chars=docs_max_chars,
        active_context_max_lines=active_context_max_lines,
        context_log_tail_lines=context_log_tail_lines,
        freshness_stale_threshold_seconds=freshness_stale_threshold_seconds,
        dispatch_interception_enabled=dispatch_interception_enabled,
        reactive_enabled=reactive_enabled,
        reactive_event_types=reactive_event_types,
        reactive_debounce_seconds=reactive_debounce_seconds,
        reactive_origin_blocklist=reactive_origin_blocklist,
        filebox_inbox_max_age_days=filebox_inbox_max_age_days,
        filebox_outbox_max_age_days=filebox_outbox_max_age_days,
        report_max_history_files=report_max_history_files,
        report_max_total_bytes=report_max_total_bytes,
        app_server_workspace_max_age_days=app_server_workspace_max_age_days,
        inbox_auto_dismiss_grace_seconds=inbox_auto_dismiss_grace_seconds,
        cleanup_require_archive=cleanup_require_archive,
        cleanup_auto_delete_orphans=cleanup_auto_delete_orphans,
        worktree_archive_profile=worktree_archive_profile,
        worktree_archive_max_snapshots_per_repo=worktree_archive_max_snapshots_per_repo,
        worktree_archive_max_age_days=worktree_archive_max_age_days,
        worktree_archive_max_total_bytes=worktree_archive_max_total_bytes,
        run_archive_max_entries=run_archive_max_entries,
        run_archive_max_age_days=run_archive_max_age_days,
        run_archive_max_total_bytes=run_archive_max_total_bytes,
        orchestration_compaction_max_hot_rows=orchestration_compaction_max_hot_rows,
        orchestration_hot_history_retention_days=orchestration_hot_history_retention_days,
        orchestration_cold_trace_retention_days=orchestration_cold_trace_retention_days,
    )


def _parse_usage_config(
    cfg: Optional[Dict[str, Any]],
    root: Path,
    defaults: Optional[Dict[str, Any]],
) -> UsageConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    cache_scope = cast(
        str,
        parse_schema_field(
            cfg.get(
                "cache_scope",
                default_from_mapping(
                    defaults, "cache_scope", USAGE_FIELD_SCHEMAS["cache_scope"]
                ),
            ),
            USAGE_FIELD_SCHEMAS["cache_scope"],
        ),
    )
    global_cache_default = defaults.get("global_cache_root")
    if global_cache_default is None:
        global_cache_default = os.environ.get("CODEX_HOME", "~/.codex")
    global_cache_raw = cfg.get("global_cache_root", global_cache_default)
    if global_cache_raw is None:
        global_cache_raw = global_cache_default
    global_cache_root = cast(
        Path,
        parse_schema_field(
            global_cache_raw,
            USAGE_FIELD_SCHEMAS["global_cache_root"],
            root=root,
        ),
    )
    repo_cache_raw = cfg.get(
        "repo_cache_path",
        default_from_mapping(
            defaults,
            "repo_cache_path",
            USAGE_FIELD_SCHEMAS["repo_cache_path"],
        ),
    )
    if repo_cache_raw is None:
        repo_cache_raw = default_from_mapping(
            defaults,
            "repo_cache_path",
            USAGE_FIELD_SCHEMAS["repo_cache_path"],
        )
    repo_cache_path = cast(
        Path,
        parse_schema_field(
            repo_cache_raw,
            USAGE_FIELD_SCHEMAS["repo_cache_path"],
            root=root,
        ),
    )
    return UsageConfig(
        cache_scope=cache_scope,
        global_cache_root=global_cache_root,
        repo_cache_path=repo_cache_path,
    )


def _parse_templates_config(
    cfg: Optional[Dict[str, Any]],
    defaults: Optional[Dict[str, Any]],
) -> TemplatesConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    enabled_raw = cfg.get("enabled", defaults.get("enabled", True))
    if "enabled" in cfg and not isinstance(enabled_raw, bool):
        raise ConfigError("templates.enabled must be boolean")
    enabled = bool(enabled_raw)
    repos_raw = cfg.get("repos", defaults.get("repos", []))
    if repos_raw is None:
        repos_raw = []
    if not isinstance(repos_raw, list):
        raise ConfigError("templates.repos must be a list")
    repos: List[TemplateRepoConfig] = []
    seen_ids: set[str] = set()
    for idx, repo in enumerate(repos_raw):
        if not isinstance(repo, dict):
            raise ConfigError(f"templates.repos[{idx}] must be a mapping")
        repo_id = repo.get("id")
        if not isinstance(repo_id, str) or not repo_id.strip():
            raise ConfigError(f"templates.repos[{idx}].id must be a non-empty string")
        repo_id = repo_id.strip()
        if repo_id in seen_ids:
            raise ConfigError(f"templates.repos[{idx}].id must be unique")
        seen_ids.add(repo_id)
        url = repo.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ConfigError(f"templates.repos[{idx}].url must be a non-empty string")
        trusted = repo.get("trusted", False)
        if "trusted" in repo and not isinstance(trusted, bool):
            raise ConfigError(f"templates.repos[{idx}].trusted must be boolean")
        default_ref = repo.get("default_ref", "main")
        if not isinstance(default_ref, str) or not default_ref.strip():
            raise ConfigError(
                f"templates.repos[{idx}].default_ref must be a non-empty string"
            )
        repos.append(
            TemplateRepoConfig(
                id=repo_id,
                url=url.strip(),
                trusted=bool(trusted),
                default_ref=default_ref.strip(),
            )
        )
    return TemplatesConfig(enabled=enabled, repos=repos)


def _parse_static_assets_config(
    cfg: Optional[Dict[str, Any]],
    root: Path,
    defaults: Dict[str, Any],
) -> StaticAssetsConfig:
    if not isinstance(cfg, dict):
        cfg = defaults
    cache_root_raw = cfg.get("cache_root", defaults.get("cache_root"))
    if cache_root_raw is None:
        raise ConfigError("static_assets.cache_root is required")
    try:
        cache_root = resolve_config_path(
            cache_root_raw,
            root,
            allow_home=True,
            scope="static_assets.cache_root",
        )
    except ConfigPathError as exc:
        raise ConfigError(str(exc)) from exc
    max_cache_entries = int(
        cfg.get("max_cache_entries", defaults.get("max_cache_entries", 0))
    )
    max_cache_age_days_raw = cfg.get(
        "max_cache_age_days", defaults.get("max_cache_age_days")
    )
    max_cache_age_days = _parse_optional_int(max_cache_age_days_raw)
    return StaticAssetsConfig(
        cache_root=cache_root,
        max_cache_entries=max_cache_entries,
        max_cache_age_days=max_cache_age_days,
    )
