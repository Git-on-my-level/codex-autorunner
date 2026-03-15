from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Optional

from ...core.flows import FlowRunStatus
from ...core.utils import canonicalize_path
from ...integrations.chat.picker_filter import filter_picker_items
from .components import DISCORD_SELECT_OPTION_MAX_OPTIONS

MODEL_SEARCH_FETCH_LIMIT = 200
REPO_AUTOCOMPLETE_TOKEN_PREFIX = "repo@"
WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX = "workspace@"
AGENT_WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX = "agent_workspace@"
FLOW_ACTIONS_WITH_RUN_PICKER = {
    "status",
    "restart",
    "resume",
    "stop",
    "archive",
    "recover",
    "reply",
}


def repo_autocomplete_value(repo_id: str) -> str:
    normalized_id = repo_id.strip()
    if len(normalized_id) <= 100:
        return normalized_id
    digest = hashlib.sha256(normalized_id.encode("utf-8")).hexdigest()[:24]
    return f"{REPO_AUTOCOMPLETE_TOKEN_PREFIX}{digest}"


def workspace_autocomplete_value(workspace_path: str) -> str:
    normalized_path = workspace_path.strip()
    if len(normalized_path) <= 100:
        return normalized_path
    digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:24]
    return f"{WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX}{digest}"


def agent_workspace_autocomplete_value(workspace_id: str) -> str:
    normalized_id = workspace_id.strip()
    if len(normalized_id) <= 100:
        return normalized_id
    digest = hashlib.sha256(normalized_id.encode("utf-8")).hexdigest()[:24]
    return f"{AGENT_WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX}{digest}"


def resolve_workspace_from_token(
    token: str,
    candidates: list[tuple[Optional[str], Optional[str], str]],
) -> Optional[tuple[Optional[str], Optional[str], str]]:
    normalized = token.strip()
    if not normalized:
        return None

    for resource_kind, resource_id, workspace_path in candidates:
        if resource_id == normalized or workspace_path == normalized:
            return resource_kind, resource_id, workspace_path

    if normalized.startswith(REPO_AUTOCOMPLETE_TOKEN_PREFIX):
        digest = normalized[len(REPO_AUTOCOMPLETE_TOKEN_PREFIX) :]
        if digest:
            matches = [
                (resource_kind, resource_id, workspace_path)
                for resource_kind, resource_id, workspace_path in candidates
                if resource_kind == "repo"
                and isinstance(resource_id, str)
                and hashlib.sha256(resource_id.encode("utf-8"))
                .hexdigest()
                .startswith(digest)
            ]
            if len(matches) == 1:
                return matches[0]

    if normalized.startswith(WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX):
        digest = normalized[len(WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX) :]
        if digest:
            matches = [
                (resource_kind, resource_id, workspace_path)
                for resource_kind, resource_id, workspace_path in candidates
                if hashlib.sha256(workspace_path.encode("utf-8"))
                .hexdigest()
                .startswith(digest)
            ]
            if len(matches) == 1:
                return matches[0]

    if normalized.startswith(AGENT_WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX):
        digest = normalized[len(AGENT_WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX) :]
        if digest:
            matches = [
                (resource_kind, resource_id, workspace_path)
                for resource_kind, resource_id, workspace_path in candidates
                if resource_kind == "agent_workspace"
                and isinstance(resource_id, str)
                and hashlib.sha256(resource_id.encode("utf-8"))
                .hexdigest()
                .startswith(digest)
            ]
            if len(matches) == 1:
                return matches[0]

    return None


def picker_items_to_autocomplete_choices(
    items: list[tuple[str, str]],
) -> list[dict[str, str]]:
    seen: set[str] = set()
    choices: list[dict[str, str]] = []
    for value, label in items:
        normalized_value = value.strip()[:100]
        if not normalized_value or normalized_value in seen:
            continue
        seen.add(normalized_value)
        choices.append(
            {
                "name": (label or value).strip()[:100],
                "value": normalized_value,
            }
        )
        if len(choices) >= DISCORD_SELECT_OPTION_MAX_OPTIONS:
            break
    return choices


def build_bind_autocomplete_choices(service: Any, query: str) -> list[dict[str, str]]:
    candidates = service._list_bind_workspace_candidates()
    search_items, _exact_aliases, filter_aliases = service._build_bind_search_items(
        candidates
    )
    filtered = filter_picker_items(
        search_items,
        query,
        limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
        aliases=filter_aliases,
    )
    return picker_items_to_autocomplete_choices(filtered)


async def build_model_autocomplete_choices(
    service: Any,
    *,
    channel_id: str,
    query: str,
) -> list[dict[str, str]]:
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        return []
    agent = service._normalize_agent(binding.get("agent"))
    try:
        model_items = await service._list_model_items_for_binding(
            binding=binding,
            agent=agent,
            limit=MODEL_SEARCH_FETCH_LIMIT,
        )
    except Exception:
        return []
    if not model_items:
        return []
    filtered = filter_picker_items(
        model_items,
        query,
        limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
    )
    return picker_items_to_autocomplete_choices(filtered)


async def build_skills_autocomplete_choices(
    service: Any,
    *,
    channel_id: str,
    query: str,
) -> list[dict[str, str]]:
    workspace_root = await service._bound_workspace_root_for_channel(channel_id)
    if workspace_root is None:
        return []
    skill_entries = await service._list_skill_entries_for_workspace(workspace_root)
    if not skill_entries:
        return []
    filtered = service._filter_skill_entries(
        skill_entries,
        query,
        limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
    )
    items = [
        (name, f"{name} - {description}" if description else name)
        for name, description in filtered
    ]
    return picker_items_to_autocomplete_choices(items)


async def build_ticket_autocomplete_choices(
    service: Any,
    *,
    channel_id: str,
    query: str,
) -> list[dict[str, str]]:
    workspace_root = await service._bound_workspace_root_for_channel(channel_id)
    if workspace_root is None:
        return []
    status_filter = service._pending_ticket_filters.get(channel_id, "all")
    filtered_choices = service._list_ticket_choices(
        workspace_root,
        status_filter=status_filter,
        search_query=query,
    )
    items = [(value, label) for value, label, _description in filtered_choices]
    return picker_items_to_autocomplete_choices(items)


async def build_session_resume_autocomplete_choices(
    service: Any,
    *,
    channel_id: str,
    query: str,
) -> list[dict[str, str]]:
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        return []

    pma_enabled = bool(binding.get("pma_enabled", False))
    workspace_raw = binding.get("workspace_path")
    workspace_root: Optional[Path] = None
    if isinstance(workspace_raw, str) and workspace_raw.strip():
        workspace_root = canonicalize_path(Path(workspace_raw))
        if not workspace_root.exists() or not workspace_root.is_dir():
            workspace_root = None
    if workspace_root is None:
        if pma_enabled:
            workspace_root = canonicalize_path(Path(service._config.root))
        else:
            return []

    thread_items = await service._list_session_threads_for_picker(
        workspace_root=workspace_root,
        current_thread_id=None,
    )
    if not thread_items:
        return []
    filtered = filter_picker_items(
        thread_items,
        query,
        limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
    )
    return picker_items_to_autocomplete_choices(filtered)


def _flow_run_matches_action(record: Any, action: str) -> bool:
    status = getattr(record, "status", None)
    if action in {"resume", "reply"}:
        return status == FlowRunStatus.PAUSED
    if action in {"stop", "recover"}:
        return hasattr(status, "is_terminal") and not status.is_terminal()
    return True


async def build_flow_run_autocomplete_choices(
    service: Any,
    *,
    channel_id: str,
    action: str,
    query: str,
) -> list[dict[str, str]]:
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None or bool(binding.get("pma_enabled", False)):
        return []
    workspace_raw = binding.get("workspace_path")
    if not isinstance(workspace_raw, str) or not workspace_raw.strip():
        return []
    workspace_root = canonicalize_path(Path(workspace_raw))
    if not workspace_root.exists() or not workspace_root.is_dir():
        return []

    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError):
        return []
    try:
        runs = store.list_flow_runs(flow_type="ticket_flow")
    except (sqlite3.Error, OSError):
        return []
    finally:
        store.close()

    matching_runs = [run for run in runs if _flow_run_matches_action(run, action)]
    if not matching_runs:
        return []

    items = [(record.id, record.status.value) for record in matching_runs]
    search_items = [(record.id, record.id) for record in matching_runs]
    aliases = {record.id: (record.status.value,) for record in matching_runs}
    filtered = filter_picker_items(
        search_items,
        query,
        limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
        aliases=aliases,
    )
    choices: list[dict[str, str]] = []
    status_by_run_id = {run_id: status for run_id, status in items}
    for run_id, _label in filtered:
        status = status_by_run_id.get(run_id, "")
        choices.append({"name": f"{run_id} [{status}]"[:100], "value": run_id[:100]})
    return choices


async def handle_command_autocomplete(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    command_path: tuple[str, ...],
    options: dict[str, Any],
    focused_name: Optional[str],
    focused_value: str,
) -> None:
    _ = options
    choices: list[dict[str, str]] = []
    if command_path == ("car", "bind") and focused_name == "workspace":
        choices = build_bind_autocomplete_choices(service, focused_value)
    elif command_path == ("car", "model") and focused_name == "name":
        choices = await build_model_autocomplete_choices(
            service,
            channel_id=channel_id,
            query=focused_value,
        )
    elif command_path == ("car", "skills") and focused_name == "search":
        choices = await build_skills_autocomplete_choices(
            service,
            channel_id=channel_id,
            query=focused_value,
        )
    elif command_path == ("car", "tickets") and focused_name == "search":
        choices = await build_ticket_autocomplete_choices(
            service,
            channel_id=channel_id,
            query=focused_value,
        )
    elif command_path == ("car", "session", "resume") and focused_name == "thread_id":
        choices = await build_session_resume_autocomplete_choices(
            service,
            channel_id=channel_id,
            query=focused_value,
        )
    elif (
        len(command_path) == 3
        and command_path[:2] == ("car", "flow")
        and command_path[2] in FLOW_ACTIONS_WITH_RUN_PICKER
        and focused_name == "run_id"
    ):
        choices = await build_flow_run_autocomplete_choices(
            service,
            channel_id=channel_id,
            action=command_path[2],
            query=focused_value,
        )
    await service._respond_autocomplete(
        interaction_id,
        interaction_token,
        choices=choices,
    )
