"""Reusable workspace-command helpers for Telegram command handlers."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .....core.logging_utils import log_event
from .....integrations.app_server.client import (
    CodexAppServerClient,
    CodexAppServerResponseError,
)
from ....chat.agents import (
    chat_agent_definitions,
    format_chat_agent_selection,
    normalize_chat_agent,
)
from ...adapter import TelegramMessage
from ...config import AppServerUnavailableError
from ...constants import AGENT_PICKER_PROMPT, AGENT_PROFILE_PICKER_PROMPT
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
    current_profile = commands._effective_agent_profile(record)
    key = await commands._resolve_topic_key(message.chat_id, message.thread_id)
    commands._agent_options.pop(key, None)
    agent_profile_options = getattr(commands, "_agent_profile_options", None)
    if not isinstance(agent_profile_options, dict):
        agent_profile_options = {}
        commands._agent_profile_options = agent_profile_options
    agent_profile_options.pop(key, None)
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
    desired = normalize_chat_agent(argv[0], context=commands)
    if desired is None:
        choices = ", ".join(
            sorted(commands._agents_supporting_capability("message_turns"))
        )
        await commands._send_message(
            message.chat_id,
            (
                f"Unknown agent '{argv[0]}'."
                + (f" Available agents: {choices}." if choices else "")
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    desired_profile = None
    if desired == "hermes" and len(argv) > 1:
        desired_profile = commands._normalize_hermes_profile(argv[1])
        if desired_profile is None:
            known = ", ".join(
                option.profile for option in commands._hermes_profile_options()
            )
            await commands._send_message(
                message.chat_id,
                (
                    f"Unknown Hermes profile '{argv[1]}'."
                    + (f" Known profiles: {known}." if known else "")
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
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
        if desired != "hermes" or desired_profile == current_profile:
            await commands._send_message(
                message.chat_id,
                f"Agent already set to {commands._effective_agent_label(record)}.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
    if (
        desired == "hermes"
        and desired_profile is None
        and commands._hermes_profile_options()
    ):
        await _send_agent_profile_picker(
            commands,
            key=key,
            current=current_profile,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            reply_to=message.message_id,
            requester_user_id=(
                str(message.from_user_id) if message.from_user_id is not None else None
            ),
        )
        return
    note = await commands._apply_agent_change(
        message.chat_id,
        message.thread_id,
        desired,
        profile=desired_profile,
    )
    await commands._send_message(
        message.chat_id,
        f"Agent set to {format_chat_agent_selection(desired, desired_profile)}{note}.",
        thread_id=message.thread_id,
        reply_to=message.message_id,
    )
    return


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
    items = _build_agent_options(
        current=current,
        availability=availability,
        context=commands,
    )
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


async def _send_agent_profile_picker(
    commands: Any,
    *,
    key: str,
    current: Optional[str],
    chat_id: int,
    thread_id: Optional[int],
    message_id: Optional[int],
    reply_to: Optional[int],
    requester_user_id: Optional[str],
) -> None:
    items = _build_agent_profile_options(current=current, context=commands)
    state = SelectionState(items=items, requester_user_id=requester_user_id)
    keyboard = commands._build_agent_profile_keyboard(state)
    agent_profile_options = getattr(commands, "_agent_profile_options", None)
    if not isinstance(agent_profile_options, dict):
        agent_profile_options = {}
        commands._agent_profile_options = agent_profile_options
    agent_profile_options[key] = state
    commands._touch_cache_timestamp("agent_profile_options", key)
    await commands._send_message(
        chat_id,
        commands._selection_prompt(AGENT_PROFILE_PICKER_PROMPT, state),
        thread_id=thread_id,
        reply_to=reply_to or message_id,
        reply_markup=keyboard,
    )


def _build_agent_options(
    *,
    current: str,
    availability: str,
    context: Any = None,
) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for definition in chat_agent_definitions(context):
        agent = definition.value
        label = agent
        if agent == current:
            label = f"{label} (current)"
        if agent == "opencode" and availability != "available":
            label = f"{label} ({availability})"
        items.append((agent, label))
    return items


def _build_agent_profile_options(
    *,
    current: Optional[str],
    context: Any = None,
) -> list[tuple[str, str]]:
    items = [("clear", "(default profile)")]
    for option in context._hermes_profile_options():
        label = option.profile
        if current == option.profile:
            label = f"{label} (current)"
        items.append((option.profile, label))
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
