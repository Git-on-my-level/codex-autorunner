from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .....core.flows import FlowStore

if TYPE_CHECKING:
    from . import FlowRoutesState

_logger = logging.getLogger(__name__)

_FLOW_DB_CORRUPT_SUFFIX = ".corrupt"
_FLOW_DB_NOTICE_SUFFIX = ".corrupt.json"


def flow_paths(repo_root: Path) -> tuple[Path, Path]:
    from ...services import flow_store as flow_store_service

    return flow_store_service.flow_paths(repo_root)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def is_probably_corrupt_flow_db_error(exc: Exception, db_path: Path) -> bool:
    if not isinstance(exc, sqlite3.Error):
        return False
    msg = str(exc).lower()
    if "file is not a database" in msg or "database disk image is malformed" in msg:
        return True
    if "disk i/o error" in msg:
        try:
            header = db_path.read_bytes()[:16]
        except Exception:
            return False
        return header not in (b"", b"SQLite format 3\x00")
    return False


def rotate_corrupt_flow_db(db_path: Path, detail: str) -> Optional[Path]:
    from .....core.utils import atomic_write

    stamp = utc_stamp()
    backup_path = db_path.with_name(f"{db_path.name}{_FLOW_DB_CORRUPT_SUFFIX}.{stamp}")
    notice_path = db_path.with_name(f"{db_path.name}{_FLOW_DB_NOTICE_SUFFIX}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup_value: str = ""
    if db_path.exists():
        try:
            db_path.replace(backup_path)
            backup_value = str(backup_path)
        except Exception:
            backup_value = ""

    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(f"{db_path.name}{suffix}")
        try:
            sidecar.unlink(missing_ok=True)
        except Exception:
            pass

    notice = {
        "status": "corrupt",
        "message": "Flow store reset due to corrupted flows.db.",
        "detail": detail,
        "detected_at": stamp,
        "backup_path": backup_value,
    }
    try:
        atomic_write(notice_path, json.dumps(notice, indent=2) + "\n")
    except Exception:
        _logger.warning("Failed to write flow DB corruption notice at %s", notice_path)
    return backup_path if backup_value else None


def evict_cached_controller(
    repo_root: Path, flow_type: str, state: FlowRoutesState
) -> None:
    key = (repo_root.resolve(), flow_type)
    with state.lock:
        controller = state.controller_cache.pop(key, None)
    if not controller:
        return
    try:
        controller.shutdown()
    except Exception:
        pass


def recover_flow_store_if_possible(
    repo_root: Path,
    flow_type: str,
    state: FlowRoutesState,
    exc: Exception,
) -> bool:
    db_path, _ = flow_paths(repo_root)
    if not is_probably_corrupt_flow_db_error(exc, db_path):
        return False

    backup_path = rotate_corrupt_flow_db(db_path, str(exc))
    evict_cached_controller(repo_root, flow_type, state)
    store = FlowStore(db_path)
    try:
        store.initialize()
        _logger.warning(
            "Recovered corrupted flow DB at %s (backup=%s, reason=%s)",
            db_path,
            str(backup_path) if backup_path else "unavailable",
            exc,
        )
        return True
    except Exception as recover_exc:
        _logger.warning(
            "Flow DB recovery failed at %s after error %s: %s",
            db_path,
            exc,
            recover_exc,
        )
        return False
    finally:
        try:
            store.close()
        except Exception:
            pass
