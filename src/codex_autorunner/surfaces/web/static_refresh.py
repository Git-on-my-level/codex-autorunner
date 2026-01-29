from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ...core.logging_utils import safe_log
from .static_assets import (
    asset_version,
    materialize_static_assets,
    missing_static_assets,
    require_static_assets,
)


def _update_static_files(static_files: object, static_dir: Path) -> None:
    try:
        static_files.directory = static_dir
        static_files.all_directories = static_files.get_directories(  # type: ignore[attr-defined]
            static_dir,
            static_files.packages,  # type: ignore[attr-defined]
        )
        static_files.config_checked = False
    except Exception:
        return


def refresh_static_assets(app: object) -> bool:
    lock = getattr(getattr(app, "state", None), "static_assets_lock", None)
    if lock is None or not lock.acquire(blocking=False):
        return False
    try:
        state = getattr(app, "state", None)
        if state is None:
            return False
        current_dir = getattr(state, "static_dir", None)
        if isinstance(current_dir, Path) and not missing_static_assets(current_dir):
            return True
        config = getattr(state, "config", None)
        logger = getattr(state, "logger", None)
        static_candidates = []
        if config is not None:
            static_candidates.append(config.static_assets)
        hub_static = getattr(state, "hub_static_assets", None)
        if hub_static is not None and (
            not static_candidates
            or hub_static.cache_root != static_candidates[0].cache_root
        ):
            static_candidates.append(hub_static)
        for static_cfg in static_candidates:
            try:
                static_dir, static_context = materialize_static_assets(
                    static_cfg.cache_root,
                    max_cache_entries=static_cfg.max_cache_entries,
                    max_cache_age_days=static_cfg.max_cache_age_days,
                    logger=logger,
                )
                require_static_assets(static_dir, logger)
            except Exception as exc:
                if logger is not None:
                    safe_log(
                        logger,
                        logging.WARNING,
                        "Static assets refresh failed for cache root %s",
                        static_cfg.cache_root,
                        exc=exc,
                    )
                continue
            old_context: Optional[object] = getattr(
                state, "static_assets_context", None
            )
            if old_context is not None:
                try:
                    old_context.close()
                except Exception:
                    pass
            state.static_dir = static_dir
            state.static_assets_context = static_context
            state.asset_version = asset_version(static_dir)
            static_files = getattr(state, "static_files", None)
            if static_files is not None:
                _update_static_files(static_files, static_dir)
            return True
        return False
    finally:
        lock.release()
