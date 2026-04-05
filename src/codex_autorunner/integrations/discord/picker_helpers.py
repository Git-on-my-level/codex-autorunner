from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

from ...agents.opencode.harness import OpenCodeHarness
from ...core.logging_utils import log_event
from ...core.utils import canonicalize_path
from ...integrations.app_server.client import CodexAppServerClient
from ...integrations.chat.model_selection import (
    _coerce_model_entries,
    _display_name_is_model_alias,
    _model_list_with_agent_compat,
)
from ..chat.review_commits import _parse_review_commit_log
from ..chat.thread_summaries import (
    _coerce_thread_list,
    _extract_thread_list_cursor,
    _extract_thread_preview_parts,
    _format_resume_picker_label,
    _format_resume_timestamp,
)
from .components import DISCORD_SELECT_OPTION_MAX_OPTIONS
from .rendering import truncate_for_discord


def _coerce_model_picker_items(
    result: Any,
    *,
    limit: Optional[int] = None,
) -> list[tuple[str, str]]:
    entries = _coerce_model_entries(result)
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    item_limit = max(
        1,
        limit if isinstance(limit, int) else DISCORD_SELECT_OPTION_MAX_OPTIONS - 1,
    )
    for entry in entries:
        model_id = entry.get("model") or entry.get("id")
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        display_name = entry.get("displayName")
        label = model_id
        if (
            isinstance(display_name, str)
            and display_name
            and not _display_name_is_model_alias(model_id, display_name)
        ):
            label = f"{model_id} ({display_name})"
        options.append((model_id, label))
        if len(options) >= item_limit:
            break
    return options


def _truncate_picker_text(text: str, *, limit: int) -> str:
    value = " ".join(text.split()).strip()
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return f"{value[: limit - 3]}..."


def _format_session_thread_picker_label(
    thread_id: str, entry: dict[str, Any], *, is_current: bool
) -> str:
    max_label_len = 100
    current_suffix = " (current)" if is_current else ""
    max_base_len = max_label_len - len(current_suffix)
    if _format_resume_timestamp(entry):
        preview_label: Optional[str] = None
        user_preview, assistant_preview = _extract_thread_preview_parts(entry)
        user_preview = _truncate_picker_text(user_preview or "", limit=72) or None
        assistant_preview = (
            _truncate_picker_text(assistant_preview or "", limit=72) or None
        )
        if user_preview and assistant_preview:
            preview_label = f"U: {user_preview} | A: {assistant_preview}"
        elif user_preview:
            preview_label = f"U: {user_preview}"
        elif assistant_preview:
            preview_label = f"A: {assistant_preview}"
        base = _format_resume_picker_label(
            thread_id,
            entry,
            limit=max_base_len,
            fallback_preview=preview_label,
        )
        return f"{base}{current_suffix}"
    user_preview, assistant_preview = _extract_thread_preview_parts(entry)
    user_preview = _truncate_picker_text(user_preview or "", limit=72) or None
    assistant_preview = _truncate_picker_text(assistant_preview or "", limit=72) or None
    fallback_preview_label: Optional[str] = None
    if user_preview and assistant_preview:
        fallback_preview_label = f"U: {user_preview} | A: {assistant_preview}"
    elif user_preview:
        fallback_preview_label = f"U: {user_preview}"
    elif assistant_preview:
        fallback_preview_label = f"A: {assistant_preview}"
    if fallback_preview_label:
        short_id = thread_id[:8]
        id_prefix = f"[{short_id}] "
        preview_budget = max(1, max_base_len - len(id_prefix))
        base = f"{id_prefix}{_truncate_picker_text(fallback_preview_label, limit=preview_budget)}"
    else:
        base = _truncate_picker_text(thread_id, limit=max_base_len)
    return f"{base}{current_suffix}"


async def list_opencode_models_for_picker(
    service: Any,
    *,
    workspace_path: Optional[str],
) -> Optional[list[tuple[str, str]]]:
    if not isinstance(workspace_path, str) or not workspace_path.strip():
        return None
    try:
        workspace_root = canonicalize_path(Path(workspace_path))
    except Exception:
        return None
    if not workspace_root.exists() or not workspace_root.is_dir():
        return None
    supervisor = await service._opencode_supervisor_for_workspace(workspace_root)
    if supervisor is None:
        raise RuntimeError("OpenCode backend unavailable for this workspace")
    harness = OpenCodeHarness(supervisor)
    catalog = await harness.model_catalog(workspace_root)
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    for model in catalog.models:
        model_id = model.id.strip() if isinstance(model.id, str) else ""
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        label = model_id
        if (
            isinstance(model.display_name, str)
            and model.display_name
            and not _display_name_is_model_alias(model_id, model.display_name)
        ):
            label = f"{model_id} ({model.display_name})"
        options.append((model_id, label))
    return options


async def list_threads_paginated(
    service: Any,
    client: CodexAppServerClient,
    *,
    limit: int,
    max_pages: int,
    needed_ids: Optional[set[str]] = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    entries: list[dict[str, Any]] = []
    found_ids: set[str] = set()
    seen_ids: set[str] = set()
    cursor: Optional[str] = None
    page_count = max(1, max_pages)
    for _ in range(page_count):
        payload = await client.thread_list(cursor=cursor, limit=limit)
        page_entries = _coerce_thread_list(payload)
        for entry in page_entries:
            if not isinstance(entry, dict):
                continue
            thread_id = entry.get("id")
            if isinstance(thread_id, str):
                if thread_id in seen_ids:
                    continue
                seen_ids.add(thread_id)
                found_ids.add(thread_id)
            entries.append(entry)
        if needed_ids is not None and needed_ids.issubset(found_ids):
            break
        cursor = _extract_thread_list_cursor(payload)
        if not cursor:
            break
    return entries, found_ids


async def list_session_threads_for_picker(
    service: Any,
    *,
    workspace_root: Path,
    current_thread_id: Optional[str],
) -> list[tuple[str, str]]:
    try:
        client = await service._client_for_workspace(str(workspace_root))
    except Exception:
        return []
    if client is None:
        return []
    try:
        entries, _found = await list_threads_paginated(
            service,
            client,
            limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
            max_pages=3,
        )
    except Exception as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.session.threads_picker.failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        return []

    items: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for entry in entries:
        thread_id = entry.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            continue
        if thread_id in seen_ids:
            continue
        seen_ids.add(thread_id)
        label = _format_session_thread_picker_label(
            thread_id,
            entry,
            is_current=thread_id == current_thread_id,
        )
        items.append((thread_id, label))
        if len(items) >= DISCORD_SELECT_OPTION_MAX_OPTIONS:
            break
    if (
        isinstance(current_thread_id, str)
        and current_thread_id
        and current_thread_id not in seen_ids
    ):
        if len(items) >= DISCORD_SELECT_OPTION_MAX_OPTIONS:
            items.pop()
        items.append(
            (
                current_thread_id,
                _format_session_thread_picker_label(
                    current_thread_id, {"id": current_thread_id}, is_current=True
                ),
            )
        )
    return items


async def list_recent_commits_for_picker(
    workspace_root: Path,
    *,
    limit: int = DISCORD_SELECT_OPTION_MAX_OPTIONS,
) -> list[tuple[str, str]]:
    cmd = [
        "git",
        "-C",
        str(workspace_root),
        "log",
        f"-n{max(1, limit)}",
        "--pretty=format:%H%x1f%s%x1e",
    ]
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception:
        return []
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    if result.returncode not in (0, None) and not stdout.strip():
        return []
    return _parse_review_commit_log(stdout)[:limit]


async def list_model_items_for_binding(
    service: Any,
    *,
    binding: dict[str, Any],
    agent: str,
    limit: int,
) -> Optional[list[tuple[str, str]]]:
    if agent == "opencode":
        return await list_opencode_models_for_picker(
            service,
            workspace_path=binding.get("workspace_path"),
        )
    client = await service._client_for_workspace(binding.get("workspace_path"))
    if client is None:
        return None
    result = await _model_list_with_agent_compat(
        client,
        params={
            "cursor": None,
            "limit": max(1, limit),
            "agent": agent,
        },
    )
    return _coerce_model_picker_items(result, limit=max(1, limit))


def format_discord_thread_picker_label(
    service: Any,
    thread: Any,
    *,
    is_current: bool,
) -> str:
    thread_id = str(getattr(thread, "thread_target_id", "") or "").strip()
    last_preview = str(getattr(thread, "last_message_preview", "") or "").strip()
    compact_seed = str(getattr(thread, "compact_seed", "") or "").strip()
    entry = {
        "created_at": getattr(thread, "created_at", None),
        "updated_at": getattr(thread, "updated_at", None),
        "status_changed_at": getattr(thread, "status_changed_at", None),
    }
    if _format_resume_timestamp(entry):
        label = _format_resume_picker_label(
            thread_id,
            entry,
            limit=100,
            fallback_preview=last_preview or compact_seed or None,
        )
    else:
        short_id = thread_id[:8] if thread_id else "unknown"
        agent = str(getattr(thread, "agent_id", "") or "").strip() or "agent"
        lifecycle_status = (
            str(getattr(thread, "lifecycle_status", "") or "").strip().lower()
        )
        display_name = str(getattr(thread, "display_name", "") or "").strip()
        base = display_name or f"{agent} {short_id}"
        parts = [base]
        if lifecycle_status and lifecycle_status not in {"active", "running"}:
            parts.append(lifecycle_status)
        if last_preview:
            preview = truncate_for_discord(last_preview, max_len=60)
            parts.append(preview)
        label = " \u00b7 ".join(parts)
    if is_current:
        label = f"{label} (current)"
    return truncate_for_discord(label, max_len=100)
