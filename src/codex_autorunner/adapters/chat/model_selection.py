"""Shared model selection utilities for Discord and Telegram chat surfaces."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..app_server.client import CodexAppServerClient

REASONING_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh")
VALID_REASONING_EFFORTS = frozenset(REASONING_EFFORT_VALUES)
MODEL_LIST_INVALID_PARAMS_ERROR_CODES = frozenset({-32600, -32602})


@dataclass(frozen=True)
class ModelOption:
    model_id: str
    label: str
    efforts: tuple[str, ...]
    default_effort: Optional[str] = None


def _normalize_model_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _display_name_is_model_alias(model: str, display_name: Any) -> bool:
    if not isinstance(display_name, str) or not display_name:
        return False
    return _normalize_model_name(display_name) == _normalize_model_name(model)


def _coerce_model_entries(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [entry for entry in result if isinstance(entry, dict)]
    if isinstance(result, dict):
        for key in ("data", "models", "items", "results"):
            value = result.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
    return []


def _is_valid_opencode_model_name(model_name: str) -> bool:
    if "/" not in model_name:
        return False
    provider_id, model_id = model_name.split("/", 1)
    return bool(provider_id.strip() and model_id.strip())


def _coerce_model_options(
    result: Any, *, include_efforts: bool = True
) -> list[ModelOption]:
    entries = _coerce_model_entries(result)
    options: list[ModelOption] = []
    for entry in entries:
        model = entry.get("model") or entry.get("id")
        if not isinstance(model, str) or not model:
            continue
        display_name = entry.get("displayName")
        label = model
        if (
            isinstance(display_name, str)
            and display_name
            and not _display_name_is_model_alias(model, display_name)
        ):
            label = f"{model} ({display_name})"
        default_effort = None
        efforts: list[str] = []
        if include_efforts:
            default_effort = entry.get("defaultReasoningEffort")
            if not isinstance(default_effort, str):
                default_effort = None
            efforts_raw = entry.get("supportedReasoningEfforts")
            if isinstance(efforts_raw, list):
                for effort in efforts_raw:
                    if isinstance(effort, dict):
                        value = effort.get("reasoningEffort")
                        if isinstance(value, str):
                            efforts.append(value)
                    elif isinstance(effort, str):
                        efforts.append(effort)
            if default_effort and default_effort not in efforts:
                efforts.append(default_effort)
            efforts = [effort for effort in efforts if effort]
            if not efforts:
                efforts = list(REASONING_EFFORT_VALUES)
            efforts = list(dict.fromkeys(efforts))
            if default_effort:
                label = f"{label} (default {default_effort})"
        options.append(
            ModelOption(
                model_id=model,
                label=label,
                efforts=tuple(efforts),
                default_effort=default_effort,
            )
        )
    return options


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
    from ..app_server.client import CodexAppServerResponseError

    request_params = {key: value for key, value in params.items() if value is not None}
    requested_agent = request_params.get("agent")
    if not isinstance(requested_agent, str) or not requested_agent:
        requested_agent = None
        request_params.pop("agent", None)
    try:
        return await client.model_list(**request_params)
    except CodexAppServerResponseError as exc:
        if (
            requested_agent is None
            or exc.code not in MODEL_LIST_INVALID_PARAMS_ERROR_CODES
        ):
            raise
        fallback_params = dict(request_params)
        fallback_params.pop("agent", None)
        return await client.model_list(**fallback_params)


def format_model_set_message(
    model_name: str, *, effort: Optional[str] = None, include_effort: bool = True
) -> str:
    effort_note = ""
    if effort and include_effort:
        effort_note = f" (effort={effort})"
    return f"Model set to {model_name}{effort_note}. Will apply on the next turn."


def reasoning_effort_command_choices() -> tuple[dict[str, str], ...]:
    return tuple(
        {"name": effort, "value": effort} for effort in REASONING_EFFORT_VALUES
    )


def reasoning_effort_description() -> str:
    return ", ".join(REASONING_EFFORT_VALUES)


__all__ = [
    "MODEL_LIST_INVALID_PARAMS_ERROR_CODES",
    "REASONING_EFFORT_VALUES",
    "VALID_REASONING_EFFORTS",
    "ModelOption",
    "_coerce_model_entries",
    "_coerce_model_options",
    "_display_name_is_model_alias",
    "_extract_model_list_cursor",
    "_is_valid_opencode_model_name",
    "_model_list_with_agent_compat",
    "_normalize_model_name",
    "format_model_set_message",
    "reasoning_effort_command_choices",
    "reasoning_effort_description",
]
