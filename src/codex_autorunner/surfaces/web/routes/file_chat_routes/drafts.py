from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from .targets import (
    _hash_content,
    _load_state,
    _save_state,
    parse_target,
    read_file,
    resolve_repo_root,
)

if TYPE_CHECKING:
    from typing import Dict


async def pending_file_patch(request: Request, target: str) -> Dict[str, Any]:
    repo_root = resolve_repo_root(request)
    resolved = parse_target(repo_root, target)
    state = _load_state(repo_root)
    drafts = state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
    draft = drafts.get(resolved.state_key)
    if not draft:
        raise HTTPException(status_code=404, detail="No pending patch")
    current_content = read_file(resolved.path)
    current_hash = _hash_content(current_content)
    return {
        "status": "ok",
        "target": resolved.target,
        "patch": draft.get("patch", ""),
        "content": draft.get("content", ""),
        "agent_message": draft.get("agent_message", ""),
        "created_at": draft.get("created_at", ""),
        "base_hash": draft.get("base_hash", ""),
        "current_hash": current_hash,
        "is_stale": draft.get("base_hash") not in (None, "")
        and draft.get("base_hash") != current_hash,
    }


async def apply_file_patch(request: Request, body: dict[str, Any]) -> Dict[str, Any]:
    repo_root = resolve_repo_root(request)
    resolved = parse_target(repo_root, str(body.get("target") or ""))
    force = bool(body.get("force", False))
    state = _load_state(repo_root)
    drafts = state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
    draft = drafts.get(resolved.state_key)
    if not draft:
        raise HTTPException(status_code=404, detail="No pending patch")

    current = read_file(resolved.path)
    if (
        not force
        and draft.get("base_hash")
        and _hash_content(current) != draft["base_hash"]
    ):
        raise HTTPException(
            status_code=409,
            detail="File changed since draft created; reload before applying.",
        )

    from .....core.utils import atomic_write

    content = draft.get("content", "")
    resolved.path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(resolved.path, content)

    drafts.pop(resolved.state_key, None)
    state["drafts"] = drafts
    _save_state(repo_root, state)

    return {
        "status": "ok",
        "target": resolved.target,
        "content": read_file(resolved.path),
        "agent_message": draft.get("agent_message", "Draft applied"),
    }


async def discard_file_patch(request: Request, body: dict[str, Any]) -> Dict[str, Any]:
    repo_root = resolve_repo_root(request)
    resolved = parse_target(repo_root, str(body.get("target") or ""))
    state = _load_state(repo_root)
    drafts = state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
    drafts.pop(resolved.state_key, None)
    state["drafts"] = drafts
    _save_state(repo_root, state)
    return {
        "status": "ok",
        "target": resolved.target,
        "content": read_file(resolved.path),
    }
