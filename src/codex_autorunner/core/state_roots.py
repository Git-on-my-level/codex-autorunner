"""
State Roots Authority Module

This module provides the single authority for resolving all canonical state
roots in Codex Autorunner. See docs/STATE_ROOTS.md for the full contract.

Canonical roots:
- Repo-local: <repo_root>/.codex-autorunner/
- Hub: <hub_root>/.codex-autorunner/
- Global: ~/.codex-autorunner/ (configurable)

Non-canonical (caches):
- System temp directories (ephemeral, disposable)
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Sequence

from .path_utils import ConfigPathError, resolve_config_path

GLOBAL_STATE_ROOT_ENV = "CAR_GLOBAL_STATE_ROOT"

REPO_STATE_DIR = ".codex-autorunner"
FLOWS_DB_FILENAME = "flows.db"
RUNNER_STATE_DB_FILENAME = "state.sqlite3"
DISCORD_STATE_DB_FILENAME = "discord_state.sqlite3"
TELEGRAM_STATE_DB_FILENAME = "telegram_state.sqlite3"
ORCHESTRATION_DB_FILENAME = "orchestration.sqlite3"
ORCHESTRATION_COMPATIBILITY_METADATA_FILENAME = "orchestration-compatibility.json"
HUB_PROJECTION_DB_FILENAME = "hub_projection.sqlite3"
HUB_MANIFEST_FILENAME = "manifest.yml"
LIFECYCLE_EVENTS_FILENAME = "lifecycle_events.json"
LIFECYCLE_EVENTS_DB_FILENAME = "lifecycle_events.sqlite3"
PMA_THREADS_DB_FILENAME = "threads.sqlite3"
GITHUB_BROKER_DB_FILENAME = "github-cli.sqlite3"
_SAFE_HUB_RESOURCE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class StateRootError(Exception):
    """Raised when a path violates the state root contract."""

    def __init__(
        self,
        message: str,
        *,
        path: Optional[Path] = None,
        allowed_roots: Optional[Sequence[Path]] = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.allowed_roots = list(allowed_roots) if allowed_roots else []


def _read_global_root_from_config(raw: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not raw:
        return None
    state_roots = raw.get("state_roots")
    if not isinstance(state_roots, Mapping):
        return None
    value = state_roots.get("global")
    return value if isinstance(value, str) else None


def resolve_global_state_root(
    *,
    config: Optional[Any] = None,
    repo_root: Optional[Path] = None,
    scope: str = "state_roots.global",
) -> Path:
    """Resolve the global state root used for cross-repo caches and locks."""
    base_root = repo_root
    raw_config = None
    if config is not None:
        base_root = getattr(config, "root", None) or base_root
        raw_config = getattr(config, "raw", None)

    if base_root is None:
        base_root = Path.cwd()

    env_value = os.environ.get(GLOBAL_STATE_ROOT_ENV)
    raw_value = env_value or _read_global_root_from_config(raw_config)
    if raw_value:
        try:
            return resolve_config_path(
                raw_value,
                base_root,
                allow_absolute=True,
                allow_home=True,
                scope=scope,
            )
        except ConfigPathError as exc:
            raise ConfigPathError(str(exc), path=raw_value, scope=scope) from exc

    return Path.home() / REPO_STATE_DIR


def resolve_repo_state_root(repo_root: Path) -> Path:
    """Return the repo-local state root (.codex-autorunner)."""
    return repo_root / REPO_STATE_DIR


def resolve_repo_flows_db_path(repo_root: Path) -> Path:
    """Return the canonical repo-local flow store SQLite path."""
    return resolve_repo_state_root(repo_root) / FLOWS_DB_FILENAME


def resolve_repo_runner_state_db_path(repo_root: Path) -> Path:
    """Return the canonical repo-local runner state SQLite path."""
    return resolve_repo_state_root(repo_root) / RUNNER_STATE_DB_FILENAME


def resolve_hub_state_root(hub_root: Path) -> Path:
    """Return the hub-scoped state root."""
    return hub_root / REPO_STATE_DIR


def _resolve_hub_configured_state_path(
    hub_root: Path,
    *,
    raw_config: Optional[Mapping[str, Any]],
    section: str,
    default_filename: str,
) -> Path:
    section_cfg = raw_config.get(section) if isinstance(raw_config, Mapping) else {}
    if not isinstance(section_cfg, Mapping):
        section_cfg = {}
    configured = section_cfg.get("state_file")
    state_file = (
        configured
        if isinstance(configured, str) and configured.strip()
        else (str(resolve_hub_state_root(hub_root) / default_filename))
    )
    return resolve_config_path(
        state_file,
        hub_root,
        allow_absolute=True,
        allow_home=True,
        scope=f"{section}.state_file",
    )


def resolve_hub_manifest_path(
    hub_root: Path,
    *,
    raw_config: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Return the canonical hub manifest path, honoring config overrides."""
    hub_cfg = raw_config.get("hub") if isinstance(raw_config, Mapping) else {}
    if not isinstance(hub_cfg, Mapping):
        hub_cfg = {}
    configured = hub_cfg.get("manifest")
    manifest = (
        configured
        if isinstance(configured, str) and configured.strip()
        else (str(resolve_hub_state_root(hub_root) / HUB_MANIFEST_FILENAME))
    )
    return resolve_config_path(
        manifest,
        hub_root,
        allow_absolute=True,
        allow_home=True,
        scope="hub.manifest",
    )


def resolve_hub_orchestration_db_path(hub_root: Path) -> Path:
    """Return the canonical orchestration SQLite path under the hub state root."""
    state_root = resolve_hub_state_root(hub_root)
    validate_path_within_roots(
        state_root,
        allowed_roots=get_canonical_roots(hub_root=hub_root),
        resolve=False,
    )
    return state_root / ORCHESTRATION_DB_FILENAME


def resolve_hub_orchestration_compatibility_metadata_path(hub_root: Path) -> Path:
    """Return the canonical orchestration compatibility metadata path."""
    state_root = resolve_hub_state_root(hub_root)
    validate_path_within_roots(
        state_root,
        allowed_roots=get_canonical_roots(hub_root=hub_root),
        resolve=False,
    )
    return state_root / ORCHESTRATION_COMPATIBILITY_METADATA_FILENAME


def resolve_hub_projection_db_path(hub_root: Path) -> Path:
    """Return the canonical hub projection cache SQLite path."""
    state_root = resolve_hub_state_root(hub_root)
    validate_path_within_roots(
        state_root,
        allowed_roots=get_canonical_roots(hub_root=hub_root),
        resolve=False,
    )
    return state_root / HUB_PROJECTION_DB_FILENAME


def resolve_hub_lifecycle_events_path(hub_root: Path) -> Path:
    """Return the legacy hub lifecycle JSON path."""
    return resolve_hub_state_root(hub_root) / LIFECYCLE_EVENTS_FILENAME


def resolve_hub_lifecycle_events_db_path(hub_root: Path) -> Path:
    """Return the legacy hub lifecycle SQLite path."""
    return resolve_hub_state_root(hub_root) / LIFECYCLE_EVENTS_DB_FILENAME


def resolve_hub_templates_root(hub_root: Path) -> Path:
    """Return the hub-scoped templates root."""
    return resolve_hub_state_root(hub_root) / "templates"


def resolve_hub_pma_root(hub_root: Path) -> Path:
    """Return the hub-scoped PMA compatibility root."""
    return resolve_hub_state_root(hub_root) / "pma"


def resolve_hub_pma_threads_db_path(hub_root: Path) -> Path:
    """Return the PMA compatibility SQLite path."""
    return resolve_hub_pma_root(hub_root) / PMA_THREADS_DB_FILENAME


def resolve_discord_state_path(
    hub_root: Path,
    raw_config: Optional[Mapping[str, Any]],
) -> Path:
    """Return the configured Discord transport-state SQLite path."""
    return _resolve_hub_configured_state_path(
        hub_root,
        raw_config=raw_config,
        section="discord_bot",
        default_filename=DISCORD_STATE_DB_FILENAME,
    )


def resolve_telegram_state_path(
    hub_root: Path,
    raw_config: Optional[Mapping[str, Any]],
) -> Path:
    """Return the configured Telegram transport-state SQLite path."""
    return _resolve_hub_configured_state_path(
        hub_root,
        raw_config=raw_config,
        section="telegram_bot",
        default_filename=TELEGRAM_STATE_DB_FILENAME,
    )


def _validate_hub_resource_segment(value: str, *, label: str) -> str:
    normalized = (value or "").strip()
    if not normalized or not _SAFE_HUB_RESOURCE_SEGMENT.match(normalized):
        raise StateRootError(
            f"{label} must be a non-empty path-safe identifier",
        )
    return normalized


def resolve_hub_runtimes_root(hub_root: Path) -> Path:
    """Return the hub-scoped root for CAR-managed runtime workspaces."""
    return resolve_hub_state_root(hub_root) / "runtimes"


def resolve_hub_traces_root(hub_root: Path) -> Path:
    """Return the hub-scoped root for cold trace artifacts."""
    return resolve_hub_state_root(hub_root) / "traces"


def resolve_hub_runtime_root(hub_root: Path, *, runtime: str) -> Path:
    """Return the managed root for a specific runtime under the hub."""
    runtime_id = _validate_hub_resource_segment(runtime, label="runtime")
    root = resolve_hub_runtimes_root(hub_root)
    path = root / runtime_id
    validate_path_within_roots(path, allowed_roots=[root], resolve=False)
    return path


def resolve_hub_agent_workspace_root(
    hub_root: Path,
    *,
    runtime: str,
    workspace_id: str,
) -> Path:
    """Return the canonical root for a managed agent workspace."""
    runtime_root = resolve_hub_runtime_root(hub_root, runtime=runtime)
    workspace_segment = _validate_hub_resource_segment(
        workspace_id, label="workspace_id"
    )
    path = runtime_root / workspace_segment
    validate_path_within_roots(path, allowed_roots=[runtime_root], resolve=False)
    return path


def resolve_cache_root() -> Path:
    """Return the system temp directory for non-canonical caches.

    WARNING: This is explicitly non-canonical. Data here is ephemeral and
    disposable. Never store durable artifacts in the cache root.
    """
    return Path(os.environ.get("TMPDIR", "/tmp"))


def resolve_global_github_broker_db_path(
    *,
    config: Optional[Any] = None,
    repo_root: Optional[Path] = None,
) -> Path:
    """Return the shared GitHub broker SQLite path under the global state root."""
    return (
        resolve_global_state_root(config=config, repo_root=repo_root)
        / "github"
        / GITHUB_BROKER_DB_FILENAME
    )


def is_within_allowed_root(
    path: Path,
    *,
    allowed_roots: Sequence[Path],
    resolve: bool = True,
) -> bool:
    """Check if a path is within one of the allowed roots.

    Args:
        path: Path to check
        allowed_roots: Sequence of allowed root directories
        resolve: If True, resolve symlinks before checking

    Returns:
        True if path is within an allowed root
    """
    check_path = path.resolve() if resolve else path
    for root in allowed_roots:
        check_root = root.resolve() if resolve else root
        try:
            check_path.relative_to(check_root)
            return True
        except ValueError:
            continue
    return False


def validate_path_within_roots(
    path: Path,
    *,
    allowed_roots: Sequence[Path],
    resolve: bool = True,
) -> Literal[True]:
    """Validate that a path is within one of the allowed roots.

    Args:
        path: Path to validate
        allowed_roots: Sequence of allowed root directories
        resolve: If True, resolve symlinks before checking

    Returns:
        True if validation passes

    Raises:
        StateRootError: If path is outside all allowed roots
    """
    if is_within_allowed_root(path, allowed_roots=allowed_roots, resolve=resolve):
        return True
    raise StateRootError(
        f"Path '{path}' is outside allowed state roots",
        path=path,
        allowed_roots=allowed_roots,
    )


def get_canonical_roots(
    repo_root: Optional[Path] = None,
    hub_root: Optional[Path] = None,
    global_root: Optional[Path] = None,
) -> list[Path]:
    """Get the list of canonical state roots for validation.

    Args:
        repo_root: Repository root to include when validating repo-local state
        hub_root: Hub root (optional)
        global_root: Global root (uses default if None)

    Returns:
        List of canonical root paths
    """
    roots: list[Path] = []

    if global_root is None:
        global_root = resolve_global_state_root()
    roots.append(global_root)

    if repo_root is not None:
        roots.append(resolve_repo_state_root(repo_root))

    if hub_root is not None:
        roots.append(resolve_hub_state_root(hub_root))

    cache_root = resolve_cache_root()
    return [root for root in roots if root != cache_root]


__all__ = [
    "DISCORD_STATE_DB_FILENAME",
    "FLOWS_DB_FILENAME",
    "GLOBAL_STATE_ROOT_ENV",
    "GITHUB_BROKER_DB_FILENAME",
    "ORCHESTRATION_DB_FILENAME",
    "HUB_PROJECTION_DB_FILENAME",
    "HUB_MANIFEST_FILENAME",
    "LIFECYCLE_EVENTS_DB_FILENAME",
    "LIFECYCLE_EVENTS_FILENAME",
    "PMA_THREADS_DB_FILENAME",
    "REPO_STATE_DIR",
    "RUNNER_STATE_DB_FILENAME",
    "StateRootError",
    "TELEGRAM_STATE_DB_FILENAME",
    "get_canonical_roots",
    "is_within_allowed_root",
    "resolve_discord_state_path",
    "resolve_cache_root",
    "resolve_global_state_root",
    "resolve_global_github_broker_db_path",
    "resolve_hub_agent_workspace_root",
    "resolve_hub_lifecycle_events_db_path",
    "resolve_hub_lifecycle_events_path",
    "resolve_hub_manifest_path",
    "resolve_hub_orchestration_db_path",
    "resolve_hub_pma_root",
    "resolve_hub_pma_threads_db_path",
    "resolve_hub_projection_db_path",
    "resolve_hub_runtime_root",
    "resolve_hub_runtimes_root",
    "resolve_hub_state_root",
    "resolve_hub_templates_root",
    "resolve_hub_traces_root",
    "resolve_repo_flows_db_path",
    "resolve_repo_runner_state_db_path",
    "resolve_repo_state_root",
    "resolve_telegram_state_path",
    "validate_path_within_roots",
]
