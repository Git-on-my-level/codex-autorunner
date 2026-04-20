"""Typed builder and loader functions for RepoConfig and HubConfig.

These builders are the single ownership point for assembling validated,
layered config dicts into the final typed config dataclasses.  They delegate
section parsing to ``config_parsers`` and type definitions live in
``config_types``.

The public facade in ``core.config`` re-exports the load functions from
this module; callers should import from ``core.config`` for the stable
public API.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional, cast

from ..housekeeping import parse_housekeeping_config
from ..manifest import ManifestError, load_manifest
from .agent_config import parse_agents_config
from .config_contract import CONFIG_VERSION, ConfigError
from .config_env import load_dotenv_for_root
from .config_layering import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    DEFAULT_REPO_CONFIG,
    _load_yaml_dict,
    derive_repo_config_data,
    find_nearest_hub_config_path,
    resolve_hub_config_data,
)
from .config_parsers import (
    _parse_app_server_config,
    _parse_destination_config_section,
    _parse_notifications_config_section,
    _parse_opencode_config,
    _parse_pma_config,
    _parse_security_config_section,
    _parse_static_assets_config,
    _parse_templates_config,
    _parse_ticket_flow_config,
    _parse_update_backend,
    _parse_update_linux_service_names,
    _parse_usage_config,
    _parse_voice_config_section,
    normalize_base_path,
    parse_flow_retention_config,
)
from .config_types import (
    DestinationConfigSection,
    HubConfig,
    LogConfig,
    RepoConfig,
)
from .config_validation import _validate_hub_config, _validate_repo_config
from .destinations import default_local_destination, resolve_effective_repo_destination
from .generated_hub_config import normalize_generated_hub_config
from .path_utils import ConfigPathError, resolve_config_path
from .utils import find_repo_root

ACTIVE_HUB_ROOT_ENV = "CAR_HUB_ROOT"


def _parse_agents_config(
    cfg: Dict[str, Any], defaults: Dict[str, Any]
) -> Dict[str, Any]:
    return parse_agents_config(cfg, defaults)


def _resolve_hub_config_path(start: Path) -> Path:
    config_path = find_nearest_hub_config_path(start)
    if not config_path:
        raise ConfigError(
            f"Missing hub config file; expected to find {CONFIG_FILENAME} in {start} or parents "
            "(pass --path on most commands, --hub-root on car render, or run 'car init' to initialize)"
        )
    return config_path


def _resolve_repo_root(start: Path) -> Path:
    search_dir = start.resolve() if start.is_dir() else start.resolve().parent
    for current in [search_dir] + list(search_dir.parents):
        if (current / ".codex-autorunner" / "state.sqlite3").exists():
            return current
        if (current / ".git").exists():
            return current
    return search_dir


def load_hub_config_data(config_path: Path) -> Dict[str, Any]:
    """Load, merge, and return a raw hub config dict for the given config path."""
    load_dotenv_for_root(config_path.parent.parent.resolve())
    data = normalize_generated_hub_config(config_path)
    mode = data.get("mode")
    if mode not in (None, "hub"):
        raise ConfigError(f"Invalid mode '{mode}'; expected 'hub'")
    root = config_path.parent.parent.resolve()
    return resolve_hub_config_data(root, data)


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
        target_root = find_repo_root(start)
    except Exception:
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
    return build_hub_config(config_path, merged)


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
    except Exception as exc:
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


def derive_repo_config(
    hub: HubConfig, repo_root: Path, *, load_env: bool = True
) -> RepoConfig:
    if load_env:
        load_dotenv_for_root(repo_root)
    merged = derive_repo_config_data(hub.raw, repo_root)
    merged["mode"] = "repo"
    merged["version"] = CONFIG_VERSION
    _validate_repo_config(merged, root=repo_root)
    repo_config = build_repo_config(repo_root / CONFIG_FILENAME, merged)
    repo_config.effective_destination = _resolve_repo_effective_destination(
        hub, repo_root
    )
    return repo_config


def load_repo_config(start: Path, hub_path: Optional[Path] = None) -> RepoConfig:
    """Load a repo config by deriving it from the nearest hub config."""
    repo_root = _resolve_repo_root(start)
    hub_config_path = _resolve_hub_path_for_repo(repo_root, hub_path)
    hub_config = load_hub_config_data(hub_config_path)
    _validate_hub_config(hub_config, root=hub_config_path.parent.parent.resolve())
    hub = build_hub_config(hub_config_path, hub_config)
    return derive_repo_config(hub, repo_root)


def build_repo_config(config_path: Path, cfg: Dict[str, Any]) -> RepoConfig:
    root = config_path.parent.parent.resolve()
    docs = {
        "active_context": Path(cfg["docs"]["active_context"]),
        "decisions": Path(cfg["docs"]["decisions"]),
        "spec": Path(cfg["docs"]["spec"]),
    }
    voice_cfg = _parse_voice_config_section(cfg.get("voice"))
    template_val = cfg["prompt"].get("template")
    template = root / template_val if template_val else None
    term_args = cfg["codex"].get("terminal_args") or []
    terminal_cfg = cfg.get("terminal") if isinstance(cfg.get("terminal"), dict) else {}
    terminal_cfg = cast(Dict[str, Any], terminal_cfg)
    idle_timeout_value = terminal_cfg.get("idle_timeout_seconds")
    idle_timeout_seconds: Optional[int]
    if idle_timeout_value is None:
        idle_timeout_seconds = None
    else:
        idle_timeout_seconds = int(idle_timeout_value)
        if idle_timeout_seconds <= 0:
            idle_timeout_seconds = None
    notifications_cfg = _parse_notifications_config_section(cfg.get("notifications"))
    security_cfg = _parse_security_config_section(cfg.get("security"))
    log_cfg = cfg.get("log", {})
    log_cfg = cast(Dict[str, Any], log_cfg if isinstance(log_cfg, dict) else {})
    server_log_cfg = cfg.get("server_log", {}) or {}
    server_log_cfg = cast(
        Dict[str, Any], server_log_cfg if isinstance(server_log_cfg, dict) else {}
    )
    update_cfg = cfg.get("update")
    update_cfg = cast(
        Dict[str, Any], update_cfg if isinstance(update_cfg, dict) else {}
    )
    update_skip_checks = bool(update_cfg.get("skip_checks", False))
    update_backend = _parse_update_backend(update_cfg)
    update_linux_service_names = _parse_update_linux_service_names(update_cfg)
    autorunner_cfg = cfg.get("autorunner")
    autorunner_cfg = cast(
        Dict[str, Any], autorunner_cfg if isinstance(autorunner_cfg, dict) else {}
    )
    reuse_session_value = autorunner_cfg.get("reuse_session")
    autorunner_reuse_session = (
        bool(reuse_session_value) if reuse_session_value is not None else False
    )
    storage_cfg = cfg.get("storage")
    storage_cfg = cast(
        Dict[str, Any], storage_cfg if isinstance(storage_cfg, dict) else {}
    )
    durable_writes = bool(storage_cfg.get("durable_writes", False))
    return RepoConfig(
        raw=cfg,
        root=root,
        version=int(cfg["version"]),
        mode="repo",
        docs=docs,
        codex_binary=cfg["codex"]["binary"],
        codex_args=list(cfg["codex"].get("args", [])),
        codex_terminal_args=list(term_args) if isinstance(term_args, list) else [],
        codex_model=cfg["codex"].get("model"),
        codex_reasoning=cfg["codex"].get("reasoning"),
        agents=_parse_agents_config(cfg, DEFAULT_REPO_CONFIG),
        prompt_prev_run_max_chars=int(cfg["prompt"]["prev_run_max_chars"]),
        prompt_template=template,
        runner_sleep_seconds=int(cfg["runner"]["sleep_seconds"]),
        runner_stop_after_runs=cfg["runner"].get("stop_after_runs"),
        runner_max_wallclock_seconds=cfg["runner"].get("max_wallclock_seconds"),
        runner_no_progress_threshold=int(cfg["runner"].get("no_progress_threshold", 3)),
        autorunner_reuse_session=autorunner_reuse_session,
        git_auto_commit=bool(cfg["git"].get("auto_commit", False)),
        git_commit_message_template=str(cfg["git"].get("commit_message_template")),
        update_skip_checks=update_skip_checks,
        update_backend=update_backend,
        update_linux_service_names=update_linux_service_names,
        ticket_flow=_parse_ticket_flow_config(
            cfg.get("ticket_flow"),
            cast(Dict[str, Any], DEFAULT_REPO_CONFIG.get("ticket_flow")),
        ),
        app_server=_parse_app_server_config(
            cfg.get("app_server"),
            root,
            DEFAULT_REPO_CONFIG["app_server"],
        ),
        opencode=_parse_opencode_config(
            cfg.get("opencode"), root, DEFAULT_REPO_CONFIG.get("opencode")
        ),
        pma=_parse_pma_config(cfg.get("pma"), root, DEFAULT_HUB_CONFIG.get("pma")),
        usage=_parse_usage_config(
            cfg.get("usage"), root, DEFAULT_REPO_CONFIG.get("usage")
        ),
        security=security_cfg,
        server_host=str(cfg["server"].get("host")),
        server_port=int(cfg["server"].get("port")),
        server_base_path=normalize_base_path(cfg["server"].get("base_path", "")),
        server_access_log=bool(cfg["server"].get("access_log", False)),
        server_auth_token_env=str(cfg["server"].get("auth_token_env", "")),
        server_allowed_hosts=list(cfg["server"].get("allowed_hosts") or []),
        server_allowed_origins=list(cfg["server"].get("allowed_origins") or []),
        notifications=notifications_cfg,
        terminal_idle_timeout_seconds=idle_timeout_seconds,
        log=LogConfig(
            path=root / log_cfg.get("path", DEFAULT_REPO_CONFIG["log"]["path"]),
            max_bytes=int(
                log_cfg.get("max_bytes", DEFAULT_REPO_CONFIG["log"]["max_bytes"])
            ),
            backup_count=int(
                log_cfg.get("backup_count", DEFAULT_REPO_CONFIG["log"]["backup_count"])
            ),
        ),
        server_log=LogConfig(
            path=root
            / server_log_cfg.get("path", DEFAULT_REPO_CONFIG["server_log"]["path"]),
            max_bytes=int(
                server_log_cfg.get(
                    "max_bytes", DEFAULT_REPO_CONFIG["server_log"]["max_bytes"]
                )
            ),
            backup_count=int(
                server_log_cfg.get(
                    "backup_count",
                    DEFAULT_REPO_CONFIG["server_log"]["backup_count"],
                )
            ),
        ),
        voice=voice_cfg,
        static_assets=_parse_static_assets_config(
            cfg.get("static_assets"), root, DEFAULT_REPO_CONFIG["static_assets"]
        ),
        housekeeping=parse_housekeeping_config(cfg.get("housekeeping")),
        flow_retention=parse_flow_retention_config(cfg.get("flow_retention")),
        durable_writes=durable_writes,
        templates=_parse_templates_config(
            cfg.get("templates"), DEFAULT_HUB_CONFIG.get("templates")
        ),
    )


def build_hub_config(config_path: Path, cfg: Dict[str, Any]) -> HubConfig:
    root = config_path.parent.parent.resolve()
    hub_cfg = cfg["hub"]
    log_cfg = hub_cfg["log"]
    server_log_cfg = cfg.get("server_log")
    if not isinstance(server_log_cfg, dict):
        server_log_cfg = {
            "path": log_cfg["path"],
            "max_bytes": log_cfg["max_bytes"],
            "backup_count": log_cfg["backup_count"],
        }

    log_path_str = log_cfg["path"]
    try:
        log_path = resolve_config_path(log_path_str, root, scope="log.path")
    except ConfigPathError as exc:
        raise ConfigError(str(exc)) from exc

    server_log_path_str = str(server_log_cfg.get("path", log_cfg["path"]))
    try:
        server_log_path = resolve_config_path(
            server_log_path_str,
            root,
            scope="server_log.path",
        )
    except ConfigPathError as exc:
        raise ConfigError(str(exc)) from exc

    update_cfg = cfg.get("update")
    update_cfg = cast(
        Dict[str, Any], update_cfg if isinstance(update_cfg, dict) else {}
    )
    update_skip_checks = bool(update_cfg.get("skip_checks", False))
    update_backend = _parse_update_backend(update_cfg)
    update_linux_service_names = _parse_update_linux_service_names(update_cfg)
    storage_cfg = cfg.get("storage")
    storage_cfg = cast(
        Dict[str, Any], storage_cfg if isinstance(storage_cfg, dict) else {}
    )
    durable_writes = bool(storage_cfg.get("durable_writes", False))

    return HubConfig(
        raw=cfg,
        root=root,
        version=int(cfg["version"]),
        mode="hub",
        repo_defaults=cast(Dict[str, Any], cfg.get("repo_defaults") or {}),
        agents=_parse_agents_config(cfg, DEFAULT_HUB_CONFIG),
        templates=_parse_templates_config(
            cfg.get("templates"), DEFAULT_HUB_CONFIG.get("templates")
        ),
        repos_root=(root / hub_cfg["repos_root"]).resolve(),
        worktrees_root=(root / hub_cfg["worktrees_root"]).resolve(),
        manifest_path=root / hub_cfg["manifest"],
        discover_depth=int(hub_cfg["discover_depth"]),
        auto_init_missing=bool(hub_cfg["auto_init_missing"]),
        include_root_repo=bool(hub_cfg.get("include_root_repo", False)),
        repo_server_inherit=bool(hub_cfg.get("repo_server_inherit", True)),
        update_repo_url=str(hub_cfg.get("update_repo_url", "")),
        update_repo_ref=str(hub_cfg.get("update_repo_ref", "main")),
        update_skip_checks=update_skip_checks,
        update_backend=update_backend,
        update_linux_service_names=update_linux_service_names,
        durable_writes=durable_writes,
        app_server=_parse_app_server_config(
            cfg.get("app_server"),
            root,
            DEFAULT_HUB_CONFIG["app_server"],
        ),
        opencode=_parse_opencode_config(
            cfg.get("opencode"), root, DEFAULT_HUB_CONFIG.get("opencode")
        ),
        pma=_parse_pma_config(cfg.get("pma"), root, DEFAULT_HUB_CONFIG.get("pma")),
        usage=_parse_usage_config(
            cfg.get("usage"), root, DEFAULT_HUB_CONFIG.get("usage")
        ),
        server_host=str(cfg["server"]["host"]),
        server_port=int(cfg["server"].get("port")),
        server_base_path=normalize_base_path(cfg["server"].get("base_path", "")),
        server_access_log=bool(cfg["server"].get("access_log", False)),
        server_auth_token_env=str(cfg["server"].get("auth_token_env", "")),
        server_allowed_hosts=list(cfg["server"].get("allowed_hosts") or []),
        server_allowed_origins=list(cfg["server"].get("allowed_origins") or []),
        log=LogConfig(
            path=log_path,
            max_bytes=int(log_cfg["max_bytes"]),
            backup_count=int(log_cfg["backup_count"]),
        ),
        server_log=LogConfig(
            path=server_log_path,
            max_bytes=int(server_log_cfg.get("max_bytes", log_cfg["max_bytes"])),
            backup_count=int(
                server_log_cfg.get("backup_count", log_cfg["backup_count"])
            ),
        ),
        static_assets=_parse_static_assets_config(
            cfg.get("static_assets"), root, DEFAULT_HUB_CONFIG["static_assets"]
        ),
        housekeeping=parse_housekeeping_config(cfg.get("housekeeping")),
    )
