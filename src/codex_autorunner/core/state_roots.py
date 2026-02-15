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
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Sequence

from .path_utils import ConfigPathError, resolve_config_path

GLOBAL_STATE_ROOT_ENV = "CAR_GLOBAL_STATE_ROOT"

REPO_STATE_DIR = ".codex-autorunner"


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


def resolve_hub_state_root(hub_root: Path) -> Path:
    """Return the hub-scoped state root."""
    return hub_root / REPO_STATE_DIR


def resolve_hub_templates_root(hub_root: Path) -> Path:
    """Return the hub-scoped templates root."""
    return resolve_hub_state_root(hub_root) / "templates"


def resolve_cache_root() -> Path:
    """Return the system temp directory for non-canonical caches.

    WARNING: This is explicitly non-canonical. Data here is ephemeral and
    disposable. Never store durable artifacts in the cache root.
    """
    return Path(os.environ.get("TMPDIR", "/tmp"))


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
        repo_root: Repository root (uses cwd if None)
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

    return roots


__all__ = [
    "GLOBAL_STATE_ROOT_ENV",
    "REPO_STATE_DIR",
    "StateRootError",
    "resolve_global_state_root",
    "resolve_repo_state_root",
    "resolve_hub_state_root",
    "resolve_hub_templates_root",
    "resolve_cache_root",
    "is_within_allowed_root",
    "validate_path_within_roots",
    "get_canonical_roots",
]
