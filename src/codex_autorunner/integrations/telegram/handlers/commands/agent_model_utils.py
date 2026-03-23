"""Reusable workspace-command helpers for Telegram command handlers."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .....core.logging_utils import log_event
from .....integrations.app_server.client import (
    CodexAppServerClient,
    CodexAppServerResponseError,
)
from ...adapter import TelegramMessage
from ...config import AppServerUnavailableError
from ...constants import AGENT_PICKER_PROMPT, VALID_AGENT_VALUES
from ...state import normalize_agent
from ...types import SelectionState

_INVALID_PARAMS_ERROR_CODES = {-32600, -32602}
_MODEL_LIST_MAX_PAGES = 10
_AGENT_PICKER_DEFAULT_MESSAGE = "Topic not bound. Use /bind <repo_id> or /bind <path>."


async def _handle_agent_command(
    commands: Any,
    message: TelegramMessage,
    args: str,
) -> None:
    record = await commands._router.ensure_topic(message.chat_id, message.thread_id)
    current = commands._effective_agent(record)
    key = await commands._resolve_topic_key(message.chat_id, message.thread_id)
    commands._agent_options.pop(key, None)
    argv = commands._parse_command_args(args)
    if not argv:
        await _send_agent_picker(
            commands,
            key=key,
            current=current,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
        )
        return
    desired = normalize_agent(argv[0])
    workspace_path, error = commands._resolve_workspace_path(record, allow_pma=True)
    if workspace_path is None:
        await commands._send_message(
            message.chat_id,
            error or _AGENT_PICKER_DEFAULT_MESSAGE,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    try:
        client = await commands._client_for_workspace(workspace_path)
    except AppServerUnavailableError as exc:
        log_event(
            commands._logger,
            logging.WARNING,
            "telegram.app_server.unavailable",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            exc=exc,
        )
        await commands._send_message(
            message.chat_id,
            "App server unavailable; try again or check logs.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    if client is None:
        await commands._send_message(
            message.chat_id,
            error or _AGENT_PICKER_DEFAULT_MESSAGE,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    if desired == "opencode" and not commands._opencode_available():
        await commands._send_message(
            message.chat_id,
            "OpenCode binary not found. Install opencode or switch to /agent codex.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    if desired == current:
        await commands._send_message(
            message.chat_id,
            f"Agent already set to {current}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    note = await commands._apply_agent_change(
        message.chat_id, message.thread_id, desired
    )
    await commands._send_message(
        message.chat_id,
        f"Agent set to {desired}{note}.",
        thread_id=message.thread_id,
        reply_to=message.message_id,
    )


async def _send_agent_picker(
    commands: Any,
    *,
    key: str,
    current: str,
    chat_id: int,
    thread_id: Optional[int],
    message_id: int,
) -> None:
    availability = "available"
    if not commands._opencode_available():
        availability = "missing binary"
    items = _build_agent_options(current=current, availability=availability)
    state = SelectionState(items=items)
    keyboard = commands._build_agent_keyboard(state)
    commands._agent_options[key] = state
    commands._touch_cache_timestamp("agent_options", key)
    await commands._send_message(
        chat_id,
        commands._selection_prompt(AGENT_PICKER_PROMPT, state),
        thread_id=thread_id,
        reply_to=message_id,
        reply_markup=keyboard,
    )


def _build_agent_options(
    *,
    current: str,
    availability: str,
) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for agent in ("codex", "opencode"):
        if agent not in VALID_AGENT_VALUES:
            continue
        label = agent
        if agent == current:
            label = f"{label} (current)"
        if agent == "opencode" and availability != "available":
            label = f"{label} ({availability})"
        items.append((agent, label))
    return items


def _extract_opencode_session_path(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("directory", "path", "workspace_path", "workspacePath"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    properties = payload.get("properties")
    if isinstance(properties, dict):
        for key in ("directory", "path", "workspace_path", "workspacePath"):
            value = properties.get(key)
            if isinstance(value, str) and value:
                return value
    session = payload.get("session")
    if isinstance(session, dict):
        return _extract_opencode_session_path(session)
    return None


def _coerce_model_list_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        for key in ("data", "models", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
    return []


def _extract_model_list_cursor(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("nextCursor", "next_cursor"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


async def _model_list_with_agent_compat(
    client: CodexAppServerClient,
    *,
    params: dict[str, Any],
) -> Any:
    request_params = {key: value for key, value in params.items() if value is not None}
    requested_agent = request_params.get("agent")
    if not isinstance(requested_agent, str) or not requested_agent:
        requested_agent = None
        request_params.pop("agent", None)
    try:
        return await client.model_list(**request_params)
    except CodexAppServerResponseError as exc:
        if requested_agent is None or exc.code not in _INVALID_PARAMS_ERROR_CODES:
            raise
        fallback_params = dict(request_params)
        fallback_params.pop("agent", None)
        return await client.model_list(**fallback_params)


async def _model_list_all_with_agent_compat(
    client: CodexAppServerClient,
    *,
    params: dict[str, Any],
    max_pages: int = _MODEL_LIST_MAX_PAGES,
) -> list[dict[str, Any]]:
    request_params = dict(params)
    merged_entries: list[dict[str, Any]] = []
    seen_model_ids: set[str] = set()
    seen_cursors: set[str] = set()
    pages = 0
    while True:
        pages += 1
        payload = await _model_list_with_agent_compat(client, params=request_params)
        for entry in _coerce_model_list_entries(payload):
            model_id = entry.get("model") or entry.get("id")
            if isinstance(model_id, str) and model_id:
                if model_id in seen_model_ids:
                    continue
                seen_model_ids.add(model_id)
            merged_entries.append(entry)
        if pages >= max_pages:
            break
        next_cursor = _extract_model_list_cursor(payload)
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        request_params = dict(request_params)
        request_params["cursor"] = next_cursor
    return merged_entries
