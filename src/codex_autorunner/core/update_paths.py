from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .state_roots import resolve_global_state_root


@dataclass(frozen=True)
class UpdatePaths:
    status_path: Path
    lock_path: Path
    cache_dir: Path
    compact_status_path: Path


def resolve_update_paths(
    *, config: Optional[Any] = None, repo_root: Optional[Path] = None
) -> UpdatePaths:
    """Resolve update status, lock, cache, and compact status paths."""
    root = resolve_global_state_root(config=config, repo_root=repo_root)
    return UpdatePaths(
        status_path=root / "update_status.json",
        lock_path=root / "update.lock",
        cache_dir=root / "update_cache",
        compact_status_path=root / "compact_status.json",
    )
