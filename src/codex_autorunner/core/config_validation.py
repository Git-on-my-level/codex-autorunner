"""Validation helpers for configuration loading."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any, Dict, Tuple, Type, Union

from .config_contract import (
    _TICKET_FLOW_APPROVAL_MODE_ALIASES,
    _TICKET_FLOW_APPROVAL_MODE_ALLOWED,
    CONFIG_VERSION,
    ConfigError,
)
from .path_utils import ConfigPathError, resolve_config_path


def _normalize_ticket_flow_approval_mode(value: Any, *, scope: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{scope} must be a string")
    normalized = value.strip().lower()
    canonical = _TICKET_FLOW_APPROVAL_MODE_ALIASES.get(normalized)
    if canonical is None:
        raise ConfigError(
            f"{scope} must be one of: {_TICKET_FLOW_APPROVAL_MODE_ALLOWED}"
        )
    return canonical


def _validate_version(cfg: Dict[str, Any]) -> None:
    if cfg.get("version") != CONFIG_VERSION:
        raise ConfigError(f"Unsupported config version; expected {CONFIG_VERSION}")


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
    if not _is_loopback_host(host) and not allowed_hosts:
        raise ConfigError(
            "server.allowed_hosts must be set when binding to a non-loopback host"
        )


def _validate_app_server_config(cfg: Dict[str, Any]) -> None:
    app_server_cfg = cfg.get("app_server")
    if app_server_cfg is None:
        return
    if not isinstance(app_server_cfg, dict):
        raise ConfigError("app_server section must be a mapping if provided")
    command = app_server_cfg.get("command")
    if command is not None and not isinstance(command, (list, str)):
        raise ConfigError("app_server.command must be a list or string if provided")
    if "state_root" in app_server_cfg and not isinstance(
        app_server_cfg.get("state_root", ""), str
    ):
        raise ConfigError("app_server.state_root must be a string path")
    if (
        "auto_restart" in app_server_cfg
        and app_server_cfg.get("auto_restart") is not None
    ):
        if not isinstance(app_server_cfg.get("auto_restart"), bool):
            raise ConfigError("app_server.auto_restart must be boolean or null")
    for key in ("max_handles", "idle_ttl_seconds"):
        if key in app_server_cfg and app_server_cfg.get(key) is not None:
            if not isinstance(app_server_cfg.get(key), int):
                raise ConfigError(f"app_server.{key} must be an integer or null")
    if (
        "turn_timeout_seconds" in app_server_cfg
        and app_server_cfg.get("turn_timeout_seconds") is not None
    ):
        if not isinstance(app_server_cfg.get("turn_timeout_seconds"), (int, float)):
            raise ConfigError(
                "app_server.turn_timeout_seconds must be a number or null"
            )
    if (
        "request_timeout" in app_server_cfg
        and app_server_cfg.get("request_timeout") is not None
    ):
        if not isinstance(app_server_cfg.get("request_timeout"), (int, float)):
            raise ConfigError("app_server.request_timeout must be a number or null")
    for key in (
        "turn_stall_timeout_seconds",
        "turn_stall_poll_interval_seconds",
        "turn_stall_recovery_min_interval_seconds",
    ):
        if key in app_server_cfg and app_server_cfg.get(key) is not None:
            if not isinstance(app_server_cfg.get(key), (int, float)):
                raise ConfigError(f"app_server.{key} must be a number or null")
    client_cfg = app_server_cfg.get("client")
    if client_cfg is not None:
        if not isinstance(client_cfg, dict):
            raise ConfigError("app_server.client must be a mapping if provided")
        for key in (
            "max_message_bytes",
            "oversize_preview_bytes",
            "max_oversize_drain_bytes",
        ):
            if key in client_cfg:
                value = client_cfg.get(key)
                if not isinstance(value, int):
                    raise ConfigError(f"app_server.client.{key} must be an integer")
                if value <= 0:
                    raise ConfigError(f"app_server.client.{key} must be > 0")
        for key in (
            "restart_backoff_initial_seconds",
            "restart_backoff_max_seconds",
            "restart_backoff_jitter_ratio",
        ):
            if key in client_cfg:
                value = client_cfg.get(key)
                if not isinstance(value, (int, float)):
                    raise ConfigError(
                        f"app_server.client.{key} must be a number if provided"
                    )
                if key == "restart_backoff_jitter_ratio":
                    if value < 0:
                        raise ConfigError(
                            "app_server.client.restart_backoff_jitter_ratio must be >= 0"
                        )
                elif value <= 0:
                    raise ConfigError(f"app_server.client.{key} must be > 0")
    prompts = app_server_cfg.get("prompts")
    if prompts is not None:
        if not isinstance(prompts, dict):
            raise ConfigError("app_server.prompts must be a mapping if provided")
        expected = {
            "doc_chat": {
                "max_chars": 1,
                "message_max_chars": 1,
                "target_excerpt_max_chars": 0,
                "recent_summary_max_chars": 0,
            },
            "spec_ingest": {
                "max_chars": 1,
                "message_max_chars": 1,
                "spec_excerpt_max_chars": 0,
            },
            "autorunner": {
                "max_chars": 1,
                "message_max_chars": 1,
                "todo_excerpt_max_chars": 0,
                "prev_run_max_chars": 0,
            },
        }
        for section, keys in expected.items():
            section_cfg = prompts.get(section)
            if section_cfg is None:
                continue
            if not isinstance(section_cfg, dict):
                raise ConfigError(f"app_server.prompts.{section} must be a mapping")
            for key, min_value in keys.items():
                if key not in section_cfg:
                    continue
                value = section_cfg.get(key)
                if value is None or not isinstance(value, int):
                    raise ConfigError(
                        f"app_server.prompts.{section}.{key} must be an integer"
                    )
                if value < min_value:
                    raise ConfigError(
                        f"app_server.prompts.{section}.{key} must be >= {min_value}"
                    )


def _validate_opencode_config(cfg: Dict[str, Any]) -> None:
    opencode_cfg = cfg.get("opencode")
    if opencode_cfg is None:
        return
    if not isinstance(opencode_cfg, dict):
        raise ConfigError("opencode section must be a mapping if provided")
    if "server_scope" in opencode_cfg and opencode_cfg.get("server_scope") is not None:
        server_scope = opencode_cfg.get("server_scope")
        if not isinstance(server_scope, str):
            raise ConfigError("opencode.server_scope must be a string or null")
        if server_scope.strip().lower() not in {"workspace", "global"}:
            raise ConfigError("opencode.server_scope must be 'workspace' or 'global'")
    if (
        "session_stall_timeout_seconds" in opencode_cfg
        and opencode_cfg.get("session_stall_timeout_seconds") is not None
    ):
        if not isinstance(
            opencode_cfg.get("session_stall_timeout_seconds"), (int, float)
        ):
            raise ConfigError(
                "opencode.session_stall_timeout_seconds must be a number or null"
            )
    if (
        "max_text_chars" in opencode_cfg
        and opencode_cfg.get("max_text_chars") is not None
    ):
        max_text_chars = opencode_cfg.get("max_text_chars")
        if not isinstance(max_text_chars, int):
            raise ConfigError("opencode.max_text_chars must be an integer or null")
    if "max_handles" in opencode_cfg and opencode_cfg.get("max_handles") is not None:
        if not isinstance(opencode_cfg.get("max_handles"), int):
            raise ConfigError("opencode.max_handles must be an integer or null")
    if (
        "idle_ttl_seconds" in opencode_cfg
        and opencode_cfg.get("idle_ttl_seconds") is not None
    ):
        if not isinstance(opencode_cfg.get("idle_ttl_seconds"), int):
            raise ConfigError("opencode.idle_ttl_seconds must be an integer or null")


def _validate_update_config(cfg: Dict[str, Any]) -> None:
    update_cfg = cfg.get("update")
    if update_cfg is None:
        return
    if not isinstance(update_cfg, dict):
        raise ConfigError("update section must be a mapping if provided")
    backend = update_cfg.get("backend")
    if backend is not None:
        if not isinstance(backend, str):
            raise ConfigError("update.backend must be a string")
        if backend.strip().lower() not in {"auto", "launchd", "systemd-user"}:
            raise ConfigError(
                "update.backend must be one of: auto, launchd, systemd-user"
            )
    if "skip_checks" in update_cfg and update_cfg.get("skip_checks") is not None:
        if not isinstance(update_cfg.get("skip_checks"), bool):
            raise ConfigError("update.skip_checks must be boolean or null")
    linux_services = update_cfg.get("linux_service_names")
    if linux_services is None:
        return
    if not isinstance(linux_services, dict):
        raise ConfigError("update.linux_service_names must be a mapping if provided")
    hub_service = linux_services.get("hub")
    telegram_service = linux_services.get("telegram")
    discord_service = linux_services.get("discord")
    if hub_service is not None:
        if not isinstance(hub_service, str) or not hub_service.strip():
            raise ConfigError(
                "update.linux_service_names.hub must be a non-empty string"
            )
    if telegram_service is not None:
        if not isinstance(telegram_service, str) or not telegram_service.strip():
            raise ConfigError(
                "update.linux_service_names.telegram must be a non-empty string"
            )
    if discord_service is not None:
        if not isinstance(discord_service, str) or not discord_service.strip():
            raise ConfigError(
                "update.linux_service_names.discord must be a non-empty string"
            )


def _validate_usage_config(cfg: Dict[str, Any], *, root: Path) -> None:
    usage_cfg = cfg.get("usage")
    if usage_cfg is None:
        return
    if not isinstance(usage_cfg, dict):
        raise ConfigError("usage section must be a mapping if provided")
    cache_scope = usage_cfg.get("cache_scope")
    if cache_scope is not None and not isinstance(cache_scope, str):
        raise ConfigError("usage.cache_scope must be a string if provided")
    if isinstance(cache_scope, str):
        scope_val = cache_scope.strip().lower()
        if scope_val and scope_val not in {"global", "repo"}:
            raise ConfigError("usage.cache_scope must be 'global' or 'repo'")
    global_cache_root = usage_cfg.get("global_cache_root")
    if global_cache_root is not None:
        if not isinstance(global_cache_root, str):
            raise ConfigError("usage.global_cache_root must be a string or null")
        try:
            resolve_config_path(
                global_cache_root,
                root,
                allow_absolute=True,
                allow_home=True,
                scope="usage.global_cache_root",
            )
        except ConfigPathError as exc:
            raise ConfigError(str(exc)) from exc
    repo_cache_path = usage_cfg.get("repo_cache_path")
    if repo_cache_path is not None:
        if not isinstance(repo_cache_path, str):
            raise ConfigError("usage.repo_cache_path must be a string or null")
        try:
            resolve_config_path(
                repo_cache_path,
                root,
                scope="usage.repo_cache_path",
            )
        except ConfigPathError as exc:
            raise ConfigError(str(exc)) from exc


def _validate_agents_config(cfg: Dict[str, Any]) -> None:
    agents_cfg = cfg.get("agents")
    if agents_cfg is None:
        return
    if not isinstance(agents_cfg, dict):
        raise ConfigError("agents section must be a mapping if provided")
    for agent_id, agent_cfg in agents_cfg.items():
        if not isinstance(agent_cfg, dict):
            raise ConfigError(f"agents.{agent_id} must be a mapping")
        binary = agent_cfg.get("binary")
        if not isinstance(binary, str) or not binary.strip():
            raise ConfigError(f"agents.{agent_id}.binary is required")
        if "serve_command" in agent_cfg and not isinstance(
            agent_cfg.get("serve_command"), (list, str)
        ):
            raise ConfigError(f"agents.{agent_id}.serve_command must be a list or str")


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
    autorunner_cfg = cfg.get("autorunner")
    if autorunner_cfg is not None and not isinstance(autorunner_cfg, dict):
        raise ConfigError("autorunner section must be a mapping if provided")
    if isinstance(autorunner_cfg, dict):
        reuse_session = autorunner_cfg.get("reuse_session")
        if reuse_session is not None and not isinstance(reuse_session, bool):
            raise ConfigError("autorunner.reuse_session must be boolean or null")
    ticket_flow_cfg = cfg.get("ticket_flow")
    if ticket_flow_cfg is not None and not isinstance(ticket_flow_cfg, dict):
        raise ConfigError("ticket_flow section must be a mapping if provided")
    if isinstance(ticket_flow_cfg, dict):
        if "approval_mode" in ticket_flow_cfg:
            _normalize_ticket_flow_approval_mode(
                ticket_flow_cfg.get("approval_mode"),
                scope="ticket_flow.approval_mode",
            )
        if "default_approval_decision" in ticket_flow_cfg and not isinstance(
            ticket_flow_cfg.get("default_approval_decision"), str
        ):
            raise ConfigError("ticket_flow.default_approval_decision must be a string")
        if "include_previous_ticket_context" in ticket_flow_cfg and not isinstance(
            ticket_flow_cfg.get("include_previous_ticket_context"), bool
        ):
            raise ConfigError(
                "ticket_flow.include_previous_ticket_context must be boolean"
            )
        if "auto_resume" in ticket_flow_cfg and not isinstance(
            ticket_flow_cfg.get("auto_resume"), bool
        ):
            raise ConfigError("ticket_flow.auto_resume must be boolean")
    ui_cfg = cfg.get("ui")
    if ui_cfg is not None and not isinstance(ui_cfg, dict):
        raise ConfigError("ui section must be a mapping if provided")
    if isinstance(ui_cfg, dict):
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
        discord_cfg = notifications_cfg.get("discord")
        if discord_cfg is not None and not isinstance(discord_cfg, dict):
            raise ConfigError("notifications.discord must be a mapping if provided")
        if isinstance(discord_cfg, dict):
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
        telegram_cfg = notifications_cfg.get("telegram")
        if telegram_cfg is not None and not isinstance(telegram_cfg, dict):
            raise ConfigError("notifications.telegram must be a mapping if provided")
        if isinstance(telegram_cfg, dict):
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
    terminal_cfg = cfg.get("terminal")
    if terminal_cfg is not None:
        if not isinstance(terminal_cfg, dict):
            raise ConfigError("terminal section must be a mapping if provided")
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
    static_cfg = cfg.get("static_assets")
    if static_cfg is not None and not isinstance(static_cfg, dict):
        raise ConfigError("static_assets section must be a mapping if provided")
    if isinstance(static_cfg, dict) and "cache_root" in static_cfg:
        if not isinstance(static_cfg.get("cache_root"), str):
            raise ConfigError("static_assets.cache_root must be a string path")
    if isinstance(static_cfg, dict) and "max_cache_entries" in static_cfg:
        max_cache_entries = static_cfg.get("max_cache_entries")
        if not isinstance(max_cache_entries, int):
            raise ConfigError("static_assets.max_cache_entries must be an integer")
        if max_cache_entries < 0:
            raise ConfigError("static_assets.max_cache_entries must be >= 0")
    if isinstance(static_cfg, dict) and "max_cache_age_days" in static_cfg:
        max_cache_age_days = static_cfg.get("max_cache_age_days")
        if not isinstance(max_cache_age_days, int):
            raise ConfigError("static_assets.max_cache_age_days must be an integer")
        if max_cache_age_days < 0:
            raise ConfigError("static_assets.max_cache_age_days must be >= 0")
    _validate_housekeeping_config(cfg)
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
    _validate_app_server_config(cfg)
    server_log_cfg = cfg.get("server_log")
    if server_log_cfg is not None and not isinstance(server_log_cfg, dict):
        raise ConfigError("server_log section must be a mapping or null")
    if isinstance(server_log_cfg, dict):
        if "path" in server_log_cfg and not isinstance(
            server_log_cfg.get("path", ""), str
        ):
            raise ConfigError("server_log.path must be a string path")
        for key in ("max_bytes", "backup_count"):
            if key in server_log_cfg and not isinstance(server_log_cfg.get(key), int):
                raise ConfigError(f"server_log.{key} must be an integer")
    _validate_static_assets_config(cfg, scope="hub")
    _validate_housekeeping_config(cfg)
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
        if isinstance(value, int) and value < min_value:
            if min_value == 0:
                raise ConfigError(f"{path}.{key} must be >= 0")
            elif min_value == 1:
                raise ConfigError(f"{path}.{key} must be > 0")
            else:
                raise ConfigError(f"{path}.{key} must be >= {min_value}")


def _validate_housekeeping_config(cfg: Dict[str, Any]) -> None:
    housekeeping_cfg = cfg.get("housekeeping")
    if housekeeping_cfg is None:
        return
    if not isinstance(housekeeping_cfg, dict):
        raise ConfigError("housekeeping section must be a mapping if provided")
    _validate_optional_type(housekeeping_cfg, "enabled", bool, path="housekeeping")
    _validate_optional_type(
        housekeeping_cfg, "interval_seconds", int, path="housekeeping"
    )
    _validate_optional_int_ge(
        housekeeping_cfg, "interval_seconds", 1, path="housekeeping"
    )
    _validate_optional_type(
        housekeeping_cfg, "min_file_age_seconds", int, path="housekeeping"
    )
    _validate_optional_int_ge(
        housekeeping_cfg, "min_file_age_seconds", 0, path="housekeeping"
    )
    _validate_optional_type(housekeeping_cfg, "dry_run", bool, path="housekeeping")
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
            for key in (
                "max_files",
                "max_total_bytes",
                "max_age_days",
                "max_bytes",
                "max_lines",
            ):
                _validate_optional_type(
                    rule, key, int, path=f"housekeeping.rules[{idx}]"
                )
                _validate_optional_int_ge(
                    rule, key, 0, path=f"housekeeping.rules[{idx}]"
                )


def _validate_static_assets_config(cfg: Dict[str, Any], scope: str) -> None:
    static_cfg = cfg.get("static_assets")
    if static_cfg is None:
        return
    if not isinstance(static_cfg, dict):
        raise ConfigError(f"{scope}.static_assets must be a mapping if provided")
    _validate_optional_type(
        static_cfg,
        "cache_root",
        str,
        path=f"{scope}.static_assets",
        allow_none=True,
    )
    _validate_optional_type(
        static_cfg,
        "max_cache_entries",
        int,
        path=f"{scope}.static_assets",
        allow_none=True,
    )
    _validate_optional_int_ge(
        static_cfg, "max_cache_entries", 0, path=f"{scope}.static_assets"
    )
    _validate_optional_type(
        static_cfg,
        "max_cache_age_days",
        int,
        path=f"{scope}.static_assets",
        allow_none=True,
    )
    _validate_optional_int_ge(
        static_cfg, "max_cache_age_days", 0, path=f"{scope}.static_assets"
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
    debug_cfg = telegram_cfg.get("debug")
    if debug_cfg is not None and not isinstance(debug_cfg, dict):
        raise ConfigError("telegram_bot.debug must be a mapping if provided")
    if isinstance(debug_cfg, dict):
        if "prefix_context" in debug_cfg and not isinstance(
            debug_cfg.get("prefix_context"), bool
        ):
            raise ConfigError("telegram_bot.debug.prefix_context must be boolean")
    for key in ("bot_token_env", "chat_id_env", "app_server_command_env"):
        if key in telegram_cfg and not isinstance(telegram_cfg.get(key), str):
            raise ConfigError(f"telegram_bot.{key} must be a string")
    for key in ("allowed_chat_ids", "allowed_user_ids"):
        if key in telegram_cfg and not isinstance(telegram_cfg.get(key), list):
            raise ConfigError(f"telegram_bot.{key} must be a list")
    if "require_topics" in telegram_cfg and not isinstance(
        telegram_cfg.get("require_topics"), bool
    ):
        raise ConfigError("telegram_bot.require_topics must be boolean")
    defaults_cfg = telegram_cfg.get("defaults")
    if defaults_cfg is not None and not isinstance(defaults_cfg, dict):
        raise ConfigError("telegram_bot.defaults must be a mapping if provided")
    if isinstance(defaults_cfg, dict):
        if "approval_mode" in defaults_cfg and not isinstance(
            defaults_cfg.get("approval_mode"), str
        ):
            raise ConfigError("telegram_bot.defaults.approval_mode must be a string")
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
    concurrency_cfg = telegram_cfg.get("concurrency")
    if concurrency_cfg is not None and not isinstance(concurrency_cfg, dict):
        raise ConfigError("telegram_bot.concurrency must be a mapping if provided")
    if isinstance(concurrency_cfg, dict):
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
    media_cfg = telegram_cfg.get("media")
    if media_cfg is not None and not isinstance(media_cfg, dict):
        raise ConfigError("telegram_bot.media must be a mapping if provided")
    if isinstance(media_cfg, dict):
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
    shell_cfg = telegram_cfg.get("shell")
    if shell_cfg is not None and not isinstance(shell_cfg, dict):
        raise ConfigError("telegram_bot.shell must be a mapping if provided")
    if isinstance(shell_cfg, dict):
        if "enabled" in shell_cfg and not isinstance(shell_cfg.get("enabled"), bool):
            raise ConfigError("telegram_bot.shell.enabled must be boolean")
        for key in ("timeout_ms", "max_output_chars"):
            value = shell_cfg.get(key)
            if value is not None and not isinstance(value, int):
                raise ConfigError(f"telegram_bot.shell.{key} must be an integer")
            if isinstance(value, int) and value <= 0:
                raise ConfigError(f"telegram_bot.shell.{key} must be greater than 0")
    cache_cfg = telegram_cfg.get("cache")
    if cache_cfg is not None and not isinstance(cache_cfg, dict):
        raise ConfigError("telegram_bot.cache must be a mapping if provided")
    if isinstance(cache_cfg, dict):
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
    command_reg_cfg = telegram_cfg.get("command_registration")
    if command_reg_cfg is not None and not isinstance(command_reg_cfg, dict):
        raise ConfigError("telegram_bot.command_registration must be a mapping")
    if isinstance(command_reg_cfg, dict):
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
    app_server_cfg = telegram_cfg.get("app_server")
    if app_server_cfg is not None and not isinstance(app_server_cfg, dict):
        raise ConfigError("telegram_bot.app_server must be a mapping if provided")
    if isinstance(app_server_cfg, dict):
        if (
            "turn_timeout_seconds" in app_server_cfg
            and app_server_cfg.get("turn_timeout_seconds") is not None
            and not isinstance(app_server_cfg.get("turn_timeout_seconds"), (int, float))
        ):
            raise ConfigError(
                "telegram_bot.app_server.turn_timeout_seconds must be a number or null"
            )
    agent_timeouts_cfg = telegram_cfg.get("agent_timeouts")
    if agent_timeouts_cfg is not None and not isinstance(agent_timeouts_cfg, dict):
        raise ConfigError("telegram_bot.agent_timeouts must be a mapping if provided")
    if isinstance(agent_timeouts_cfg, dict):
        for _key, value in agent_timeouts_cfg.items():
            if value is None:
                continue
            if not isinstance(value, (int, float)):
                raise ConfigError(
                    "telegram_bot.agent_timeouts values must be numbers or null"
                )
    polling_cfg = telegram_cfg.get("polling")
    if polling_cfg is not None and not isinstance(polling_cfg, dict):
        raise ConfigError("telegram_bot.polling must be a mapping if provided")
    if isinstance(polling_cfg, dict):
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

    command_registration = discord_cfg.get("command_registration")
    if command_registration is not None and not isinstance(command_registration, dict):
        raise ConfigError("discord_bot.command_registration must be a mapping")
    if isinstance(command_registration, dict):
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

    media_cfg = discord_cfg.get("media")
    if media_cfg is not None and not isinstance(media_cfg, dict):
        raise ConfigError("discord_bot.media must be a mapping")
    if isinstance(media_cfg, dict):
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
