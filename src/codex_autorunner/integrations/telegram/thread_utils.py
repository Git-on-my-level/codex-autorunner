from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from ...core.utils import (
    RepoNotFoundError,
    canonicalize_path,
    find_repo_root,
    is_within,
)
from ...integrations.chat.thread_summaries import _coerce_thread_list
from .constants import (
    DEFAULT_PAGE_SIZE,
    THREAD_LIST_PAGE_LIMIT,
)
from .state import TelegramState, TelegramTopicRecord, ThreadSummary, topic_key


def _extract_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("threadId", "thread_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    thread = payload.get("thread")
    if isinstance(thread, dict):
        for key in ("id", "threadId", "thread_id"):
            value = thread.get(key)
            if isinstance(value, str):
                return value
    return None


_THREAD_PATH_KEYS_PRIMARY = (
    "cwd",
    "workspace_path",
    "workspacePath",
    "repoPath",
    "repo_path",
    "projectRoot",
    "project_root",
)
_THREAD_PATH_CONTAINERS = (
    "workspace",
    "project",
    "repo",
    "metadata",
    "context",
    "config",
)


def _extract_thread_path(entry: dict[str, Any]) -> Optional[str]:
    for key in _THREAD_PATH_KEYS_PRIMARY:
        value = entry.get(key)
        if isinstance(value, str):
            return value
    for container_key in _THREAD_PATH_CONTAINERS:
        nested = entry.get(container_key)
        if isinstance(nested, dict):
            for key in _THREAD_PATH_KEYS_PRIMARY:
                value = nested.get(key)
                if isinstance(value, str):
                    return value
    return None


def _extract_thread_info(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else None
    workspace_path = _extract_thread_path(payload)
    if not workspace_path and isinstance(thread, dict):
        workspace_path = _extract_thread_path(thread)
    rollout_path = None
    if isinstance(thread, dict):
        rollout_path = (
            thread.get("path") if isinstance(thread.get("path"), str) else None
        )
    if rollout_path is None and isinstance(payload.get("path"), str):
        rollout_path = payload.get("path")
    agent = None
    if isinstance(payload.get("agent"), str):
        agent = payload.get("agent")
    if (
        agent is None
        and isinstance(thread, dict)
        and isinstance(thread.get("agent"), str)
    ):
        agent = thread.get("agent")
    model = None
    for key in ("model", "modelId"):
        value = payload.get(key)
        if isinstance(value, str):
            model = value
            break
    if model is None and isinstance(thread, dict):
        for key in ("model", "modelId"):
            value = thread.get(key)
            if isinstance(value, str):
                model = value
                break
    effort = payload.get("reasoningEffort") or payload.get("effort")
    if not isinstance(effort, str) and isinstance(thread, dict):
        effort = thread.get("reasoningEffort") or thread.get("effort")
    if not isinstance(effort, str):
        effort = None
    summary = payload.get("summary") or payload.get("summaryMode")
    if not isinstance(summary, str) and isinstance(thread, dict):
        summary = thread.get("summary") or thread.get("summaryMode")
    if not isinstance(summary, str):
        summary = None
    approval_policy = payload.get("approvalPolicy") or payload.get("approval_policy")
    if not isinstance(approval_policy, str) and isinstance(thread, dict):
        approval_policy = thread.get("approvalPolicy") or thread.get("approval_policy")
    if not isinstance(approval_policy, str):
        approval_policy = None
    sandbox_policy = payload.get("sandboxPolicy") or payload.get("sandbox")
    if not isinstance(sandbox_policy, (dict, str)) and isinstance(thread, dict):
        sandbox_policy = thread.get("sandboxPolicy") or thread.get("sandbox")
    if not isinstance(sandbox_policy, (dict, str)):
        sandbox_policy = None
    return {
        "thread_id": _extract_thread_id(payload),
        "workspace_path": workspace_path,
        "rollout_path": rollout_path,
        "agent": agent,
        "model": model,
        "effort": effort,
        "summary": summary,
        "approval_policy": approval_policy,
        "sandbox_policy": sandbox_policy,
    }


def _set_thread_summary(
    record: "TelegramTopicRecord",
    thread_id: str,
    *,
    user_preview: Optional[str] = None,
    assistant_preview: Optional[str] = None,
    last_used_at: Optional[str] = None,
    workspace_path: Optional[str] = None,
    rollout_path: Optional[str] = None,
) -> None:
    if not isinstance(thread_id, str) or not thread_id:
        return
    summary = record.thread_summaries.get(thread_id)
    if summary is None:
        summary = ThreadSummary()
    if user_preview is not None:
        summary.user_preview = user_preview
    if assistant_preview is not None:
        summary.assistant_preview = assistant_preview
    if last_used_at is not None:
        summary.last_used_at = last_used_at
    if workspace_path is not None:
        summary.workspace_path = workspace_path
    if rollout_path is not None:
        summary.rollout_path = rollout_path
    record.thread_summaries[thread_id] = summary
    if record.thread_ids:
        keep = set(record.thread_ids)
        for key in list(record.thread_summaries.keys()):
            if key not in keep:
                record.thread_summaries.pop(key, None)


def _set_pending_compact_seed(
    record: "TelegramTopicRecord", seed_text: str, thread_id: Optional[str]
) -> None:
    record.pending_compact_seed = seed_text
    record.pending_compact_seed_thread_id = thread_id


def _clear_pending_compact_seed(record: "TelegramTopicRecord") -> None:
    record.pending_compact_seed = None
    record.pending_compact_seed_thread_id = None


def _clear_thread_mirror(record: "TelegramTopicRecord") -> None:
    record.active_thread_id = None
    record.thread_ids = []
    record.thread_summaries = {}
    record.rollout_path = None
    _clear_pending_compact_seed(record)


def _format_conversation_id(chat_id: int, thread_id: Optional[int]) -> str:
    return topic_key(chat_id, thread_id)


def _with_conversation_id(
    message: str, *, chat_id: int, thread_id: Optional[int]
) -> str:
    conversation_id = _format_conversation_id(chat_id, thread_id)
    return f"{message} (conversation {conversation_id})"


def _format_persist_note(message: str, *, persist: bool) -> str:
    if not persist:
        return message
    return f"{message} (Persistence is not supported in Telegram; applied to this topic only.)"


def _coerce_id(value: Any) -> Optional[str]:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _extract_turn_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for candidate in (payload, payload.get("turn"), payload.get("item")):
        if not isinstance(candidate, dict):
            continue
        for key in ("threadId", "thread_id"):
            thread_id = _coerce_id(candidate.get(key))
            if thread_id:
                return thread_id
        thread = candidate.get("thread")
        if isinstance(thread, dict):
            thread_id = _coerce_id(
                thread.get("id") or thread.get("threadId") or thread.get("thread_id")
            )
            if thread_id:
                return thread_id
    return None


def _coerce_thread_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    thread = payload.get("thread")
    if isinstance(thread, dict):
        merged = dict(thread)
        for key, value in payload.items():
            if key != "thread" and key not in merged:
                merged[key] = value
        return merged
    return dict(payload)


def _find_thread_entry(payload: Any, thread_id: str) -> Optional[dict[str, Any]]:
    for entry in _coerce_thread_list(payload):
        if entry.get("id") == thread_id:
            return entry
    return None


def _extract_rollout_path(entry: Any) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    for key in ("rollout_path", "rolloutPath", "path"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    thread = entry.get("thread")
    if isinstance(thread, dict):
        value = thread.get("path")
        if isinstance(value, str):
            return value
    return None


def _path_within(*, root: Path, target: Path) -> bool:
    try:
        root = canonicalize_path(root)
        target = canonicalize_path(target)
    except (OSError, ValueError):
        return False
    return is_within(root=root, target=target)


def _repo_root(path: Path) -> Optional[Path]:
    try:
        return find_repo_root(path)
    except RepoNotFoundError:
        return None


def _paths_compatible(workspace_root: Path, resumed_root: Path) -> bool:
    if _path_within(root=workspace_root, target=resumed_root):
        return True
    if _path_within(root=resumed_root, target=workspace_root):
        workspace_repo = _repo_root(workspace_root)
        resumed_repo = _repo_root(resumed_root)
        if workspace_repo is None or resumed_repo is None:
            return False
        if workspace_repo != resumed_repo:
            return False
        return resumed_root == workspace_repo
    workspace_repo = _repo_root(workspace_root)
    resumed_repo = _repo_root(resumed_root)
    if workspace_repo is None or resumed_repo is None:
        return False
    if workspace_repo != resumed_repo:
        return False
    return _path_within(root=workspace_repo, target=resumed_root)


def _partition_threads(
    threads: Any, workspace_path: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    if not isinstance(threads, list):
        return [], [], False
    workspace = Path(workspace_path).expanduser().resolve()
    filtered: list[dict[str, Any]] = []
    unscoped: list[dict[str, Any]] = []
    saw_path = False
    for entry in threads:
        if not isinstance(entry, dict):
            continue
        cwd = _extract_thread_path(entry)
        if not isinstance(cwd, str):
            unscoped.append(entry)
            continue
        saw_path = True
        try:
            candidate = Path(cwd).expanduser().resolve()
        except (OSError, ValueError):
            continue
        if _paths_compatible(workspace, candidate):
            filtered.append(entry)
    return filtered, unscoped, saw_path


def _local_workspace_threads(
    state: "TelegramState",
    workspace_path: Optional[str],
    *,
    current_key: str,
) -> tuple[list[str], dict[str, str], dict[str, set[str]]]:
    from .helpers import _thread_summary_preview

    thread_ids: list[str] = []
    previews: dict[str, str] = {}
    topic_keys_by_thread: dict[str, set[str]] = {}
    if not isinstance(workspace_path, str) or not workspace_path.strip():
        return thread_ids, previews, topic_keys_by_thread
    workspace_key = workspace_path.strip()
    workspace_root: Optional[Path] = None
    try:
        workspace_root = Path(workspace_key).expanduser().resolve()
    except (OSError, ValueError):
        workspace_root = None

    def matches(candidate_path: Optional[str]) -> bool:
        if not isinstance(candidate_path, str) or not candidate_path.strip():
            return False
        candidate_path = candidate_path.strip()
        if workspace_root is not None:
            try:
                candidate_root = Path(candidate_path).expanduser().resolve()
            except (OSError, ValueError):
                return False
            return _paths_compatible(workspace_root, candidate_root)
        return candidate_path == workspace_key

    def add_record(key: str, record: "TelegramTopicRecord") -> None:
        if not matches(record.workspace_path):
            return
        for thread_id in record.thread_ids:
            topic_keys_by_thread.setdefault(thread_id, set()).add(key)
            if thread_id not in previews:
                preview = _thread_summary_preview(record, thread_id)
                if preview:
                    previews[thread_id] = preview
            if thread_id in seen:
                continue
            seen.add(thread_id)
            thread_ids.append(thread_id)

    seen: set[str] = set()
    current = state.topics.get(current_key)
    if current is not None:
        add_record(current_key, current)
    for key, record in state.topics.items():
        if key == current_key:
            continue
        add_record(key, record)
    return thread_ids, previews, topic_keys_by_thread


def _head_text_lines(path: Path, max_lines: int) -> list[str]:
    if max_lines <= 0:
        return []
    try:
        lines: list[str] = []
        with path.open("rb") as handle:
            for _ in range(max_lines):
                line = handle.readline()
                if not line:
                    break
                lines.append(line.decode("utf-8", errors="replace"))
        return lines
    except OSError:
        return []


def _resume_thread_list_limit(thread_ids: Sequence[str]) -> int:
    desired = max(DEFAULT_PAGE_SIZE, len(thread_ids) or DEFAULT_PAGE_SIZE)
    return min(THREAD_LIST_PAGE_LIMIT, desired)


def _split_topic_key(key: str) -> tuple[int, Optional[int]]:
    parts = key.split(":", 2)
    chat_raw = parts[0] if parts else ""
    thread_raw = parts[1] if len(parts) > 1 else ""
    chat_id = int(chat_raw)
    thread_id = None
    if thread_raw and thread_raw != "root":
        thread_id = int(thread_raw)
    return chat_id, thread_id


def _consume_raw_token(raw: str) -> tuple[Optional[str], str]:
    stripped = raw.lstrip()
    if not stripped:
        return None, ""
    for idx, ch in enumerate(stripped):
        if ch.isspace():
            return stripped[:idx], stripped[idx:]
    return stripped, ""


def _extract_first_bold_span(text: str) -> Optional[str]:
    if not text:
        return None
    start = text.find("**")
    if start < 0:
        return None
    end = text.find("**", start + 2)
    if end < 0:
        return None
    content = text[start + 2 : end].strip()
    return content or None
