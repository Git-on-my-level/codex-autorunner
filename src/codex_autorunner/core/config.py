import dataclasses
import logging
import os
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    cast,
)

import yaml

from ..housekeeping import (
    HousekeepingConfig,
    HousekeepingRule,
    parse_housekeeping_config,
)
from ..manifest import ManifestError, load_manifest
from .agent_config import (  # noqa: F401 — backward-compat re-exports
    AgentConfig,
    AgentProfileConfig,
    ResolvedAgentTarget,
    parse_agents_config,
    resolve_agent_target_from_agents,
)
from .app_server_command import (
    resolve_app_server_command,
)
from .config_contract import CONFIG_VERSION, ConfigError
from .destinations import default_local_destination, resolve_effective_repo_destination
from .path_utils import ConfigPathError, resolve_config_path
from .report_retention import (
    DEFAULT_REPORT_MAX_HISTORY_FILES,
    DEFAULT_REPORT_MAX_TOTAL_BYTES,
)
from .utils import atomic_write

logger = logging.getLogger("codex_autorunner.core.config")

ACTIVE_HUB_ROOT_ENV = "CAR_HUB_ROOT"
TWELVE_HOUR_SECONDS = 12 * 60 * 60
PMA_DEFAULT_TURN_TIMEOUT_SECONDS = 7200

from .config_builders import (  # noqa: E402
    build_hub_config as _build_hub_config_impl,
)
from .config_builders import (  # noqa: E402
    build_repo_config as _build_repo_config_impl,
)
from .config_env import (  # noqa: E402
    collect_env_overrides,  # noqa: F401 — backward-compat re-export
    load_dotenv_for_root,  # noqa: F401 — backward-compat re-export
    resolve_env_for_root,  # noqa: F401 — backward-compat re-export
)
from .config_layering import (  # noqa: E402
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,  # noqa: F401 — backward-compat re-export
    DEFAULT_REPO_CONFIG,  # noqa: F401 — backward-compat re-export
    GENERATED_CONFIG_HEADER,  # noqa: F401 — backward-compat re-export
    PMA_DEFAULT_MAX_TEXT_CHARS,
    REPO_OVERRIDE_FILENAME,  # noqa: F401 — backward-compat re-export
    ROOT_CONFIG_FILENAME,  # noqa: F401 — backward-compat re-export
    ROOT_OVERRIDE_FILENAME,
    _default_housekeeping_section,
    _default_update_linux_service_names,
    _load_yaml_dict,
    derive_repo_config_data,
    find_nearest_hub_config_path,
    load_root_defaults,  # noqa: F401 — backward-compat re-export
    resolve_hub_config_data,
)
from .generated_hub_config import normalize_generated_hub_config  # noqa: E402


def _parse_update_backend(update_cfg: Mapping[str, Any]) -> str:
    raw = update_cfg.get("backend")
    if raw is None:
        return "auto"
    value = str(raw).strip().lower()
    return value or "auto"


def _parse_update_linux_service_names(update_cfg: Mapping[str, Any]) -> Dict[str, str]:
    merged = dict(_default_update_linux_service_names())
    raw = update_cfg.get("linux_service_names")
    if not isinstance(raw, dict):
        return merged
    for key in ("hub", "telegram", "discord"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            merged[key] = value.strip()
    return merged


def resolve_housekeeping_rule(
    config: object,
    name: str,
) -> Optional[HousekeepingRule]:
    if not isinstance(config, HousekeepingConfig):
        return None
    wanted = name.strip().lower()
    if not wanted:
        return None
    for rule in config.rules:
        if rule.name.strip().lower() == wanted:
            return rule
    return None


def default_housekeeping_rule_named(
    name: str,
    *,
    include_repo_review_runs: bool = False,
    include_hub_update_rules: bool = False,
) -> Optional[HousekeepingRule]:
    default_config = parse_housekeeping_config(
        _default_housekeeping_section(
            include_repo_review_runs=include_repo_review_runs,
            include_hub_update_rules=include_hub_update_rules,
        )
    )
    return resolve_housekeeping_rule(default_config, name)


# Import validation helpers after approval-mode constants are defined.
from .config_validation import (  # noqa: E402,I001
    _is_loopback_host as _is_loopback_host_impl,
    _normalize_ticket_flow_approval_mode,
    _validate_hub_config,
    _validate_repo_config,
)

__all__ = [
    "ConfigError",
    "ConfigPathError",
]


def _is_loopback_host(host: str) -> bool:
    return _is_loopback_host_impl(host)


from .config_parsers import (  # noqa: E402
    parse_flow_retention_config,  # noqa: F401 — backward-compat re-export
)
from .config_types import (  # noqa: E402
    AppServerAutorunnerPromptConfig,
    AppServerClientConfig,
    AppServerConfig,
    AppServerDocChatPromptConfig,
    AppServerOutputConfig,
    AppServerPromptsConfig,
    AppServerSpecIngestPromptConfig,
    DestinationConfigSection,
    FlowRetentionConfig,  # noqa: F401 — backward-compat re-export
    HubConfig,
    LogConfig,  # noqa: F401 — backward-compat re-export
    NotificationsConfigSection,
    NotificationTargetSection,
    OpenCodeConfig,
    PmaConfig,
    RepoConfig,
    SecurityConfigSection,
    StaticAssetsConfig,
    TemplateRepoConfig,
    TemplatesConfig,
    TicketFlowConfig,
    UsageConfig,
    VoiceConfigSection,
)


def _parse_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


# Alias used by existing code paths that only support repo mode
Config = RepoConfig


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


def _parse_destination_config_section(raw: object) -> DestinationConfigSection:
    if not isinstance(raw, dict):
        return cast(DestinationConfigSection, default_local_destination())
    return cast(DestinationConfigSection, dict(raw))


def _parse_ticket_flow_config(
    cfg: Optional[Dict[str, Any]],
    defaults: Optional[Dict[str, Any]],
) -> TicketFlowConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    approval_mode = _normalize_ticket_flow_approval_mode(
        cfg.get("approval_mode", defaults.get("approval_mode", "yolo")),
        scope="ticket_flow.approval_mode",
    )
    default_approval_decision = cfg.get(
        "default_approval_decision", defaults.get("default_approval_decision", "accept")
    )
    if not isinstance(default_approval_decision, str):
        raise ConfigError("ticket_flow.default_approval_decision must be a string")
    include_previous_ticket_context = cfg.get(
        "include_previous_ticket_context",
        defaults.get("include_previous_ticket_context", False),
    )
    if not isinstance(include_previous_ticket_context, bool):
        raise ConfigError("ticket_flow.include_previous_ticket_context must be boolean")
    auto_resume = cfg.get("auto_resume", defaults.get("auto_resume", False))
    if not isinstance(auto_resume, bool):
        raise ConfigError("ticket_flow.auto_resume must be boolean")
    max_total_turns = cfg.get("max_total_turns", defaults.get("max_total_turns"))
    if max_total_turns is not None:
        if (
            isinstance(max_total_turns, bool)
            or not isinstance(max_total_turns, int)
            or max_total_turns < 1
        ):
            raise ConfigError(
                "ticket_flow.max_total_turns must be a positive integer or null"
            )
    return TicketFlowConfig(
        approval_mode=approval_mode,
        default_approval_decision=default_approval_decision,
        include_previous_ticket_context=include_previous_ticket_context,
        auto_resume=auto_resume,
        max_total_turns=max_total_turns,
    )


def update_override_templates(repo_root: Path, repos: List[Dict[str, Any]]) -> None:
    """
    Update templates.repos in the root override file, preserving other settings.

    This writes to `codex-autorunner.override.yml` (gitignored) at the provided repo_root.
    """
    override_path = repo_root / ROOT_OVERRIDE_FILENAME
    data = _load_yaml_dict(override_path)
    templates = data.get("templates")
    if templates is None or not isinstance(templates, dict):
        templates = {}
        data["templates"] = templates
    templates["repos"] = list(repos or [])
    rendered = yaml.safe_dump(data, sort_keys=False).rstrip() + "\n"
    atomic_write(override_path, rendered)


def _normalize_base_path(path: Optional[str]) -> str:
    """Normalize base path to either '' or a single-leading-slash path without trailing slash."""
    if not path:
        return ""
    normalized = str(path).strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    normalized = normalized.rstrip("/")
    return normalized or ""


def _parse_prompt_int(cfg: Dict[str, Any], defaults: Dict[str, Any], key: str) -> int:
    raw = cfg.get(key)
    if raw is None:
        raw = defaults.get(key, 0)
    return int(raw)


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
    return AppServerPromptsConfig(
        doc_chat=AppServerDocChatPromptConfig(
            max_chars=_parse_prompt_int(doc_chat_cfg, doc_chat_defaults, "max_chars"),
            message_max_chars=_parse_prompt_int(
                doc_chat_cfg, doc_chat_defaults, "message_max_chars"
            ),
            target_excerpt_max_chars=_parse_prompt_int(
                doc_chat_cfg, doc_chat_defaults, "target_excerpt_max_chars"
            ),
            recent_summary_max_chars=_parse_prompt_int(
                doc_chat_cfg, doc_chat_defaults, "recent_summary_max_chars"
            ),
        ),
        spec_ingest=AppServerSpecIngestPromptConfig(
            max_chars=_parse_prompt_int(
                spec_ingest_cfg, spec_ingest_defaults, "max_chars"
            ),
            message_max_chars=_parse_prompt_int(
                spec_ingest_cfg, spec_ingest_defaults, "message_max_chars"
            ),
            spec_excerpt_max_chars=_parse_prompt_int(
                spec_ingest_cfg, spec_ingest_defaults, "spec_excerpt_max_chars"
            ),
        ),
        autorunner=AppServerAutorunnerPromptConfig(
            max_chars=_parse_prompt_int(
                autorunner_cfg, autorunner_defaults, "max_chars"
            ),
            message_max_chars=_parse_prompt_int(
                autorunner_cfg, autorunner_defaults, "message_max_chars"
            ),
            todo_excerpt_max_chars=_parse_prompt_int(
                autorunner_cfg, autorunner_defaults, "todo_excerpt_max_chars"
            ),
            prev_run_max_chars=_parse_prompt_int(
                autorunner_cfg, autorunner_defaults, "prev_run_max_chars"
            ),
        ),
    )


_APP_SERVER_OUTPUT_POLICIES = {"final_only", "all_agent_messages"}


def _parse_app_server_output_config(
    cfg: Optional[Dict[str, Any]],
    defaults: Optional[Dict[str, Any]],
) -> AppServerOutputConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    policy_raw = cfg.get("policy", defaults.get("policy"))
    policy = str(policy_raw).strip().lower() if policy_raw is not None else ""
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
    raw_command = cfg.get("command", dataclasses.MISSING)
    if raw_command is dataclasses.MISSING:
        command = resolve_app_server_command(
            defaults.get("command"),
            env=os.environ,
        )
    else:
        command = resolve_app_server_command(
            raw_command,
            env=os.environ,
            fallback=(),
        )
    state_root_raw = cfg.get("state_root", defaults.get("state_root"))
    if state_root_raw is None:
        raise ConfigError("app_server.state_root is required")
    state_root = resolve_config_path(
        state_root_raw,
        root,
        allow_home=True,
        scope="app_server.state_root",
    )
    auto_restart_raw = cfg.get("auto_restart", defaults.get("auto_restart"))
    if auto_restart_raw is None:
        auto_restart = None
    else:
        auto_restart = bool(auto_restart_raw)
    max_handles_raw = cfg.get("max_handles", defaults.get("max_handles"))
    max_handles = _parse_optional_int(max_handles_raw)
    if max_handles is not None and max_handles <= 0:
        max_handles = None
    idle_ttl_raw = cfg.get("idle_ttl_seconds", defaults.get("idle_ttl_seconds"))
    idle_ttl_seconds = _parse_optional_int(idle_ttl_raw)
    if idle_ttl_seconds is not None and idle_ttl_seconds <= 0:
        idle_ttl_seconds = None
    turn_timeout_raw = cfg.get(
        "turn_timeout_seconds", defaults.get("turn_timeout_seconds")
    )
    turn_timeout_seconds = (
        float(turn_timeout_raw) if turn_timeout_raw is not None else None
    )
    if turn_timeout_seconds is not None and turn_timeout_seconds <= 0:
        turn_timeout_seconds = None
    stall_timeout_raw = cfg.get(
        "turn_stall_timeout_seconds", defaults.get("turn_stall_timeout_seconds")
    )
    turn_stall_timeout_seconds = (
        float(stall_timeout_raw) if stall_timeout_raw is not None else None
    )
    if turn_stall_timeout_seconds is not None and turn_stall_timeout_seconds <= 0:
        turn_stall_timeout_seconds = None
    stall_poll_raw = cfg.get(
        "turn_stall_poll_interval_seconds",
        defaults.get("turn_stall_poll_interval_seconds"),
    )
    turn_stall_poll_interval_seconds = (
        float(stall_poll_raw) if stall_poll_raw is not None else None
    )
    if (
        turn_stall_poll_interval_seconds is not None
        and turn_stall_poll_interval_seconds <= 0
    ):
        turn_stall_poll_interval_seconds = defaults.get(
            "turn_stall_poll_interval_seconds"
        )
    stall_recovery_raw = cfg.get(
        "turn_stall_recovery_min_interval_seconds",
        defaults.get("turn_stall_recovery_min_interval_seconds"),
    )
    turn_stall_recovery_min_interval_seconds = (
        float(stall_recovery_raw) if stall_recovery_raw is not None else None
    )
    if (
        turn_stall_recovery_min_interval_seconds is not None
        and turn_stall_recovery_min_interval_seconds < 0
    ):
        turn_stall_recovery_min_interval_seconds = defaults.get(
            "turn_stall_recovery_min_interval_seconds"
        )
    stall_max_attempts_raw = cfg.get(
        "turn_stall_max_recovery_attempts",
        defaults.get("turn_stall_max_recovery_attempts"),
    )
    turn_stall_max_recovery_attempts = _parse_optional_int(stall_max_attempts_raw)
    if (
        turn_stall_max_recovery_attempts is not None
        and turn_stall_max_recovery_attempts <= 0
    ):
        turn_stall_max_recovery_attempts = None
    request_timeout_raw = cfg.get("request_timeout", defaults.get("request_timeout"))
    request_timeout = (
        float(request_timeout_raw) if request_timeout_raw is not None else None
    )
    if request_timeout is not None and request_timeout <= 0:
        request_timeout = None
    client_defaults = defaults.get("client")
    client_defaults = client_defaults if isinstance(client_defaults, dict) else {}
    client_cfg_raw = cfg.get("client")
    client_cfg = client_cfg_raw if isinstance(client_cfg_raw, dict) else {}

    def _client_int(key: str) -> int:
        value = client_cfg.get(key, client_defaults.get(key))
        value = int(value) if value is not None else 0
        if value <= 0:
            value = int(client_defaults.get(key) or 0)
        return value

    def _client_float(key: str, *, allow_zero: bool = False) -> float:
        value = client_cfg.get(key, client_defaults.get(key))
        value = float(value) if value is not None else 0.0
        if value < 0 or (not allow_zero and value <= 0):
            value = float(client_defaults.get(key) or 0.0)
        return value

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
            max_message_bytes=_client_int("max_message_bytes"),
            oversize_preview_bytes=_client_int("oversize_preview_bytes"),
            max_oversize_drain_bytes=_client_int("max_oversize_drain_bytes"),
            restart_backoff_initial_seconds=_client_float(
                "restart_backoff_initial_seconds"
            ),
            restart_backoff_max_seconds=_client_float("restart_backoff_max_seconds"),
            restart_backoff_jitter_ratio=_client_float(
                "restart_backoff_jitter_ratio", allow_zero=True
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
    server_scope_raw = cfg.get(
        "server_scope", defaults.get("server_scope", "workspace")
    )
    server_scope = str(server_scope_raw).strip().lower() or "workspace"
    if server_scope not in {"workspace", "global"}:
        raise ConfigError("opencode.server_scope must be 'workspace' or 'global'")
    stall_timeout_raw = cfg.get(
        "session_stall_timeout_seconds",
        defaults.get("session_stall_timeout_seconds"),
    )
    stall_timeout_seconds = (
        float(stall_timeout_raw) if stall_timeout_raw is not None else None
    )
    if stall_timeout_seconds is not None and stall_timeout_seconds <= 0:
        stall_timeout_seconds = None
    max_text_chars_raw = cfg.get("max_text_chars", defaults.get("max_text_chars"))
    max_text_chars = (
        int(max_text_chars_raw)
        if isinstance(max_text_chars_raw, int) and max_text_chars_raw > 0
        else None
    )
    max_handles_raw = cfg.get("max_handles", defaults.get("max_handles"))
    max_handles = _parse_optional_int(max_handles_raw)
    if max_handles is not None and max_handles <= 0:
        max_handles = None
    idle_ttl_raw = cfg.get("idle_ttl_seconds", defaults.get("idle_ttl_seconds"))
    idle_ttl_seconds = _parse_optional_int(idle_ttl_raw)
    if idle_ttl_seconds is not None and idle_ttl_seconds <= 0:
        idle_ttl_seconds = None
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
    try:
        max_upload_bytes = int(max_upload_bytes_raw)
    except (ValueError, TypeError):
        max_upload_bytes = 10_000_000
    if max_upload_bytes <= 0:
        max_upload_bytes = 10_000_000

    def _parse_positive_int(key: str, fallback: int) -> int:
        raw = cfg.get(key, defaults.get(key, fallback))
        if isinstance(raw, bool):
            return fallback
        try:
            value = int(raw)
        except (ValueError, TypeError):
            return fallback
        return value if value > 0 else fallback

    turn_timeout_seconds = _parse_positive_int(
        "turn_timeout_seconds",
        PMA_DEFAULT_TURN_TIMEOUT_SECONDS,
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
        turn_timeout_seconds=turn_timeout_seconds,
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
    cache_scope = str(cfg.get("cache_scope", defaults.get("cache_scope", "global")))
    cache_scope = cache_scope.lower().strip() or "global"
    global_cache_raw = cfg.get("global_cache_root", defaults.get("global_cache_root"))
    if global_cache_raw is None:
        global_cache_raw = os.environ.get("CODEX_HOME", "~/.codex")
    global_cache_root = resolve_config_path(
        global_cache_raw,
        root,
        allow_absolute=True,
        allow_home=True,
        scope="usage.global_cache_root",
    )
    repo_cache_raw = cfg.get("repo_cache_path", defaults.get("repo_cache_path"))
    if repo_cache_raw is None:
        repo_cache_raw = ".codex-autorunner/usage/usage_series_cache.json"
    repo_cache_path = resolve_config_path(
        repo_cache_raw,
        root,
        scope="usage.repo_cache_path",
    )
    return UsageConfig(
        cache_scope=cache_scope,
        global_cache_root=global_cache_root,
        repo_cache_path=repo_cache_path,
    )


def _parse_agents_config(
    cfg: Optional[Dict[str, Any]], defaults: Dict[str, Any]
) -> Dict[str, AgentConfig]:
    return parse_agents_config(cfg, defaults)


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
    cache_root = resolve_config_path(
        cache_root_raw,
        root,
        allow_home=True,
        scope="static_assets.cache_root",
    )
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


def load_hub_config_data(config_path: Path) -> Dict[str, Any]:
    """Load, merge, and return a raw hub config dict for the given config path."""
    load_dotenv_for_root(config_path.parent.parent.resolve())
    data = normalize_generated_hub_config(config_path)
    mode = data.get("mode")
    if mode not in (None, "hub"):
        raise ConfigError(f"Invalid mode '{mode}'; expected 'hub'")
    root = config_path.parent.parent.resolve()
    return resolve_hub_config_data(root, data)


def _resolve_hub_config_path(start: Path) -> Path:
    config_path = find_nearest_hub_config_path(start)
    if not config_path:
        raise ConfigError(
            f"Missing hub config file; expected to find {CONFIG_FILENAME} in {start} or parents "
            "(pass --path on most commands, --hub-root on car render, or run 'car init' to initialize)"
        )
    return config_path


def ensure_hub_config_at(start: Path) -> tuple[Path, bool]:
    """
    Ensure a hub config exists at or above the given start path.

    Returns a tuple of (config_path, did_initialize) where:
    - config_path is the path to the hub config file
    - did_initialize is True if we created a new config, False if it already existed
    """
    existing = find_nearest_hub_config_path(start)
    if existing:
        return (existing, False)

    try:
        from .utils import find_repo_root

        target_root = find_repo_root(start)
    except Exception:  # intentional: find_repo_root may raise undocumented exceptions
        target_root = start

    from ..bootstrap import seed_hub_files

    seed_hub_files(target_root)
    new_path = find_nearest_hub_config_path(target_root)
    if not new_path:
        raise ConfigError(f"Failed to initialize hub config at {target_root}")
    return (new_path, True)


def load_hub_config(start: Path) -> HubConfig:
    """Load the nearest hub config walking upward from the provided path."""
    config_path = _resolve_hub_config_path(start)
    merged = load_hub_config_data(config_path)
    _validate_hub_config(merged, root=config_path.parent.parent.resolve())
    return _build_hub_config(config_path, merged)


def _resolve_hub_path_for_repo(repo_root: Path, hub_path: Optional[Path]) -> Path:
    if hub_path:
        candidate = hub_path
        if candidate.is_dir():
            candidate = candidate / CONFIG_FILENAME
        if not candidate.exists():
            raise ConfigError(f"Hub config not found at {candidate}")
        data = _load_yaml_dict(candidate)
        mode = data.get("mode")
        if mode not in (None, "hub"):
            raise ConfigError(f"Invalid hub config mode '{mode}'; expected 'hub'")
        return candidate
    local_candidate = find_nearest_hub_config_path(repo_root)
    if local_candidate:
        return local_candidate
    env_hub_root = os.environ.get(ACTIVE_HUB_ROOT_ENV, "").strip()
    if env_hub_root:
        candidate = Path(env_hub_root).expanduser()
        if candidate.is_dir():
            candidate = candidate / CONFIG_FILENAME
        if candidate.exists():
            data = _load_yaml_dict(candidate)
            mode = data.get("mode")
            if mode not in (None, "hub"):
                raise ConfigError(
                    f"Invalid hub config mode '{mode}' from {ACTIVE_HUB_ROOT_ENV}; expected 'hub'"
                )
            return candidate
    return _resolve_hub_config_path(repo_root)


def derive_repo_config(
    hub: HubConfig, repo_root: Path, *, load_env: bool = True
) -> RepoConfig:
    if load_env:
        load_dotenv_for_root(repo_root)
    merged = derive_repo_config_data(hub.raw, repo_root)
    merged["mode"] = "repo"
    merged["version"] = CONFIG_VERSION
    _validate_repo_config(merged, root=repo_root)
    repo_config = _build_repo_config(repo_root / CONFIG_FILENAME, merged)
    repo_config.effective_destination = _resolve_repo_effective_destination(
        hub, repo_root
    )
    return repo_config


def _resolve_repo_effective_destination(
    hub: HubConfig, repo_root: Path
) -> DestinationConfigSection:
    try:
        manifest = load_manifest(hub.manifest_path, hub.root)
    except ManifestError as exc:
        raise ConfigError(
            "Failed to resolve effective destination from hub manifest: "
            f"{hub.manifest_path}: {exc}"
        ) from exc
    except Exception as exc:  # intentional: defensive guard beyond known ManifestError
        raise ConfigError(
            "Failed to resolve effective destination from hub manifest: "
            f"{hub.manifest_path}: {exc}"
        ) from exc
    repo = manifest.get_by_path(hub.root, repo_root)
    if repo is None:
        return _parse_destination_config_section(default_local_destination())
    repos_by_id = {entry.id: entry for entry in manifest.repos}
    resolution = resolve_effective_repo_destination(repo, repos_by_id)
    return _parse_destination_config_section(resolution.to_dict())


def _resolve_repo_root(start: Path) -> Path:
    search_dir = start.resolve() if start.is_dir() else start.resolve().parent
    for current in [search_dir] + list(search_dir.parents):
        if (current / ".codex-autorunner" / "state.sqlite3").exists():
            return current
        if (current / ".git").exists():
            return current
    return search_dir


def load_repo_config(start: Path, hub_path: Optional[Path] = None) -> RepoConfig:
    """Load a repo config by deriving it from the nearest hub config."""
    repo_root = _resolve_repo_root(start)
    hub_config_path = _resolve_hub_path_for_repo(repo_root, hub_path)
    hub_config = load_hub_config_data(hub_config_path)
    _validate_hub_config(hub_config, root=hub_config_path.parent.parent.resolve())
    hub = _build_hub_config(hub_config_path, hub_config)
    return derive_repo_config(hub, repo_root)


def _build_repo_config(config_path: Path, cfg: Dict[str, Any]) -> RepoConfig:
    return _build_repo_config_impl(config_path, cfg)


def _build_hub_config(config_path: Path, cfg: Dict[str, Any]) -> HubConfig:
    return _build_hub_config_impl(config_path, cfg)
