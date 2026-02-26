from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

_DISCORD_THREAD_BRANCH_RE = re.compile(r"^thread-[0-9]+$")
_TELEGRAM_THREAD_BRANCH_RE = re.compile(
    r"^thread-chat-[0-9]+-(thread-[0-9]+|msg-[0-9]+-upd-[0-9]+|unscoped)$"
)
_CHAT_MANAGED_REPO_ID_RE = re.compile(r".*--(?:discord|tg)-[0-9]+$")
_CHAT_MANAGED_PATH_MARKERS = {"chat-app-managed", "chat_app_managed"}


def _normalize_path_parts(path: Optional[Union[str, Path]]) -> set[str]:
    if path is None:
        return set()
    raw = str(path).strip()
    if not raw:
        return set()
    return {part.lower() for part in Path(raw).parts}


def is_chat_bound_worktree_identity(
    *,
    branch: Optional[str],
    repo_id: Optional[str],
    source_path: Optional[Union[str, Path]],
) -> bool:
    """Best-effort identity check for chat-managed worktrees.

    This intentionally accepts known chat-managed path/repo conventions so
    hub UI and cleanup safety guards work even when PMA thread bindings are
    absent or inactive.
    """

    path_parts = _normalize_path_parts(source_path)
    if _CHAT_MANAGED_PATH_MARKERS.intersection(path_parts):
        return True

    if isinstance(repo_id, str) and _CHAT_MANAGED_REPO_ID_RE.match(repo_id.strip()):
        return True

    if isinstance(branch, str):
        normalized_branch = branch.strip()
        if _DISCORD_THREAD_BRANCH_RE.match(normalized_branch):
            return True
        if _TELEGRAM_THREAD_BRANCH_RE.match(normalized_branch):
            return True

    return False
