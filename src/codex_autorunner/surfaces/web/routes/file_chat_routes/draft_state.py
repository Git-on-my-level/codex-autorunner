from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .....core import drafts as draft_utils
from .....core.state import now_iso
from .targets import (
    _hash_content,
    _load_state,
    _save_state,
    _Target,
    build_patch,
    read_file,
)


def relative_to_repo(repo_root: Path, path: Path) -> str:
    if path.is_relative_to(repo_root):
        return str(path.relative_to(repo_root))
    return str(path)


def load_draft_snapshot(
    repo_root: Path,
    target: _Target,
) -> tuple[Dict[str, Any], Dict[str, Any], str, str, str, str, str, Path]:
    state = _load_state(repo_root)
    drafts = state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
    existing = drafts.get(target.state_key)
    live_before = read_file(target.path)
    current_hash = _hash_content(live_before)
    draft_before = live_before
    base_content = live_before
    base_hash = current_hash
    created_at = now_iso()

    if isinstance(existing, dict):
        draft_before = (
            draft_utils.read_draft_working_content(
                repo_root,
                existing,
                state_key=target.state_key,
                rel_path=target.rel_path,
            )
            or draft_before
        )
        base_content = (
            draft_utils.read_draft_base_content(
                repo_root,
                existing,
                state_key=target.state_key,
                rel_path=target.rel_path,
            )
            or base_content
        )
        if existing.get("base_hash"):
            base_hash = str(existing["base_hash"])
        elif base_content == live_before:
            base_hash = current_hash
        created_at = str(existing.get("created_at") or created_at)

    work_path, _ = draft_utils.write_draft_files(
        repo_root,
        target.state_key,
        target.rel_path,
        base_content=base_content,
        working_content=draft_before,
        preserve_base=bool(existing),
    )
    return (
        state,
        drafts,
        live_before,
        draft_before,
        base_content,
        base_hash,
        created_at,
        work_path,
    )


def persist_draft(
    repo_root: Path,
    target: _Target,
    *,
    state: Dict[str, Any],
    drafts: Dict[str, Any],
    content: str,
    base_content: str,
    base_hash: str,
    created_at: str,
    agent_message: str,
) -> Dict[str, Any]:
    work_path = draft_utils.draft_work_path(
        repo_root, target.state_key, target.rel_path
    )
    base_path = draft_utils.draft_base_path(
        repo_root, target.state_key, target.rel_path
    )
    patch = build_patch(target.rel_path, base_content, content)
    draft = {
        "content": content,
        "patch": patch,
        "agent_message": agent_message,
        "created_at": created_at,
        "base_hash": base_hash,
        "target": target.target,
        "rel_path": target.rel_path,
        "draft_rel_path": relative_to_repo(repo_root, work_path),
        "base_rel_path": relative_to_repo(repo_root, base_path),
    }
    drafts[target.state_key] = draft
    state["drafts"] = drafts
    _save_state(repo_root, state)
    return draft
