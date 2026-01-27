from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import atomic_write

FILE_CHAT_STATE_NAME = "file_chat_state.json"


def state_path(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / FILE_CHAT_STATE_NAME


def hash_content(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def load_state(repo_root: Path) -> Dict[str, Any]:
    path = state_path(repo_root)
    if not path.exists():
        return {"drafts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {"drafts": {}}
    except Exception:
        return {"drafts": {}}


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
