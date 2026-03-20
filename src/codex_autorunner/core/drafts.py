from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import atomic_write

FILE_CHAT_STATE_NAME = "file_chat_state.json"
FILE_CHAT_STATE_CORRUPT_SUFFIX = ".corrupt"
FILE_CHAT_STATE_NOTICE_SUFFIX = ".corrupt.json"
FILE_CHAT_DRAFTS_DIR_NAME = "file-chat-drafts"

logger = logging.getLogger(__name__)


def state_path(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / FILE_CHAT_STATE_NAME


def drafts_root(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / FILE_CHAT_DRAFTS_DIR_NAME


def hash_content(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def draft_dir(repo_root: Path, state_key: str) -> Path:
    safe_key = re.sub(r"[^A-Za-z0-9._-]+", "_", state_key or "").strip("._")
    if not safe_key:
        safe_key = "draft"
    return drafts_root(repo_root) / safe_key


def draft_work_path(repo_root: Path, state_key: str, rel_path: str) -> Path:
    name = Path(rel_path or "").name or "draft.txt"
    return draft_dir(repo_root, state_key) / name


def draft_base_path(repo_root: Path, state_key: str, rel_path: str) -> Path:
    suffix = Path(rel_path or "").suffix or ".txt"
    return draft_dir(repo_root, state_key) / f".base{suffix}"


def write_draft_files(
    repo_root: Path,
    state_key: str,
    rel_path: str,
    *,
    base_content: str,
    working_content: str,
    preserve_base: bool = True,
) -> tuple[Path, Path]:
    work_path = draft_work_path(repo_root, state_key, rel_path)
    base_path = draft_base_path(repo_root, state_key, rel_path)
    work_path.parent.mkdir(parents=True, exist_ok=True)
    if not preserve_base or not base_path.exists():
        atomic_write(base_path, base_content)
    atomic_write(work_path, working_content)
    return work_path, base_path


def read_draft_working_content(
    repo_root: Path,
    draft: Dict[str, Any],
    *,
    state_key: str,
    rel_path: str,
) -> Optional[str]:
    work_rel_path = draft.get("draft_rel_path")
    candidates = []
    if isinstance(work_rel_path, str) and work_rel_path.strip():
        candidates.append(repo_root / work_rel_path)
    candidates.append(draft_work_path(repo_root, state_key, rel_path))
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    content = draft.get("content")
    return content if isinstance(content, str) else None


def read_draft_base_content(
    repo_root: Path,
    draft: Dict[str, Any],
    *,
    state_key: str,
    rel_path: str,
) -> Optional[str]:
    base_rel_path = draft.get("base_rel_path")
    candidates = []
    if isinstance(base_rel_path, str) and base_rel_path.strip():
        candidates.append(repo_root / base_rel_path)
    candidates.append(draft_base_path(repo_root, state_key, rel_path))
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    return None


def clear_draft_artifacts(repo_root: Path, state_key: str) -> None:
    path = draft_dir(repo_root, state_key)
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        logger.warning(
            "Failed to remove file chat draft artifacts at %s: %s", path, exc
        )


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
    clear_draft_artifacts(repo_root, state_key)
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
            clear_draft_artifacts(repo_root, key)
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
