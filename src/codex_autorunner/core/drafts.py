from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import atomic_write

FILE_CHAT_STATE_NAME = "file_chat_state.json"
FILE_CHAT_STATE_CORRUPT_SUFFIX = ".corrupt"
FILE_CHAT_STATE_NOTICE_SUFFIX = ".corrupt.json"

logger = logging.getLogger(__name__)


def state_path(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / FILE_CHAT_STATE_NAME


def hash_content(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def load_state(repo_root: Path) -> Dict[str, Any]:
    path = state_path(repo_root)
    if not path.exists():
        return {"drafts": {}}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read file chat state at %s: %s", path, exc)
        return {"drafts": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _handle_corrupt_state(path, str(exc))
        return {"drafts": {}}
    if not isinstance(data, dict):
        _handle_corrupt_state(path, f"Expected JSON object, got {type(data).__name__}")
        return {"drafts": {}}
    return data


def save_state(repo_root: Path, state: Dict[str, Any]) -> None:
    path = state_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, indent=2) + "\n")


def load_drafts(repo_root: Path) -> Dict[str, Any]:
    state = load_state(repo_root)
    drafts = state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
    return drafts


def save_drafts(repo_root: Path, drafts: Dict[str, Any]) -> None:
    state = load_state(repo_root)
    state["drafts"] = drafts
    save_state(repo_root, state)


def remove_draft(repo_root: Path, state_key: str) -> Optional[Dict[str, Any]]:
    drafts = load_drafts(repo_root)
    removed = drafts.pop(state_key, None)
    save_drafts(repo_root, drafts)
    return removed if isinstance(removed, dict) else None


def invalidate_drafts_for_path(repo_root: Path, rel_path: str) -> list[str]:
    """Remove any drafts that target the provided repo-relative path."""

    def _norm(value: str) -> str:
        try:
            return Path(value).as_posix().lstrip("./")
        except Exception:
            return value

    target_norm = _norm(rel_path)

    drafts = load_drafts(repo_root)
    removed_keys: list[str] = []
    for key, value in list(drafts.items()):
        if not isinstance(value, dict):
            continue
        candidate = _norm(str(value.get("rel_path", "")))
        if candidate == target_norm:
            drafts.pop(key, None)
            removed_keys.append(key)

    if removed_keys:
        save_drafts(repo_root, drafts)
    return removed_keys


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _notice_path(path: Path) -> Path:
    return path.with_name(f"{path.name}{FILE_CHAT_STATE_NOTICE_SUFFIX}")


def _handle_corrupt_state(path: Path, detail: str) -> None:
    stamp = _stamp()
    backup_path = path.with_name(f"{path.name}{FILE_CHAT_STATE_CORRUPT_SUFFIX}.{stamp}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.replace(backup_path)
        backup_value = str(backup_path)
    except OSError:
        backup_value = ""
    notice = {
        "status": "corrupt",
        "message": "Draft state reset due to corrupted file_chat_state.json.",
        "detail": detail,
        "detected_at": stamp,
        "backup_path": backup_value,
    }
    notice_path = _notice_path(path)
    try:
        atomic_write(notice_path, json.dumps(notice, indent=2) + "\n")
    except Exception:
        logger.warning("Failed to write draft corruption notice at %s", notice_path)
    try:
        atomic_write(path, json.dumps({"drafts": {}}, indent=2) + "\n")
    except Exception:
        logger.warning("Failed to reset draft state at %s", path)
    logger.warning(
        "Corrupted file chat state detected; backup=%s notice=%s detail=%s",
        backup_value or "unavailable",
        notice_path,
        detail,
    )
