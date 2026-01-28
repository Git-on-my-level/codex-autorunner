from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional

from .path_utils import ConfigPathError, resolve_config_path

GLOBAL_STATE_ROOT_ENV = "CAR_GLOBAL_STATE_ROOT"


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

    return Path.home() / ".codex-autorunner"


def resolve_repo_state_root(repo_root: Path) -> Path:
    """Return the repo-local state root (.codex-autorunner)."""
    return repo_root / ".codex-autorunner"
